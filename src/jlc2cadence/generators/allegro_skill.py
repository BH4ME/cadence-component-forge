from __future__ import annotations

import csv
from hashlib import sha256
import json
import math
import re
from pathlib import Path
from dataclasses import replace

from .. import __version__
from ..errors import UnsupportedGeometryError
from ..ir.geometry import cadence_safe_name, footprint_bounds
from ..ir.schema import Footprint, Graphic, LibraryIR, Pad
from ..ir.units import fmt_mm


SOURCE_APERTURES = (
    ("SOLDERMASK_TOP", "Source_Mask_Top_mm"),
    ("SOLDERMASK_BOTTOM", "Source_Mask_Bottom_mm"),
    ("PASTEMASK_TOP", "Source_Paste_Top_mm"),
    ("PASTEMASK_BOTTOM", "Source_Paste_Bottom_mm"),
)

SOURCE_GRAPHIC_LAYERS = {
    "top": "ETCH/TOP",
    "bottom": "ETCH/BOTTOM",
    "copper_top": "ETCH/TOP",
    "copper_bottom": "ETCH/BOTTOM",
    "silkscreen_top": "PACKAGE GEOMETRY/SILKSCREEN_TOP",
    "silkscreen_bottom": "PACKAGE GEOMETRY/SILKSCREEN_BOTTOM",
    "assembly_top": "PACKAGE GEOMETRY/ASSEMBLY_TOP",
    "assembly_bottom": "PACKAGE GEOMETRY/ASSEMBLY_BOTTOM",
    "place_bound_top": "PACKAGE GEOMETRY/PLACE_BOUND_TOP",
    "place_bound_bottom": "PACKAGE GEOMETRY/PLACE_BOUND_BOTTOM",
    "paste_top": "PACKAGE GEOMETRY/PASTEMASK_TOP",
    "paste_bottom": "PACKAGE GEOMETRY/PASTEMASK_BOTTOM",
    "mask_top": "PACKAGE GEOMETRY/SOLDERMASK_TOP",
    "mask_bottom": "PACKAGE GEOMETRY/SOLDERMASK_BOTTOM",
    "document_top": "PACKAGE GEOMETRY/JLC_DOCUMENT",
    "mechanical": "PACKAGE GEOMETRY/JLC_MECHANICAL",
    "source_component_shape": "PACKAGE GEOMETRY/JLC_COMPONENT_SHAPE",
    "source_lead_shape": "PACKAGE GEOMETRY/JLC_LEAD_SHAPE",
    "source_component_polarity": "PACKAGE GEOMETRY/JLC_COMPONENT_POLARITY",
}


def write_allegro_outputs(library: LibraryIR, allegro_dir: Path, scripts_dir: Path) -> None:
    for footprint in library.footprints:
        if _source_exact(footprint):
            for warning in _source_data_warnings(footprint):
                if warning not in library.warnings:
                    library.warnings.append(warning)
    allegro_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    padstack_dir = allegro_dir / "padstacks"
    symbols_dir = allegro_dir / "symbols"
    build_scripts_dir = symbols_dir / "build_scripts"
    padstack_dir.mkdir(parents=True, exist_ok=True)
    symbols_dir.mkdir(parents=True, exist_ok=True)
    build_scripts_dir.mkdir(parents=True, exist_ok=True)
    write_padstack_specs(library, padstack_dir / "padstack_specs.csv")
    write_footprint_data(library, symbols_dir / "footprints.json")
    write_symbol_build_plan(library, symbols_dir / "symbol_build_plan.json")
    write_symbol_generation_scripts(library, build_scripts_dir)
    write_allegro_skill(library, scripts_dir / "generate_allegro.il")
    write_allegro_script(scripts_dir / "run_generate_allegro.scr")
    write_create_symbols_script(library, scripts_dir / "create_symbols.ps1")
    write_allegro_readme(allegro_dir / "README.md")


def write_padstack_specs(library: LibraryIR, path: Path) -> Path:
    rows = []
    seen = set()
    for footprint in library.footprints:
        for pad in footprint.pads:
            key = pad.padstack or _padstack_name(pad)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "padstack": key,
                    "role": pad.role,
                    "kind": pad.kind,
                    "shape": pad.shape,
                    "x_mm": fmt_mm(pad.size[0]),
                    "y_mm": fmt_mm(pad.size[1]),
                    "hole_mm": fmt_mm(pad.hole_diameter),
                    "slot_x_mm": fmt_mm(pad.slot_size[0]) if pad.slot_size else "",
                    "slot_y_mm": fmt_mm(pad.slot_size[1]) if pad.slot_size else "",
                    "plated": _plating_text(pad),
                    "layers": ",".join(pad.layers),
                }
            )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "padstack",
                "role",
                "kind",
                "shape",
                "x_mm",
                "y_mm",
                "hole_mm",
                "slot_x_mm",
                "slot_y_mm",
                "plated",
                "layers",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_footprint_data(library: LibraryIR, path: Path) -> Path:
    payload = {
        "footprints": [footprint.to_dict() for footprint in library.footprints],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_symbol_build_plan(library: LibraryIR, path: Path) -> Path:
    rows = []
    for footprint in library.footprints:
        dra_name = f"{footprint.name}.dra"
        psm_name = f"{footprint.name}.psm"
        rows.append(
            {
                "footprint": footprint.name,
                "dra": dra_name,
                "psm": psm_name,
                "type": "package",
                "skill_script": f"build_scripts/{_script_stem(footprint)}.il",
                "allegro_script": f"build_scripts/{_script_stem(footprint)}.scr",
                "padstacks": list(dict.fromkeys(pad.padstack or _padstack_name(pad) for pad in footprint.pads)),
                "build_command": f"allegro -nograph -symbol package {footprint.name} -s build_scripts/{_script_stem(footprint)}.scr",
                "compile_command": f"allegro_batch create_sym -p {dra_name} {psm_name}",
                "status": "script_ready",
                **({"refdes": source_refdes_report(footprint)} if _source_exact(footprint) else {}),
            }
        )
    payload = {
        "tool": "allegro symbol editor + allegro_batch create_sym",
        "notes": [
            "Cadence 17.4 help describes create_sym as the command line version of File->Create Symbol.",
            "Each footprint now has a generated SKILL build script and Allegro replay script.",
            "Run scripts/create_symbols.ps1 or convert --run-cadence to create .dra and then compile .psm.",
        ],
        "symbols": rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_symbol_generation_scripts(library: LibraryIR, build_scripts_dir: Path) -> None:
    for footprint in library.footprints:
        stem = _script_stem(footprint)
        ref_prefix = _ref_prefix_for_footprint(library, footprint)
        write_footprint_build_skill(footprint, build_scripts_dir / f"{stem}.il", ref_prefix=ref_prefix)
        write_footprint_build_script(footprint, build_scripts_dir / f"{stem}.scr")


def write_footprint_build_skill(footprint: Footprint, path: Path, ref_prefix: str = "U") -> Path:
    source_exact = _source_exact(footprint)
    if source_exact:
        _validate_source_footprint(footprint)
    proc_name = _procedure_name(footprint)
    lines = [
        f"; Generated by jlc2cadence v{__version__}",
        f"; Package symbol build script for {footprint.name}.",
        "; Run from Allegro symbol editor with: allegro -nograph -symbol package <name> -s <script>",
        "",
        f"procedure({proc_name}()",
        "  let((pad_list drill_data ps_id textOrient pinText result saveResult sourcePath sourceTextBlock sourceRefdes sourceNativeText sourceRefdesVerified)",
        f"    printf(\"jlc2cadence building package symbol %s\\n\" \"{_skill_string(footprint.name)}\")",
        "    axlDBChangeDesignUnits(\"millimeters\" 4)",
    ]
    if source_exact:
        lines.extend([
            '    unless(car(axlDBGetDesignUnits()) == "millimeters" && cadr(axlDBGetDesignUnits()) == 4',
            '      error("Source-exact: native millimeter units/accuracy could not be established\\n")',
            '    )',
        ])
    lines.extend(_design_extents_skill_lines(footprint))
    if source_exact:
        lines.append("    ; Source-exact conversion: no synthesized package geometry or apertures.")
        for warning in _source_data_warnings(footprint):
            lines.append(f'    printf("WARN: %s\\n" "{_skill_string(warning)}")')
        for layer in sorted({_source_graphic_layer(graphic) for graphic in footprint.graphics
                             if not _source_graphic_issue(graphic)} - {None}):
            if not layer.startswith("ETCH/"):
                lines.extend([
                    f'    unless(axlLayerCreateNonConductor("{layer}")',
                    f'      error("Source-exact: could not create source layer %s\\n" "{layer}")',
                    '    )',
                ])
    lines.append("")

    for pad in _unique_padstacks(footprint):
        lines.extend(_padstack_skill_lines(pad, source_exact=source_exact))
        lines.append("")

    lines.extend(
        [
            "    textOrient = make_axlTextOrientation(?textBlock \"1\" ?rotation 0.0 ?mirrored nil ?justify \"center\")",
        ]
    )
    for pad in footprint.pads:
        lines.extend(_pin_skill_lines(pad, source_exact=source_exact))

    if footprint.graphics:
        lines.append("")
        lines.append("    ; Source graphics")
    for graphic in footprint.graphics:
        lines.extend(_graphic_skill_lines(graphic, source_exact=source_exact))

    lines.append("")
    if not source_exact:
        lines.extend(_place_bound_skill_lines(footprint))
        lines.extend(_reference_text_skill_lines(footprint, ref_prefix))
    else:
        lines.extend(_source_reference_text_skill_lines(footprint))
    lines.extend(
        [
            "",
            "    saveResult = axlSaveDesign(?mode \"nocheck\" ?noConfirm t ?writeModel t)",
            "    unless(saveResult",
            f"      {'error' if source_exact else 'printf'}(\"ERROR: axlSaveDesign failed for %s\\n\" \"{_skill_string(footprint.name)}\")",
            "    )",
            f"    printf(\"jlc2cadence finished package symbol %s\\n\" \"{_skill_string(footprint.name)}\")",
            "    saveResult",
            "  )",
            ")",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_footprint_build_script(footprint: Footprint, path: Path) -> Path:
    stem = _script_stem(footprint)
    proc_name = _procedure_name(footprint)
    path.write_text(
        "\n".join(
            [
                "# Allegro replay script generated by jlc2cadence.",
                "setwindow pcb",
                "scriptmode +noerrors",
                "scriptmode +invisible",
                f"skill load(\"build_scripts/{stem}.il\")",
                f"({proc_name})",
                "exit",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_allegro_skill(library: LibraryIR, path: Path) -> Path:
    lines = [
        f"; Generated by jlc2cadence v{__version__}",
        "; Purpose: reviewable Allegro 17.4 generation scaffold.",
        "; The data below is intentionally plain SKILL list data so the next stage can",
        "; bind it to axlDBCreateSymbol/axlDBCreatePin/padstack creation APIs.",
        "; This file also includes a verified-style compile helper based on Cadence",
        "; bundled examples that call axlOpenDesignForBatch/axlCompileSymbol.",
        "",
        "procedure(j2cPrintSummary()",
        "  printf(\"jlc2cadence footprints: %%d\\n\" %d)" % len(library.footprints),
        ")",
        "",
        "j2cFootprints = list(",
    ]
    for footprint in library.footprints:
        lines.extend(_footprint_skill_lines(footprint))
    lines.extend(
        [
            ")",
            "",
            "procedure(j2cCompileExistingDra(symbolDir)",
            "  let((oldDir files draList dra saveFile openResult)",
            "    oldDir = getWorkingDir()",
            "    changeWorkingDir(symbolDir)",
            "    files = getDirFiles(symbolDir)",
            "    draList = setof(x files (lastelem(axlDMFileParts(x)) == \"dra\"))",
            "    foreach(dra draList",
            "      printf(\"Compiling %s\\n\" dra)",
            "      openResult = axlOpenDesignForBatch(dra \"wf\")",
            "      if(openResult == nil then",
            "        printf(\"ERROR opening %s\\n\" dra)",
            "      else",
            "        saveFile = caddr(axlDMFileParts(axlGetDrawingName()))",
            "        axlSaveDesign(?design saveFile ?mode \"nocheck\" ?noConfirm t ?writeModel t)",
            "        axlCompileSymbol(?symbol saveFile ?silent t ?silentOnOverwrite t)",
            "      )",
            "    )",
            "    changeWorkingDir(oldDir)",
            "  )",
            ")",
            "",
            "j2cPrintSummary()",
            "",
            "; TODO next stage:",
            "; 1. Review allegro/symbols/build_scripts/*.il for per-footprint .dra creation.",
            "; 2. Run scripts/create_symbols.ps1 or convert --run-cadence to build .dra and .psm.",
            "; 3. Use j2cCompileExistingDra(\"<symbols-dir>\") to recompile reviewed .dra files.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_allegro_script(path: Path) -> None:
    path.write_text(
        """# Allegro script generated by jlc2cadence.
skill load("generate_allegro.il")
; After .dra files exist, run:
; j2cCompileExistingDra("../allegro/symbols")
""",
        encoding="utf-8",
    )


def write_create_symbols_script(library: LibraryIR, path: Path) -> None:
    lines = [
        f"# Generated by jlc2cadence v{__version__}",
        "param(",
        "  [string]$Allegro = 'allegro.exe',",
        "  [string]$AllegroBatch = 'allegro_batch.exe',",
        "  [string]$SymbolDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'allegro\\symbols')",
        ")",
        "$ErrorActionPreference = 'Stop'",
        "$Plan = Join-Path $SymbolDir 'symbol_build_plan.json'",
        "if (-not (Test-Path -LiteralPath $Plan)) { throw \"Missing symbol build plan: $Plan\" }",
        "$Data = Get-Content -LiteralPath $Plan -Raw | ConvertFrom-Json",
        "$Results = @()",
        "foreach ($Symbol in $Data.symbols) {",
        "  $Dra = Join-Path $SymbolDir $Symbol.dra",
        "  $Psm = Join-Path $SymbolDir $Symbol.psm",
        "  $Scr = Join-Path $SymbolDir $Symbol.allegro_script",
        "  $BuildStdout = Join-Path $SymbolDir \"$($Symbol.footprint).allegro.stdout.log\"",
        "  $BuildStderr = Join-Path $SymbolDir \"$($Symbol.footprint).allegro.stderr.log\"",
        "  $RemovedStaleTargets = @()",
        "  $BuildExit = $null",
        "  $BuildStatus = 'not_run'",
        "  if (-not (Test-Path -LiteralPath $Scr)) {",
        "    Write-Warning \"Missing Allegro build script for $($Symbol.footprint): $Scr\"",
        "    $BuildStatus = 'missing_build_script'",
        "  } else {",
        "    foreach ($Target in @($Dra, $Psm)) {",
        "      if (Test-Path -LiteralPath $Target) {",
        "        Remove-Item -LiteralPath $Target -Force",
        "        $RemovedStaleTargets += $Target",
        "      }",
        "    }",
        "    $Args = @('-nograph', '-symbol', 'package', $Symbol.footprint, '-s', $Scr)",
        "    $Proc = Start-Process -FilePath $Allegro -ArgumentList $Args -WorkingDirectory $SymbolDir -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $BuildStdout -RedirectStandardError $BuildStderr",
        "    $BuildExit = $Proc.ExitCode",
        "    $BuildStatus = if ($BuildExit -eq 0 -and (Test-Path -LiteralPath $Dra)) { 'created_dra' } else { 'failed_dra' }",
        "  }",
        "  if (-not (Test-Path -LiteralPath $Dra)) {",
        "    Write-Warning \"Missing DRA for $($Symbol.footprint): $Dra\"",
        "    $Results += [pscustomobject]@{ footprint=$Symbol.footprint; dra=$Dra; psm=$Psm; status=$BuildStatus; buildExitCode=$BuildExit; compileExitCode=$null }",
        "    continue",
        "  }",
        "  & $AllegroBatch create_sym -p $Dra $Psm",
        "  $ExitCode = $LASTEXITCODE",
        "  $Status = if ($ExitCode -eq 0 -and (Test-Path -LiteralPath $Psm)) { 'created_psm' } else { 'failed' }",
        "  $Results += [pscustomobject]@{ footprint=$Symbol.footprint; dra=$Dra; psm=$Psm; status=$Status; buildStatus=$BuildStatus; buildExitCode=$BuildExit; compileExitCode=$ExitCode; removedStaleTargets=$RemovedStaleTargets }",
        "}",
        "$Results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $SymbolDir 'create_symbols.results.json') -Encoding UTF8",
        "$Results",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_allegro_readme(path: Path) -> None:
    path.write_text(
        """# Allegro output

This directory contains footprint and padstack generation data.

- `padstacks/padstack_specs.csv`: normalized padstack definitions.
- `symbols/footprints.json`: normalized footprint geometry for `.dra/.psm` generation.
- `symbols/symbol_build_plan.json`: expected `.dra -> .psm` build plan.
- `symbols/build_scripts/*.il`: per-footprint SKILL scripts that create padstacks, pins, and graphics.
- `symbols/build_scripts/*.scr`: Allegro replay scripts for package symbol editor automation.
- `../scripts/generate_allegro.il`: SKILL scaffold with footprint data.
- `../scripts/create_symbols.ps1`: PowerShell wrapper for Allegro `.dra` build and `allegro_batch create_sym -p`.

The MVP does not directly write Cadence binary database files. Use the generated data for
Cadence 17.4 SKILL/batch automation. The per-footprint scripts are generated from IR
and are intended to create `.pad` and `.dra` files in Allegro package symbol editor.
The PowerShell wrapper then uses Cadence's verified command-line `create_sym` path
to compile `.dra` files into `.psm`.
""",
        encoding="utf-8",
    )


def _footprint_skill_lines(footprint: Footprint) -> list[str]:
    lines = [
        "  list(",
        f"    list('name \"{_skill_string(footprint.name)}\")",
        "    list('pads",
        "      list(",
    ]
    for pad in footprint.pads:
        lines.append(
            "        list('number \"{number}\" 'x {x} 'y {y} 'rotation {rotation} 'padstack \"{padstack}\" 'role \"{role}\")".format(
                number=_skill_string(pad.number),
                x=fmt_mm(pad.x),
                y=fmt_mm(pad.y),
                rotation=fmt_mm(pad.rotation),
                padstack=_skill_string(pad.padstack or _padstack_name(pad)),
                role=_skill_string(pad.role),
            )
        )
    lines.extend(["      )", "    )", "  )"])
    return lines


def _unique_padstacks(footprint: Footprint) -> list[Pad]:
    seen: dict[str, Pad] = {}
    pads: list[Pad] = []
    for pad in footprint.pads:
        name = pad.padstack or _padstack_name(pad)
        if name in seen:
            if _source_exact(footprint) and _source_padstack_signature(seen[name]) != _source_padstack_signature(pad):
                raise ValueError(f"Source-exact footprint {footprint.name}: conflicting padstack definitions for {name}")
            continue
        seen[name] = pad
        pads.append(pad)
    return pads


def _padstack_skill_lines(pad: Pad, source_exact: bool = False) -> list[str]:
    name = _skill_string(pad.padstack or _padstack_name(pad))
    drill = _drill_skill_expr(pad)
    lines = [
        f"    ; Padstack {name}",
        "    pad_list = nil",
    ]
    for layer in _regular_pad_layers(pad):
        lines.extend(_padstack_pad_lines(pad, layer, "REGULAR"))
    if source_exact:
        for layer, key in SOURCE_APERTURES:
            size = _source_aperture(pad, key)
            if size is not None:
                lines.extend(_padstack_pad_lines(pad, layer, "REGULAR", size=size))
    else:
        for layer in _mask_layers(pad):
            lines.extend(_padstack_pad_lines(pad, layer, "REGULAR"))
    lines.extend(
        [
            f"    drill_data = {drill}",
            f"    ps_id = axlDBCreatePadStack(\"{name}\" drill_data pad_list t)",
            "    unless(ps_id",
            f"      {'error' if source_exact else 'printf'}(\"ERROR: failed to create padstack %s\\n\" \"{name}\")",
            "    )",
            "    when(ps_id",
            f"      {'unless(' if source_exact else ''}axlPadstackToDisk(\"{name}\" \"../padstacks/{name}\")",
            *([f'        error("Source-exact: could not write padstack %s\\n" "{name}")', "      )"] if source_exact else []),
            "    )",
        ]
    )
    return lines


def _padstack_pad_lines(pad: Pad, layer: str, pad_type: str, size: tuple[float, float] | None = None) -> list[str]:
    size = pad.size if size is None else size
    figure_pad = pad if size == pad.size else replace(pad, size=size)
    figure, extra = _pad_figure(figure_pad)
    return [
        "    pad_list = cons(make_axlPadStackPad(",
        f"      ?layer \"{layer}\" ?type '{pad_type} ?figure {figure}",
        f"      ?figureSize {_skill_point(size[0], size[1])}{extra}) pad_list)",
    ]


def _pin_skill_lines(pad: Pad, source_exact: bool = False) -> list[str]:
    name = _skill_string(pad.padstack or _padstack_name(pad))
    number = _skill_string(pad.number)
    if not pad.is_electrical():
        return [
            f"    ; Mechanical pad {number} role={_skill_string(pad.role)}",
            f"    result = axlDBCreatePin(\"{name}\" {_skill_point(pad.x, pad.y)} nil {_skill_num(pad.rotation)})",
            "    unless(result",
            f"      {'error' if source_exact else 'printf'}(\"ERROR: failed to create mechanical pad %s with padstack %s\\n\" \"{number}\" \"{name}\")",
            "    )",
        ]
    return [
        f"    pinText = make_axlPinText(?number \"{number}\" ?offset 0:0 ?text textOrient)",
        f"    result = axlDBCreatePin(\"{name}\" {_skill_point(pad.x, pad.y)} pinText {_skill_num(pad.rotation)})",
        "    unless(result",
        f"      {'error' if source_exact else 'printf'}(\"ERROR: failed to create pin %s with padstack %s\\n\" \"{number}\" \"{name}\")",
        "    )",
    ]


def _graphic_skill_lines(graphic, source_exact: bool = False) -> list[str]:
    if source_exact:
        return _source_graphic_skill_lines(graphic)
    layer = _graphic_layer_to_allegro(graphic.layer)
    width = _skill_num(graphic.width)
    if graphic.kind == "line" and len(graphic.points) >= 2:
        points = " ".join(_skill_point(x, y) for x, y in graphic.points)
        return [f"    axlDBCreateLine(list({points}) {width} \"{layer}\")"]
    if graphic.kind == "region" and len(graphic.points) >= 2:
        points = list(graphic.points)
        if points[0] != points[-1]:
            points.append(points[0])
        point_text = " ".join(_skill_point(x, y) for x, y in points)
        return [f"    axlDBCreateLine(list({point_text}) {width or '0.01'} \"{layer}\")"]
    if graphic.kind == "text" and graphic.text:
        text = _skill_string(graphic.text)
        return [
            "    textOrient = make_axlTextOrientation(?textBlock \"1\" ?rotation %s ?mirrored nil ?justify \"center\")"
            % _skill_num(graphic.rotation),
            f"    axlDBCreateText(\"{text}\" {_skill_point(graphic.x, graphic.y)} textOrient \"{layer}\" nil)",
        ]
    return [f"    ; Unsupported graphic skipped: {graphic.kind} on {graphic.layer}"]


def _source_exact(item) -> bool:
    return item.properties.get("Conversion_Policy") == "source_exact"


def _source_aperture(pad: Pad, key: str) -> tuple[float, float] | None:
    raw = pad.properties.get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Source-exact pad {pad.number}: invalid aperture {key}") from exc
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"Source-exact pad {pad.number}: {key} must be [width,height] or null")
    if any(isinstance(dimension, bool) or not isinstance(dimension, (int, float))
           or not math.isfinite(dimension) or dimension <= 0 for dimension in value):
        raise ValueError(f"Source-exact pad {pad.number}: {key} has invalid dimensions")
    return float(value[0]), float(value[1])


def _source_pad_radius(pad: Pad) -> float:
    try:
        radius = float(pad.properties["corner_radius_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Source-exact pad {pad.number}: roundrect corner_radius_mm is missing or invalid") from exc
    if not math.isfinite(radius) or not 0 <= radius <= min(pad.size) / 2:
        raise ValueError(f"Source-exact pad {pad.number}: invalid roundrect corner_radius_mm")
    return radius


def _source_padstack_signature(pad: Pad) -> str:
    payload = {
        "kind": pad.kind, "role": pad.role, "shape": pad.shape,
        "size": pad.size, "hole_diameter": pad.hole_diameter, "slot_size": pad.slot_size,
        "layers": sorted(pad.layers), "plated": pad.properties.get("plated", "unknown"),
        "corner_radius_mm": pad.properties.get("corner_radius_mm", "unknown"),
        "slot_shape": pad.properties.get("Source_Slot_Shape", "unknown"),
        "copper_absent": pad.properties.get("Source_Copper_Absent", "false"),
        "apertures": {key: _source_aperture(pad, key) if key in pad.properties else "unknown"
                      for _, key in SOURCE_APERTURES},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_source_footprint(footprint: Footprint) -> None:
    if footprint.units != "mm":
        raise ValueError(f"Source-exact footprint {footprint.name}: normalized millimeter units are required")
    for pad in footprint.pads:
        if not _source_exact(pad):
            raise ValueError(f"Source-exact footprint {footprint.name}: pad {pad.number} is missing its conversion policy")
        if pad.kind not in {"smd", "thru_hole"}:
            raise ValueError(f"Source-exact pad {pad.number}: unsupported pad kind {pad.kind}")
        if pad.shape.lower() not in {"rect", "rectangle", "square", "circle", "oval", "octagon", "roundrect"}:
            error_type = UnsupportedGeometryError if pad.shape.lower() in {"polygon", "custom"} else ValueError
            raise error_type(f"Source-exact pad {pad.number}: unsupported copper shape {pad.shape}")
        values = (pad.x, pad.y, pad.rotation, *pad.size, pad.hole_diameter, *(pad.slot_size or ()))
        if any(not math.isfinite(value) for value in values) or min(pad.size) <= 0 or pad.hole_diameter < 0:
            raise ValueError(f"Source-exact pad {pad.number}: invalid copper or drill dimensions")
        if pad.shape.lower() in {"circle", "square"} and not math.isclose(*pad.size, abs_tol=1e-9):
            raise ValueError(f"Source-exact pad {pad.number}: {pad.shape} requires equal source width and height")
        if pad.is_electrical() and not pad.number:
            raise ValueError("Source-exact electrical pad is missing its source pin number")
        if pad.properties.get("Source_Copper_Absent") == "true":
            if pad.layers or pad.role != "npth" or pad.hole_diameter <= 0 or pad.properties.get("plated") != "false":
                raise ValueError(f"Source-exact pad {pad.number}: inconsistent source-only mounting hole")
        elif not pad.layers or set(pad.layers) - {"top", "bottom", "all", "inner"}:
            raise ValueError(f"Source-exact pad {pad.number}: unsupported copper layers {pad.layers}")
        if pad.kind == "smd" and pad.layers not in (["top"], ["bottom"]):
            raise ValueError(f"Source-exact pad {pad.number}: unsupported SMD copper layer combination {pad.layers}")
        if pad.kind == "thru_hole" and pad.properties.get("plated") not in {"true", "false"}:
            raise ValueError(f"Source-exact pad {pad.number}: source drill plating is missing")
        if pad.kind == "smd" and (pad.hole_diameter or pad.slot_size):
            raise UnsupportedGeometryError(f"Source-exact pad {pad.number}: drilled SMD geometry is unsupported")
        if pad.slot_size and (min(pad.slot_size) <= 0 or pad.properties.get("Source_Slot_Shape") not in {"oval", "rect"}):
            raise ValueError(f"Source-exact pad {pad.number}: source slot shape or dimensions are missing")
        for key in ("hole_offset_x_mm", "hole_offset_y_mm", "hole_rotation"):
            value = float(pad.properties.get(key, "0"))
            if value != 0:
                error_type = UnsupportedGeometryError if math.isfinite(value) else ValueError
                raise error_type(f"Source-exact pad {pad.number}: unsupported nonzero {key}")
        if "Source_Drill_Offset_mm" in pad.properties:
            valid_offset = False
            try:
                offset = json.loads(pad.properties["Source_Drill_Offset_mm"])
                valid_offset = isinstance(offset, list) and len(offset) == 2 and all(math.isfinite(float(value)) for value in offset)
                centered = valid_offset and all(abs(float(value)) <= 1e-10 for value in offset)
            except (TypeError, ValueError):
                centered = False
            if not centered:
                error_type = UnsupportedGeometryError if valid_offset else ValueError
                raise error_type(f"Source-exact pad {pad.number}: unsupported offset drill geometry")
        if pad.slot_size:
            try:
                relative = (float(pad.properties["Source_Drill_Rotation_deg"]) - pad.rotation) % 180
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Source-exact pad {pad.number}: source slot orientation is missing") from exc
            if not math.isclose(relative, 90, abs_tol=1e-8):
                error_type = UnsupportedGeometryError if math.isfinite(relative) else ValueError
                raise error_type(f"Source-exact pad {pad.number}: unsupported source slot angle relative to copper")
        if pad.shape.lower() == "roundrect":
            _source_pad_radius(pad)
        for _, key in SOURCE_APERTURES:
            size = _source_aperture(pad, key)
            if pad.shape.lower() == "roundrect" and size is not None and size != pad.size:
                raise ValueError(f"Source-exact pad {pad.number}: {key} roundrect aperture radius is not specified")
    _unique_padstacks(footprint)
    for graphic in footprint.graphics:
        if graphic.properties.get("region_type") == "npth":
            raise UnsupportedGeometryError(f"Source-exact footprint {footprint.name}: unsupported source non-plated drill region on {graphic.layer}")
        issue = _source_graphic_issue(graphic)
        if issue and _source_is_copper(graphic):
            error_type = UnsupportedGeometryError if issue.endswith(": source copper cutout/drill polygon unsupported") else ValueError
            raise error_type(f"Source-exact footprint {footprint.name}: {issue}")


def _source_is_copper(graphic: Graphic) -> bool:
    return graphic.layer.lower() in {"top", "bottom", "copper_top", "copper_bottom", "1", "2", "11"}


def _source_graphic_layer(graphic: Graphic) -> str | None:
    layer = SOURCE_GRAPHIC_LAYERS.get(graphic.layer.lower())
    region_type = graphic.properties.get("region_type", "").lower()
    if layer and region_type in {"cutout", "npth"} and not _source_is_copper(graphic):
        if graphic.layer.lower() in {"source_component_shape", "source_lead_shape", "source_component_polarity"}:
            return f"{layer}_{region_type.upper()}"
        return None
    if layer and graphic.kind == "text" and graphic.properties.get("text_type") == "N":
        if graphic.layer.lower() in {"silkscreen_top", "silkscreen_bottom", "assembly_top", "assembly_bottom"}:
            return layer.replace("PACKAGE GEOMETRY/", "REF DES/")
    return layer


def _source_text_issue(graphic: Graphic) -> str | None:
    props = graphic.properties
    if props.get("visible") == "false" or props.get("source_geometry_exported") == "true":
        return None
    for key in ("width_mm", "height_mm", "stroke_width_mm", "line_spacing_mm", "char_spacing_mm"):
        try:
            value = float(props[key])
        except (KeyError, TypeError, ValueError):
            return f"source text {graphic.text!r} on {graphic.layer}: {key} unavailable; native text omitted"
        if not math.isfinite(value) or value < 0 or (key in {"width_mm", "height_mm"} and value <= 0):
            return f"source text {graphic.text!r} on {graphic.layer}: invalid {key}; native text omitted"
    if props.get("justify") not in {"left", "center", "right"}:
        return f"source text {graphic.text!r} on {graphic.layer}: justification unavailable; native text omitted"
    if props.get("font") != "cadence_stroke":
        return f"source text {graphic.text!r} on {graphic.layer}: source font has no verified native equivalent; native text omitted"
    if props.get("visible") not in {"true", "false"} or props.get("mirrored") not in {"true", "false"}:
        return f"source text {graphic.text!r} on {graphic.layer}: source visibility/mirroring unavailable; native text omitted"
    return None


def _source_graphic_issue(graphic: Graphic) -> str | None:
    label = f"source {graphic.kind} on {graphic.layer}"
    if _source_graphic_layer(graphic) is None:
        return f"{label}: unsupported source layer or negative-region semantics; native graphic omitted"
    values = (graphic.x, graphic.y, graphic.width, graphic.rotation, *(value for point in graphic.points for value in point))
    if any(not math.isfinite(value) for value in values) or graphic.width < 0:
        return f"{label}: invalid coordinates or stroke; native graphic omitted"
    if graphic.kind == "line":
        if len(graphic.points) < 2:
            return f"{label}: source path has fewer than two points; native graphic omitted"
    elif graphic.kind == "circle":
        try:
            radius = float(graphic.properties["radius_mm"])
        except (KeyError, TypeError, ValueError):
            return f"{label}: source radius missing; native graphic omitted"
        if len(graphic.points) != 1 or not math.isfinite(radius) or radius <= 0:
            return f"{label}: invalid source circle geometry; native graphic omitted"
    elif graphic.kind == "arc":
        try:
            center = float(graphic.properties["center_x_mm"]), float(graphic.properties["center_y_mm"])
        except (KeyError, TypeError, ValueError):
            return f"{label}: source center missing; native graphic omitted"
        if len(graphic.points) != 2 or not all(math.isfinite(value) for value in center) or graphic.properties.get("clockwise") not in {"true", "false"}:
            return f"{label}: source arc endpoints/direction missing; native graphic omitted"
        radii = [math.dist(point, center) for point in graphic.points]
        if min(radii) <= 0 or not math.isclose(*radii, rel_tol=1e-8, abs_tol=1e-8):
            return f"{label}: inconsistent source arc radii; native graphic omitted"
    elif graphic.kind == "region":
        if len(graphic.points) < 3 or graphic.properties.get("filled") not in {"true", "false"}:
            return f"{label}: source polygon/fill semantics unavailable; native graphic omitted"
        if graphic.properties.get("region_type") in {"cutout", "npth"} and _source_is_copper(graphic):
            return f"{label}: source copper cutout/drill polygon unsupported"
    elif graphic.kind == "text":
        return _source_text_issue(graphic)
    else:
        return f"{label}: unsupported source primitive; native graphic omitted"
    return None


def _native_refdes_case(text: str) -> str:
    return text.translate(str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def _refdes_case_adaptation(text: str) -> dict | None:
    native = _native_refdes_case(text)
    if native == text:
        return None
    return {
        "rule": "allegro_refdes_ascii_uppercase",
        "source_text": text,
        "native_text": native,
        "documentation": "Cadence SPB 17.4 algroplace/chap6.html: reference designators and symbol names are stored in uppercase",
    }


def source_refdes_report(footprint: Footprint) -> dict:
    props = footprint.properties
    source = {
        "prefix": props.get("Source_Refdes_Prefix", ""),
        "field": props.get("Source_Refdes_Prefix_Source", ""),
        "raw": props.get("Source_Refdes_Prefix_Raw", ""),
        "component": props.get("Source_Refdes_Component", ""),
        "status": props.get("Source_Refdes_Prefix_Status", "missing"),
    }
    original = [graphic for graphic in footprint.graphics
                if graphic.kind == "text" and graphic.properties.get("text_type") == "N"]
    native = [graphic for graphic in original
              if graphic.properties.get("visible") != "false"
              and graphic.properties.get("source_geometry_exported") != "true"
              and not _source_graphic_issue(graphic)
              and (_source_graphic_layer(graphic) or "").startswith("REF DES/")]
    report = {
        "status": "missing_source_prefix",
        "original_refdes_count": len(original),
        "native_source_refdes_count": len(native),
        "source": source,
        "target": None,
        "position_policy": None,
        "native_case_adaptations": [],
    }
    if original:
        report["status"] = "source_preserved" if native else "source_refdes_unavailable"
        report["position_policy"] = "original_source"
        report["native_case_adaptations"] = [adaptation for graphic in native
                                             if (adaptation := _refdes_case_adaptation(graphic.text))]
        return report
    if source["status"] == "conflict":
        report["status"] = "conflicting_source_prefix"
        return report
    if source["status"] != "verified":
        return report
    prefix = source["prefix"]
    if (not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z]+", prefix)
            or not all(isinstance(source[key], str) and source[key] for key in ("field", "raw", "component"))
            or not re.fullmatch(re.escape(prefix) + r"(?:[?*]|[0-9]+)?", source["raw"].strip())):
        report["status"] = "invalid_source_prefix"
        return report
    min_x, min_y, max_x, max_y = _source_refdes_bounds(footprint)
    report.update({
        "status": "derived_from_component",
        "position_policy": "center_above_conservative_original_geometry_bounds_by_0.5mm",
        "source_geometry_bounds_mm": [min_x, min_y, max_x, max_y],
        "target": {
            "text": _native_refdes_case(prefix) + "*",
            "layer": "REF DES/ASSEMBLY_TOP",
            "position_mm": [(min_x + max_x) / 2, max_y + 0.5],
            "rotation": 0.0,
            "mirrored": False,
            "justification": "center",
            "text_block": "2",
            "text_style_policy": "existing_native_text_block_2",
            "role": "cadence_required_dynamic_refdes",
        },
    })
    adaptation = _refdes_case_adaptation(prefix + "*")
    if adaptation:
        report["native_case_adaptations"].append(adaptation)
    return report


def _source_refdes_bounds(footprint: Footprint) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = footprint_bounds(footprint)
    for pad in footprint.pads:
        angle = math.radians(pad.rotation)
        half_x = (abs(pad.size[0] * math.cos(angle)) + abs(pad.size[1] * math.sin(angle))) / 2
        half_y = (abs(pad.size[0] * math.sin(angle)) + abs(pad.size[1] * math.cos(angle))) / 2
        min_x, max_x = min(min_x, pad.x - half_x), max(max_x, pad.x + half_x)
        min_y, max_y = min(min_y, pad.y - half_y), max(max_y, pad.y + half_y)
    for graphic in footprint.graphics:
        if _source_graphic_issue(graphic):
            continue
        if graphic.kind == "circle":
            center = graphic.points[0]
            radius = float(graphic.properties["radius_mm"]) + graphic.width / 2
        elif graphic.kind == "arc":
            center = float(graphic.properties["center_x_mm"]), float(graphic.properties["center_y_mm"])
            radius = math.dist(graphic.points[0], center) + graphic.width / 2
        elif graphic.kind == "text" and graphic.properties.get("visible") != "false" and graphic.properties.get("source_geometry_exported") != "true":
            center = graphic.x, graphic.y
            lines = graphic.text.splitlines() or [""]
            longest = max(map(len, lines))
            radius = math.hypot(longest * (float(graphic.properties["width_mm"]) + float(graphic.properties["char_spacing_mm"])),
                                len(lines) * (float(graphic.properties["height_mm"]) + float(graphic.properties["line_spacing_mm"])))
        else:
            for x, y in graphic.points:
                min_x, max_x = min(min_x, x - graphic.width / 2), max(max_x, x + graphic.width / 2)
                min_y, max_y = min(min_y, y - graphic.width / 2), max(max_y, y + graphic.width / 2)
            continue
        min_x, max_x = min(min_x, center[0] - radius), max(max_x, center[0] + radius)
        min_y, max_y = min(min_y, center[1] - radius), max(max_y, center[1] + radius)
    return min_x, min_y, max_x, max_y


def _source_data_warnings(footprint: Footprint) -> list[str]:
    warnings = []
    for layer, key in SOURCE_APERTURES:
        missing = [pad.number or "<mechanical>" for pad in footprint.pads if key not in pad.properties]
        if missing:
            warnings.append(f"Source-exact footprint {footprint.name}: {layer} source aperture data unavailable for pads {', '.join(missing)}; omitted without inferred expansion")
    for index, graphic in enumerate(footprint.graphics, 1):
        issue = _source_graphic_issue(graphic)
        if issue:
            warnings.append(f"Source-exact footprint {footprint.name}, graphic {index}: {issue}")
        elif graphic.properties.get("region_type") in {"cutout", "npth"}:
            warnings.append(f"Source-exact footprint {footprint.name}, graphic {index}: source {graphic.properties['region_type']} on {graphic.layer} has only its original boundary retained on diagnostic layer {_source_graphic_layer(graphic)}; negative fill semantics are not converted")
    refdes = source_refdes_report(footprint)
    if refdes["status"] == "derived_from_component":
        source = refdes["source"]
        warnings.append(f"Source-exact footprint {footprint.name}: Cadence-required dynamic reference text {refdes['target']['text']} added on REF DES/ASSEMBLY_TOP from component {source['component']} field {source['field']}={source['raw']!r}; positioned above original geometry with native text block 2; no original footprint geometry changed")
    elif refdes["status"] != "source_preserved":
        warnings.append(f"Source-exact footprint {footprint.name}: native reference-designator text unavailable ({refdes['status']}); no helper text added; Cadence package compilation may fail with SPMHCS-1")
    for adaptation in refdes["native_case_adaptations"]:
        warnings.append(f"Source-exact footprint {footprint.name}: Allegro stores reference-designator text {adaptation['source_text']!r} as {adaptation['native_text']!r}; ASCII case adaptation recorded without changing the original component prefix or graphics")
    if not any(graphic.layer.lower().startswith("place_bound_") for graphic in footprint.graphics):
        warnings.append(f"Source-exact footprint {footprint.name}: source placement boundary unavailable; no package boundary synthesized")
    values = [value for pad in footprint.pads for value in (pad.x, pad.y, *pad.size, pad.hole_diameter, *(pad.slot_size or ()))]
    values += [value for graphic in footprint.graphics for value in (graphic.x, graphic.y, graphic.width, *(value for point in graphic.points for value in point))]
    if any(abs(value - round(value, 4)) > 1e-10 for value in values):
        warnings.append(f"Source-exact footprint {footprint.name}: Allegro 17.4 millimeter accuracy is limited to four decimals; native database quantization is at most 0.00005 mm per coordinate/dimension; full source precision remains in IR")
    return warnings


def _source_graphic_skill_lines(graphic: Graphic) -> list[str]:
    issue = _source_graphic_issue(graphic)
    if issue:
        if _source_is_copper(graphic):
            error_type = UnsupportedGeometryError if issue.endswith(": source copper cutout/drill polygon unsupported") else ValueError
            raise error_type(f"Source-exact: {issue}")
        return [f"    ; OMITTED: {issue.replace(chr(10), ' ').replace(chr(13), ' ')}"]
    if graphic.kind == "text" and (graphic.properties.get("visible") == "false"
                                   or graphic.properties.get("source_geometry_exported") == "true"):
        return ["    ; Source text retained as metadata; hidden or exported using original glyph paths."]
    layer = _source_graphic_layer(graphic)
    width = _skill_num(graphic.width)
    if graphic.kind == "line":
        points = " ".join(_skill_point(x, y) for x, y in graphic.points)
        lines = [f'    result = axlDBCreateLine(list({points}) {width} "{layer}")']
    elif graphic.kind == "circle":
        center = _skill_point(*graphic.points[0])
        radius = _skill_num(float(graphic.properties["radius_mm"]))
        lines = [f'    result = axlDBCreateCircle(list({center} {radius}) {width} "{layer}")']
    elif graphic.kind == "arc":
        start, end = (_skill_point(*point) for point in graphic.points)
        center = _skill_point(float(graphic.properties["center_x_mm"]), float(graphic.properties["center_y_mm"]))
        clockwise = "t" if graphic.properties["clockwise"] == "true" else "nil"
        lines = [f"    sourcePath = axlPathStart(list({start}))",
                 f"    unless(axlPathArcCenter(sourcePath {width} {end} {clockwise} {center})",
                 '      error("Source-exact: source circular arc could not be constructed\\n")',
                 "    )",
                 f'    result = axlDBCreatePath(sourcePath "{layer}")']
    elif graphic.kind == "region":
        points = " ".join(_skill_point(x, y) for x, y in graphic.points)
        negative = graphic.properties.get("region_type") in {"cutout", "npth"}
        filled = "t" if graphic.properties["filled"] == "true" and not negative else "nil"
        lines = [f"    sourcePath = axlPathStart(list({points}) {width})",
                 f'    result = axlDBCreateShape(sourcePath {filled} "{layer}")']
    else:
        props = graphic.properties
        mirrored = "t" if props["mirrored"] == "true" else "nil"
        lines = [
            "    sourceTextBlock = axlDBTextBlockCreate(0 ?width %s ?height %s ?lineSpace %s ?charSpace %s ?photoWidth %s)"
            % tuple(_skill_num(float(props[key])) for key in ("width_mm", "height_mm", "line_spacing_mm", "char_spacing_mm", "stroke_width_mm")),
            '    unless(sourceTextBlock error("Source-exact: source text metrics could not be created\\n"))',
            f'    textOrient = make_axlTextOrientation(?textBlock sourceTextBlock ?rotation {_skill_num(graphic.rotation)} ?mirrored {mirrored} ?justify "{props["justify"]}")',
            f'    result = axlDBCreateText("{_skill_string(graphic.text)}" {_skill_point(graphic.x, graphic.y)} textOrient "{layer}" nil)',
        ]
    lines.extend(["    unless(result",
                  f'      error("Source-exact: failed creating source %s on %s\\n" "{graphic.kind}" "{layer}")',
                  "    )"])
    if graphic.kind == "text" and layer.startswith("REF DES/"):
        lines.extend(_native_refdes_check_skill_lines(graphic.text, layer))
    return lines


def _native_refdes_check_skill_lines(text: str, layer: str) -> list[str]:
    text, layer = _skill_string(_native_refdes_case(text)), _skill_string(layer)
    return [
        "    unless(result && car(result) && length(car(result)) == 1",
        '      error("Source-exact: native reference-designator creation returned no single text object\\n")',
        "    )",
        "    sourceRefdes = caar(result)",
        '    unless(sourceRefdes->objType == "text"',
        f'           && sourceRefdes->layer == "{layer}" && sourceRefdes->text == "{text}"',
        '      error("Source-exact: native reference-designator object has wrong text or layer\\n")',
        "    )",
        "    sourceRefdesVerified = nil",
        "    foreach(sourceNativeText axlDBGetDesign()->text",
        "      when(sourceNativeText == sourceRefdes sourceRefdesVerified = t)",
        "    )",
        "    unless(sourceRefdesVerified",
        '      error("Source-exact: native reference-designator is absent from design text\\n")',
        "    )",
    ]


def _source_reference_text_skill_lines(footprint: Footprint) -> list[str]:
    report = source_refdes_report(footprint)
    if report["status"] != "derived_from_component":
        return []
    source, target = report["source"], report["target"]
    lines = [
        "    ; Cadence-required dynamic reference text derived from original component metadata.",
        f"    ; Source: {_skill_string(source['component'])} {_skill_string(source['field'])}={_skill_string(source['raw'])}",
        f"    ; Position: {report['position_policy']}; original footprint geometry is untouched.",
        '    textOrient = make_axlTextOrientation(?textBlock "2" ?rotation 0.0 ?mirrored nil ?justify "center")',
        f'    result = axlDBCreateText("{_skill_string(target["text"])}" {_skill_point(*target["position_mm"])} textOrient "{target["layer"]}" nil)',
    ]
    lines.extend(_native_refdes_check_skill_lines(target["text"], target["layer"]))
    return lines


def _place_bound_skill_lines(footprint: Footprint) -> list[str]:
    min_x, min_y, max_x, max_y = footprint_bounds(footprint)
    margin = 0.25
    return [
        "    ; Conservative package place bound",
        "    axlDBCreateRectangle(",
        "      list(%s %s) t \"PACKAGE GEOMETRY/PLACE_BOUND_TOP\")"
        % (_skill_point(min_x - margin, min_y - margin), _skill_point(max_x + margin, max_y + margin)),
    ]


def _design_extents_skill_lines(footprint: Footprint) -> list[str]:
    min_x, min_y, max_x, max_y = footprint_bounds(footprint)
    span = max(max_x - min_x, max_y - min_y, 1.0)
    text_margin = max(len(footprint.name), 4) * 0.75
    margin = max(10.0, span * 0.75, text_margin)
    return [
        "    ; Expand drawing extents before adding centered footprint geometry.",
        "    unless(axlDBChangeDesignExtents(list(%s %s))"
        % (_skill_point(min_x - margin, min_y - margin), _skill_point(max_x + margin, max_y + margin)),
        "      printf(\"WARN: axlDBChangeDesignExtents failed\\n\")",
        "    )",
    ]


def _reference_text_skill_lines(footprint: Footprint, ref_prefix: str) -> list[str]:
    min_x, min_y, max_x, max_y = footprint_bounds(footprint)
    center_x = (min_x + max_x) / 2
    refdes = f"{cadence_safe_name(ref_prefix or 'U', 'U')}*"
    return [
        "    ; Standard reference/value helper text",
        f"    axlDBCreateText(\"{_skill_string(refdes)}\" {_skill_point(center_x, max_y + 0.5)} \"2\" \"REF DES/ASSEMBLY_TOP\" nil)",
        f"    axlDBCreateText(\"{_skill_string(footprint.name)}\" {_skill_point(center_x, min_y - 0.5)} \"1\" \"PACKAGE GEOMETRY/ASSEMBLY_TOP\" nil)",
    ]


def _ref_prefix_for_footprint(library: LibraryIR, footprint: Footprint) -> str:
    for component in library.components:
        if component.footprint == footprint.name and component.ref_prefix:
            return component.ref_prefix.rstrip("?*") or "U"
    return "U"


def _regular_pad_layers(pad: Pad) -> list[str]:
    if _source_exact(pad) and pad.properties.get("Source_Copper_Absent") == "true":
        return []
    if pad.kind == "thru_hole":
        if _source_exact(pad):
            if "all" in pad.layers:
                return ["TOP", "DEFAULT_INTERNAL", "BOTTOM"]
            mapping = {"top": "TOP", "inner": "DEFAULT_INTERNAL", "bottom": "BOTTOM"}
            return list(dict.fromkeys(mapping[layer] for layer in pad.layers))
        return ["TOP", "DEFAULT_INTERNAL", "BOTTOM"]
    if "bottom" in pad.layers and "top" not in pad.layers:
        return ["BOTTOM"]
    return ["TOP"]


def _mask_layers(pad: Pad) -> list[str]:
    if pad.kind == "thru_hole":
        return ["SOLDERMASK_TOP", "SOLDERMASK_BOTTOM"]
    if "bottom" in pad.layers and "top" not in pad.layers:
        return ["SOLDERMASK_BOTTOM", "PASTEMASK_BOTTOM"]
    return ["SOLDERMASK_TOP", "PASTEMASK_TOP"]


def _drill_skill_expr(pad: Pad) -> str:
    if pad.slot_size:
        slot_shape = pad.properties.get("Source_Slot_Shape") if _source_exact(pad) else pad.shape.lower()
        hole_type = "'OVAL_SLOT" if slot_shape in {"oval", "roundrect"} else "'RECTANGLE_SLOT"
        return (
            "make_axlPadStackDrill(?holeType %s ?slotSize %s "
            "?plating %s)"
        ) % (hole_type, _skill_point(pad.slot_size[0], pad.slot_size[1]), _plating_symbol(pad))
    if pad.hole_diameter > 0:
        return (
            "make_axlPadStackDrill(?holeType 'CIRCLE_DRILL ?drillDiameter %s "
            "?plating %s)"
        ) % (_skill_num(pad.hole_diameter), _plating_symbol(pad))
    return "nil"


def _pad_figure(pad: Pad) -> tuple[str, str]:
    shape = pad.shape.lower()
    if shape == "circle":
        return "'CIRCLE", ""
    if shape == "roundrect":
        radius = _source_pad_radius(pad) if _source_exact(pad) else max(min(pad.size) * 0.125, 0.0)
        return "'ROUNDED_RECTANGLE", f" ?radius {_skill_num(radius)} ?corners \"UL-UR-LR-LL\""
    if shape == "oval":
        return ("'OBLONG_X" if pad.size[0] >= pad.size[1] else "'OBLONG_Y"), ""
    if shape == "octagon":
        return "'OCTAGON", ""
    if abs(pad.size[0] - pad.size[1]) < 0.000001:
        return "'SQUARE", ""
    return "'RECTANGLE", ""


def _graphic_layer_to_allegro(layer: str) -> str:
    mapping = {
        "silkscreen_top": "PACKAGE GEOMETRY/SILKSCREEN_TOP",
        "silkscreen_bottom": "PACKAGE GEOMETRY/SILKSCREEN_BOTTOM",
        "assembly_top": "PACKAGE GEOMETRY/ASSEMBLY_TOP",
        "assembly_bottom": "PACKAGE GEOMETRY/ASSEMBLY_BOTTOM",
        "place_bound_top": "PACKAGE GEOMETRY/PLACE_BOUND_TOP",
        "place_bound_bottom": "PACKAGE GEOMETRY/PLACE_BOUND_BOTTOM",
    }
    return mapping.get(layer.lower(), layer.upper())


def _script_stem(footprint: Footprint) -> str:
    return cadence_safe_name(footprint.name, "FOOTPRINT")


def _procedure_name(footprint: Footprint) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", _script_stem(footprint)).strip("_")
    if not stem or stem[0].isdigit():
        stem = f"FP_{stem}"
    return f"j2cBuildPackage_{stem}"


def _skill_point(x: float, y: float) -> str:
    return f"{_skill_num(x)}:{_skill_num(y)}"


def _skill_num(value: float) -> str:
    if abs(value) < 0.0000005:
        value = 0.0
    text = fmt_mm(value)
    if "." not in text:
        text = f"{text}.0"
    return text


def _padstack_name(pad: Pad) -> str:
    if _source_exact(pad):
        signature = _source_padstack_signature(pad)
        digest = sha256(signature.encode("utf-8")).hexdigest()[:16]
        return cadence_safe_name(f"source_{pad.kind}_{pad.shape}_{digest}")
    drill_part = (
        f"slot_{pad.slot_size[0]:.3f}x{pad.slot_size[1]:.3f}"
        if pad.slot_size
        else f"{pad.hole_diameter:.3f}"
    )
    qualifiers: list[str] = []
    if pad.role != "electrical":
        qualifiers.append(pad.role)
    if pad.kind == "thru_hole" and _plating_text(pad) == "false" and "np" not in pad.role:
        qualifiers.append("nonplated")
    prefix = "_".join(qualifiers + [pad.kind])
    return cadence_safe_name(f"{prefix}_{pad.shape}_{pad.size[0]:.3f}x{pad.size[1]:.3f}_{drill_part}")


def _plating_symbol(pad: Pad) -> str:
    return "'NON_PLATED" if _plating_text(pad) == "false" else "'PLATED"


def _plating_text(pad: Pad) -> str:
    return str(pad.properties.get("plated", "true")).strip().lower() or "true"


def _skill_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
