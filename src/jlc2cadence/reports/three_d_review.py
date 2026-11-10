from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..generators.placeholder3d import is_placeholder_model
from ..validators.noncomponent3d import NonComponent3DException, detect_noncomponent_3d_exceptions
from ..validators.step_mapping import StepModelEntry, validate_step_mapping_output


READY_FOR_CANVAS_REVIEW = "ready_for_canvas_review"
CANVAS_SIGNED_OFF = "canvas_signed_off"
CANVAS_SIGNOFF_INCOMPLETE = "canvas_signoff_incomplete"
CANVAS_SIGNOFF_FAILED = "canvas_signoff_failed"
NONCOMPONENT_3D_EXEMPT = "noncomponent_3d_exempt"
READY_FOR_CANVAS_REVIEW_WITH_EXCEPTIONS = "ready_for_canvas_review_with_noncomponent_exceptions"
CANVAS_SIGNED_OFF_WITH_EXCEPTIONS = "canvas_signed_off_with_noncomponent_exceptions"

_PASS_STATUSES = {"pass", "passed", "ok", "complete", "completed", "yes", "signed_off"}
_FAIL_STATUSES = {"fail", "failed", "no", "reject", "rejected"}
_TODO_STATUSES = {"", "todo", "pending", "incomplete", "not_checked"}


@dataclass
class ThreeDManualCheck:
    name: str
    status: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "notes": self.notes,
        }


@dataclass
class ThreeDReviewItem:
    footprint: str
    review_status: str
    cadence_canvas_ready: bool
    cadence_blocker: str
    step_file: str = ""
    model_file: str = ""
    model_exists: bool = False
    model_status: str = ""
    is_placeholder: bool = False
    is_noncomponent_3d_exempt: bool = False
    noncomponent_reason: str = ""
    dra_exists: bool = False
    psm_exists: bool = False
    mapping_status: str = "not_checked"
    mapping_properties: list[str] = field(default_factory=list)
    facet_status: str = "not_checked"
    manual_checks: list[ThreeDManualCheck] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "footprint": self.footprint,
            "review_status": self.review_status,
            "cadence_canvas_ready": self.cadence_canvas_ready,
            "cadence_blocker": self.cadence_blocker,
            "step_file": self.step_file,
            "model_file": self.model_file,
            "model_exists": self.model_exists,
            "model_status": self.model_status,
            "is_placeholder": self.is_placeholder,
            "is_noncomponent_3d_exempt": self.is_noncomponent_3d_exempt,
            "noncomponent_reason": self.noncomponent_reason,
            "dra_exists": self.dra_exists,
            "psm_exists": self.psm_exists,
            "mapping_status": self.mapping_status,
            "mapping_properties": list(self.mapping_properties),
            "facet_status": self.facet_status,
            "manual_checks": [check.to_dict() for check in self.manual_checks],
            "next_action": self.next_action,
        }


@dataclass
class ThreeDReviewReport:
    version: str
    out_dir: str
    allow_noncomponent_3d_exceptions: bool
    status: str
    counts: dict[str, int]
    items: list[ThreeDReviewItem]
    signoff_file: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "out_dir": self.out_dir,
            "allow_noncomponent_3d_exceptions": self.allow_noncomponent_3d_exceptions,
            "status": self.status,
            "counts": dict(self.counts),
            "signoff_file": self.signoff_file,
            "items": [item.to_dict() for item in self.items],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }


def build_3d_review_report(
    out_dir: Path,
    signoff_path: Path | None = None,
    allow_noncomponent_3d_exceptions: bool = False,
) -> ThreeDReviewReport:
    out_dir = out_dir.expanduser().resolve()
    ir, ir_warning = _read_json_dict(out_dir / "reports" / "library.ir.json")
    model_map, model_map_warning = _read_json_dict(out_dir / "3d" / "3d_model_map.json")
    step_report = validate_step_mapping_output(out_dir)

    warnings = []
    if ir_warning:
        warnings.append(ir_warning)
    if model_map_warning:
        warnings.append(model_map_warning)
    warnings.extend(step_report.errors)
    warnings.extend(step_report.warnings)

    footprint_rows = _footprint_rows(ir, model_map)
    step_entries = {entry.package: entry for entry in step_report.entries}
    ordered_names = _ordered_footprint_names(ir, model_map, step_report.entries)
    noncomponent_exceptions = {row.footprint: row for row in detect_noncomponent_3d_exceptions(ir)}
    items = [
        _build_item(
            out_dir,
            name,
            footprint_rows.get(name, {}),
            step_entries.get(name),
            step_report.facet_status,
            noncomponent_exceptions.get(name),
            allow_noncomponent_3d_exceptions,
        )
        for name in ordered_names
    ]

    errors = []
    if not items:
        errors.append("No footprint or STEP mapping entries were found to review.")
    signoff_file = ""
    signoff_notes: list[str] = []
    if signoff_path is not None:
        signoff_path = signoff_path.expanduser().resolve()
        signoff_file = str(signoff_path)
        signoff_rows, signoff_warnings = _read_signoff_file(signoff_path)
        warnings.extend(signoff_warnings)
        _apply_signoff(items, signoff_rows, errors, warnings)
        signoff_notes.append(f"Manual signoff data was loaded from {signoff_path}.")
    counts = _counts(items, len(step_report.entries))
    status = _overall_status(items, errors)
    warnings.extend(_status_warnings(counts))
    notes = [
        "Manual checklist fields are intentionally left as todo until a person opens the .dra in Cadence 3D Canvas.",
        "Generated placeholder STEP envelopes can prove the Cadence mapping workflow but cannot satisfy mechanical signoff.",
    ]
    if counts.get("allowed_noncomponent_3d_exemptions", 0):
        notes.append(
            f"{counts['allowed_noncomponent_3d_exemptions']} non-component PCB feature(s) are explicitly exempt from package 3D coverage."
        )
    notes.extend(signoff_notes)
    return ThreeDReviewReport(
        version=f"v{__version__}",
        out_dir=str(out_dir),
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
        status=status,
        counts=counts,
        items=items,
        signoff_file=signoff_file,
        errors=errors,
        warnings=list(dict.fromkeys(warnings)),
        notes=notes,
    )


def write_3d_review_report(
    out_dir: Path,
    reports_dir: Path | None = None,
    signoff_path: Path | None = None,
    allow_noncomponent_3d_exceptions: bool = False,
) -> ThreeDReviewReport:
    out_dir = out_dir.expanduser().resolve()
    reports_dir = reports_dir or out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = build_3d_review_report(
        out_dir,
        signoff_path=signoff_path,
        allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
    )
    (reports_dir / "3d_review.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_3d_review_markdown(report, reports_dir / "3d_review.md")
    return report


def write_3d_signoff_template(
    out_dir: Path,
    path: Path,
    allow_noncomponent_3d_exceptions: bool = False,
) -> Path:
    """Write an editable signoff file containing only Canvas-ready footprints."""
    out_dir = out_dir.expanduser().resolve()
    path = path.expanduser().resolve()
    report = build_3d_review_report(out_dir, allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions)
    payload = {
        "version": report.version,
        "out_dir": report.out_dir,
        "instructions": [
            "Open each listed .dra in Cadence 3D Canvas.",
            "Set every manual_checks[].status to pass, fail, or todo.",
            "Keep the notes as evidence of the visual check.",
            "Pass this file to 3d-review --signoff after review.",
        ],
        "items": [
            {
                "footprint": item.footprint,
                "review_status": item.review_status,
                "step_file": item.step_file,
                "model_file": item.model_file,
                "manual_checks": [
                    {
                        "name": check.name,
                        "status": "todo",
                        "notes": check.notes,
                    }
                    for check in item.manual_checks
                ],
            }
            for item in report.items
            if item.cadence_canvas_ready and not item.is_placeholder
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_3d_review_markdown(report: ThreeDReviewReport, path: Path) -> Path:
    data = report.to_dict()
    lines = [
        "# jlc2cadence 3D review",
        "",
        f"- Version: {data['version']}",
        f"- Status: {data['status']}",
        f"- Output: {data['out_dir']}",
        f"- Allow non-component 3D exceptions: {str(data['allow_noncomponent_3d_exceptions']).lower()}",
    ]
    if data.get("signoff_file"):
        lines.append(f"- Signoff: {data['signoff_file']}")
    lines.extend([
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ])
    for key, value in data["counts"].items():
        lines.append(f"| {_md(key)} | {value} |")

    lines.extend(
        [
            "",
            "## Footprint Review",
            "",
            "| Footprint | Status | STEP | DRA | PSM | Mapping | Facets | Placeholder | Non-component | Next Action |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in data["items"]:
        lines.append(
            "| {footprint} | {status} | {step} | {dra} | {psm} | {mapping} | {facet} | {placeholder} | {noncomponent} | {action} |".format(
                footprint=_md(item["footprint"]),
                status=_md(item["review_status"]),
                step=_md(item["step_file"] or "-"),
                dra="yes" if item["dra_exists"] else "no",
                psm="yes" if item["psm_exists"] else "no",
                mapping=_md(item["mapping_status"]),
                facet=_md(item["facet_status"]),
                placeholder="yes" if item["is_placeholder"] else "no",
                noncomponent=_md(item["noncomponent_reason"] or "-"),
                action=_md(item["next_action"]),
            )
        )

    ready_items = [item for item in data["items"] if item["cadence_canvas_ready"]]
    lines.extend(["", "## Manual Canvas Checklist", ""])
    if ready_items:
        for item in ready_items:
            lines.append(f"### {_md(item['footprint'])}")
            lines.append("")
            for check in item["manual_checks"]:
                marker = "x" if check["status"] == "pass" else " "
                lines.append(f"- [{marker}] {_md(check['name'])} ({_md(check['status'])}): {_md(check['notes'])}")
            lines.append("")
    else:
        lines.append("No footprint is ready for Cadence 3D Canvas checklist review yet.")
        lines.append("")

    lines.extend(["## Issues", ""])
    if data["errors"]:
        lines.append("Errors:")
        lines.extend(f"- {_md(error)}" for error in data["errors"])
        lines.append("")
    if data["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- {_md(warning)}" for warning in data["warnings"])
        lines.append("")
    if not data["errors"] and not data["warnings"]:
        lines.append("No report-generation errors or review blockers were found.")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _build_item(
    out_dir: Path,
    footprint: str,
    model_row: dict[str, Any],
    entry: StepModelEntry | None,
    facet_status: str,
    noncomponent_exception: NonComponent3DException | None = None,
    allow_noncomponent_3d_exceptions: bool = False,
) -> ThreeDReviewItem:
    model = model_row.get("model3d") if isinstance(model_row, dict) else None
    step_file = _step_file(model_row, entry, model)
    model_file = _model_file(model, step_file, out_dir)
    model_status = str(model_row.get("status", "")) if isinstance(model_row, dict) else ""
    placeholder = model_status == "placeholder_step_map_ready" or is_placeholder_model(model)
    model_exists = entry.model_exists if entry else _path_exists(model_file)
    blocker = _cadence_blocker(model_row, entry, step_file, model_exists, facet_status)
    noncomponent_reason = noncomponent_exception.reason if noncomponent_exception is not None else ""
    noncomponent_exempt = bool(noncomponent_exception and allow_noncomponent_3d_exceptions)
    if noncomponent_exempt:
        blocker = ""
        canvas_ready = False
        review_status = NONCOMPONENT_3D_EXEMPT
        manual_checks: list[ThreeDManualCheck] = []
    else:
        canvas_ready = blocker == ""
        review_status = "placeholder_not_for_signoff" if placeholder else (blocker or READY_FOR_CANVAS_REVIEW)
        manual_checks = _manual_checks(canvas_ready, placeholder, blocker)
    return ThreeDReviewItem(
        footprint=footprint,
        review_status=review_status,
        cadence_canvas_ready=canvas_ready,
        cadence_blocker=blocker,
        step_file=step_file,
        model_file=model_file,
        model_exists=model_exists,
        model_status=model_status,
        is_placeholder=placeholder,
        is_noncomponent_3d_exempt=noncomponent_exempt,
        noncomponent_reason=noncomponent_reason,
        dra_exists=bool(entry and entry.dra_exists),
        psm_exists=bool(entry and entry.psm_exists),
        mapping_status=entry.mapping_status if entry else "not_checked",
        mapping_properties=list(entry.mapping_properties) if entry else [],
        facet_status=facet_status,
        manual_checks=manual_checks,
        next_action=_next_action(review_status, blocker, canvas_ready),
    )


def _read_signoff_file(path: Path) -> tuple[dict[str, dict[str, dict[str, str]]], list[str]]:
    payload, warning = _read_json_dict(path)
    if payload is None:
        return {}, [warning]

    rows: dict[str, dict[str, dict[str, str]]] = {}
    warnings: list[str] = []

    if isinstance(payload.get("items"), list):
        for item in payload["items"]:
            if not isinstance(item, dict):
                continue
            footprint = str(item.get("footprint", "")).strip()
            checks = _parse_signoff_checks(item.get("manual_checks"))
            if footprint and checks:
                rows[footprint] = checks
        return rows, warnings

    footprints = payload.get("footprints", payload)
    if isinstance(footprints, list):
        for item in footprints:
            if not isinstance(item, dict):
                continue
            footprint = str(item.get("footprint", "")).strip()
            checks = _parse_signoff_checks(item.get("manual_checks", item.get("checks")))
            if footprint and checks:
                rows[footprint] = checks
        return rows, warnings

    if not isinstance(footprints, dict):
        warnings.append(f"{path} does not contain a usable signoff mapping.")
        return rows, warnings

    for footprint, value in footprints.items():
        footprint_name = str(footprint).strip()
        checks = _parse_signoff_checks(value)
        if footprint_name and checks:
            rows[footprint_name] = checks
    return rows, warnings


def _parse_signoff_checks(value: object) -> dict[str, dict[str, str]]:
    checks: dict[str, dict[str, str]] = {}
    if isinstance(value, list):
        for row in value:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            checks[name] = {
                "status": _normalize_signoff_status(str(row.get("status", ""))),
                "notes": str(row.get("notes", "")),
            }
        return checks

    if not isinstance(value, dict):
        return checks
    if "manual_checks" in value or "checks" in value:
        return _parse_signoff_checks(value.get("manual_checks", value.get("checks")))

    for name, raw in value.items():
        check_name = str(name).strip()
        if not check_name:
            continue
        if isinstance(raw, dict):
            status = str(raw.get("status", ""))
            notes = str(raw.get("notes", ""))
        else:
            status = str(raw)
            notes = ""
        checks[check_name] = {
            "status": _normalize_signoff_status(status),
            "notes": notes,
        }
    return checks


def _normalize_signoff_status(status: str) -> str:
    normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _PASS_STATUSES:
        return "pass"
    if normalized in _FAIL_STATUSES:
        return "fail"
    if normalized in _TODO_STATUSES:
        return "todo"
    return normalized or "todo"


def _apply_signoff(
    items: list[ThreeDReviewItem],
    signoff_rows: dict[str, dict[str, dict[str, str]]],
    errors: list[str],
    warnings: list[str],
) -> None:
    items_by_name = {item.footprint: item for item in items}
    for footprint in signoff_rows:
        if footprint not in items_by_name:
            warnings.append(f"Signoff file references unknown footprint {footprint}.")

    for item in items:
        checks = signoff_rows.get(item.footprint)
        if not checks:
            continue
        if item.is_placeholder or item.cadence_blocker or not item.cadence_canvas_ready:
            errors.append(
                f"Cannot sign off {item.footprint}: review status is {item.review_status}, not a real Canvas-ready STEP package."
            )
            continue

        expected_names = {check.name for check in item.manual_checks}
        for check_name in checks:
            if check_name not in expected_names:
                warnings.append(f"Signoff file references unknown check {item.footprint}.{check_name}.")

        for check in item.manual_checks:
            row = checks.get(check.name)
            if row is None:
                continue
            status = row.get("status", "todo")
            check.status = status
            notes = row.get("notes", "").strip()
            if notes:
                check.notes = notes
            if status == "fail":
                errors.append(f"Manual 3D Canvas check failed for {item.footprint}: {check.name}.")
            elif status not in {"pass", "todo"}:
                warnings.append(f"Manual 3D Canvas check {item.footprint}.{check.name} has unrecognized status {status}.")

        statuses = {check.status for check in item.manual_checks}
        if "fail" in statuses:
            item.review_status = CANVAS_SIGNOFF_FAILED
            item.cadence_blocker = "manual_check_failed"
            item.next_action = "Fix the failed Cadence 3D Canvas placement check, then rerun 3d-review with updated signoff."
        elif statuses == {"pass"}:
            item.review_status = CANVAS_SIGNED_OFF
            item.next_action = "Manual Cadence 3D Canvas checks are signed off; keep this report with the library handoff."
        else:
            item.review_status = CANVAS_SIGNOFF_INCOMPLETE
            item.next_action = "Complete every manual Cadence 3D Canvas checklist row before production mechanical signoff."


def _cadence_blocker(
    model_row: dict[str, Any],
    entry: StepModelEntry | None,
    step_file: str,
    model_exists: bool,
    facet_status: str,
) -> str:
    model_status = str(model_row.get("status", "")) if isinstance(model_row, dict) else ""
    if model_status == "missing" and not entry:
        return "missing_model"
    if model_status == "manual_review_non_step":
        return "manual_review_non_step"
    if not step_file:
        return "not_in_step_map"
    if entry is None:
        return "not_in_step_map"
    if not model_exists:
        return "missing_model"
    if not entry.dra_exists or not entry.psm_exists:
        return "not_built"
    if entry.mapping_status != "mapped":
        return "not_mapped"
    if facet_status == "missing":
        return "needs_facet"
    return ""


def _manual_checks(canvas_ready: bool, placeholder: bool, blocker: str) -> list[ThreeDManualCheck]:
    names = [
        ("visible_in_3d_canvas", "STEP body is visible in Cadence 3D Canvas."),
        ("centered_on_pads", "Body is centered over the pad field."),
        ("seated_on_board", "Body is seated on the board side without floating or sinking."),
        ("scale_unit_ok", "Scale and units look correct against package pitch/body size."),
        ("rotation_mirror_ok", "Rotation and mirror orientation match pin 1 and silkscreen intent."),
        ("offset_ok", "XY/Z offsets do not create obvious drift from the footprint."),
    ]
    if canvas_ready and not placeholder:
        status = "todo"
        suffix = ""
    elif canvas_ready and placeholder:
        status = "placeholder_only"
        suffix = " Workflow-only placeholder; replace before signoff."
    else:
        status = "blocked"
        suffix = f" Blocked by {blocker}."
    return [ThreeDManualCheck(name, status, note + suffix) for name, note in names]


def _next_action(review_status: str, blocker: str, canvas_ready: bool) -> str:
    if review_status == READY_FOR_CANVAS_REVIEW:
        return "Open the .dra in Cadence 3D Canvas and complete the manual placement checklist."
    if review_status == NONCOMPONENT_3D_EXEMPT:
        return "No package STEP/STP body is required for this PCB feature; keep the exception evidence with the 3D review report."
    if review_status == "placeholder_not_for_signoff":
        if canvas_ready:
            return "Use only for workflow review; replace the generated placeholder with a real STEP/STP before signoff."
        return "Replace the generated placeholder with a real STEP/STP, then rerun Cadence mapping."
    if blocker == "missing_model":
        return "Export, fetch, or alias a matching real STEP/STP model for this footprint."
    if blocker == "manual_review_non_step":
        return "Provide a STEP/STP model for Cadence package mapping; keep OBJ as a reference asset only."
    if blocker == "not_in_step_map":
        return "Regenerate conversion outputs or check 3d_model_map.json so this footprint enters cadence_step_package.map."
    if blocker == "not_built":
        return "Run convert with --run-cadence or otherwise build the .dra/.psm before 3D Canvas review."
    if blocker == "not_mapped":
        return "Run the generated Cadence STEP mapping helper or Allegro STEP Package Mapping, save the .dra, then rerun review-3d."
    if blocker == "needs_facet":
        return "Open/save the mapped package in Cadence so stepFacetFiles4Map data exists, then rerun review-3d."
    return "Inspect the generated 3D reports and rerun validation after correcting the blocker."


def _counts(items: list[ThreeDReviewItem], step_entry_count: int) -> dict[str, int]:
    required_items = [item for item in items if not item.is_noncomponent_3d_exempt]
    linked_required = sum(1 for item in required_items if item.step_file and not item.is_placeholder)
    counts = {
        "footprints": len(items),
        "step_entries": step_entry_count,
        "canvas_ready": sum(1 for item in items if item.cadence_canvas_ready),
        "real_step_candidates": sum(1 for item in items if item.step_file and not item.is_placeholder),
        "noncomponent_3d_exemptions": sum(1 for item in items if item.noncomponent_reason),
        "allowed_noncomponent_3d_exemptions": sum(1 for item in items if item.is_noncomponent_3d_exempt),
        "required_3d_footprints": len(required_items),
        "linked_required_3d_models": linked_required,
        "missing_required_3d_models": max(len(required_items) - linked_required, 0),
    }
    for item in items:
        counts[item.review_status] = counts.get(item.review_status, 0) + 1
        if item.cadence_blocker:
            key = f"blocked_{item.cadence_blocker}"
            counts[key] = counts.get(key, 0) + 1
    counts.setdefault(READY_FOR_CANVAS_REVIEW, 0)
    counts.setdefault(CANVAS_SIGNED_OFF, 0)
    counts.setdefault(CANVAS_SIGNED_OFF_WITH_EXCEPTIONS, 0)
    counts.setdefault(CANVAS_SIGNOFF_INCOMPLETE, 0)
    counts.setdefault(CANVAS_SIGNOFF_FAILED, 0)
    counts.setdefault(NONCOMPONENT_3D_EXEMPT, 0)
    counts.setdefault(READY_FOR_CANVAS_REVIEW_WITH_EXCEPTIONS, 0)
    counts.setdefault("placeholder_not_for_signoff", 0)
    return counts


def _overall_status(items: list[ThreeDReviewItem], errors: list[str]) -> str:
    if errors:
        return "failed"
    if not items:
        return "no_footprints"
    required_items = [item for item in items if not item.is_noncomponent_3d_exempt]
    exempt_count = len(items) - len(required_items)
    if not required_items:
        return NONCOMPONENT_3D_EXEMPT if exempt_count else "no_footprints"
    ready = sum(1 for item in required_items if item.review_status == READY_FOR_CANVAS_REVIEW)
    signed_off = sum(1 for item in required_items if item.review_status == CANVAS_SIGNED_OFF)
    incomplete_signoff = sum(1 for item in required_items if item.review_status == CANVAS_SIGNOFF_INCOMPLETE)
    failed_signoff = sum(1 for item in required_items if item.review_status == CANVAS_SIGNOFF_FAILED)
    canvas_ready = sum(1 for item in required_items if item.cadence_canvas_ready)
    placeholders = sum(1 for item in required_items if item.is_placeholder)
    required_count = len(required_items)
    if failed_signoff == required_count:
        return CANVAS_SIGNOFF_FAILED
    if signed_off == required_count:
        return CANVAS_SIGNED_OFF_WITH_EXCEPTIONS if exempt_count else CANVAS_SIGNED_OFF
    if incomplete_signoff and signed_off + incomplete_signoff == required_count:
        return CANVAS_SIGNOFF_INCOMPLETE if signed_off == 0 else "partial_canvas_signed_off"
    if signed_off:
        return "partial_canvas_signed_off"
    if incomplete_signoff:
        return CANVAS_SIGNOFF_INCOMPLETE
    if ready == required_count:
        return READY_FOR_CANVAS_REVIEW_WITH_EXCEPTIONS if exempt_count else READY_FOR_CANVAS_REVIEW
    if ready:
        return "partial_review_ready"
    if canvas_ready and placeholders == required_count:
        return "placeholder_review_only"
    if canvas_ready:
        return "partial_placeholder_review"
    return "blocked"


def _status_warnings(counts: dict[str, int]) -> list[str]:
    warnings = []
    for key, value in counts.items():
        if not value:
            continue
        if key == "placeholder_not_for_signoff":
            warnings.append(f"{value} generated placeholder STEP envelope model(s) are not acceptable for mechanical signoff.")
        elif key == CANVAS_SIGNOFF_INCOMPLETE:
            warnings.append(f"{value} footprint(s) have incomplete manual Cadence 3D Canvas signoff.")
        elif key == CANVAS_SIGNOFF_FAILED:
            warnings.append(f"{value} footprint(s) have failed manual Cadence 3D Canvas signoff checks.")
        elif key.startswith("blocked_"):
            warnings.append(f"{value} footprint(s) are blocked by {key.removeprefix('blocked_')}.")
    return warnings


def _footprint_rows(ir: dict[str, Any] | None, model_map: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for footprint in _as_list((ir or {}).get("footprints")):
        if not isinstance(footprint, dict):
            continue
        name = str(footprint.get("name", ""))
        if name:
            model = footprint.get("model3d")
            rows[name] = {
                "footprint": name,
                "model3d": model,
                "status": "step_map_ready" if model else "missing",
                "cadence_step_file": Path(str(model.get("file", ""))).name if isinstance(model, dict) else None,
            }
    for row in _as_list((model_map or {}).get("footprints")):
        if not isinstance(row, dict):
            continue
        name = str(row.get("footprint", ""))
        if name:
            rows[name] = {**rows.get(name, {}), **row}
    return rows


def _ordered_footprint_names(
    ir: dict[str, Any] | None,
    model_map: dict[str, Any] | None,
    entries: list[StepModelEntry],
) -> list[str]:
    names: list[str] = []
    for footprint in _as_list((ir or {}).get("footprints")):
        if isinstance(footprint, dict):
            _append_unique(names, str(footprint.get("name", "")))
    for row in _as_list((model_map or {}).get("footprints")):
        if isinstance(row, dict):
            _append_unique(names, str(row.get("footprint", "")))
    for entry in entries:
        _append_unique(names, entry.package)
    return names


def _append_unique(values: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in values:
        values.append(value)


def _step_file(model_row: dict[str, Any], entry: StepModelEntry | None, model: object) -> str:
    if entry and entry.step_file:
        return entry.step_file
    if isinstance(model_row, dict) and model_row.get("cadence_step_file"):
        return str(model_row["cadence_step_file"])
    if isinstance(model, dict) and _is_step_model(model):
        return Path(str(model.get("file", ""))).name
    return ""


def _model_file(model: object, step_file: str, out_dir: Path) -> str:
    if isinstance(model, dict) and model.get("file"):
        path = Path(str(model.get("file"))).expanduser()
        if not path.is_absolute():
            path = out_dir / "3d" / path.name
        return str(path)
    if step_file:
        return str(out_dir / "3d" / Path(step_file).name)
    return ""


def _path_exists(path_text: str) -> bool:
    if not path_text:
        return False
    return Path(path_text).expanduser().exists()


def _is_step_model(model: dict[str, Any]) -> bool:
    fmt = str(model.get("format", "")).lower().lstrip(".")
    suffix = Path(str(model.get("file", ""))).suffix.lower().lstrip(".")
    return fmt in {"step", "stp"} or suffix in {"step", "stp"}


def _read_json_dict(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, f"Missing optional 3D review input: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Unable to parse {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path} root must be a JSON object."
    return data, ""


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in [None, ""]:
        return []
    return [value]


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
