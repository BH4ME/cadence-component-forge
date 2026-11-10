from __future__ import annotations

import importlib.util
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import __version__
from ..extractors.altium_ascii import parse_altium_design_files
from ..extractors.archive import find_design_files, prepare_input_files
from ..extractors.easyeda_pro import EASYEDA_PRO_ARCHIVE_SUFFIXES, EASYEDA_PRO_JSONL_SUFFIXES, looks_like_easyeda_pro_jsonl, parse_easyeda_pro_design_files
from ..extractors.easyeda_legacy import parse_easyeda_legacy_design_files, looks_like_easyeda_legacy
from ..extractors.model_aliases import ModelAliasSet, collect_model_alias_assets, load_model_aliases
from ..extractors.models import MODEL_SUFFIXES, collect_model_assets
from ..extractors.pads_ascii import parse_pads_design_files
from ..ir.normalize import attach_models, ensure_components_for_footprints, merge_libraries, model_match_candidates
from ..ir.schema import LibraryIR, Model3D
from .cadence_runner import CadenceTools
from .library_checks import validate_library


_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ALTIUM_LIBRARY_SUFFIXES = {".schdoc", ".schlib", ".pcbdoc", ".pcblib", ".libpkg"}
PROPRIETARY_SUFFIXES = EASYEDA_PRO_ARCHIVE_SUFFIXES


def build_doctor_report(
    input_path: Path | None = None,
    models_path: Path | None = None,
    model_aliases_path: Path | None = None,
    default_unit: str = "mm",
    cadence_root: Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "version": f"v{__version__}",
        "status": "ok",
        "cadence": {},
        "input": None,
        "models": None,
        "checks": checks,
    }

    _check_cadence_tools(checks, report, cadence_root)
    _check_capture_process(checks)
    _check_optional_dependencies(checks)

    if input_path is not None:
        _check_input_and_models(checks, report, input_path, models_path, model_aliases_path, default_unit)
    elif models_path is not None:
        _check_standalone_models(checks, report, models_path)

    report["status"] = _overall_status(checks)
    report["summary"] = {
        "errors": sum(1 for check in checks if check["status"] == "error"),
        "warnings": sum(1 for check in checks if check["status"] == "warning"),
        "ok": sum(1 for check in checks if check["status"] == "ok"),
    }
    return report


def _check_cadence_tools(checks: list[dict[str, Any]], report: dict[str, Any], cadence_root: Path | None) -> None:
    tools = CadenceTools.discover(cadence_root)
    tool_data = tools.to_dict()
    report["cadence"] = tool_data
    missing = [name for name in ["allegro", "allegro_batch", "capture", "padstack_editor"] if not tool_data.get(name)]
    if missing:
        _add_check(
            checks,
            "cadence_tools",
            "warning",
            "Some Cadence 17.4 tools were not found; offline generation still works, but Cadence execution may be incomplete.",
            {"missing": missing, "tools": tool_data},
        )
    else:
        _add_check(checks, "cadence_tools", "ok", "Cadence 17.4 tool paths were found.", tool_data)


def _check_capture_process(checks: list[dict[str, Any]]) -> None:
    if platform.system().lower() != "windows":
        _add_check(checks, "capture_process", "ok", "Capture process check skipped on non-Windows platform.")
        return
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Capture.exe", "/NH"],
            text=True,
            capture_output=True,
            check=False,
            creationflags=_WINDOWS_NO_WINDOW,
        )
    except OSError as exc:
        _add_check(checks, "capture_process", "warning", f"Could not query Capture.exe process state: {exc}")
        return
    output = (proc.stdout or "") + (proc.stderr or "")
    if "Capture.exe" in output:
        _add_check(
            checks,
            "capture_process",
            "warning",
            "Capture.exe is already running; Capture 17.4 may ignore replay arguments until existing instances are closed.",
        )
    else:
        _add_check(checks, "capture_process", "ok", "No running Capture.exe process was detected.")


def _check_optional_dependencies(checks: list[dict[str, Any]]) -> None:
    if importlib.util.find_spec("olefile") is None:
        _add_check(
            checks,
            "optional_olefile",
            "warning",
            "Python package 'olefile' is not installed; binary/OLE Altium containers will be reported as unsupported instead of parsed.",
        )
    else:
        _add_check(checks, "optional_olefile", "ok", "Python package 'olefile' is installed.")


def _check_input_and_models(
    checks: list[dict[str, Any]],
    report: dict[str, Any],
    input_path: Path,
    models_path: Path | None,
    model_aliases_path: Path | None,
    default_unit: str,
) -> None:
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        _add_check(checks, "input_exists", "error", f"Input path does not exist: {input_path}")
        return
    _add_check(checks, "input_exists", "ok", "Input path exists.", {"input": str(input_path)})

    with tempfile.TemporaryDirectory(prefix="jlc2cadence_doctor_") as tmp:
        tmp_dir = Path(tmp)
        try:
            input_paths = prepare_input_files(input_path, work_dir=tmp_dir / "input")
        except Exception as exc:
            _add_check(checks, "input_extract", "error", f"Could not read or extract input: {exc}")
            return

        design_files = find_design_files(input_paths)
        scan = _scan_files(input_paths)
        try:
            model_aliases = load_model_aliases(model_aliases_path)
        except Exception as exc:
            _add_check(checks, "model_aliases", "error", f"Could not read model aliases: {exc}")
            return
        model_assets = _collect_models_for_doctor(models_path, input_paths, model_aliases, tmp_dir)
        model_summary = _model_summary(model_assets)
        if model_aliases.rows:
            model_summary["aliases"] = model_aliases.to_dict()
        report["models"] = model_summary
        report["input"] = {
            "path": str(input_path),
            "file_count": len(input_paths),
            "design_file_count": len(design_files),
            "design_files": [str(path) for path in design_files],
            "component_count": 0,
            "footprint_count": 0,
            "model_count": model_summary["attached_candidates"],
            "library_errors": [],
            "library_warnings": [],
            "scan": scan,
        }

        if design_files:
            _add_check(checks, "design_files", "ok", "Design/library files were found.", {"count": len(design_files)})
        else:
            _add_check(checks, "design_files", "error", "No supported Altium ASCII, PADS 9.5, or EasyEDA/JLC EDA Pro V3 JSONL design/library files were found.")

        for path in scan["binary_altium_files"]:
            _add_check(
                checks,
                "unsupported_binary_altium",
                "error",
                f"Unsupported binary Altium/OLE file detected: {path}. Export JLC EDA Pro as Altium ASCII 5.0 or PADS 9.5.",
            )
        for path in scan["proprietary_files"]:
            _add_check(
                checks,
                "unsupported_proprietary_input",
                "warning",
                f"Potential unsupported JLC/EasyEDA Pro archive detected: {path}. Use a ZIP-based .epro/.epro2/.elibz package or export to Altium ASCII/PADS 9.5.",
            )
        if scan["easyeda_pro_jsonl_files"]:
            _add_check(
                checks,
                "easyeda_pro_jsonl",
                "ok",
                "EasyEDA/JLC EDA Pro V3 JSONL source files were found.",
                {"files": scan["easyeda_pro_jsonl_files"]},
            )
        if scan["easyeda_legacy_files"]:
            _add_check(
                checks,
                "easyeda_legacy_json",
                "ok",
                "Legacy EasyEDA Pro JSON-array source files were found.",
                {"files": scan["easyeda_legacy_files"]},
            )

        try:
            library = merge_libraries(
                parse_altium_design_files(design_files, default_unit=default_unit),
                parse_pads_design_files(design_files, default_unit=default_unit),
                parse_easyeda_pro_design_files(design_files, default_unit=default_unit),
                parse_easyeda_legacy_design_files(input_paths, default_unit=default_unit),
            )
            attach_models(library, model_assets, model_aliases.mapping)
            ensure_components_for_footprints(library)
            validation = validate_library(library)
        except Exception as exc:
            _add_check(checks, "input_parse", "error", f"Input parser failed: {exc}")
            return

        assert report["input"] is not None
        report["input"]["component_count"] = len(library.components)
        report["input"]["footprint_count"] = len(library.footprints)
        report["input"]["model_count"] = sum(1 for footprint in library.footprints if footprint.model3d)
        report["input"]["library_errors"] = list(validation.errors)
        report["input"]["library_warnings"] = list(validation.warnings)

        if validation.errors:
            _add_check(checks, "library_validation", "error", "Library validation found errors.", validation.to_dict())
        elif validation.warnings:
            _add_check(checks, "library_validation", "warning", "Library validation found warnings.", validation.to_dict())
        else:
            _add_check(checks, "library_validation", "ok", "Library validation passed.", validation.to_dict())

        binding_summary = _model_binding_summary(library, model_assets, model_aliases.mapping)
        model_summary["binding"] = binding_summary
        _check_model_aliases(checks, model_aliases)
        _check_model_readiness(checks, model_summary, len(library.footprints))
        _check_model_binding(checks, binding_summary, len(library.footprints), model_summary["total"])


def _check_standalone_models(checks: list[dict[str, Any]], report: dict[str, Any], models_path: Path) -> None:
    models_path = models_path.expanduser().resolve()
    if not models_path.exists():
        _add_check(checks, "models_exists", "error", f"Models path does not exist: {models_path}")
        return
    with tempfile.TemporaryDirectory(prefix="jlc2cadence_doctor_models_") as tmp:
        models = collect_model_assets(models_path, Path(tmp) / "models")
        summary = _model_summary(models)
    report["models"] = summary
    _check_model_readiness(checks, summary, 0)


def _collect_models_for_doctor(
    models_path: Path | None,
    input_paths: list[Path],
    model_aliases: ModelAliasSet,
    tmp_dir: Path,
) -> list[Model3D]:
    model_assets = []
    if models_path:
        model_assets.extend(collect_model_assets(models_path, tmp_dir / "models"))
    for path in input_paths:
        if path.suffix.lower() in MODEL_SUFFIXES:
            model_assets.extend(collect_model_assets(path, tmp_dir / "models"))
    model_assets.extend(collect_model_alias_assets(model_aliases, tmp_dir / "models"))
    return _dedupe_models(model_assets)


def _model_summary(model_assets: list[Model3D]) -> dict[str, Any]:
    by_format: dict[str, int] = {}
    for model in model_assets:
        by_format[model.format] = by_format.get(model.format, 0) + 1
    return {
        "total": len(model_assets),
        "attached_candidates": len(model_assets),
        "by_format": by_format,
        "files": [model.source_file or model.file for model in model_assets],
    }


def _model_binding_summary(
    library: LibraryIR,
    model_assets: list[Model3D],
    model_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    matched_model_files = {
        footprint.model3d.file
        for footprint in library.footprints
        if footprint.model3d is not None
    }
    footprints = []
    for footprint in library.footprints:
        model = footprint.model3d
        footprints.append(
            {
                "footprint": footprint.name,
                "status": "matched" if model else "missing",
                "candidates": model_match_candidates(footprint, model_aliases=model_aliases),
                "model_file": _display_model_file(model) if model else None,
                "model_work_file": _model_work_file(model) if model else None,
                "model_source_file": model.source_file if model else None,
                "model_format": model.format if model else None,
            }
        )
    unmatched = [
        {
            "file": _display_model_file(model),
            "work_file": _model_work_file(model),
            "source_file": model.source_file,
            "format": model.format,
            "key": model.key,
        }
        for model in model_assets
        if model.file not in matched_model_files
    ]
    missing = [entry for entry in footprints if entry["status"] == "missing"]
    return {
        "footprint_count": len(footprints),
        "matched_count": len(footprints) - len(missing),
        "missing_count": len(missing),
        "unmatched_model_count": len(unmatched),
        "footprints": footprints,
        "missing": missing,
        "unmatched_models": unmatched,
    }


def _check_model_aliases(checks: list[dict[str, Any]], aliases: ModelAliasSet) -> None:
    if not aliases.rows:
        return
    missing_files = [row for row in aliases.rows if row.get("target_is_file") and not row.get("target_exists")]
    if missing_files:
        _add_check(checks, "model_aliases", "warning", "Some model alias target files are missing.", aliases.to_dict())
    else:
        _add_check(checks, "model_aliases", "ok", "Model alias file was loaded.", aliases.to_dict())


def _display_model_file(model: Model3D) -> str:
    return model.source_file or model.file


def _model_work_file(model: Model3D) -> str | None:
    if model.source_file and model.file != model.source_file:
        return model.file
    return None


def _check_model_readiness(checks: list[dict[str, Any]], summary: dict[str, Any], footprint_count: int) -> None:
    step_count = int(summary["by_format"].get("step", 0)) + int(summary["by_format"].get("stp", 0))
    obj_count = int(summary["by_format"].get("obj", 0))
    if summary["total"] == 0 and footprint_count > 0:
        _add_check(checks, "models", "warning", "No STEP/STP/OBJ models were found for parsed footprints.", summary)
    elif step_count == 0 and obj_count > 0:
        _add_check(checks, "models", "warning", "Only OBJ models were found; STEP/STP is preferred for Cadence 17.4 mapping.", summary)
    else:
        _add_check(checks, "models", "ok", "3D model inputs look usable for the current converter path.", summary)


def _check_model_binding(
    checks: list[dict[str, Any]],
    binding_summary: dict[str, Any],
    footprint_count: int,
    model_count: int,
) -> None:
    if footprint_count == 0:
        return
    if binding_summary["missing_count"] == 0 and binding_summary["unmatched_model_count"] == 0:
        _add_check(checks, "model_binding", "ok", "All parsed footprints have matched 3D models.", binding_summary)
        return
    if model_count == 0:
        _add_check(
            checks,
            "model_binding",
            "warning",
            "No 3D model files are available; see expected model candidate keys for each footprint.",
            binding_summary,
        )
        return
    _add_check(
        checks,
        "model_binding",
        "warning",
        "Some 3D model files did not bind automatically; compare expected candidate keys with unmatched model keys.",
        binding_summary,
    )


def _scan_files(paths: list[Path]) -> dict[str, Any]:
    binary_altium_files = []
    proprietary_files = []
    easyeda_pro_jsonl_files = []
    easyeda_legacy_files = []
    model_files = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in MODEL_SUFFIXES:
            model_files.append(str(path))
        if (suffix in EASYEDA_PRO_JSONL_SUFFIXES and looks_like_easyeda_pro_jsonl(path)) or looks_like_easyeda_pro_jsonl(path):
            easyeda_pro_jsonl_files.append(str(path))
        if suffix in {".esym", ".efoo", ".esch", ".epcb"} or looks_like_easyeda_legacy(path):
            easyeda_legacy_files.append(str(path))
        if suffix in PROPRIETARY_SUFFIXES:
            proprietary_files.append(str(path))
        if suffix in ALTIUM_LIBRARY_SUFFIXES and _has_ole_magic(path):
            binary_altium_files.append(str(path))
    return {
        "binary_altium_files": binary_altium_files,
        "proprietary_files": proprietary_files,
        "easyeda_pro_jsonl_files": easyeda_pro_jsonl_files,
        "easyeda_legacy_files": easyeda_legacy_files,
        "model_files": model_files,
    }


def _has_ole_magic(path: Path) -> bool:
    try:
        return path.read_bytes()[: len(OLE_MAGIC)] == OLE_MAGIC
    except OSError:
        return False


def _dedupe_models(models: list[Model3D]) -> list[Model3D]:
    deduped = []
    seen = set()
    for model in models:
        key = (model.file, model.source_file, model.key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(model)
    return deduped


def _overall_status(checks: list[dict[str, Any]]) -> str:
    if any(check["status"] == "error" for check in checks):
        return "failed"
    if any(check["status"] == "warning" for check in checks):
        return "warning"
    return "ok"


def _add_check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    message: str,
    details: Any | None = None,
) -> None:
    check = {"name": name, "status": status, "message": message}
    if details is not None:
        check["details"] = details
    checks.append(check)
