from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .. import __version__


PAD_ROLES = ("electrical", "shield", "mechanical", "mounting", "npth")


def build_padstack_coverage_report(
    library: Any,
    *,
    source: dict[str, Any] | None = None,
    require_kinds: list[str] | tuple[str, ...] | None = None,
    require_roles: list[str] | tuple[str, ...] | None = None,
    require_shapes: list[str] | tuple[str, ...] | None = None,
    require_slot: bool = False,
    require_drill: bool = False,
    require_nonplated: bool = False,
) -> dict[str, Any]:
    data = _library_to_dict(library)
    footprints = [fp for fp in _as_list(data.get("footprints")) if isinstance(fp, dict)]
    errors: list[str] = []
    warnings: list[str] = []
    kind_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    padstack_rows: dict[str, dict[str, Any]] = {}
    footprint_rows: list[dict[str, Any]] = []
    pad_total = 0
    slot_pads = 0
    drilled_pads = 0
    plated_drill_pads = 0
    nonplated_drill_pads = 0

    for footprint in footprints:
        name = str(footprint.get("name") or "<unnamed>")
        pads = [pad for pad in _as_list(footprint.get("pads")) if isinstance(pad, dict)]
        fp_kind_counts: Counter[str] = Counter()
        fp_role_counts: Counter[str] = Counter()
        fp_shape_counts: Counter[str] = Counter()
        fp_padstacks: set[str] = set()
        fp_slot_pads = 0
        fp_drilled_pads = 0
        fp_nonplated_drills = 0
        pad_total += len(pads)

        for index, pad in enumerate(pads, start=1):
            number = str(pad.get("number") or "?")
            kind = _normal_category(pad.get("kind"), "unknown")
            role = _normal_category(pad.get("role"), "electrical")
            shape = _normal_category(pad.get("shape"), "unknown")
            size = _number_pair(pad.get("size"))
            hole = _number(pad.get("hole_diameter"))
            slot = _slot_size(pad.get("slot_size"))
            layers = _layers(pad.get("layers"))
            plated = _plating(pad)
            padstack = str(pad.get("padstack") or "").strip()
            padstack_key = padstack or f"<unnamed:{name}:{number}:{index}>"

            if not padstack:
                warnings.append(f"Pad {name}.{number} has no padstack name.")

            has_slot = slot is not None
            has_drill = hole > 0 or has_slot
            if has_slot:
                slot_pads += 1
                fp_slot_pads += 1
            if has_drill:
                drilled_pads += 1
                fp_drilled_pads += 1
                if plated == "false":
                    nonplated_drill_pads += 1
                    fp_nonplated_drills += 1
                else:
                    plated_drill_pads += 1

            kind_counts[kind] += 1
            role_counts[role] += 1
            shape_counts[shape] += 1
            fp_kind_counts[kind] += 1
            fp_role_counts[role] += 1
            fp_shape_counts[shape] += 1
            for layer in layers:
                layer_counts[layer] += 1

            signature = _pad_signature(kind, role, shape, size, hole, slot, plated)
            row = padstack_rows.get(padstack_key)
            if row is None:
                row = {
                    "padstack": padstack_key,
                    "source_name": padstack,
                    "kind": kind,
                    "role": role,
                    "shape": shape,
                    "size": list(size),
                    "hole_diameter": hole,
                    "slot_size": list(slot) if slot else None,
                    "plated": plated,
                    "layers": list(layers),
                    "pad_count": 0,
                    "footprints": [],
                    "pad_numbers": [],
                    "_signature": signature,
                }
                padstack_rows[padstack_key] = row
            elif row.get("_signature") != signature:
                errors.append(f"Padstack {padstack_key} has multiple geometries or plating definitions.")

            row["pad_count"] = int(row["pad_count"]) + 1
            for layer in layers:
                if layer not in row["layers"]:
                    row["layers"].append(layer)
            if name not in row["footprints"]:
                row["footprints"].append(name)
            pad_ref = f"{name}.{number}"
            if pad_ref not in row["pad_numbers"]:
                row["pad_numbers"].append(pad_ref)
            fp_padstacks.add(padstack_key)

        footprint_rows.append(
            {
                "name": name,
                "pad_count": len(pads),
                "padstack_count": len(fp_padstacks),
                "padstacks": sorted(fp_padstacks),
                "kind_counts": _sorted_counts(fp_kind_counts),
                "role_counts": _sorted_counts(fp_role_counts),
                "shape_counts": _sorted_counts(fp_shape_counts),
                "slot_pads": fp_slot_pads,
                "drilled_pads": fp_drilled_pads,
                "nonplated_drill_pads": fp_nonplated_drills,
            }
        )

    if not footprints:
        warnings.append("No footprints were found in the input library IR.")
    elif pad_total == 0:
        warnings.append("Footprints were found, but none contain pads.")

    requirements = {
        "kinds": [_normal_category(value, "") for value in require_kinds or [] if _normal_category(value, "")],
        "roles": [_normal_category(value, "") for value in require_roles or [] if _normal_category(value, "")],
        "shapes": [_normal_category(value, "") for value in require_shapes or [] if _normal_category(value, "")],
        "slot": bool(require_slot),
        "drill": bool(require_drill),
        "nonplated": bool(require_nonplated),
    }
    _apply_requirements(errors, requirements, kind_counts, role_counts, shape_counts, slot_pads, drilled_pads, nonplated_drill_pads)

    padstacks = []
    for row in padstack_rows.values():
        clean = dict(row)
        clean.pop("_signature", None)
        clean["footprints"] = sorted(clean["footprints"])
        clean["pad_numbers"] = clean["pad_numbers"][:50]
        padstacks.append(clean)
    padstacks.sort(key=lambda row: str(row["padstack"]))

    counts = {
        "footprints": len(footprints),
        "footprints_with_pads": sum(1 for row in footprint_rows if row["pad_count"] > 0),
        "pads": pad_total,
        "padstacks": len(padstacks),
        "slot_pads": slot_pads,
        "drilled_pads": drilled_pads,
        "plated_drill_pads": plated_drill_pads,
        "nonplated_drill_pads": nonplated_drill_pads,
        "electrical_pads": role_counts.get("electrical", 0),
        "shield_pads": role_counts.get("shield", 0),
        "mechanical_pads": role_counts.get("mechanical", 0),
        "mounting_pads": role_counts.get("mounting", 0),
        "npth_pads": role_counts.get("npth", 0),
    }
    coverage = {
        "kind_counts": _sorted_counts(kind_counts),
        "role_counts": _sorted_counts(role_counts),
        "shape_counts": _sorted_counts(shape_counts),
        "layer_counts": _sorted_counts(layer_counts),
        "has_smd": kind_counts.get("smd", 0) > 0,
        "has_thru_hole": kind_counts.get("thru_hole", 0) > 0,
        "has_slot": slot_pads > 0,
        "has_drill": drilled_pads > 0,
        "has_nonplated": nonplated_drill_pads > 0,
        "has_shield": role_counts.get("shield", 0) > 0,
        "has_mechanical": role_counts.get("mechanical", 0) > 0,
        "has_mounting": role_counts.get("mounting", 0) > 0,
        "has_npth": role_counts.get("npth", 0) > 0,
    }
    return {
        "version": f"v{__version__}",
        "status": "failed" if errors else ("warning" if warnings else "complete"),
        "source": source or {},
        "counts": counts,
        "coverage": coverage,
        "requirements": requirements,
        "padstacks": padstacks,
        "footprints": footprint_rows,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def write_padstack_coverage_report_json(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_padstack_coverage_report_markdown(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_padstack_coverage_markdown(report), encoding="utf-8")
    return path


def render_padstack_coverage_markdown(report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    coverage = report.get("coverage", {})
    requirements = report.get("requirements", {})
    lines = [
        "# jlc2cadence padstack coverage report",
        "",
        f"- Version: {report.get('version', '')}",
        f"- Status: {report.get('status', '')}",
    ]
    source = report.get("source") or {}
    if source:
        lines.append(f"- Source: {source.get('type', 'unknown')} {source.get('path', '')}")

    lines.extend(["", "## Counts", "", "| Metric | Count |", "|---|---:|"])
    for key in [
        "footprints",
        "footprints_with_pads",
        "pads",
        "padstacks",
        "slot_pads",
        "drilled_pads",
        "plated_drill_pads",
        "nonplated_drill_pads",
        "electrical_pads",
        "shield_pads",
        "mechanical_pads",
        "mounting_pads",
        "npth_pads",
    ]:
        lines.append(f"| {key} | {counts.get(key, 0)} |")

    lines.extend(["", "## Coverage", "", "| Category | Counts |", "|---|---|"])
    lines.append(f"| Kinds | {_counts_text(coverage.get('kind_counts'))} |")
    lines.append(f"| Roles | {_counts_text(coverage.get('role_counts'))} |")
    lines.append(f"| Shapes | {_counts_text(coverage.get('shape_counts'))} |")
    lines.append(f"| Layers | {_counts_text(coverage.get('layer_counts'))} |")

    if _has_requirements(requirements):
        lines.extend(["", "## Requirements", "", "| Requirement | Status |", "|---|---|"])
        lines.extend(_requirements_lines(report))

    lines.extend(
        [
            "",
            "## Padstacks",
            "",
            "| Padstack | Pads | Footprints | Role | Kind | Shape | Size mm | Drill/Slot mm | Plated | Layers |",
            "|---|---:|---:|---|---|---|---|---|---|---|",
        ]
    )
    for row in report.get("padstacks", []):
        size = "x".join(_fmt_number(value) for value in _as_list(row.get("size")))
        drill = _drill_text(row)
        lines.append(
            "| {padstack} | {pad_count} | {footprints} | {role} | {kind} | {shape} | {size} | {drill} | {plated} | {layers} |".format(
                padstack=_md(row.get("padstack")),
                pad_count=row.get("pad_count", 0),
                footprints=len(_as_list(row.get("footprints"))),
                role=_md(row.get("role")),
                kind=_md(row.get("kind")),
                shape=_md(row.get("shape")),
                size=_md(size),
                drill=_md(drill),
                plated=_md(row.get("plated")),
                layers=_md(",".join(str(layer) for layer in _as_list(row.get("layers")))),
            )
        )

    lines.extend(
        [
            "",
            "## Footprints",
            "",
            "| Footprint | Pads | Padstacks | Kinds | Roles | Shapes | Slots | Drills | Nonplated |",
            "|---|---:|---:|---|---|---|---:|---:|---:|",
        ]
    )
    for row in report.get("footprints", []):
        lines.append(
            "| {name} | {pads} | {padstacks} | {kinds} | {roles} | {shapes} | {slots} | {drills} | {nonplated} |".format(
                name=_md(row.get("name")),
                pads=row.get("pad_count", 0),
                padstacks=row.get("padstack_count", 0),
                kinds=_md(_counts_text(row.get("kind_counts"))),
                roles=_md(_counts_text(row.get("role_counts"))),
                shapes=_md(_counts_text(row.get("shape_counts"))),
                slots=row.get("slot_pads", 0),
                drills=row.get("drilled_pads", 0),
                nonplated=row.get("nonplated_drill_pads", 0),
            )
        )

    lines.extend(["", "## Issues", ""])
    errors = _as_list(report.get("errors"))
    warnings = _as_list(report.get("warnings"))
    if errors:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    if not errors and not warnings:
        lines.append("No padstack coverage issues were found.")
        lines.append("")
    return "\n".join(lines) + "\n"


def _apply_requirements(
    errors: list[str],
    requirements: dict[str, Any],
    kind_counts: Counter[str],
    role_counts: Counter[str],
    shape_counts: Counter[str],
    slot_pads: int,
    drilled_pads: int,
    nonplated_drill_pads: int,
) -> None:
    for kind in requirements.get("kinds", []):
        if kind_counts.get(kind, 0) <= 0:
            errors.append(f"Required pad kind is missing: {kind}")
    for role in requirements.get("roles", []):
        if role_counts.get(role, 0) <= 0:
            errors.append(f"Required pad role is missing: {role}")
    for shape in requirements.get("shapes", []):
        if shape_counts.get(shape, 0) <= 0:
            errors.append(f"Required pad shape is missing: {shape}")
    if requirements.get("slot") and slot_pads <= 0:
        errors.append("Required slot pad coverage is missing.")
    if requirements.get("drill") and drilled_pads <= 0:
        errors.append("Required drilled pad coverage is missing.")
    if requirements.get("nonplated") and nonplated_drill_pads <= 0:
        errors.append("Required nonplated drill/slot coverage is missing.")


def _requirements_lines(report: dict[str, Any]) -> list[str]:
    requirements = report.get("requirements", {})
    coverage = report.get("coverage", {})
    lines: list[str] = []
    for kind in requirements.get("kinds", []):
        lines.append(f"| kind={_md(kind)} | {_pass_fail((coverage.get('kind_counts') or {}).get(kind, 0) > 0)} |")
    for role in requirements.get("roles", []):
        lines.append(f"| role={_md(role)} | {_pass_fail((coverage.get('role_counts') or {}).get(role, 0) > 0)} |")
    for shape in requirements.get("shapes", []):
        lines.append(f"| shape={_md(shape)} | {_pass_fail((coverage.get('shape_counts') or {}).get(shape, 0) > 0)} |")
    for key, label in [("slot", "slot"), ("drill", "drill"), ("nonplated", "nonplated")]:
        if requirements.get(key):
            coverage_key = "has_nonplated" if key == "nonplated" else f"has_{key}"
            lines.append(f"| {label} | {_pass_fail(bool(coverage.get(coverage_key)))} |")
    return lines


def _has_requirements(requirements: dict[str, Any]) -> bool:
    return any(
        [
            requirements.get("kinds"),
            requirements.get("roles"),
            requirements.get("shapes"),
            requirements.get("slot"),
            requirements.get("drill"),
            requirements.get("nonplated"),
        ]
    )


def _pass_fail(ok: bool) -> str:
    return "pass" if ok else "failed"


def _library_to_dict(library: Any) -> dict[str, Any]:
    if isinstance(library, dict):
        return library
    to_dict = getattr(library, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return data if isinstance(data, dict) else {}
    return {}


def _pad_signature(
    kind: str,
    role: str,
    shape: str,
    size: tuple[float, float],
    hole: float,
    slot: tuple[float, float] | None,
    plated: str,
) -> tuple[Any, ...]:
    return (
        kind,
        role,
        shape,
        _signature_number(size[0]),
        _signature_number(size[1]),
        _signature_number(hole),
        tuple(_signature_number(value) for value in slot) if slot else None,
        plated,
    )


def _signature_number(value: float) -> float:
    return round(value, 3)


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}


def _counts_text(value: object) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={value[key]}" for key in sorted(value)) or "none"
    return "none"


def _drill_text(row: dict[str, Any]) -> str:
    slot = row.get("slot_size")
    if isinstance(slot, list) and len(slot) >= 2:
        return f"slot {_fmt_number(slot[0])}x{_fmt_number(slot[1])}"
    hole = _number(row.get("hole_diameter"))
    if hole > 0:
        return f"drill {_fmt_number(hole)}"
    return "none"


def _number_pair(value: object) -> tuple[float, float]:
    values = _as_list(value)
    if len(values) >= 2:
        return (_number(values[0]), _number(values[1]))
    if len(values) == 1:
        number = _number(values[0])
        return (number, number)
    return (0.0, 0.0)


def _slot_size(value: object) -> tuple[float, float] | None:
    if value in [None, ""]:
        return None
    pair = _number_pair(value)
    if pair[0] <= 0 or pair[1] <= 0:
        return None
    return pair


def _number(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _fmt_number(value: object) -> str:
    number = _number(value)
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _layers(value: object) -> list[str]:
    layers = [str(item).strip().lower() for item in _as_list(value) if str(item).strip()]
    return layers or ["unknown"]


def _plating(pad: dict[str, Any]) -> str:
    role = _normal_category(pad.get("role"), "electrical")
    if role == "npth":
        return "false"
    properties = pad.get("properties")
    raw = properties.get("plated") if isinstance(properties, dict) else None
    if raw is None:
        return "true"
    value = str(raw).strip().lower()
    if value in {"0", "false", "no", "non_plated", "non-plated"}:
        return "false"
    return "true"


def _normal_category(value: object, fallback: str) -> str:
    text = str(value if value not in [None, ""] else fallback).strip().lower().replace("-", "_")
    return text or fallback


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in [None, ""]:
        return []
    return [value]


def _md(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|")
