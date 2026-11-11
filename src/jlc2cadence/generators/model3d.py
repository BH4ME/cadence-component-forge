from __future__ import annotations

import json
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement, indent

from .. import __version__
from ..ir.schema import LibraryIR, Model3D
from .placeholder3d import is_placeholder_model


STEP_FORMATS = {"step", "stp"}
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def write_model_map(
    library: LibraryIR,
    models: list[Model3D],
    output_dir: Path,
    scripts_dir: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = _step_map_entries(library)
    footprint_rows = [_footprint_model_row(footprint.name, footprint.model3d) for footprint in library.footprints]
    transform_rows = _easyeda_transform_rows(library)
    placeholder_count = sum(1 for footprint in library.footprints if is_placeholder_model(footprint.model3d))
    payload = {
        "version": f"v{__version__}",
        "cadence": {
            "step_map": "cadence_step_package.map",
            "symbol_list": "sym_3d_update_symbols.txt",
            "importable_step_models": len(entries),
            "placeholder_step_models": placeholder_count,
            "easyeda_transform_sidecar": "easyeda_3d_transforms.json" if transform_rows else None,
            "status": "step_map_ready" if entries else "no_step_models",
        },
        "models": [model.to_dict() for model in models],
        "footprints": footprint_rows,
        "notes": [
            "Cadence Allegro 17.4 STEP binding is generated as a STEP Package Mapping .map file.",
            "Only STEP/STP models are written into cadence_step_package.map; OBJ models require manual review.",
            "Generated placeholder STEP envelope models are marked in model properties and should be replaced before mechanical signoff.",
            "The converter does not write PKGDEF_STEP_FILE or PKGDEF_STEP_TRANSFORMATION directly.",
        ],
    }
    path = output_dir / "3d_model_map.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    write_cadence_step_map(entries, output_dir / "cadence_step_package.map")
    write_sym_3d_update_list(entries, output_dir / "sym_3d_update_symbols.txt")
    write_easyeda_transform_sidecar(transform_rows, output_dir / "easyeda_3d_transforms.json")
    write_3d_readme(entries, footprint_rows, output_dir / "README.md")
    if scripts_dir is not None:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        write_step_mapping_script(entries, scripts_dir / "prepare_step_mapping.ps1")
        write_step_mapping_replay(scripts_dir / "run_step_package_mapping.scr")
        write_step_mapping_skill(
            entries,
            output_dir.parent / "allegro" / "symbols",
            scripts_dir / "apply_3d_mapping.il",
        )
        write_step_mapping_skill_replay(scripts_dir / "run_apply_3d_mapping.scr")
    return path


def write_cadence_step_map(entries: list[tuple[str, Model3D]], path: Path) -> Path:
    root = Element(
        "STEP-3D-MAPPING",
        {
            "revision": "A",
            "xmlns:xsi": XSI_NS,
            "xmlns:xsd": XSD_NS,
        },
    )
    SubElement(root, "Header", {"lengthUnit": "MM"})
    body = SubElement(root, "Body")
    for package, model in entries:
        item = SubElement(body, "MapItem", {"package": package})
        step_model = SubElement(item, "StepModel", {"type": "PRIMARY", "file": _model_file_name(model)})
        SubElement(
            step_model,
            "Offset",
            {
                "x": _fmt_offset(model.offset[0]),
                "y": _fmt_offset(model.offset[1]),
                "z": _fmt_offset(model.offset[2]),
            },
        )
        SubElement(
            step_model,
            "Rotation",
            {
                "x": _fmt_rotation(model.rotation[0]),
                "y": _fmt_rotation(model.rotation[1]),
                "z": _fmt_rotation(model.rotation[2]),
            },
        )
    tree = ElementTree(root)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def write_sym_3d_update_list(entries: list[tuple[str, Model3D]], path: Path) -> Path:
    lines = [f"{package}.dra" for package, _model in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def write_3d_readme(entries: list[tuple[str, Model3D]], footprint_rows: list[dict[str, object]], path: Path) -> Path:
    mapped = len(entries)
    manual = sum(1 for row in footprint_rows if row["status"] == "manual_review_non_step")
    missing = sum(1 for row in footprint_rows if row["status"] == "missing")
    placeholder = sum(1 for row in footprint_rows if row["status"] == "placeholder_step_map_ready")
    sized = sum(
        1
        for row in footprint_rows
        if isinstance(row.get("model3d"), dict) and row["model3d"].get("target_size") is not None
    )
    path.write_text(
        "\n".join(
            [
                "# 3D model output",
                "",
                "This directory contains copied 3D model files and Cadence Allegro 17.4 mapping assets.",
                "",
                "- `3d_model_map.json`: review data for every collected model and footprint link.",
                "- `cadence_step_package.map`: Cadence STEP Package Mapping XML for STEP/STP models.",
                "- `sym_3d_update_symbols.txt`: `.dra` symbol list for optional `sym_3d_update.exe` use.",
                "- `easyeda_3d_transforms.json`: normalized EasyEDA Pro V3 size/rotation/offset data when present.",
                "",
                f"Cadence STEP mapping entries: {mapped}",
                f"Generated placeholder STEP envelopes: {placeholder}",
                f"Non-STEP models needing manual review: {manual}",
                f"Footprints without a linked 3D model: {missing}",
                f"Models with EasyEDA Pro V3 target-size data: {sized}",
                "",
                "Recommended Cadence flow:",
                "",
                "1. Build or review the package symbols in `../allegro/symbols`.",
                "2. Make this directory visible to Cadence path variables `STEPPATH`, `STEP_MAPPING_PATH`, and `STEP_FACET_PATH`.",
                "3. In Allegro PCB Editor or Package Symbol Editor, run `step pkg map` or choose `Setup - STEP Package Mapping`.",
                "4. Use the STEP Package Mapping dialog's Import action to load `cadence_step_package.map`.",
                "5. Review offsets, rotations, and any target-size data visually, then save the mapping from Cadence.",
                "",
                "Cadence's import flow may also require `stepFacetFiles4Map` facet data for 3D Canvas.",
                "The converter does not fabricate Cadence facet XML; if the import dialog reports missing",
                "facet data, generate or save the mapping through Cadence and place the resulting facet",
                "directory/archive where `step_facet_path` points.",
                "",
                "Cadence owns the package-symbol properties `PKGDEF_STEP_FILE` and",
                "`PKGDEF_STEP_TRANSFORMATION`; this converter intentionally does not write those",
                "properties directly.",
                "",
                "After mapping exists in symbols, `sym_3d_update.exe` can be used on Cadence 17.4",
                "ISR019+ installations to update symbols to the newer internal 3D representation.",
                "Use `../scripts/prepare_step_mapping.ps1` for path checks and the optional update command.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_step_mapping_script(entries: list[tuple[str, Model3D]], path: Path) -> Path:
    lines = [
        f"# Generated by jlc2cadence v{__version__}",
        "param(",
        "  [string]$Allegro = 'allegro.exe',",
        "  [ValidatePattern('^[A-Za-z0-9_][A-Za-z0-9_.-]*$')][string]$Product,",
        "  [string]$Sym3dUpdate,",
        "  [string]$SymbolDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'allegro\\symbols'),",
        "  [string]$UpdatedSymbolDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'allegro\\symbols_3d'),",
        "  [switch]$OpenMapper,",
        "  [switch]$RunSym3dUpdate",
        ")",
        "$ErrorActionPreference = 'Stop'",
        "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path",
        "$Root = Split-Path -Parent $Here",
        "$ModelDir = Join-Path $Root '3d'",
        "$Map = Join-Path $ModelDir 'cadence_step_package.map'",
        "$SymbolList = Join-Path $ModelDir 'sym_3d_update_symbols.txt'",
        "$Replay = Join-Path $Here 'run_step_package_mapping.scr'",
        "if (-not (Test-Path -LiteralPath $Map)) { throw \"Missing STEP mapping file: $Map\" }",
        "[xml]$MapXml = Get-Content -LiteralPath $Map -Raw",
        "$MapItems = @($MapXml.'STEP-3D-MAPPING'.Body.MapItem)",
        "Write-Host \"STEP mapping file: $Map\"",
        "Write-Host \"STEP model directory: $ModelDir\"",
        "Write-Host \"STEP mapping entries: $($MapItems.Count)\"",
        "$MissingModels = @()",
        "foreach ($Item in $MapItems) {",
        "  $StepFile = $Item.StepModel.file",
        "  if ($StepFile -and -not (Test-Path -LiteralPath (Join-Path $ModelDir $StepFile))) {",
        "    $MissingModels += $StepFile",
        "  }",
        "}",
        "if ($MissingModels.Count -gt 0) {",
        "  throw \"Missing STEP model files in ${ModelDir}: $($MissingModels -join ', ')\"",
        "}",
        "function Add-CadencePath {",
        "  param([string]$Name, [string]$Value)",
        "  $Existing = [Environment]::GetEnvironmentVariable($Name, 'Process')",
        "  if ([string]::IsNullOrWhiteSpace($Existing)) {",
        "    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')",
        "  } elseif ($Existing -notmatch [regex]::Escape($Value)) {",
        "    [Environment]::SetEnvironmentVariable($Name, \"$Value $Existing\", 'Process')",
        "  }",
        "}",
        "Add-CadencePath 'STEPPATH' $ModelDir",
        "Add-CadencePath 'STEP_MAPPING_PATH' $ModelDir",
        "Add-CadencePath 'STEP_FACET_PATH' $ModelDir",
        "Write-Host \"Set process STEPPATH=$env:STEPPATH\"",
        "Write-Host \"Set process STEP_MAPPING_PATH=$env:STEP_MAPPING_PATH\"",
        "Write-Host \"Set process STEP_FACET_PATH=$env:STEP_FACET_PATH\"",
        "Write-Host \"Cadence command: step pkg map\"",
        "Write-Host \"Menu path: Setup - STEP Package Mapping\"",
        "Write-Host \"Import this file from the STEP Package Mapping dialog: $Map\"",
        "if ($OpenMapper) {",
        "  if (-not (Test-Path -LiteralPath $Replay)) { throw \"Missing Allegro replay script: $Replay\" }",
        "  # Use the official product name for your normal Cadence startup; no automatic fallback.",
        "  # Cadence documents `allegro -product help` for available product names.",
        "  $LaunchArgs = @()",
        "  if ($Product) { $LaunchArgs += @('-product', $Product) }",
        "  $LaunchArgs += @('-s', ('\"' + $Replay + '\"'))",
        "  Start-Process -FilePath $Allegro -ArgumentList $LaunchArgs -WorkingDirectory $Root",
        "}",
        "if ($RunSym3dUpdate) {",
        "  if (-not (Test-Path -LiteralPath $SymbolList)) { throw \"Missing sym_3d_update list: $SymbolList\" }",
        "  if (-not (Test-Path -LiteralPath $SymbolDir)) { throw \"Missing symbol directory: $SymbolDir\" }",
        "  New-Item -ItemType Directory -Force -Path $UpdatedSymbolDir | Out-Null",
        "  $CadenceEnvDir = Join-Path $Root '_cadence_pcbenv'",
        "  New-Item -ItemType Directory -Force -Path $CadenceEnvDir | Out-Null",
        "  $CadenceEnv = Join-Path $CadenceEnvDir 'env'",
        "  @(",
        "    '# Generated by jlc2cadence for sym_3d_update.',",
        "    'source $TELENV',",
        "    \"set steppath = `\"$ModelDir`\"\",",
        "    \"set step_mapping_path = `\"$ModelDir`\"\",",
        "    \"set step_facet_path = `\"$ModelDir`\"\"",
        "  ) | Set-Content -LiteralPath $CadenceEnv -Encoding utf8",
        "  $env:ALLEGRO_PCBENV = $CadenceEnvDir",
        "  if (-not $Sym3dUpdate) {",
        "    $Candidates = @()",
        "    if ($env:CDSROOT) { $Candidates += (Join-Path $env:CDSROOT 'tools\\bin\\sym_3d_update.exe') }",
        "    $Candidates += @('D:\\Cadence_17_4\\Cadence\\SPB_17.4\\tools\\bin\\sym_3d_update.exe', 'sym_3d_update.exe')",
        "    foreach ($Candidate in $Candidates) {",
        "      if ($Candidate -and (Test-Path -LiteralPath $Candidate)) { $Sym3dUpdate = $Candidate; break }",
        "      if ($Candidate -and (Get-Command $Candidate -ErrorAction SilentlyContinue)) { $Sym3dUpdate = $Candidate; break }",
        "    }",
        "  }",
        "  if (-not $Sym3dUpdate) { throw 'Unable to locate sym_3d_update.exe. Pass -Sym3dUpdate explicitly.' }",
        "  Write-Host \"Running sym_3d_update after STEP mapping import/review...\"",
        "  Write-Host \"Updated symbol destination: $UpdatedSymbolDir\"",
        "  & $Sym3dUpdate -c $SymbolList -s $SymbolDir -d $UpdatedSymbolDir",
        "  if ($LASTEXITCODE -ne 0) { throw \"sym_3d_update failed with exit code $LASTEXITCODE\" }",
        "}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_step_mapping_replay(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Allegro replay script generated by jlc2cadence.",
                "# Opens the Cadence STEP Package Mapping UI for manual import/review.",
                "setwindow pcb",
                "step pkg map",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_step_mapping_skill(
    entries: list[tuple[str, Model3D]],
    symbols_dir: Path,
    path: Path,
) -> Path:
    """Generate a Cadence SKILL batch pass that applies STEP mappings to .dra files."""
    symbol_dir_text = _skill_path(symbols_dir)
    lines = [
        f"; Generated by jlc2cadence v{__version__}",
        "; Applies primary STEP mappings to existing package .dra symbols.",
        "; Cadence 17.4 provides axlStepSet for this operation.",
        "",
        "procedure(j2cApply3dMappings()",
        "  let((oldDir symbolDir mappings mapping package stepFile rotationX rotationY rotationZ offsetX offsetY offsetZ draName openResult mapResult saveResult)",
        f'    symbolDir = "{symbol_dir_text}"',
        "    oldDir = getWorkingDir()",
        "    changeWorkingDir(symbolDir)",
        "    mappings = list(",
    ]
    if entries:
        for package, model in entries:
            lines.append(
                '      list("{package}" "{step_file}" {offset_x} {offset_y} {offset_z} {rotation_x} {rotation_y} {rotation_z})'.format(
                    package=_skill_string(package),
                    step_file=_skill_string(_model_file_name(model)),
                    offset_x=_skill_num(model.offset[0]),
                    offset_y=_skill_num(model.offset[1]),
                    offset_z=_skill_num(model.offset[2]),
                    rotation_x=_skill_num(model.rotation[0]),
                    rotation_y=_skill_num(model.rotation[1]),
                    rotation_z=_skill_num(model.rotation[2]),
                )
            )
    lines.extend(
        [
            "    )",
            "    foreach(mapping mappings",
            "      package = car(mapping)",
            "      stepFile = cadr(mapping)",
            "      offsetX = caddr(mapping)",
            "      offsetY = cadddr(mapping)",
            "      offsetZ = car(cddddr(mapping))",
            "      rotationX = cadr(cddddr(mapping))",
            "      rotationY = caddr(cddddr(mapping))",
            "      rotationZ = cadddr(cddddr(mapping))",
            "      draName = strcat(package \".dra\")",
            "      openResult = axlOpenDesignForBatch(draName \"wf\")",
            "      if(openResult == nil then",
            '        printf("J2C_3D_MAPPING_RESULT package=%s status=open_failed\\n" package)',
            "      else",
            "        mapResult = axlStepSet('primary nil stepFile list(",
            "          'offset_x offsetX",
            "          'offset_y offsetY",
            "          'offset_z offsetZ",
            "          'rotation_x rotationX",
            "          'rotation_y rotationY",
            "          'rotation_z rotationZ",
            "        ))",
            "        if(mapResult then",
            "          saveResult = axlSaveDesign(?mode \"nocheck\" ?noConfirm t ?writeModel t)",
            "          if(saveResult then",
            '            printf("J2C_3D_MAPPING_RESULT package=%s status=mapped file=%s\\n" package stepFile)',
            "          else",
            '            printf("J2C_3D_MAPPING_RESULT package=%s status=save_failed\\n" package)',
            "          )",
            "        else",
            '          printf("J2C_3D_MAPPING_RESULT package=%s status=map_failed file=%s\\n" package stepFile)',
            "        )",
            "      )",
            "    )",
            "    changeWorkingDir(oldDir)",
            "  )",
            ")",
            "",
            "j2cApply3dMappings()",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_step_mapping_skill_replay(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Allegro replay script generated by jlc2cadence.",
                "setwindow pcb",
                "scriptmode +noerrors",
                "scriptmode +invisible",
                "skill load(\"scripts/apply_3d_mapping.il\")",
                "exit",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _step_map_entries(library: LibraryIR) -> list[tuple[str, Model3D]]:
    entries: list[tuple[str, Model3D]] = []
    for footprint in library.footprints:
        model = footprint.model3d
        if model and _is_step_model(model) and model.transform_source != "missing_original":
            entries.append((footprint.name, model))
    return entries


def write_easyeda_transform_sidecar(rows: list[dict[str, object]], path: Path) -> Path:
    payload = {
        "version": f"v{__version__}",
        "source": "EasyEDA Pro V3 3D Model Transform",
        "units": "mm",
        "rotation_units": "degrees",
        "matrix_note": "Target size is preserved for review; Cadence .map output carries offset and rotation only.",
        "footprints": rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _easyeda_transform_rows(library: LibraryIR) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for footprint in library.footprints:
        model = footprint.model3d
        if model is None or model.transform_source != "easyeda_pro_v3":
            continue
        rows.append(
            {
                "footprint": footprint.name,
                "model": Path(model.file).name,
                "model_id": footprint.properties.get("EasyEDA_3D_Model_Id", ""),
                "model_title": footprint.properties.get("EasyEDA_3D_Model_Title", ""),
                "target_size": list(model.target_size) if model.target_size is not None else None,
                "rotation": list(model.rotation),
                "offset": list(model.offset),
                "source_transform": footprint.properties.get("EasyEDA_3D_Model_Transform", ""),
            }
        )
    return rows


def _footprint_model_row(footprint_name: str, model: Model3D | None) -> dict[str, object]:
    if model is None:
        return {"footprint": footprint_name, "model3d": None, "status": "missing"}
    is_step = _is_step_model(model)
    if model.transform_source == "missing_original":
        status = "missing_source_transform"
    elif is_step:
        status = "placeholder_step_map_ready" if is_placeholder_model(model) else "step_map_ready"
    else:
        status = "manual_review_non_step"
    return {
        "footprint": footprint_name,
        "model3d": model.to_dict(),
        "cadence_step_file": _model_file_name(model) if is_step else None,
        "status": status,
    }


def _is_step_model(model: Model3D) -> bool:
    fmt = model.format.lower().lstrip(".")
    suffix = Path(model.file).suffix.lower().lstrip(".")
    return fmt in STEP_FORMATS or suffix in STEP_FORMATS


def _model_file_name(model: Model3D) -> str:
    return Path(model.file).name


def _fmt_offset(value: float) -> str:
    return f"{_clean_float(value):.6f}"


def _fmt_rotation(value: float) -> str:
    return f"{_clean_float(value):.3f}"


def _clean_float(value: float) -> float:
    if abs(value) < 0.0000005:
        return 0.0
    return float(value)


def _skill_path(path: Path) -> str:
    return str(path.expanduser().resolve()).replace("\\", "/").replace('"', '\\"')


def _skill_num(value: float) -> str:
    value = _clean_float(value)
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0.0"


def _skill_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
