from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import __version__
from ..validators.output_package import validate_output_package


_INCLUDED_DIRECTORIES = {"reports", "capture", "allegro", "3d", "scripts"}
_TOP_LEVEL_SUFFIXES = {
    ".csv",
    ".il",
    ".jrl",
    ".json",
    ".log",
    ".md",
    ".ps1",
    ".scr",
    ".tcl",
    ".txt",
    ".xml",
}
_EXCLUDED_DIRECTORY_NAMES = {"__pycache__"}


def build_handoff_bundle(
    out_dir: Path,
    *,
    package_out: Path | None = None,
    manifest_out: Path | None = None,
    include_extract: bool = False,
    include_cadence_pcbenv: bool = False,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
) -> dict[str, Any]:
    """Validate and package a conversion output for Cadence handoff."""
    output_dir = out_dir.expanduser().resolve()
    if not output_dir.exists() or not output_dir.is_dir():
        raise FileNotFoundError(f"Conversion output directory does not exist: {output_dir}")

    validation = validate_output_package(
        output_dir,
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )

    package_path = (
        package_out.expanduser().resolve()
        if package_out
        else output_dir.parent / f"{output_dir.name}.cadence-package.zip"
    )
    manifest_path = manifest_out.expanduser().resolve() if manifest_out else None
    excluded_paths = {
        path
        for path in (package_path, manifest_path)
        if path is not None and _is_relative_to(path, output_dir)
    }
    files = list(
        _iter_bundle_files(
            output_dir,
            include_extract=include_extract,
            include_cadence_pcbenv=include_cadence_pcbenv,
            excluded_paths=excluded_paths,
        )
    )
    generated_files = _generated_handoff_files(validation.to_dict())
    file_entries = [
        {
            "path": archive_path,
            "category": archive_path.split("/", 1)[0] if "/" in archive_path else "root",
            "bytes": file_path.stat().st_size,
            "sha256": _sha256_file(file_path),
            "generated": False,
        }
        for file_path, archive_path in files
    ]
    file_entries.extend(
        {
            "path": archive_path,
            "category": archive_path.split("/", 1)[0] if "/" in archive_path else "root",
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
            "generated": True,
        }
        for archive_path, content in generated_files
    )

    excluded_transient_directories: list[str] = []
    if not include_extract:
        excluded_transient_directories.append("_extract")
    if not include_cadence_pcbenv:
        excluded_transient_directories.append("_cadence_pcbenv")

    manifest: dict[str, Any] = {
        "version": f"v{__version__}",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_output": str(output_dir),
        "package_path": str(package_path),
        "status": _package_status(validation.errors, validation.warnings),
        "validation": validation.to_dict(),
        "selection": {
            "included_directories": sorted(_INCLUDED_DIRECTORIES),
            "generated_handoff_files": [archive_path for archive_path, _content in generated_files],
            "include_extract": include_extract,
            "include_cadence_pcbenv": include_cadence_pcbenv,
            "excluded_transient_directories": excluded_transient_directories,
        },
        "counts": {
            "files": len(file_entries),
            "bytes": sum(entry["bytes"] for entry in file_entries),
            "by_category": _count_by_category(file_entries),
        },
        "files": file_entries,
    }
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    markdown_text = render_handoff_bundle_markdown(manifest)

    package_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path, archive_path in files:
            archive.write(file_path, arcname=archive_path)
        for archive_path, content in generated_files:
            archive.writestr(archive_path, content)
        archive.writestr("package_manifest.json", manifest_text)
        archive.writestr("package_manifest.md", markdown_text)

    package_sha256 = _sha256_file(package_path)
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        external_manifest = dict(manifest)
        external_manifest["package_sha256"] = package_sha256
        manifest_path.write_text(
            json.dumps(external_manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    result = {
        "version": f"v{__version__}",
        "status": manifest["status"],
        "package": str(package_path),
        "manifest": str(manifest_path) if manifest_path else None,
        "package_sha256": package_sha256,
        "counts": dict(manifest["counts"]),
        "validation": validation.to_dict(),
        "selection": dict(manifest["selection"]),
    }
    return result


def render_handoff_bundle_markdown(manifest: dict[str, Any]) -> str:
    validation = manifest.get("validation", {}) or {}
    counts = manifest.get("counts", {}) or {}
    lines = [
        "# JLC2Cadence Cadence Handoff Package",
        "",
        f"- Tool version: `{manifest.get('version', '')}`",
        f"- Validation status: `{validation.get('status', '')}`",
        f"- Package status: `{manifest.get('status', '')}`",
        f"- Source output: `{manifest.get('source_output', '')}`",
        f"- Included files: `{counts.get('files', 0)}`",
        f"- Included bytes: `{counts.get('bytes', 0)}`",
        "",
        "## Included Areas",
        "",
    ]
    for category, count in sorted((counts.get("by_category", {}) or {}).items()):
        lines.append(f"- `{category}`: {count} files")

    errors = list(validation.get("errors", []) or [])
    warnings = list(validation.get("warnings", []) or [])
    if errors:
        lines.extend(["", "## Validation Errors", ""])
        lines.extend(f"- {error}" for error in errors)
    if warnings:
        lines.extend(["", "## Validation Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(
        [
            "",
            "## Import Notes",
            "",
            "- Capture symbol library is under `capture/`.",
            "- Allegro footprint assets are under `allegro/`.",
            "- STEP/STP models and Cadence mapping data are under `3d/`.",
            "- Validation and audit evidence are under `reports/`.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _generated_handoff_files(validation: dict[str, Any]) -> list[tuple[str, bytes]]:
    files = {
        "handoff/cadence_handoff_readme.md": _render_cadence_handoff_readme(validation),
        "handoff/setup_cadence_paths.ps1": _render_setup_cadence_paths_ps1(),
        "handoff/setup_cadence_paths.cmd": _render_setup_cadence_paths_cmd(),
    }
    return [(path, text.encode("utf-8")) for path, text in files.items()]


def _render_cadence_handoff_readme(validation: dict[str, Any]) -> str:
    counts = validation.get("counts", {}) or {}
    status = str(validation.get("status", ""))
    require_cadence = bool(validation.get("require_cadence"))
    require_3d = bool(validation.get("require_3d"))
    lines = [
        "# Cadence 17.4 Handoff Quickstart",
        "",
        "This directory is generated by `jlc2cadence package-output` for the engineer who receives the ZIP.",
        "",
        "## Package Status",
        "",
        f"- Validation status: `{status}`",
        f"- Components: `{counts.get('components', 0)}`",
        f"- Footprints: `{counts.get('footprints', 0)}`",
        f"- Linked STEP/STP models: `{counts.get('linked_models', 0)}`",
        f"- Placeholder STEP models: `{counts.get('placeholder_models', 0)}`",
        f"- Required component 3D models linked: `{counts.get('linked_required_3d_models', 0)}` / `{counts.get('required_3d_footprints', 0)}`",
        f"- Non-component 3D exemptions: `{counts.get('noncomponent_3d_exemptions', 0)}`",
        f"- Packaged with Cadence assets required: `{str(require_cadence).lower()}`",
        f"- Packaged with complete 3D required: `{str(require_3d).lower()}`",
        "",
        "## Files To Add In Cadence",
        "",
        "- Capture symbol library: `capture/library.olb`",
        "- Capture XML source and build evidence: `capture/library.olb.xml`, `capture/capture_build.results.json`",
        "- Allegro package symbols: `allegro/symbols/*.psm`",
        "- Allegro editable symbols: `allegro/symbols/*.dra`",
        "- Allegro padstacks: `allegro/padstacks/*.pad`",
        "- STEP/STP models and mapping: `3d/`",
        "- Validation evidence: `reports/output_validation.json`, `reports/audit.md`, `reports/3d_review.md`",
        "",
        "## Path Setup",
        "",
        "Extract the ZIP, then run one of these from the extracted package root:",
        "",
        "```powershell",
        ". .\\handoff\\setup_cadence_paths.ps1",
        "```",
        "",
        "```cmd",
        "call handoff\\setup_cadence_paths.cmd",
        "```",
        "",
        "Run Cadence from the same shell after setting the paths. Use `-PreferUpdatedSymbols` with the PowerShell script when `allegro/symbols_3d` was created by `sym_3d_update` and should be preferred over the original `allegro/symbols` directory.",
        "",
        "## Review Gates",
        "",
        "1. Add `capture/library.olb` to the Capture project library list.",
        "2. Make Allegro library search paths include the symbol and padstack directories printed by the setup script.",
        "3. For 3D, open `3d/cadence_step_package.map` with Cadence STEP Package Mapping if mapping needs to be reviewed or regenerated.",
        "4. Review `reports/audit.md` and `reports/3d_review.md` before production handoff.",
        "5. Treat any `missing_model`, `placeholder_not_for_signoff`, `not_mapped`, or failed Canvas checklist row as a blocker for complete mechanical 3D signoff.",
        "",
    ]
    warnings = list(validation.get("warnings", []) or [])
    errors = list(validation.get("errors", []) or [])
    if errors:
        lines.extend(["## Validation Errors", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    if warnings:
        lines.extend(["## Validation Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_setup_cadence_paths_ps1() -> str:
    lines = [
        f"# Generated by jlc2cadence v{__version__}",
        "param(",
        "  [switch]$PreferUpdatedSymbols,",
        "  [switch]$PrintOnly",
        ")",
        "$ErrorActionPreference = 'Stop'",
        "$HandoffDir = Split-Path -Parent $MyInvocation.MyCommand.Path",
        "$PackageRoot = Split-Path -Parent $HandoffDir",
        "$PrimarySymbolDir = Join-Path $PackageRoot 'allegro\\symbols'",
        "$UpdatedSymbolDir = Join-Path $PackageRoot 'allegro\\symbols_3d'",
        "$SymbolDir = $PrimarySymbolDir",
        "if ($PreferUpdatedSymbols -and (Test-Path -LiteralPath $UpdatedSymbolDir)) {",
        "  $SymbolDir = $UpdatedSymbolDir",
        "}",
        "$PadstackDir = Join-Path $PackageRoot 'allegro\\padstacks'",
        "$ModelDir = Join-Path $PackageRoot '3d'",
        "$FacetCandidates = @(",
        "  (Join-Path $SymbolDir 'stepFacetFiles4Map'),",
        "  (Join-Path $PrimarySymbolDir 'stepFacetFiles4Map'),",
        "  (Join-Path (Join-Path $PackageRoot 'allegro') 'stepFacetFiles4Map'),",
        "  (Join-Path $ModelDir 'stepFacetFiles4Map'),",
        "  $ModelDir",
        ")",
        "$FacetDir = $ModelDir",
        "foreach ($Candidate in $FacetCandidates) {",
        "  if ($Candidate -and (Test-Path -LiteralPath $Candidate)) { $FacetDir = $Candidate; break }",
        "}",
        "$CadencePaths = [ordered]@{",
        "  psmpath = $SymbolDir",
        "  padpath = $PadstackDir",
        "  steppath = $ModelDir",
        "  step_mapping_path = $ModelDir",
        "  step_facet_path = $FacetDir",
        "}",
        "function Add-CadencePath {",
        "  param([string]$Name, [string]$Value)",
        "  if ([string]::IsNullOrWhiteSpace($Value)) { return }",
        "  $Existing = [Environment]::GetEnvironmentVariable($Name, 'Process')",
        "  if ([string]::IsNullOrWhiteSpace($Existing)) {",
        "    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')",
        "  } elseif ($Existing -notmatch [regex]::Escape($Value)) {",
        "    [Environment]::SetEnvironmentVariable($Name, \"$Value $Existing\", 'Process')",
        "  }",
        "}",
        "foreach ($Item in $CadencePaths.GetEnumerator()) {",
        "  if (-not (Test-Path -LiteralPath $Item.Value)) { Write-Warning \"Path for $($Item.Key) does not exist yet: $($Item.Value)\" }",
        "  if ($PrintOnly) {",
        "    Write-Host \"set $($Item.Key) = `\"$($Item.Value)`\"\"",
        "  } else {",
        "    Add-CadencePath $Item.Key $Item.Value",
        "    $Current = [Environment]::GetEnvironmentVariable($Item.Key, 'Process')",
        "    Write-Host \"$($Item.Key)=$Current\"",
        "  }",
        "}",
        "Write-Host \"Capture library: $(Join-Path $PackageRoot 'capture\\library.olb')\"",
        "Write-Host \"STEP package map: $(Join-Path $ModelDir 'cadence_step_package.map')\"",
        "if (-not $PrintOnly) {",
        "  Write-Host 'Launch Capture or Allegro from this same PowerShell session so the process inherits these paths.'",
        "}",
        "",
    ]
    return "\n".join(lines)


def _render_setup_cadence_paths_cmd() -> str:
    lines = [
        f"rem Generated by jlc2cadence v{__version__}",
        "@echo off",
        "set \"JLC2CADENCE_HANDOFF=%~dp0\"",
        "for %%I in (\"%JLC2CADENCE_HANDOFF%..\") do set \"JLC2CADENCE_ROOT=%%~fI\"",
        "set \"JLC2CADENCE_SYMBOLS=%JLC2CADENCE_ROOT%\\allegro\\symbols\"",
        "if /I \"%~1\"==\"prefer-updated-symbols\" if exist \"%JLC2CADENCE_ROOT%\\allegro\\symbols_3d\" set \"JLC2CADENCE_SYMBOLS=%JLC2CADENCE_ROOT%\\allegro\\symbols_3d\"",
        "set \"JLC2CADENCE_FACETS=%JLC2CADENCE_ROOT%\\3d\"",
        "if exist \"%JLC2CADENCE_SYMBOLS%\\stepFacetFiles4Map\" set \"JLC2CADENCE_FACETS=%JLC2CADENCE_SYMBOLS%\\stepFacetFiles4Map\"",
        "if exist \"%JLC2CADENCE_ROOT%\\allegro\\stepFacetFiles4Map\" set \"JLC2CADENCE_FACETS=%JLC2CADENCE_ROOT%\\allegro\\stepFacetFiles4Map\"",
        "if exist \"%JLC2CADENCE_ROOT%\\3d\\stepFacetFiles4Map\" set \"JLC2CADENCE_FACETS=%JLC2CADENCE_ROOT%\\3d\\stepFacetFiles4Map\"",
        "call :prepend psmpath \"%JLC2CADENCE_SYMBOLS%\"",
        "call :prepend padpath \"%JLC2CADENCE_ROOT%\\allegro\\padstacks\"",
        "call :prepend steppath \"%JLC2CADENCE_ROOT%\\3d\"",
        "call :prepend step_mapping_path \"%JLC2CADENCE_ROOT%\\3d\"",
        "call :prepend step_facet_path \"%JLC2CADENCE_FACETS%\"",
        "echo psmpath=%psmpath%",
        "echo padpath=%padpath%",
        "echo steppath=%steppath%",
        "echo step_mapping_path=%step_mapping_path%",
        "echo step_facet_path=%step_facet_path%",
        "echo Capture library: %JLC2CADENCE_ROOT%\\capture\\library.olb",
        "echo STEP package map: %JLC2CADENCE_ROOT%\\3d\\cadence_step_package.map",
        "echo Launch Capture or Allegro from this same Command Prompt so the process inherits these paths.",
        "goto :eof",
        "",
        ":prepend",
        "set \"J2C_NAME=%~1\"",
        "set \"J2C_VALUE=%~2\"",
        "call set \"J2C_CURRENT=%%%J2C_NAME%%%\"",
        "if \"%J2C_CURRENT%\"==\"\" (",
        "  call set \"%J2C_NAME%=%J2C_VALUE%\"",
        ") else (",
        "  echo %J2C_CURRENT% | find /I \"%J2C_VALUE%\" >nul",
        "  if errorlevel 1 call set \"%J2C_NAME%=%J2C_VALUE% %J2C_CURRENT%\"",
        ")",
        "goto :eof",
        "",
    ]
    return "\r\n".join(lines)


def _iter_bundle_files(
    output_dir: Path,
    *,
    include_extract: bool,
    include_cadence_pcbenv: bool,
    excluded_paths: set[Path],
) -> Iterable[tuple[Path, str]]:
    for file_path in sorted(output_dir.rglob("*")):
        if not file_path.is_file():
            continue
        resolved = file_path.resolve()
        if resolved in excluded_paths:
            continue
        relative = file_path.relative_to(output_dir)
        parts = relative.parts
        lowered_parts = {part.casefold() for part in parts}
        if "__pycache__" in lowered_parts:
            continue
        if "_extract" in lowered_parts and not include_extract:
            continue
        if "_cadence_pcbenv" in lowered_parts and not include_cadence_pcbenv:
            continue
        first = parts[0].casefold() if parts else ""
        if first not in _INCLUDED_DIRECTORIES and (
            len(parts) != 1 or file_path.suffix.casefold() not in _TOP_LEVEL_SUFFIXES
        ):
            continue
        yield file_path, relative.as_posix()


def _count_by_category(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        category = str(entry.get("category", "root"))
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def _package_status(errors: list[str], warnings: list[str]) -> str:
    if errors:
        return "validation_failed"
    if warnings:
        return "ready_with_warnings"
    return "ready"


def _is_relative_to(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
