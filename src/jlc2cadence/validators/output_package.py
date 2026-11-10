from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..generators.placeholder3d import is_placeholder_model
from .noncomponent3d import NonComponent3DException, detect_noncomponent_3d_exceptions
from .step_mapping import validate_step_mapping_output


@dataclass
class ArtifactCheck:
    path: str
    exists: bool
    required: bool = True
    status: str = "unchecked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "required": self.required,
            "status": self.status,
        }


@dataclass
class OutputSection:
    name: str
    status: str = "unchecked"
    artifacts: list[ArtifactCheck] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "details": dict(self.details),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass
class OutputValidationReport:
    version: str
    out_dir: str
    require_cadence: bool = False
    require_3d: bool = False
    allow_noncomponent_3d_exceptions: bool = False
    status: str = "unchecked"
    counts: dict[str, int] = field(default_factory=dict)
    sections: list[OutputSection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "out_dir": self.out_dir,
            "require_cadence": self.require_cadence,
            "require_3d": self.require_3d,
            "allow_noncomponent_3d_exceptions": self.allow_noncomponent_3d_exceptions,
            "status": self.status,
            "counts": dict(self.counts),
            "sections": [section.to_dict() for section in self.sections],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_output_package(
    out_dir: Path,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
) -> OutputValidationReport:
    out_dir = out_dir.expanduser().resolve()
    report = OutputValidationReport(
        version=f"v{__version__}",
        out_dir=str(out_dir),
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )

    reports_section, ir, summary = _validate_reports_section(out_dir)
    noncomponent_exceptions = detect_noncomponent_3d_exceptions(ir)
    report.counts = _counts_from_ir_and_summary(
        ir,
        summary,
        noncomponent_exceptions=noncomponent_exceptions,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )
    sections = [
        reports_section,
        _validate_scripts_section(out_dir),
        _validate_capture_section(out_dir, ir, require_cadence),
        _validate_allegro_section(out_dir, ir, require_cadence),
        _validate_3d_section(
            out_dir,
            ir,
            require_cadence,
            require_3d,
            allow_noncomponent_3d_exceptions,
            noncomponent_exceptions,
        ),
    ]

    for section in sections:
        _finalize_section(section)
        report.sections.append(section)
        report.errors.extend(f"{section.name}: {error}" for error in section.errors)
        report.warnings.extend(f"{section.name}: {warning}" for warning in section.warnings)

    report.status = _overall_status(report)
    return report


def write_output_validation_report(
    out_dir: Path,
    path: Path,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
) -> OutputValidationReport:
    report = validate_output_package(
        out_dir,
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _validate_reports_section(out_dir: Path) -> tuple[OutputSection, dict[str, Any] | None, dict[str, Any] | None]:
    section = OutputSection("reports")
    ir_path = _add_artifact(section, out_dir, "reports/library.ir.json")
    summary_path = _add_artifact(section, out_dir, "reports/summary.json")
    _add_artifact(section, out_dir, "reports/summary.html")
    _add_artifact(section, out_dir, "reports/3d_validation.json")
    _add_artifact(section, out_dir, "reports/symbol_preview.svg")
    _add_artifact(section, out_dir, "reports/footprint_preview.svg")

    ir = _read_json_dict(ir_path, section, "library.ir.json")
    summary = _read_json_dict(summary_path, section, "summary.json")
    if ir is not None:
        tool_version = str(ir.get("properties", {}).get("tool_version", ""))
        section.details["tool_version"] = tool_version
        if not tool_version:
            section.warnings.append("library.ir.json has no properties.tool_version.")
        elif tool_version != f"v{__version__}":
            section.warnings.append(f"library.ir.json was generated by {tool_version}, current tool is v{__version__}.")
    if summary is not None:
        section.details["library_errors"] = len(_as_list(summary.get("errors")))
        section.details["library_warnings"] = len(_as_list(summary.get("warnings")))
        for error in _as_list(summary.get("errors")):
            section.errors.append(f"Library validation error: {error}")
        for warning in _as_list(summary.get("warnings")):
            section.warnings.append(f"Library validation warning: {warning}")
    if ir is not None and summary is not None:
        components = len(_as_list(ir.get("components")))
        footprints = len(_as_list(ir.get("footprints")))
        if int(summary.get("component_count", -1)) != components:
            section.errors.append(
                f"summary.json component_count is {summary.get('component_count')}, library.ir.json has {components}."
            )
        if int(summary.get("footprint_count", -1)) != footprints:
            section.errors.append(
                f"summary.json footprint_count is {summary.get('footprint_count')}, library.ir.json has {footprints}."
            )
        section.details["components"] = components
        section.details["footprints"] = footprints
    return section, ir, summary


def _validate_scripts_section(out_dir: Path) -> OutputSection:
    section = OutputSection("scripts")
    for rel_path in [
        "scripts/generate_allegro.il",
        "scripts/run_generate_allegro.scr",
        "scripts/create_symbols.ps1",
        "scripts/create_capture_olb.tcl",
        "scripts/run_capture_import.tcl",
        "scripts/generate_capture_library.ps1",
        "scripts/prepare_step_mapping.ps1",
        "scripts/run_step_package_mapping.scr",
        "scripts/apply_3d_mapping.il",
        "scripts/run_apply_3d_mapping.scr",
        "scripts/cadence_run_notes.md",
        "run_capture_import.tcl",
    ]:
        _add_artifact(section, out_dir, rel_path)
    return section


def _validate_capture_section(
    out_dir: Path,
    ir: dict[str, Any] | None,
    require_cadence: bool,
) -> OutputSection:
    section = OutputSection("capture")
    component_count = len(_as_list((ir or {}).get("components")))
    _add_artifact(section, out_dir, "capture/library.capture.xml")
    _add_artifact(section, out_dir, "capture/library.olb.xml")
    plan_path = _add_artifact(section, out_dir, "capture/capture_build_plan.json")
    _add_artifact(section, out_dir, "capture/README.md")

    plan = _read_json_dict(plan_path, section, "capture_build_plan.json")
    if plan is not None:
        plan_components = len(_as_list(plan.get("components")))
        section.details["plan_components"] = plan_components
        if component_count and plan_components != component_count:
            section.errors.append(
                f"capture_build_plan.json lists {plan_components} components, library.ir.json has {component_count}."
            )

    result_path = _add_artifact(
        section,
        out_dir,
        "capture/capture_build.results.json",
        required=require_cadence and component_count > 0,
    )
    result = _read_json_dict(result_path, section, "capture_build.results.json", required=False)
    olb_path = out_dir / "capture" / "library.olb"
    if result is None:
        _add_artifact(section, out_dir, "capture/library.olb", required=require_cadence and component_count > 0)
        if component_count > 0:
            message = "Capture OLB build result is missing; generated XML/scripts are ready but .olb is not proven."
            if require_cadence:
                section.errors.append(message)
            else:
                section.warnings.append(message)
        section.status = "script_ready"
        return section

    status = str(result.get("status", ""))
    section.details["result_status"] = status
    section.details["exitCode"] = result.get("exitCode")
    if result.get("olb"):
        olb_path = _resolve_reported_path(out_dir / "capture", result.get("olb"))
    section.artifacts.append(
        ArtifactCheck(
            path=_display_path(out_dir, olb_path),
            exists=olb_path.exists(),
            required=True,
            status="present" if olb_path.exists() else "missing",
        )
    )
    if status != "created_olb":
        section.errors.append(f"Capture OLB result status is {status or '<missing>'}, expected created_olb.")
    if not olb_path.exists():
        section.errors.append(f"Capture OLB file is missing: {_display_path(out_dir, olb_path)}")
    section.status = "created_olb" if not section.errors else "failed"
    return section


def _validate_allegro_section(
    out_dir: Path,
    ir: dict[str, Any] | None,
    require_cadence: bool,
) -> OutputSection:
    section = OutputSection("allegro")
    footprint_count = len(_as_list((ir or {}).get("footprints")))
    _add_artifact(section, out_dir, "allegro/padstacks/padstack_specs.csv")
    _add_artifact(section, out_dir, "allegro/symbols/footprints.json")
    plan_path = _add_artifact(section, out_dir, "allegro/symbols/symbol_build_plan.json")
    _add_artifact(section, out_dir, "allegro/README.md")

    plan = _read_json_dict(plan_path, section, "symbol_build_plan.json")
    symbols = []
    if plan is not None:
        symbols = _as_list(plan.get("symbols"))
        section.details["planned_symbols"] = len(symbols)
        if footprint_count and len(symbols) != footprint_count:
            section.errors.append(
                f"symbol_build_plan.json lists {len(symbols)} symbols, library.ir.json has {footprint_count} footprints."
            )
        for symbol in symbols:
            if not isinstance(symbol, dict):
                section.errors.append("symbol_build_plan.json contains a non-object symbol entry.")
                continue
            if symbol.get("skill_script"):
                _add_artifact(section, out_dir, _symbols_relative(str(symbol["skill_script"])))
            if symbol.get("allegro_script"):
                _add_artifact(section, out_dir, _symbols_relative(str(symbol["allegro_script"])))

    result_path = _add_artifact(
        section,
        out_dir,
        "allegro/symbols/create_symbols.results.json",
        required=require_cadence and footprint_count > 0,
    )
    result_data = _read_json(result_path, section, "create_symbols.results.json", required=False)
    if result_data is None:
        if footprint_count > 0:
            message = "Allegro symbol build result is missing; generated SKILL/replay scripts are ready but .dra/.psm are not proven."
            if require_cadence:
                section.errors.append(message)
            else:
                section.warnings.append(message)
        section.status = "script_ready"
        return section

    results = _as_list(result_data)
    section.details["result_count"] = len(results)
    if symbols and len(results) != len(symbols):
        section.errors.append(
            f"create_symbols.results.json has {len(results)} result rows, symbol_build_plan.json has {len(symbols)}."
        )
    for result in results:
        if not isinstance(result, dict):
            section.errors.append("create_symbols.results.json contains a non-object result entry.")
            continue
        footprint = str(result.get("footprint", "<unknown>"))
        status = str(result.get("status", ""))
        if status not in {"created_psm", "reused_psm"}:
            section.errors.append(
                f"Allegro result for {footprint} is {status or '<missing>'}, "
                "expected created_psm or reused_psm."
            )
        _check_reported_file(section, out_dir, out_dir / "allegro" / "symbols", result.get("dra"), f"{footprint} .dra")
        _check_reported_file(section, out_dir, out_dir / "allegro" / "symbols", result.get("psm"), f"{footprint} .psm")
    section.status = "created_psm" if not section.errors else "failed"
    return section


def _validate_3d_section(
    out_dir: Path,
    ir: dict[str, Any] | None,
    require_cadence: bool,
    require_3d: bool,
    allow_noncomponent_3d_exceptions: bool,
    noncomponent_exceptions: list[NonComponent3DException],
) -> OutputSection:
    section = OutputSection("3d")
    footprints = [footprint for footprint in _as_list((ir or {}).get("footprints")) if isinstance(footprint, dict)]
    footprint_count = len(footprints)
    linked_model_count = sum(1 for footprint in footprints if footprint.get("model3d"))
    exempt_names = {row.footprint for row in noncomponent_exceptions}
    component_footprints = [footprint for footprint in footprints if str(footprint.get("name", "")) not in exempt_names]
    component_footprint_count = len(component_footprints)
    linked_component_model_count = sum(1 for footprint in component_footprints if footprint.get("model3d"))
    _add_artifact(section, out_dir, "3d/3d_model_map.json")
    _add_artifact(section, out_dir, "3d/cadence_step_package.map")
    _add_artifact(section, out_dir, "3d/sym_3d_update_symbols.txt")
    _add_artifact(section, out_dir, "3d/README.md")

    step_report = validate_step_mapping_output(out_dir)
    step_payload = step_report.to_dict()
    section.details["step_status"] = step_payload["status"]
    section.details["entry_count"] = step_payload["entry_count"]
    section.details["facet_status"] = step_payload.get("facet_status", "unknown")
    section.details["facet_path"] = step_payload.get("facet_path", "")
    section.details["mapped_entry_count"] = step_payload.get("mapped_entry_count", 0)
    section.details["unmapped_entry_count"] = step_payload.get("unmapped_entry_count", 0)
    section.details["linked_model_count"] = linked_model_count
    section.details["missing_model_count"] = max(footprint_count - linked_model_count, 0)
    section.details["component_3d_footprint_count"] = component_footprint_count
    section.details["linked_component_3d_model_count"] = linked_component_model_count
    section.details["missing_component_3d_model_count"] = max(component_footprint_count - linked_component_model_count, 0)
    if allow_noncomponent_3d_exceptions:
        section.details["required_3d_footprint_count"] = component_footprint_count
        section.details["linked_required_3d_model_count"] = linked_component_model_count
    else:
        section.details["required_3d_footprint_count"] = footprint_count
        section.details["linked_required_3d_model_count"] = linked_model_count
    section.details["missing_required_3d_model_count"] = max(
        int(section.details["required_3d_footprint_count"]) - int(section.details["linked_required_3d_model_count"]),
        0,
    )
    section.details["noncomponent_3d_exemption_count"] = len(noncomponent_exceptions)
    section.details["noncomponent_3d_exemptions"] = [row.to_dict() for row in noncomponent_exceptions]
    placeholder_model_count = _placeholder_model_count(ir)
    section.details["placeholder_model_count"] = placeholder_model_count
    section.details["entries"] = [
        {
            "package": entry["package"],
            "status": entry["status"],
            "step_file": entry["step_file"],
            "mapping_status": entry.get("mapping_status", "unknown"),
            "mapping_properties": entry.get("mapping_properties", []),
        }
        for entry in step_payload["entries"]
    ]
    for error in step_payload["errors"]:
        section.errors.append(f"STEP mapping validation error: {error}")
    for warning in step_payload["warnings"]:
        section.warnings.append(f"STEP mapping validation warning: {warning}")
    if placeholder_model_count:
        section.warnings.append(
            f"3D output contains {placeholder_model_count} generated placeholder STEP envelope model(s); "
            "replace them with real vendor/JLC STEP files before mechanical signoff."
        )
    if require_3d:
        required_footprint_count = footprint_count
        required_linked_model_count = linked_model_count
        if allow_noncomponent_3d_exceptions:
            required_footprint_count = component_footprint_count
            required_linked_model_count = linked_component_model_count
        if placeholder_model_count:
            section.errors.append(
                f"3D coverage includes {placeholder_model_count} generated placeholder STEP envelope model(s); "
                "strict complete-3D acceptance requires real STEP/STP assets."
            )
        if required_footprint_count > 0 and required_linked_model_count == 0:
            section.errors.append(
                _missing_3d_message(required_linked_model_count, required_footprint_count, noncomponent_exceptions, allow_noncomponent_3d_exceptions)
            )
        elif required_footprint_count > 0 and required_linked_model_count < required_footprint_count:
            section.errors.append(
                _missing_3d_message(required_linked_model_count, required_footprint_count, noncomponent_exceptions, allow_noncomponent_3d_exceptions)
            )
        elif linked_model_count > 0 and int(step_payload["entry_count"]) != linked_model_count:
            section.errors.append(
                f"STEP mapping has {step_payload['entry_count']} entries, expected one STEP/STP entry for each of {linked_model_count} linked footprints."
            )
        for entry in step_payload["entries"]:
            if require_cadence:
                expected = "model_ready_symbols_built"
                if entry["status"] != expected:
                    section.errors.append(
                        f"STEP package {entry['package']} is {entry['status']}, expected {expected}."
                    )
            elif entry["status"] not in {
                "model_ready_symbol_planned",
                "model_ready_dra_built",
                "model_ready_symbols_built",
            }:
                section.errors.append(
                    f"STEP package {entry['package']} is {entry['status']}, expected a ready STEP/STP model entry."
                )
    if step_payload["entries"] and all(entry["status"] == "model_ready_symbols_built" for entry in step_payload["entries"]):
        section.status = "model_ready_symbols_built"
    else:
        section.status = str(step_payload["status"])
    return section


def _missing_3d_message(
    linked_model_count: int,
    footprint_count: int,
    noncomponent_exceptions: list[NonComponent3DException],
    allow_noncomponent_3d_exceptions: bool,
) -> str:
    if not allow_noncomponent_3d_exceptions:
        if linked_model_count == 0:
            return (
                f"STEP mapping has no entries for {footprint_count} footprints; "
                "no linked STEP/STP model is available."
            )
        return (
            f"STEP/STP 3D model coverage is incomplete: "
            f"{linked_model_count} of {footprint_count} footprints have linked models."
        )
    suffix = ""
    if noncomponent_exceptions:
        suffix = f" {len(noncomponent_exceptions)} non-component footprint(s) are explicitly exempt."
    if linked_model_count == 0:
        return (
            f"STEP mapping has no entries for {footprint_count} required component footprint(s); "
            "no linked STEP/STP model is available."
            f"{suffix}"
        )
    return (
        f"STEP/STP 3D model coverage is incomplete: "
        f"{linked_model_count} of {footprint_count} required component footprint(s) have linked models."
        f"{suffix}"
    )


def _add_artifact(
    section: OutputSection,
    out_dir: Path,
    rel_path: str,
    required: bool = True,
) -> Path:
    path = out_dir / rel_path
    exists = path.exists()
    normalized_path = rel_path.replace("\\", "/")
    section.artifacts.append(
        ArtifactCheck(
            path=normalized_path,
            exists=exists,
            required=required,
            status="present" if exists else "missing",
        )
    )
    if required and not exists:
        section.errors.append(f"Missing required output artifact: {normalized_path}")
    return path


def _read_json(path: Path, section: OutputSection, label: str, required: bool = True) -> Any:
    if not path.exists():
        if required:
            section.errors.append(f"Unable to read missing JSON file: {label}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        section.errors.append(f"Unable to parse {label}: {exc}")
        return None


def _read_json_dict(path: Path, section: OutputSection, label: str, required: bool = True) -> dict[str, Any] | None:
    data = _read_json(path, section, label, required=required)
    if data is None:
        return None
    if not isinstance(data, dict):
        section.errors.append(f"{label} root must be a JSON object.")
        return None
    return data


def _check_reported_file(section: OutputSection, out_dir: Path, base_dir: Path, value: object, label: str) -> None:
    if not value:
        section.errors.append(f"Missing reported path for {label}.")
        return
    path = _resolve_reported_path(base_dir, value)
    exists = path.exists()
    section.artifacts.append(
        ArtifactCheck(
            path=_display_path(out_dir, path),
            exists=exists,
            required=True,
            status="present" if exists else "missing",
        )
    )
    if not exists:
        section.errors.append(f"Reported {label} file is missing: {_display_path(out_dir, path)}")


def _resolve_reported_path(base_dir: Path, value: object) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _display_path(out_dir: Path, path: Path) -> str:
    path = path.expanduser().resolve()
    try:
        return path.relative_to(out_dir).as_posix()
    except ValueError:
        return str(path)


def _symbols_relative(path: str) -> str:
    return (Path("allegro") / "symbols" / Path(path)).as_posix()


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in [None, ""]:
        return []
    return [value]


def _counts_from_ir_and_summary(
    ir: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    noncomponent_exceptions: list[NonComponent3DException] | None = None,
    allow_noncomponent_3d_exceptions: bool = False,
) -> dict[str, int]:
    counts = {
        "components": 0,
        "footprints": 0,
        "linked_models": 0,
        "placeholder_models": 0,
        "noncomponent_3d_exemptions": 0,
        "component_3d_footprints": 0,
        "linked_component_3d_models": 0,
        "missing_component_3d_models": 0,
        "required_3d_footprints": 0,
        "linked_required_3d_models": 0,
        "missing_required_3d_models": 0,
        "library_errors": 0,
        "library_warnings": 0,
    }
    if ir is not None:
        noncomponent_exceptions = noncomponent_exceptions or detect_noncomponent_3d_exceptions(ir)
        exempt_names = {row.footprint for row in noncomponent_exceptions}
        footprints = _as_list(ir.get("footprints"))
        counts["components"] = len(_as_list(ir.get("components")))
        counts["footprints"] = len(footprints)
        counts["linked_models"] = sum(1 for footprint in footprints if isinstance(footprint, dict) and footprint.get("model3d"))
        counts["placeholder_models"] = _placeholder_model_count(ir)
        component_footprints = [
            footprint
            for footprint in footprints
            if isinstance(footprint, dict) and str(footprint.get("name", "")) not in exempt_names
        ]
        counts["noncomponent_3d_exemptions"] = len(noncomponent_exceptions)
        counts["component_3d_footprints"] = len(component_footprints)
        counts["linked_component_3d_models"] = sum(1 for footprint in component_footprints if footprint.get("model3d"))
        counts["missing_component_3d_models"] = max(
            counts["component_3d_footprints"] - counts["linked_component_3d_models"],
            0,
        )
        if allow_noncomponent_3d_exceptions:
            counts["required_3d_footprints"] = counts["component_3d_footprints"]
            counts["linked_required_3d_models"] = counts["linked_component_3d_models"]
        else:
            counts["required_3d_footprints"] = counts["footprints"]
            counts["linked_required_3d_models"] = counts["linked_models"]
        counts["missing_required_3d_models"] = max(
            counts["required_3d_footprints"] - counts["linked_required_3d_models"],
            0,
        )
    if summary is not None:
        counts["library_errors"] = len(_as_list(summary.get("errors")))
        counts["library_warnings"] = len(_as_list(summary.get("warnings")))
    return counts


def _placeholder_model_count(ir: dict[str, Any] | None) -> int:
    if ir is None:
        return 0
    count = 0
    for footprint in _as_list(ir.get("footprints")):
        if not isinstance(footprint, dict):
            continue
        model = footprint.get("model3d")
        if isinstance(model, dict) and is_placeholder_model(model):
            count += 1
    return count


def _finalize_section(section: OutputSection) -> None:
    if section.errors:
        section.status = "failed"
    elif section.status == "unchecked":
        section.status = "warning" if section.warnings else "complete"


def _overall_status(report: OutputValidationReport) -> str:
    if report.errors:
        return "failed"
    statuses = {section.name: section.status for section in report.sections}
    if report.require_cadence:
        return "complete"
    if statuses.get("capture") == "script_ready" or statuses.get("allegro") == "script_ready":
        return "script_ready"
    if report.warnings:
        return "warning"
    return "complete"
