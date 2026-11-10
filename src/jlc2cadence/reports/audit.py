from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..validators.output_package import OutputValidationReport, validate_output_package
from ..validators.step_mapping import StepMappingValidationReport, validate_step_mapping_output


@dataclass
class ChecklistItem:
    name: str
    status: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "notes": list(self.notes),
        }


@dataclass
class OutputAuditReport:
    version: str
    out_dir: str
    require_cadence: bool
    require_3d: bool
    allow_noncomponent_3d_exceptions: bool
    status: str
    counts: dict[str, int]
    deliverables: dict[str, Any]
    checklist: list[ChecklistItem]
    issues: dict[str, list[str]]
    next_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "out_dir": self.out_dir,
            "require_cadence": self.require_cadence,
            "require_3d": self.require_3d,
            "allow_noncomponent_3d_exceptions": self.allow_noncomponent_3d_exceptions,
            "status": self.status,
            "counts": dict(self.counts),
            "deliverables": self.deliverables,
            "checklist": [item.to_dict() for item in self.checklist],
            "issues": {
                "errors": list(self.issues.get("errors", [])),
                "warnings": list(self.issues.get("warnings", [])),
            },
            "next_actions": list(self.next_actions),
        }


def build_output_audit(
    out_dir: Path,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
) -> OutputAuditReport:
    out_dir = out_dir.expanduser().resolve()
    output = validate_output_package(
        out_dir,
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )
    step = validate_step_mapping_output(out_dir)
    ir = _read_json_dict(out_dir / "reports" / "library.ir.json")
    summary = _read_json_dict(out_dir / "reports" / "summary.json")
    model_map = _read_json_dict(out_dir / "3d" / "3d_model_map.json")
    transforms = _read_json_dict(out_dir / "3d" / "easyeda_3d_transforms.json")

    deliverables = {
        "reports": _reports_deliverable(out_dir, output, ir, summary),
        "capture": _capture_deliverable(out_dir, output, ir),
        "allegro": _allegro_deliverable(out_dir, output, ir),
        "models_3d": _models_3d_deliverable(output, step, model_map, transforms),
    }
    checklist = _build_checklist(output, step, deliverables, require_cadence, require_3d)
    issues = {
        "errors": list(output.errors),
        "warnings": list(dict.fromkeys(output.warnings + step.warnings)),
    }
    status = _audit_status(output, require_cadence, require_3d)
    next_actions = _next_actions(status, checklist, deliverables, output, require_cadence, require_3d)

    return OutputAuditReport(
        version=f"v{__version__}",
        out_dir=str(out_dir),
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
        status=status,
        counts=dict(output.counts),
        deliverables=deliverables,
        checklist=checklist,
        issues=issues,
        next_actions=next_actions,
    )


def write_output_audit(
    out_dir: Path,
    reports_dir: Path | None = None,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
) -> OutputAuditReport:
    out_dir = out_dir.expanduser().resolve()
    reports_dir = reports_dir or out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = build_output_audit(
        out_dir,
        require_cadence=require_cadence,
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )
    (reports_dir / "audit.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_output_audit_markdown(report, reports_dir / "audit.md")
    return report


def write_output_audit_markdown(report: OutputAuditReport, path: Path) -> Path:
    data = report.to_dict()
    deliverables = data["deliverables"]
    lines = [
        "# jlc2cadence output audit",
        "",
        f"- Version: {data['version']}",
        f"- Status: {data['status']}",
        f"- Output: {data['out_dir']}",
        f"- Require Cadence assets: {str(data['require_cadence']).lower()}",
        f"- Require 3D models: {str(data['require_3d']).lower()}",
        f"- Allow non-component 3D exceptions: {str(data['allow_noncomponent_3d_exceptions']).lower()}",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    for key, value in data["counts"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(
        [
            "",
            "## Deliverables",
            "",
            "| Area | Status | Evidence |",
            "|---|---|---|",
            f"| Capture | {deliverables['capture']['status']} | {_capture_evidence(deliverables['capture'])} |",
            f"| Allegro | {deliverables['allegro']['status']} | {_allegro_evidence(deliverables['allegro'])} |",
            f"| 3D models | {deliverables['models_3d']['status']} | {_model_evidence(deliverables['models_3d'])} |",
            "",
            "## Checklist",
            "",
            "| Step | Status | Notes |",
            "|---|---|---|",
        ]
    )
    for item in data["checklist"]:
        lines.append(f"| {item['name']} | {item['status']} | {'; '.join(item['notes'])} |")

    lines.extend(["", "## Issues", ""])
    if data["issues"]["errors"]:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in data["issues"]["errors"])
        lines.append("")
    if data["issues"]["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in data["issues"]["warnings"])
        lines.append("")
    if not data["issues"]["errors"] and not data["issues"]["warnings"]:
        lines.append("No blocking issues or warnings were found.")
        lines.append("")

    lines.extend(["## Next Actions", ""])
    lines.extend(f"- {action}" for action in data["next_actions"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _reports_deliverable(
    out_dir: Path,
    output: OutputValidationReport,
    ir: dict[str, Any] | None,
    summary: dict[str, Any] | None,
) -> dict[str, Any]:
    section = _section(output, "reports")
    return {
        "status": section.status if section else "missing",
        "ir": _artifact(out_dir, "reports/library.ir.json"),
        "summary": _artifact(out_dir, "reports/summary.json"),
        "html": _artifact(out_dir, "reports/summary.html"),
        "component_names": _component_names(ir),
        "footprint_names": _footprint_names(ir),
        "library_errors": len(_as_list((summary or {}).get("errors"))),
        "library_warnings": len(_as_list((summary or {}).get("warnings"))),
    }


def _capture_deliverable(out_dir: Path, output: OutputValidationReport, ir: dict[str, Any] | None) -> dict[str, Any]:
    section = _section(output, "capture")
    result = _read_json_dict(out_dir / "capture" / "capture_build.results.json")
    return {
        "status": section.status if section else "missing",
        "component_count": len(_as_list((ir or {}).get("components"))),
        "xml": _artifact(out_dir, "capture/library.olb.xml"),
        "olb": _artifact(out_dir, "capture/library.olb"),
        "result_status": str((result or {}).get("status", "")),
        "exit_code": (result or {}).get("exitCode"),
    }


def _allegro_deliverable(out_dir: Path, output: OutputValidationReport, ir: dict[str, Any] | None) -> dict[str, Any]:
    section = _section(output, "allegro")
    result_rows = _as_list(_read_json(out_dir / "allegro" / "symbols" / "create_symbols.results.json"))
    result_statuses = {}
    for row in result_rows:
        if isinstance(row, dict):
            result_statuses[str(row.get("footprint", "<unknown>"))] = str(row.get("status", ""))
    return {
        "status": section.status if section else "missing",
        "footprint_count": len(_as_list((ir or {}).get("footprints"))),
        "padstack_csv": _artifact(out_dir, "allegro/padstacks/padstack_specs.csv"),
        "padstack_count": _padstack_count(out_dir / "allegro" / "padstacks" / "padstack_specs.csv"),
        "pad_file_count": len(list((out_dir / "allegro" / "padstacks").glob("*.pad"))),
        "planned_symbols": _planned_symbol_count(out_dir / "allegro" / "symbols" / "symbol_build_plan.json"),
        "dra_count": len(list((out_dir / "allegro" / "symbols").glob("*.dra"))),
        "psm_count": len(list((out_dir / "allegro" / "symbols").glob("*.psm"))),
        "result_statuses": result_statuses,
    }


def _models_3d_deliverable(
    output: OutputValidationReport,
    step: StepMappingValidationReport,
    model_map: dict[str, Any] | None,
    transforms: dict[str, Any] | None,
) -> dict[str, Any]:
    section = _section(output, "3d")
    footprint_rows = _as_list((model_map or {}).get("footprints"))
    status_counts: dict[str, int] = {}
    for row in footprint_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    placeholder_count = int(
        (model_map or {}).get("cadence", {}).get(
            "placeholder_step_models",
            status_counts.get("placeholder_step_map_ready", 0),
        )
        or 0
    )
    transform_rows = _as_list((transforms or {}).get("footprints"))
    return {
        "status": section.status if section else step.status,
        "step_map": _artifact(Path(output.out_dir), "3d/cadence_step_package.map"),
        "step_entry_count": len(step.entries),
        "step_statuses": [{"package": entry.package, "status": entry.status, "step_file": entry.step_file} for entry in step.entries],
        "mapping_statuses": [
            {
                "package": entry.package,
                "mapping_status": entry.mapping_status,
                "mapping_properties": list(entry.mapping_properties),
            }
            for entry in step.entries
        ],
        "mapped_entry_count": step.mapped_entry_count,
        "unmapped_entry_count": step.unmapped_entry_count,
        "facet_status": step.facet_status,
        "facet_path": step.facet_path,
        "model_status_counts": status_counts,
        "placeholder_step_models": placeholder_count,
        "easyeda_transform_count": len(transform_rows),
        "easyeda_transforms": transform_rows,
    }


def _build_checklist(
    output: OutputValidationReport,
    step: StepMappingValidationReport,
    deliverables: dict[str, Any],
    require_cadence: bool,
    require_3d: bool,
) -> list[ChecklistItem]:
    reports = deliverables["reports"]
    capture = deliverables["capture"]
    allegro = deliverables["allegro"]
    models = deliverables["models_3d"]
    return [
        ChecklistItem(
            "parse_input",
            _failed_if_errors("complete", reports["library_errors"] > 0),
            [f"{reports['component_names']['count']} components", f"{reports['footprint_names']['count']} footprints"],
        ),
        ChecklistItem(
            "capture_xml",
            "complete" if capture["xml"]["exists"] else "failed",
            ["library.olb.xml generated"] if capture["xml"]["exists"] else ["missing Capture XML"],
        ),
        ChecklistItem(
            "capture_olb",
            _cadence_artifact_status(capture["olb"]["exists"], capture["component_count"], require_cadence),
            [f"result={capture['result_status'] or 'not_run'}"],
        ),
        ChecklistItem(
            "allegro_padstacks",
            "complete" if allegro["padstack_csv"]["exists"] else "failed",
            [f"{allegro['padstack_count']} padstack specs", f"{allegro['pad_file_count']} .pad files"],
        ),
        ChecklistItem(
            "allegro_dra_psm",
            _cadence_symbol_status(allegro, require_cadence),
            [f"{allegro['dra_count']} .dra", f"{allegro['psm_count']} .psm"],
        ),
        ChecklistItem(
            "step_package_mapping",
            _step_package_mapping_status(step, require_3d, output),
            [
                f"{models['step_entry_count']} STEP/STP entries",
                f"{output.counts.get('linked_models', 0)}/{output.counts.get('footprints', 0)} footprints linked",
                (
                    f"{output.counts.get('linked_required_3d_models', output.counts.get('linked_models', 0))}/"
                    f"{output.counts.get('required_3d_footprints', output.counts.get('footprints', 0))} required component footprints linked"
                ),
                f"{output.counts.get('noncomponent_3d_exemptions', 0)} non-component exceptions",
                f"{models['placeholder_step_models']} placeholder STEP envelopes",
                f"status={step.status}",
            ],
        ),
        ChecklistItem(
            "easyeda_3d_transform_review",
            "complete" if models["easyeda_transform_count"] else "not_applicable",
            [f"{models['easyeda_transform_count']} transform rows"],
        ),
        ChecklistItem(
            "cadence_3d_canvas_import",
            "manual_review" if models["step_entry_count"] else "not_applicable",
            [
                "Import cadence_step_package.map with Allegro step pkg map and inspect alignment.",
                f"facet_status={models['facet_status']}",
            ],
        ),
        ChecklistItem(
            "cadence_3d_symbol_mapping",
            (
                "complete"
                if models["step_entry_count"] and models["mapped_entry_count"] == models["step_entry_count"]
                else ("manual_review" if models["step_entry_count"] else "not_applicable")
            ),
            [
                f"mapped={models['mapped_entry_count']}/{models['step_entry_count']}",
                "run validate-3d --require-mapped after saving Cadence mapping",
            ],
        ),
    ]


def _audit_status(output: OutputValidationReport, require_cadence: bool, require_3d: bool) -> str:
    if output.errors or output.status == "failed":
        return "failed"
    if require_cadence and output.status == "complete":
        return "cadence_ready"
    if output.status == "script_ready":
        return "script_ready"
    if output.warnings:
        return "warning"
    return output.status


def _next_actions(
    status: str,
    checklist: list[ChecklistItem],
    deliverables: dict[str, Any],
    output: OutputValidationReport,
    require_cadence: bool,
    require_3d: bool,
) -> list[str]:
    failed = [item for item in checklist if item.status == "failed"]
    if failed:
        actions = [f"Fix {item.name}: {'; '.join(item.notes)}" for item in failed]
        if require_3d and _missing_required_3d(output):
            actions.append("Export or fetch matching STEP/STP models, then rerun conversion with those models attached.")
        return actions
    if status == "script_ready":
        return [
            "Review generated scripts, then rerun convert with --run-cadence to create .olb, .dra, and .psm files.",
            "Run audit again with --require-cadence after Cadence assets are generated.",
        ]
    actions = []
    capture = deliverables["capture"]
    allegro = deliverables["allegro"]
    if require_cadence and capture["status"] != "created_olb":
        actions.append("Run or repair Capture XML-to-OLB generation.")
    if require_cadence and allegro["status"] != "created_psm":
        actions.append("Run or repair Allegro package symbol generation.")
    if require_3d and _missing_required_3d(output):
        actions.append("Export or fetch matching STEP/STP models, then rerun conversion with those models attached.")
    elif require_3d and not deliverables["models_3d"]["step_entry_count"]:
        actions.append("Export or fetch matching STEP/STP models, then rerun conversion with those models attached.")
    if output.counts.get("placeholder_models", 0):
        actions.append("Replace generated placeholder STEP envelopes with real vendor/JLC STEP files before mechanical signoff.")
    if deliverables["models_3d"]["step_entry_count"]:
        models = deliverables["models_3d"]
        if models["mapped_entry_count"] < models["step_entry_count"]:
            actions.append(
                "In Allegro Package mode, run step pkg map, import 3d/cadence_step_package.map, "
                "save each package symbol, then rerun validate-3d --require-mapped."
            )
        if models["facet_status"] == "missing":
            actions.append(
                "Ensure Cadence stepFacetFiles4Map facet data is available before checking 3D Canvas."
            )
        actions.append("Review offsets, rotations, and alignment in Cadence 3D Canvas.")
    if not actions:
        actions.append("Use this output in the real-sample manifest or compare it against the expected package counts.")
    return actions


def _section(output: OutputValidationReport, name: str):
    for section in output.sections:
        if section.name == name:
            return section
    return None


def _component_names(ir: dict[str, Any] | None) -> dict[str, Any]:
    names = [str(item.get("name", "")) for item in _as_list((ir or {}).get("components")) if isinstance(item, dict)]
    return _name_summary(names)


def _footprint_names(ir: dict[str, Any] | None) -> dict[str, Any]:
    names = [str(item.get("name", "")) for item in _as_list((ir or {}).get("footprints")) if isinstance(item, dict)]
    return _name_summary(names)


def _name_summary(names: list[str], limit: int = 50) -> dict[str, Any]:
    return {
        "count": len(names),
        "shown": names[:limit],
        "truncated": max(0, len(names) - limit),
    }


def _artifact(out_dir: Path, rel_path: str) -> dict[str, Any]:
    path = out_dir / rel_path
    return {
        "path": rel_path.replace("\\", "/"),
        "exists": path.exists(),
    }


def _padstack_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            return sum(1 for _row in csv.DictReader(handle))
    except OSError:
        return 0


def _planned_symbol_count(path: Path) -> int:
    plan = _read_json_dict(path)
    return len(_as_list((plan or {}).get("symbols")))


def _cadence_artifact_status(exists: bool, expected_count: int, require_cadence: bool) -> str:
    if expected_count == 0:
        return "not_applicable"
    if exists:
        return "complete"
    return "failed" if require_cadence else "script_ready"


def _step_package_mapping_status(
    step: StepMappingValidationReport,
    require_3d: bool,
    output: OutputValidationReport,
) -> str:
    if step.errors:
        return "failed"
    if require_3d and _missing_required_3d(output):
        return "failed"
    if step.status == "no_step_entries" and output.counts.get("required_3d_footprints", 0):
        return "failed" if require_3d else "warning"
    return "complete"


def _missing_required_3d(output: OutputValidationReport) -> bool:
    linked = output.counts.get("linked_required_3d_models", output.counts.get("linked_models", 0))
    required = output.counts.get("required_3d_footprints", output.counts.get("footprints", 0))
    return linked < required


def _cadence_symbol_status(allegro: dict[str, Any], require_cadence: bool) -> str:
    if allegro["footprint_count"] == 0:
        return "not_applicable"
    result_statuses = dict(allegro.get("result_statuses") or {})
    if result_statuses and any(status != "created_psm" for status in result_statuses.values()):
        return "failed"
    if allegro["psm_count"] >= allegro["footprint_count"] and allegro["dra_count"] >= allegro["footprint_count"]:
        return "complete"
    return "failed" if require_cadence else "script_ready"


def _failed_if_errors(ok_status: str, failed: bool) -> str:
    return "failed" if failed else ok_status


def _capture_evidence(capture: dict[str, Any]) -> str:
    return f"components={capture['component_count']}, olb={str(capture['olb']['exists']).lower()}"


def _allegro_evidence(allegro: dict[str, Any]) -> str:
    return (
        f"footprints={allegro['footprint_count']}, padstacks={allegro['padstack_count']}, "
        f"dra={allegro['dra_count']}, psm={allegro['psm_count']}"
    )


def _model_evidence(models: dict[str, Any]) -> str:
    return (
        f"step_entries={models['step_entry_count']}, "
        f"placeholders={models.get('placeholder_step_models', 0)}, "
        f"transforms={models['easyeda_transform_count']}"
    )


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in [None, ""]:
        return []
    return [value]
