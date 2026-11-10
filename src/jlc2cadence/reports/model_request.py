from __future__ import annotations

import csv
import io
import json
import shutil
import zipfile
from string import ascii_uppercase
from xml.etree import ElementTree
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .. import __version__
from ..extractors.model_aliases import load_model_aliases
from ..ir.geometry import cadence_safe_name, normalized_key
from .repair_kit import (
    STEP_SUFFIXES,
    _alias_target_text,
    _is_relative_to,
    _known_placeholder_model_files,
    _load_repair_alias_data,
    _relative_alias_target,
    _set_alias_target,
    _unique_model_target,
    check_model_repair_kit,
    load_model_repair_kit,
)
from .three_d_review import NONCOMPONENT_3D_EXEMPT


def build_3d_model_request_report(
    repair_kit_path: Path,
    *,
    project_name: str = "",
    build_out: Path | None = None,
    model_drop_dir: Path | None = None,
    output_name: str = "",
    allow_noncomponent_3d_exceptions: bool = False,
) -> dict[str, Any]:
    """Build a supplier-facing request list from unresolved repair-kit rows."""
    kit = load_model_repair_kit(repair_kit_path)
    repair_status = check_model_repair_kit(repair_kit_path)
    errors: list[str] = []
    warnings: list[str] = []

    for error in repair_status.get("errors", []) or []:
        if "input export does not exist" in str(error):
            warnings.append(str(error))
        else:
            errors.append(str(error))
    warnings.extend(str(warning) for warning in repair_status.get("warnings", []) or [])

    alias_targets, alias_error = _load_alias_targets(kit.aliases_path)
    if alias_error:
        errors.append(alias_error)

    review_report, review_items, review_warning = _load_review(build_out)
    if review_warning:
        warnings.append(review_warning)
    if allow_noncomponent_3d_exceptions and build_out is None:
        warnings.append(
            "--allow-noncomponent-3d-exceptions needs --build-out to filter already classified PCB features."
        )

    drop_dir = (model_drop_dir or kit.models_dir).expanduser().resolve()
    next_out = _future_output_name(output_name, build_out)
    request_rows: list[dict[str, Any]] = []
    resolved_rows: list[dict[str, Any]] = []
    exception_rows: list[dict[str, Any]] = []

    for index, row in enumerate(kit.report.get("missing_footprints", []) or [], start=1):
        if not isinstance(row, dict):
            continue
        footprint = str(row.get("footprint", "") or "").strip()
        alias_key = str(row.get("alias_key", "") or footprint).strip()
        target = _target_for_alias(alias_key, alias_targets)
        if not target:
            target = str(row.get("alias_target_template", "") or "").strip()
        target_status = _target_status(target, kit.directory)
        review_item = review_items.get(footprint, {})

        if allow_noncomponent_3d_exceptions and _is_noncomponent_exempt(review_item):
            exception_rows.append(_exception_row(row, review_item, target))
            continue

        common = _request_row(index, row, target, target_status, review_item, drop_dir)
        if target_status == "resolved_step":
            resolved_rows.append(_resolved_row(common))
        else:
            common["priority"] = len(request_rows) + 1
            request_rows.append(common)

    status = "failed" if errors else ("blocked_missing_models" if request_rows else "complete")
    if status == "complete" and exception_rows:
        status = "complete_with_noncomponent_exceptions"

    report = {
        "version": f"v{__version__}",
        "status": status,
        "project_name": project_name or kit.directory.name,
        "repair_kit": str(kit.directory),
        "build_out": str(build_out.expanduser().resolve()) if build_out else "",
        "model_drop_dir": str(drop_dir),
        "future_output": str(next_out),
        "allow_noncomponent_3d_exceptions": allow_noncomponent_3d_exceptions,
        "exact_source_required": True,
        "counts": {
            "repair_kit_missing_footprints": len(kit.report.get("missing_footprints", []) or []),
            "requested_models": len(request_rows),
            "resolved_missing_report_rows": len(resolved_rows),
            "excluded_noncomponent_3d_exceptions": len(exception_rows),
            "repair_kit_placeholder_aliases": int(
                repair_status.get("counts", {}).get("placeholder_aliases", 0) or 0
            ),
            "repair_kit_missing_alias_targets": int(
                repair_status.get("counts", {}).get("missing_alias_targets", 0) or 0
            ),
            "review_required_3d_footprints": _review_count(review_report, "required_3d_footprints"),
            "review_linked_required_3d_models": _review_count(review_report, "linked_required_3d_models"),
            "review_missing_required_3d_models": _review_count(review_report, "missing_required_3d_models"),
        },
        "requests": request_rows,
        "resolved_rows": resolved_rows,
        "excluded_noncomponent_3d_exceptions": exception_rows,
        "source_policy": _source_policy(),
        "vendor_request_text": _vendor_request_text(request_rows),
        "rerun_commands": _rerun_commands(
            kit.directory,
            next_out,
            allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
        ),
        "repair_kit_status": {
            "status": repair_status.get("status", ""),
            "counts": repair_status.get("counts", {}),
            "errors": repair_status.get("errors", []),
            "warnings": repair_status.get("warnings", []),
        },
        "3d_review": _review_summary(review_report),
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }
    return report


def write_3d_model_request_report(
    report: dict[str, Any],
    *,
    json_path: Path | None = None,
    markdown_path: Path | None = None,
    csv_path: Path | None = None,
    xlsx_path: Path | None = None,
) -> dict[str, str]:
    outputs: dict[str, str] = {}
    if json_path is not None:
        json_path = json_path.expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outputs["json"] = str(json_path)
    if markdown_path is not None:
        markdown_path = markdown_path.expanduser().resolve()
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_3d_model_request_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(markdown_path)
    if csv_path is not None:
        csv_path = csv_path.expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(render_3d_model_request_csv(report), encoding="utf-8", newline="")
        outputs["csv"] = str(csv_path)
    if xlsx_path is not None:
        xlsx_path = xlsx_path.expanduser().resolve()
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        write_3d_model_request_xlsx(report, xlsx_path)
        outputs["xlsx"] = str(xlsx_path)
    return outputs


def render_3d_model_request_markdown(report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    lines = [
        "# 3D model request package",
        "",
        f"- Project: {report.get('project_name') or '-'}",
        f"- Converter version: {report.get('version') or f'v{__version__}'}",
        f"- Status: {report.get('status') or '-'}",
        f"- Repair kit: `{report.get('repair_kit') or '-'}`",
        f"- Model drop folder: `{report.get('model_drop_dir') or '-'}`",
        f"- Exact-source requirement: real vendor/JLC STEP/STP only",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Models requested | {counts.get('requested_models', 0)} |",
        f"| Resolved rows skipped | {counts.get('resolved_missing_report_rows', 0)} |",
        f"| Non-component exceptions skipped | {counts.get('excluded_noncomponent_3d_exceptions', 0)} |",
        f"| Required component 3D footprints in review | {_count_text(counts.get('review_required_3d_footprints'))} |",
        f"| Linked required 3D models in review | {_count_text(counts.get('review_linked_required_3d_models'))} |",
        f"| Missing required 3D models in review | {_count_text(counts.get('review_missing_required_3d_models'))} |",
        "",
        "## Requested Models",
        "",
    ]
    requests = report.get("requests", []) or []
    if requests:
        lines.extend(
            [
                "| Priority | Part candidates | Model/title | Footprint | Suggested filename | Reason |",
                "|---:|---|---|---|---|---|",
            ]
        )
        for row in requests:
            lines.append(
                "| {priority} | {parts} | {models} | {footprint} | {filename} | {reason} |".format(
                    priority=row.get("priority", ""),
                    parts=_md(_join(row.get("part_candidates", []) or [])),
                    models=_md(_join([*(row.get("candidate_model_ids", []) or []), *(row.get("model_titles", []) or [])])),
                    footprint=_md(row.get("footprint", "")),
                    filename=_md(row.get("suggested_filename", "")),
                    reason=_md(row.get("reason", "")),
                )
            )
    else:
        lines.append("No unresolved STEP/STP model requests remain in this repair kit.")

    exceptions = report.get("excluded_noncomponent_3d_exceptions", []) or []
    if exceptions:
        lines.extend(["", "## Excluded Non-Component Rows", ""])
        lines.extend(
            [
                "| Footprint | Reason |",
                "|---|---|",
            ]
        )
        for row in exceptions:
            lines.append(f"| {_md(row.get('footprint', ''))} | {_md(row.get('reason', ''))} |")

    lines.extend(
        [
            "",
            "## Source Requirements",
            "",
        ]
    )
    for item in report.get("source_policy", []) or []:
        lines.append(f"- {item}")

    vendor_text = str(report.get("vendor_request_text", "") or "").strip()
    if vendor_text:
        lines.extend(["", "## Supplier Request Text", "", vendor_text])

    lines.extend(["", "## Rerun Commands", ""])
    for command in report.get("rerun_commands", []) or []:
        lines.extend(["```powershell", command, "```", ""])

    errors = report.get("errors", []) or []
    warnings = report.get("warnings", []) or []
    if errors or warnings:
        lines.extend(["## Diagnostics", ""])
        lines.extend(f"- Error: {error}" for error in errors)
        lines.extend(f"- Warning: {warning}" for warning in warnings)

    return "\n".join(lines).rstrip() + "\n"


REQUEST_TABLE_FIELDS = [
    "priority",
    "footprint",
    "alias_key",
    "component_names",
    "part_candidates",
    "lcsc_codes",
    "candidate_model_ids",
    "model_titles",
    "suggested_filename",
    "drop_path",
    "reason",
    "target_status",
    "alias_target",
    "source_file",
    "description",
    "sourcing_queries",
    "sourcing_urls",
    "review_status",
    "cadence_blocker",
    "supplier_source_url",
    "supplier_contact",
    "supplier_notes",
    "received_filename",
]

REQUEST_LIST_FIELDS = {
    "component_names",
    "part_candidates",
    "lcsc_codes",
    "candidate_model_ids",
    "model_titles",
    "candidate_keys",
    "sourcing_queries",
    "sourcing_urls",
}


def receive_3d_model_request_models(
    repair_kit_path: Path,
    *,
    request_path: Path | None = None,
    model_roots: list[Path] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Accept exact supplier-returned STEP/STP files from a request table."""
    kit = load_model_repair_kit(repair_kit_path)
    before = check_model_repair_kit(repair_kit_path)
    warnings: list[str] = []
    errors: list[str] = []
    for error in before.get("errors", []) or []:
        if "input export does not exist" in str(error):
            warnings.append(str(error))
        else:
            errors.append(str(error))
    request_file = _select_request_file(kit.directory, request_path)
    request_rows: list[dict[str, Any]] = []
    request_format = ""
    if request_file is None:
        errors.append(
            "3D model request file was not found. Expected 3d_model_request.csv, "
            "3d_model_request.xlsx, or 3d_model_request.json."
        )
    else:
        try:
            request_rows, request_format = _load_request_rows(request_file)
        except Exception as exc:
            errors.append(f"Could not read 3D model request file {request_file}: {exc}")

    roots: list[Path] = []
    for root in model_roots or []:
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    if request_file is not None:
        request_parent = request_file.parent.resolve()
        if request_parent not in roots:
            roots.append(request_parent)
    if kit.models_dir.resolve() not in roots:
        roots.append(kit.models_dir.resolve())
    missing_files: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    ambiguous_files: list[dict[str, Any]] = []
    alias_row_missing: list[dict[str, Any]] = []
    placeholder_sources: list[dict[str, Any]] = []
    imported: list[dict[str, Any]] = []
    copied_models: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []

    if not errors:
        try:
            alias_data, alias_mapping = _load_repair_alias_data(kit.aliases_path)
        except Exception as exc:
            errors.append(f"Could not read repair kit aliases: {exc}")
            alias_data = {}
            alias_mapping = {}
    else:
        alias_data = {}
        alias_mapping = {}

    placeholder_files = {
        path.resolve()
        for path in _known_placeholder_model_files([*roots, kit.models_dir])
    }
    used_names = {
        path.name.casefold()
        for path in kit.models_dir.rglob("*")
        if path.is_file()
    } if kit.models_dir.exists() else set()
    existing_models = {
        path.name.casefold(): path.resolve()
        for path in kit.models_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in STEP_SUFFIXES
    } if kit.models_dir.exists() else {}
    planned_sources: dict[str, dict[str, Any]] = {}
    planned_aliases: set[str] = set()

    for row_number, raw_row in enumerate(request_rows, start=2):
        row = _normalize_request_row(raw_row)
        alias_key = str(row.get("alias_key", "") or "").strip()
        requested_filename = str(
            row.get("received_filename", "")
            or row.get("suggested_filename", "")
            or ""
        ).strip()
        base = {
            "row": row_number,
            "alias_key": alias_key,
            "footprint": str(row.get("footprint", "") or "").strip(),
            "received_filename": str(row.get("received_filename", "") or "").strip(),
            "suggested_filename": str(row.get("suggested_filename", "") or "").strip(),
            "requested_filename": requested_filename,
        }
        if not alias_key:
            invalid_rows.append({**base, "reason": "missing_alias_key"})
            continue
        if alias_key in planned_aliases:
            duplicate_rows.append({**base, "reason": "duplicate_alias_key"})
            continue
        if alias_key not in alias_mapping:
            alias_row_missing.append({**base, "reason": "alias_row_missing"})
            continue
        current_target = _alias_target_text(alias_mapping.get(alias_key))
        current_status = _target_status(current_target, kit.directory)
        if current_status == "resolved_step":
            preserved.append(
                {
                    **base,
                    "target": current_target,
                    "reason": "alias_already_resolved",
                }
            )
            continue
        if not requested_filename:
            invalid_rows.append({**base, "reason": "missing_received_or_suggested_filename"})
            continue
        requested_path = Path(requested_filename).expanduser()
        if requested_path.suffix.lower() not in STEP_SUFFIXES:
            invalid_rows.append(
                {
                    **base,
                    "reason": "received_model_must_be_step_or_stp",
                }
            )
            continue
        matches = _find_received_model_files(requested_path, roots)
        if not matches:
            missing_files.append(
                {
                    **base,
                    "reason": "received_model_file_not_found",
                    "searched_roots": [str(root) for root in roots],
                }
            )
            continue
        if len(matches) > 1:
            ambiguous_files.append(
                {
                    **base,
                    "reason": "multiple_exact_filename_matches",
                    "matches": [str(path) for path in matches],
                }
            )
            continue
        source = matches[0].resolve()
        if source in placeholder_files:
            placeholder_sources.append(
                {
                    **base,
                    "reason": "known_placeholder_model_source",
                    "source_file": str(source),
                }
            )
            continue

        source_key = str(source)
        import_info = planned_sources.get(source_key)
        if import_info is None:
            target = source
            copied = False
            reuse_existing = False
            if not _is_relative_to(source, kit.models_dir):
                existing = existing_models.get(source.name.casefold())
                if existing is not None and existing not in placeholder_files:
                    target = existing
                    reuse_existing = True
                else:
                    target = _unique_model_target(kit.models_dir, source.name, used_names)
                    copied = True
            import_info = {
                "source_file": str(source),
                "target_file": str(target),
                "relative_target": _relative_alias_target(target, kit.directory),
                "copied": copied,
                "reused_existing": reuse_existing,
            }
            planned_sources[source_key] = import_info
        if write and import_info["copied"]:
            target_path = Path(import_info["target_file"])
            try:
                kit.models_dir.mkdir(parents=True, exist_ok=True)
                if not target_path.exists():
                    shutil.copy2(source, target_path)
                if import_info not in copied_models:
                    copied_models.append(import_info)
            except OSError as exc:
                errors.append(f"Could not copy received STEP/STP file {source}: {exc}")
                continue
        new_target = str(import_info["relative_target"])
        _set_alias_target(alias_mapping, alias_key, new_target)
        planned_aliases.add(alias_key)
        imported.append(
            {
                **base,
                "old_target": current_target,
                "new_target": new_target,
                "source_file": str(source),
                "target_file": str(import_info["target_file"]),
                "copied": bool(import_info["copied"]),
                "reused_existing": bool(import_info["reused_existing"]),
                "supplier_source_url": str(row.get("supplier_source_url", "") or ""),
                "supplier_contact": str(row.get("supplier_contact", "") or ""),
                "supplier_notes": str(row.get("supplier_notes", "") or ""),
            }
        )

    if missing_files:
        warnings.append("Some requested supplier STEP/STP files were not found.")
    if invalid_rows:
        warnings.append("Some request rows were invalid and were not imported.")
    if ambiguous_files:
        warnings.append("Some request filenames matched more than one file and were not imported.")
    if alias_row_missing:
        warnings.append("Some request rows referenced aliases that are not present in model_aliases.json.")
    if placeholder_sources:
        warnings.append("Known generated placeholder STEP/STP sources were rejected.")
    if duplicate_rows:
        warnings.append("Duplicate alias rows were ignored after the first row.")
    if write and imported and not errors:
        kit.aliases_path.write_text(
            json.dumps(alias_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    after = check_model_repair_kit(repair_kit_path) if write else before
    status = "failed" if errors else str(after.get("status", "unknown"))
    return {
        "version": f"v{__version__}",
        "status": status,
        "repair_kit": str(kit.directory),
        "request": str(request_file) if request_file else "",
        "request_format": request_format,
        "model_roots": [str(root) for root in roots],
        "write": write,
        "aliases": str(kit.aliases_path),
        "models_dir": str(kit.models_dir),
        "before": {
            "status": before.get("status", "unknown"),
            "counts": before.get("counts", {}),
        },
        "after": {
            "status": after.get("status", "unknown"),
            "counts": after.get("counts", {}),
        },
        "counts": {
            "request_rows": len(request_rows),
            "imported_aliases": len(imported),
            "copied_models": len(copied_models),
            "reused_existing_models": sum(1 for item in imported if item.get("reused_existing")),
            "preserved_aliases": len(preserved),
            "missing_files": len(missing_files),
            "invalid_rows": len(invalid_rows),
            "ambiguous_files": len(ambiguous_files),
            "alias_row_missing": len(alias_row_missing),
            "placeholder_sources": len(placeholder_sources),
            "duplicate_rows": len(duplicate_rows),
        },
        "imported_aliases": imported,
        "copied_models": copied_models,
        "preserved_aliases": preserved,
        "missing_files": missing_files,
        "invalid_rows": invalid_rows,
        "ambiguous_files": ambiguous_files,
        "alias_row_missing": alias_row_missing,
        "placeholder_sources": placeholder_sources,
        "duplicate_rows": duplicate_rows,
        "errors": errors,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _select_request_file(kit_dir: Path, request_path: Path | None) -> Path | None:
    if request_path is not None:
        path = request_path.expanduser().resolve()
        return path if path.exists() else path
    candidates = [
        kit_dir / "3d_model_request.csv",
        kit_dir / "3d_model_request.xlsx",
        kit_dir / "3d_model_request.json",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    return max(
        existing,
        key=lambda path: (
            path.stat().st_mtime_ns,
            {".xlsx": 3, ".csv": 2, ".json": 1}.get(path.suffix.lower(), 0),
        ),
    )


def _load_request_rows(path: Path) -> tuple[list[dict[str, Any]], str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        raw_rows = data.get("requests", []) if isinstance(data, dict) else data
        if not isinstance(raw_rows, list):
            raise ValueError("JSON request package must contain a 'requests' array.")
        return [row for row in raw_rows if isinstance(row, dict)], "json"
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)], "csv"
    if suffix == ".xlsx":
        return _load_xlsx_request_rows(path), "xlsx"
    raise ValueError("Supported request formats are .json, .csv, and .xlsx.")


def _load_xlsx_request_rows(path: Path) -> list[dict[str, Any]]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        worksheet_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        if not worksheet_names:
            raise ValueError("XLSX workbook contains no worksheet.")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.itertext())
                for node in shared_root.findall("main:si", namespace)
            ]
        root = ElementTree.fromstring(archive.read(worksheet_names[0]))
    rows: list[list[str]] = []
    for row_element in root.findall(".//main:row", namespace):
        values: dict[int, str] = {}
        for cell in row_element.findall("main:c", namespace):
            reference = str(cell.get("r", "") or "")
            column = _xlsx_column_index(reference)
            if not column:
                continue
            cell_type = str(cell.get("t", "") or "")
            if cell_type == "inlineStr":
                value = "".join(cell.find("main:is", namespace).itertext()) if cell.find("main:is", namespace) is not None else ""
            else:
                value_element = cell.find("main:v", namespace)
                value = value_element.text if value_element is not None and value_element.text else ""
                if cell_type == "s" and value:
                    index = int(value)
                    value = shared_strings[index] if 0 <= index < len(shared_strings) else ""
            values[column] = str(value or "")
        if values:
            width = max(values)
            rows.append([values.get(index, "") for index in range(1, width + 1)])
    if not rows:
        return []
    headers = [str(value).strip() for value in rows[0]]
    return [
        {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers)) if headers[index]}
        for row in rows[1:]
    ]


def _xlsx_column_index(reference: str) -> int:
    letters = "".join(character for character in str(reference) if character.isalpha())
    if not letters:
        return 0
    value = 0
    for character in letters.upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _normalize_request_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in REQUEST_LIST_FIELDS:
        value = result.get(field, [])
        if isinstance(value, str):
            result[field] = [part.strip() for part in value.split(";") if part.strip()]
        elif isinstance(value, list):
            result[field] = [str(part).strip() for part in value if str(part).strip()]
        else:
            result[field] = []
    return result


def _find_received_model_files(filename: Path, roots: list[Path]) -> list[Path]:
    if filename.is_absolute():
        resolved = filename.expanduser().resolve()
        if resolved.is_file() and resolved.suffix.lower() in STEP_SUFFIXES:
            return [resolved]
    matches: dict[str, Path] = {}
    relative_text = filename.as_posix().replace("\\", "/").casefold()
    for root in roots:
        root = root.expanduser().resolve()
        candidates = [root] if root.is_file() else (
            list(root.rglob("*")) if root.is_dir() else []
        )
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in STEP_SUFFIXES:
                continue
            candidate = candidate.resolve()
            if filename.is_absolute():
                if candidate == filename.resolve():
                    matches[str(candidate).casefold()] = candidate
                continue
            if root.is_file():
                relative = candidate.name.casefold()
            else:
                try:
                    relative = candidate.relative_to(root).as_posix().casefold()
                except ValueError:
                    continue
            if (
                relative == relative_text
                or (filename.parent == Path(".") and candidate.name.casefold() == filename.name.casefold())
            ):
                matches[str(candidate).casefold()] = candidate
    return sorted(matches.values(), key=lambda item: str(item).casefold())


def render_3d_model_request_csv(report: dict[str, Any]) -> str:
    """Render a flat Excel-compatible procurement table for unresolved models."""
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=REQUEST_TABLE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in report.get("requests", []) or []:
        writer.writerow(_request_table_row(row))
    return handle.getvalue()


def write_3d_model_request_xlsx(report: dict[str, Any], path: Path) -> Path:
    """Write a dependency-free XLSX workbook containing the procurement table."""
    rows = [REQUEST_TABLE_FIELDS]
    rows.extend(
        [_request_table_row(row)[field] for field in REQUEST_TABLE_FIELDS]
        for row in report.get("requests", []) or []
    )
    worksheet = _xlsx_worksheet(rows)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="3D Model Requests" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>
"""
    path = path.expanduser().resolve()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return path


def _request_table_row(row: dict[str, Any]) -> dict[str, str]:
    values = {
        "priority": row.get("priority", ""),
        "footprint": row.get("footprint", ""),
        "alias_key": row.get("alias_key", ""),
        "component_names": _join(row.get("component_names", []) or []),
        "part_candidates": _join(row.get("part_candidates", []) or []),
        "lcsc_codes": _join(row.get("lcsc_codes", []) or []),
        "candidate_model_ids": _join(row.get("candidate_model_ids", []) or []),
        "model_titles": _join(row.get("model_titles", []) or []),
        "suggested_filename": row.get("suggested_filename", ""),
        "drop_path": row.get("drop_path", ""),
        "reason": row.get("reason", ""),
        "target_status": row.get("target_status", ""),
        "alias_target": row.get("alias_target", ""),
        "source_file": row.get("source_file", ""),
        "description": row.get("description", ""),
        "sourcing_queries": _join(row.get("sourcing_queries", []) or []),
        "sourcing_urls": _join(row.get("sourcing_urls", []) or []),
        "review_status": row.get("review_status", ""),
        "cadence_blocker": row.get("cadence_blocker", ""),
        "supplier_source_url": row.get("supplier_source_url", ""),
        "supplier_contact": row.get("supplier_contact", ""),
        "supplier_notes": row.get("supplier_notes", ""),
        "received_filename": row.get("received_filename", ""),
    }
    return {field: str(values.get(field, "") or "") for field in REQUEST_TABLE_FIELDS}


def _xlsx_worksheet(rows: list[list[Any]]) -> str:
    row_xml: list[str] = []
    max_column = len(rows[0]) if rows else 1
    max_row = max(len(rows), 1)
    dimension = f"A1:{_xlsx_column(max_column)}{max_row}"
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_number, value in enumerate(row, start=1):
            reference = f"{_xlsx_column(column_number)}{row_number}"
            text = escape(str(value or ""))
            style = ' s="1"' if row_number == 1 else ""
            cells.append(
                f'<c r="{reference}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<dimension ref=\"{dimension}\"/>"
        "<sheetViews><sheetView workbookViewId=\"0\"><pane ySplit=\"1\" topLeftCell=\"A2\" activePane=\"bottomLeft\" state=\"frozen\"/></sheetView></sheetViews>"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        f"<autoFilter ref=\"{dimension}\"/>"
        "</worksheet>"
    )


def _xlsx_column(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = ascii_uppercase[remainder] + result
    return result or "A"


def _load_alias_targets(path: Path) -> tuple[dict[str, str], str]:
    if not path.exists():
        return {}, f"Repair-kit alias file is missing: {path}"
    try:
        aliases = load_model_aliases(path)
    except Exception as exc:
        return {}, f"Could not read repair-kit aliases: {exc}"
    targets: dict[str, str] = {}
    for row in aliases.rows:
        candidate = str(row.get("candidate", "") or "").strip()
        target = str(row.get("target", "") or "").strip()
        if candidate and target:
            targets[candidate] = target
            targets[normalized_key(candidate)] = target
    return targets, ""


def _load_review(build_out: Path | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    if build_out is None:
        return {}, {}, ""
    path = build_out.expanduser().resolve() / "reports" / "3d_review.json"
    if not path.exists():
        return {}, {}, f"3D review report was not found: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, {}, f"Could not read 3D review report {path}: {exc}"
    if not isinstance(data, dict):
        return {}, {}, f"3D review report is not a JSON object: {path}"
    items = {
        str(item.get("footprint", "") or ""): item
        for item in data.get("items", []) or []
        if isinstance(item, dict) and str(item.get("footprint", "") or "")
    }
    return data, items, ""


def _target_for_alias(alias_key: str, alias_targets: dict[str, str]) -> str:
    if not alias_key:
        return ""
    return alias_targets.get(alias_key, alias_targets.get(normalized_key(alias_key), ""))


def _target_status(target: str, base_dir: Path) -> str:
    if not target:
        return "missing_alias_target"
    target_path = Path(target)
    if target_path.suffix.lower() not in STEP_SUFFIXES:
        return "non_step_target" if target_path.suffix else "non_file_target"
    if "REPLACE_WITH_" in target_path.name.upper():
        return "placeholder_alias"
    if not target_path.is_absolute():
        target_path = base_dir / target_path
    if not target_path.expanduser().resolve().exists():
        return "missing_alias_target"
    return "resolved_step"


def _request_row(
    priority: int,
    row: dict[str, Any],
    target: str,
    target_status: str,
    review_item: dict[str, Any],
    drop_dir: Path,
) -> dict[str, Any]:
    footprint = str(row.get("footprint", "") or "").strip()
    suggested = _suggested_filename(row, target)
    part_candidates = _unique(row.get("component_part_candidates", []) or [])
    return {
        "priority": priority,
        "footprint": footprint,
        "alias_key": str(row.get("alias_key", "") or footprint),
        "alias_target": target,
        "target_status": target_status,
        "reason": _reason_text(target_status, review_item),
        "suggested_filename": suggested,
        "drop_path": str(drop_dir / suggested),
        "component_names": _unique(row.get("component_names", []) or []),
        "part_candidates": part_candidates,
        "lcsc_codes": [value for value in part_candidates if _looks_like_lcsc(value)],
        "candidate_model_ids": _unique(row.get("candidate_model_ids", []) or []),
        "model_titles": _unique(row.get("model_titles", []) or []),
        "candidate_keys": _unique(row.get("candidate_keys", []) or []),
        "source_file": Path(str(row.get("source_file", "") or "")).name,
        "description": str(row.get("description", "") or ""),
        "sourcing_queries": _unique(row.get("sourcing_queries", []) or []),
        "sourcing_urls": _unique(row.get("sourcing_urls", []) or []),
        "review_status": str(review_item.get("review_status", "") or ""),
        "cadence_blocker": str(review_item.get("cadence_blocker", "") or ""),
    }


def _exception_row(row: dict[str, Any], review_item: dict[str, Any], target: str) -> dict[str, Any]:
    return {
        "footprint": str(row.get("footprint", "") or ""),
        "alias_key": str(row.get("alias_key", "") or ""),
        "alias_target": target,
        "reason": str(review_item.get("noncomponent_reason", "") or "non-component PCB feature"),
        "review_status": str(review_item.get("review_status", "") or NONCOMPONENT_3D_EXEMPT),
    }


def _resolved_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "footprint": row.get("footprint", ""),
        "alias_key": row.get("alias_key", ""),
        "alias_target": row.get("alias_target", ""),
        "suggested_filename": row.get("suggested_filename", ""),
        "review_status": row.get("review_status", ""),
    }


def _suggested_filename(row: dict[str, Any], target: str) -> str:
    target_name = Path(str(target or "")).name
    if target_name:
        if target_name.upper().startswith("REPLACE_WITH_"):
            target_name = target_name[len("REPLACE_WITH_") :]
        if Path(target_name).suffix.lower() in STEP_SUFFIXES:
            return target_name
    footprint = cadence_safe_name(str(row.get("footprint", "") or "MODEL"))
    return f"{footprint}.step"


def _reason_text(target_status: str, review_item: dict[str, Any]) -> str:
    blocker = str(review_item.get("cadence_blocker", "") or "")
    if blocker == "missing_model":
        return "required component has no linked real STEP/STP model"
    if target_status == "placeholder_alias":
        return "repair-kit alias still points at a REPLACE_WITH placeholder"
    if target_status == "missing_alias_target":
        return "repair-kit alias target file is missing"
    if target_status == "non_step_target":
        return "Cadence package mapping requires STEP/STP, not the current target format"
    if target_status == "non_file_target":
        return "repair-kit alias target is a model key instead of a STEP/STP file"
    return target_status


def _is_noncomponent_exempt(review_item: dict[str, Any]) -> bool:
    return bool(review_item.get("is_noncomponent_3d_exempt")) or str(
        review_item.get("review_status", "")
    ) == NONCOMPONENT_3D_EXEMPT


def _future_output_name(output_name: str, build_out: Path | None) -> Path:
    if output_name:
        return Path(output_name)
    if build_out is not None:
        build_out = build_out.expanduser().resolve()
        return build_out.with_name(f"{build_out.name}_full3d")
    return Path("out_full3d_cadence")


def _review_count(report: dict[str, Any], key: str) -> int | None:
    if not report:
        return None
    return int((report.get("counts", {}) or {}).get(key, 0) or 0)


def _review_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    counts = report.get("counts", {}) or {}
    return {
        "status": report.get("status", ""),
        "counts": {
            "required_3d_footprints": counts.get("required_3d_footprints", 0),
            "linked_required_3d_models": counts.get("linked_required_3d_models", 0),
            "missing_required_3d_models": counts.get("missing_required_3d_models", 0),
            "allowed_noncomponent_3d_exemptions": counts.get("allowed_noncomponent_3d_exemptions", 0),
        },
    }


def _source_policy() -> list[str]:
    return [
        "Use real vendor/JLC STEP/STP files for production 3D signoff.",
        "Do not use generated placeholder/envelope STEP files as production models.",
        "Do not substitute adjacent pin counts, nearby package lookalikes, or manually edited neighbor models.",
        "OBJ/WRL files can be reference material, but final Cadence package mapping must use STEP/STP.",
        "Keep provenance for each supplied model: source URL, vendor contact, datasheet, or exported JLC/EasyEDA evidence.",
    ]


def _vendor_request_text(requests: list[dict[str, Any]]) -> str:
    if not requests:
        return ""
    lines = [
        "Please provide exact mechanical 3D models for the following parts. STEP/STP is preferred.",
        "Each model must match the listed LCSC/manufacturer part and footprint; adjacent variants or generated approximations are not acceptable.",
        "",
    ]
    for row in requests:
        parts = _join(row.get("part_candidates", []) or [])
        titles = _join([*(row.get("candidate_model_ids", []) or []), *(row.get("model_titles", []) or [])])
        lines.append(
            f"- {row.get('footprint')}: {parts or titles or 'part details in attached repair kit'}; requested filename {row.get('suggested_filename')}."
        )
    return "\n".join(lines)


def _rerun_commands(
    repair_kit: Path,
    output: Path,
    *,
    allow_noncomponent_3d_exceptions: bool,
) -> list[str]:
    exception_flag = " --allow-noncomponent-3d-exceptions" if allow_noncomponent_3d_exceptions else ""
    kit = _ps_path(repair_kit)
    out = _ps_path(output)
    return [
        f"jlc2cadence sync-repair-kit --repair-kit {kit} --strict",
        (
            f"jlc2cadence repair-kit-build --repair-kit {kit} --out {out} "
            f"--run-cadence --require-cadence --require-3d{exception_flag} --strict"
        ),
        f"jlc2cadence verify-output --out {out} --require-cadence --require-3d{exception_flag} --strict --write",
        f"jlc2cadence validate-3d --out {out} --require-mapped --require-facet --strict --write",
        f"jlc2cadence update-3d --out {out} --strict",
        f"jlc2cadence 3d-review --out {out}{exception_flag} --strict --write",
    ]


def _ps_path(path: Path) -> str:
    text = str(path)
    if not text:
        return '""'
    escaped = text.replace('"', '`"')
    return f'"{escaped}"'


def _looks_like_lcsc(value: str) -> bool:
    text = str(value or "").strip().upper()
    return len(text) > 1 and text[0] == "C" and text[1:].isdigit()


def _unique(values: list[Any]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _join(values: list[Any]) -> str:
    return "; ".join(_unique(values))


def _count_text(value: object) -> str:
    return "-" if value is None else str(value)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
