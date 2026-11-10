from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError, fromstring


STEP_SUFFIXES = {".step", ".stp"}


@dataclass
class StepModelEntry:
    package: str
    step_file: str
    model_type: str = ""
    model_exists: bool = False
    symbol_planned: bool = False
    dra_exists: bool = False
    psm_exists: bool = False
    mapping_status: str = "not_checked"
    mapping_properties: list[str] = field(default_factory=list)
    status: str = "unchecked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "step_file": self.step_file,
            "model_type": self.model_type,
            "model_exists": self.model_exists,
            "symbol_planned": self.symbol_planned,
            "dra_exists": self.dra_exists,
            "psm_exists": self.psm_exists,
            "mapping_status": self.mapping_status,
            "mapping_properties": list(self.mapping_properties),
            "status": self.status,
        }


@dataclass
class StepMappingValidationReport:
    status: str = "unchecked"
    map_file: str = ""
    model_dir: str = ""
    facet_status: str = "not_checked"
    facet_path: str = ""
    mapped_entry_count: int = 0
    unmapped_entry_count: int = 0
    entries: list[StepModelEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "map_file": self.map_file,
            "model_dir": self.model_dir,
            "facet_status": self.facet_status,
            "facet_path": self.facet_path,
            "mapped_entry_count": self.mapped_entry_count,
            "unmapped_entry_count": self.unmapped_entry_count,
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_step_mapping_output(
    out_dir: Path,
    require_mapped: bool = False,
    require_facet: bool = False,
) -> StepMappingValidationReport:
    out_dir = out_dir.expanduser().resolve()
    model_dir = out_dir / "3d"
    map_path = model_dir / "cadence_step_package.map"
    symbol_plan_path = out_dir / "allegro" / "symbols" / "symbol_build_plan.json"
    symbol_list_path = model_dir / "sym_3d_update_symbols.txt"
    model_map_path = model_dir / "3d_model_map.json"

    report = StepMappingValidationReport(map_file=str(map_path), model_dir=str(model_dir))
    _set_facet_status(out_dir, model_dir, report)
    if not map_path.exists():
        report.errors.append(f"Missing Cadence STEP mapping file: {map_path}")
        report.status = "failed"
        return report

    try:
        root = fromstring(map_path.read_text(encoding="utf-8"))
    except (OSError, ParseError) as exc:
        report.errors.append(f"Unable to parse Cadence STEP mapping file {map_path}: {exc}")
        report.status = "failed"
        return report

    if root.tag != "STEP-3D-MAPPING":
        report.errors.append(f"Unexpected STEP mapping root tag: {root.tag}")
    if root.attrib.get("revision") != "A":
        report.warnings.append("STEP mapping revision is not Cadence sample revision A.")

    header = root.find("Header")
    if header is None:
        report.errors.append("STEP mapping is missing Header.")
    elif header.attrib.get("lengthUnit") != "MM":
        report.warnings.append(f"STEP mapping lengthUnit is {header.attrib.get('lengthUnit')!r}, expected 'MM'.")

    body = root.find("Body")
    if body is None:
        report.errors.append("STEP mapping is missing Body.")
    else:
        for item in body.findall("MapItem"):
            _validate_map_item(item, model_dir, report)

    if not report.entries and not report.errors:
        report.status = "no_step_entries"
        report.warnings.append("No STEP/STP mapping entries were generated.")
    else:
        _cross_check_symbol_plan(symbol_plan_path, report)
        _cross_check_symbol_list(symbol_list_path, report)
        _cross_check_model_map(model_map_path, report)
        _set_entry_statuses(report)
        if report.facet_status == "missing" and not require_mapped:
            report.warnings.append(
                "Cadence 3D Canvas facet data is missing. STEP Package Mapping import may require "
                "stepFacetFiles4Map files or a Cadence-side mapping save."
            )
        if require_mapped:
            _require_symbol_mapping(report)
        if require_facet:
            _require_facet_data(report)
        report.status = "failed" if report.errors else (
            "mapped_symbols_ready" if require_mapped else "ready_for_cadence_import"
        )
    return report


def write_step_mapping_validation(out_dir: Path, path: Path) -> Path:
    report = validate_step_mapping_output(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _validate_map_item(item, model_dir: Path, report: StepMappingValidationReport) -> None:
    package = (item.attrib.get("package") or "").strip()
    if not package:
        report.errors.append("MapItem is missing package attribute.")
    step_models = item.findall("StepModel")
    if not step_models:
        report.errors.append(f"MapItem {package or '<missing>'} has no StepModel.")
        return

    primary_count = 0
    for model in step_models:
        model_type = (model.attrib.get("type") or "").strip()
        step_file = (model.attrib.get("file") or "").strip()
        if model_type.upper() == "PRIMARY":
            primary_count += 1
        entry = StepModelEntry(
            package=package,
            step_file=step_file,
            model_type=model_type,
            model_exists=False,
        )
        report.entries.append(entry)
        _validate_step_model(entry, model, model_dir, report)
    if primary_count != 1:
        report.errors.append(f"MapItem {package or '<missing>'} has {primary_count} PRIMARY StepModel entries.")


def _validate_step_model(entry: StepModelEntry, model, model_dir: Path, report: StepMappingValidationReport) -> None:
    prefix = f"MapItem {entry.package or '<missing>'}"
    if not entry.step_file:
        report.errors.append(f"{prefix} has a StepModel without file.")
        return
    if entry.model_type.upper() not in {"PRIMARY", "SECONDARY"}:
        report.errors.append(f"{prefix} StepModel has unsupported type {entry.model_type!r}.")
    if Path(entry.step_file).name != entry.step_file:
        report.warnings.append(f"{prefix} StepModel file should be a basename for Cadence search paths: {entry.step_file}")
    if Path(entry.step_file).suffix.lower() not in STEP_SUFFIXES:
        report.errors.append(f"{prefix} StepModel file is not STEP/STP: {entry.step_file}")

    model_path = model_dir / Path(entry.step_file).name
    entry.model_exists = model_path.exists()
    if not entry.model_exists:
        report.errors.append(f"{prefix} references missing STEP model file: {model_path}")

    for child_name in ["Offset", "Rotation"]:
        child = model.find(child_name)
        if child is None:
            report.errors.append(f"{prefix} StepModel is missing {child_name}.")
            continue
        for axis in ["x", "y", "z"]:
            value = child.attrib.get(axis)
            if value is None:
                report.errors.append(f"{prefix} {child_name} is missing {axis}.")
                continue
            try:
                float(value)
            except ValueError:
                report.errors.append(f"{prefix} {child_name}.{axis} is not numeric: {value!r}")


def _cross_check_symbol_plan(plan_path: Path, report: StepMappingValidationReport) -> None:
    if not plan_path.exists():
        report.warnings.append(f"Missing Allegro symbol build plan: {plan_path}")
        return
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"Unable to parse Allegro symbol build plan {plan_path}: {exc}")
        return

    symbol_dir = plan_path.parent
    by_footprint = {str(row.get("footprint", "")): row for row in plan.get("symbols", [])}
    for entry in report.entries:
        symbol = by_footprint.get(entry.package)
        if symbol is None:
            report.warnings.append(f"STEP mapping package {entry.package} is not present in symbol_build_plan.json.")
            continue
        entry.symbol_planned = True
        entry.dra_exists = (symbol_dir / str(symbol.get("dra", ""))).exists()
        entry.psm_exists = (symbol_dir / str(symbol.get("psm", ""))).exists()
        if entry.dra_exists:
            entry.mapping_status, entry.mapping_properties = _inspect_symbol_mapping(
                symbol_dir / str(symbol.get("dra", "")),
                expected_step_file=entry.step_file,
                package=entry.package,
            )


def _cross_check_symbol_list(symbol_list_path: Path, report: StepMappingValidationReport) -> None:
    expected = {f"{entry.package}.dra" for entry in report.entries if entry.package}
    if not symbol_list_path.exists():
        report.warnings.append(f"Missing sym_3d_update symbol list: {symbol_list_path}")
        return
    actual = {
        line.strip()
        for line in symbol_list_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        report.warnings.append(f"sym_3d_update list is missing symbols: {', '.join(missing)}.")
    if extra:
        report.warnings.append(f"sym_3d_update list has extra symbols: {', '.join(extra)}.")


def _cross_check_model_map(model_map_path: Path, report: StepMappingValidationReport) -> None:
    if not model_map_path.exists():
        report.warnings.append(f"Missing 3D model map JSON: {model_map_path}")
        return
    try:
        model_map = json.loads(model_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"Unable to parse 3D model map JSON {model_map_path}: {exc}")
        return
    expected_count = model_map.get("cadence", {}).get("importable_step_models")
    if expected_count is not None and int(expected_count) != len(report.entries):
        report.warnings.append(
            f"3d_model_map.json reports {expected_count} importable STEP models, "
            f"but cadence_step_package.map has {len(report.entries)} entries."
        )


def _set_entry_statuses(report: StepMappingValidationReport) -> None:
    report.mapped_entry_count = 0
    report.unmapped_entry_count = 0
    for entry in report.entries:
        if not entry.model_exists:
            entry.status = "missing_model"
        elif not entry.symbol_planned:
            entry.status = "model_ready_symbol_not_planned"
        elif entry.dra_exists and entry.psm_exists:
            entry.status = "model_ready_symbols_built"
        elif entry.dra_exists:
            entry.status = "model_ready_dra_built"
        else:
            entry.status = "model_ready_symbol_planned"
        if entry.mapping_status == "mapped":
            report.mapped_entry_count += 1
        elif entry.mapping_status == "not_mapped":
            report.unmapped_entry_count += 1


def _set_facet_status(out_dir: Path, model_dir: Path, report: StepMappingValidationReport) -> None:
    candidates = [
        out_dir / "stepFacetFiles4Map",
        model_dir / "stepFacetFiles4Map",
        out_dir / "allegro" / "stepFacetFiles4Map",
        out_dir / "allegro" / "symbols" / "stepFacetFiles4Map",
        out_dir / "allegro" / "symbols_3d" / "stepFacetFiles4Map",
        out_dir / "stepFacetFiles4Map.zip",
        model_dir / "stepFacetFiles4Map.zip",
        out_dir / "allegro" / "stepFacetFiles4Map.zip",
        out_dir / "allegro" / "symbols" / "stepFacetFiles4Map.zip",
        out_dir / "allegro" / "symbols_3d" / "stepFacetFiles4Map.zip",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.rglob("*")):
            report.facet_status = "present_directory"
            report.facet_path = str(candidate)
            return
        if candidate.is_file():
            report.facet_status = "present_archive"
            report.facet_path = str(candidate)
            return
    report.facet_status = "missing"
    report.facet_path = str(candidates[0])


def _require_symbol_mapping(report: StepMappingValidationReport) -> None:
    for entry in report.entries:
        if entry.mapping_status != "mapped":
            report.errors.append(
                f"STEP package {entry.package} is not mapped in its .dra; "
                "run Allegro STEP Package Mapping in Package mode and save the symbol."
            )


def _require_facet_data(report: StepMappingValidationReport) -> None:
    if report.facet_status == "missing":
        report.errors.append(
            "Cadence 3D Canvas facet data is missing; provide stepFacetFiles4Map files "
            "or save the mapping through Cadence before strict 3D acceptance."
        )


def _inspect_symbol_mapping(
    path: Path,
    expected_step_file: str = "",
    package: str = "",
) -> tuple[str, list[str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return "unknown", []
    if not raw:
        return "unknown", []

    texts = [
        raw.decode("ascii", errors="ignore"),
        raw.decode("utf-16le", errors="ignore"),
    ]
    markers = [
        "PKGDEF_STEP_FILE",
        "PKGDEF_STEP_TRANSFORMATION",
        "PKGDEF_ALT_STEP_FILE",
        "PKGDEF_ALT_STEP_TRANSFORMATION",
    ]
    found = [marker for marker in markers if any(marker in text for text in texts)]
    step_file = Path(expected_step_file).name
    if step_file and package:
        attachment_marker = f"STEP3D_{package}"
        attachment_marker_upper = attachment_marker.upper()
        step_file_lower = step_file.lower()
        if any(attachment_marker_upper in text.upper() and step_file_lower in text.lower() for text in texts):
            found.extend([attachment_marker_upper, f"STEP_FILE:{step_file}"])
    return ("mapped" if found else "not_mapped"), found
