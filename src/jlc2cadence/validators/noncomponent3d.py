from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NonComponent3DException:
    footprint: str
    reason: str
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "footprint": self.footprint,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def detect_noncomponent_3d_exceptions(ir: dict[str, Any] | None) -> list[NonComponent3DException]:
    """Classify missing-3D PCB features that do not have a component package body."""
    if ir is None:
        return []
    components_by_footprint = _components_by_footprint(ir)
    exceptions: list[NonComponent3DException] = []
    for footprint in _as_list(ir.get("footprints")):
        if not isinstance(footprint, dict) or footprint.get("model3d"):
            continue
        name = str(footprint.get("name", "")).strip()
        if not name:
            continue
        components = components_by_footprint.get(name, [])
        row = _classify_missing_footprint(footprint, components)
        if row is not None:
            exceptions.append(row)
    return exceptions


def _classify_missing_footprint(
    footprint: dict[str, Any],
    components: list[dict[str, Any]],
) -> NonComponent3DException | None:
    name = str(footprint.get("name", "")).strip()
    text = _search_text(footprint, components)
    compact = _compact(text)
    pad_types = _pad_types(footprint)
    ref_prefixes = {str(component.get("ref_prefix", "")).casefold() for component in components}

    if _looks_like_test_point(compact, text, pad_types, ref_prefixes):
        return NonComponent3DException(
            footprint=name,
            reason="test_point_no_package_body",
            evidence=_evidence(footprint, components, pad_types),
        )

    if _looks_like_mounting_feature(compact, text, ref_prefixes, footprint):
        return NonComponent3DException(
            footprint=name,
            reason="mounting_or_screw_hole_no_package_body",
            evidence=_evidence(footprint, components, pad_types),
        )
    return None


def _looks_like_test_point(
    compact: str,
    text: str,
    pad_types: set[str],
    ref_prefixes: set[str],
) -> bool:
    return (
        "test_point" in compact
        or "\u6d4b\u8bd5\u70b9" in text
        or ("tp" in ref_prefixes and "test" in pad_types)
    )


def _looks_like_mounting_feature(
    compact: str,
    text: str,
    ref_prefixes: set[str],
    footprint: dict[str, Any],
) -> bool:
    mounting_words = {
        "mounting_hole",
        "screw_hole",
        "\u87ba\u4e1d\u5b54",
    }
    has_mounting_word = any(word in compact or word in text for word in mounting_words)
    has_metric_screw_name = bool(re.search(r"(?:^|_)m\d+(?:_\d+)?(?:_|$)", compact))
    has_single_drilled_pad = _single_drilled_pad(footprint)
    return has_single_drilled_pad and (has_mounting_word or ("mh" in ref_prefixes and has_metric_screw_name))


def _components_by_footprint(ir: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_footprint: dict[str, list[dict[str, Any]]] = {}
    for component in _as_list(ir.get("components")):
        if not isinstance(component, dict):
            continue
        footprint = str(component.get("footprint", "")).strip()
        if footprint:
            by_footprint.setdefault(footprint, []).append(component)
    return by_footprint


def _search_text(footprint: dict[str, Any], components: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for key in ["name", "description"]:
        values.append(str(footprint.get(key, "")))
    for value in (footprint.get("properties") or {}).values():
        values.append(str(value))
    for component in components:
        for key in ["name", "ref_prefix", "value", "description"]:
            values.append(str(component.get(key, "")))
        for value in (component.get("properties") or {}).values():
            values.append(str(value))
    return " ".join(value for value in values if value).casefold()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _pad_types(footprint: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for pad in _as_list(footprint.get("pads")):
        if not isinstance(pad, dict):
            continue
        properties = pad.get("properties") or {}
        for key in ["EasyEDA_Pro_PadType", "pad_type", "type"]:
            value = str(properties.get(key, "") or pad.get(key, "")).strip().casefold()
            if value:
                values.add(value)
    return values


def _single_drilled_pad(footprint: dict[str, Any]) -> bool:
    pads = [pad for pad in _as_list(footprint.get("pads")) if isinstance(pad, dict)]
    if len(pads) != 1:
        return False
    pad = pads[0]
    if str(pad.get("kind", "")).casefold() != "thru_hole":
        return False
    try:
        return float(pad.get("hole_diameter") or 0) > 0
    except (TypeError, ValueError):
        return False


def _evidence(
    footprint: dict[str, Any],
    components: list[dict[str, Any]],
    pad_types: set[str],
) -> list[str]:
    evidence = []
    name = str(footprint.get("name", "")).strip()
    title = str((footprint.get("properties") or {}).get("EasyEDA_Pro_Title", "")).strip()
    if name:
        evidence.append(f"footprint={name}")
    if title and title != name:
        evidence.append(f"title={title}")
    refs = sorted({str(component.get("ref_prefix", "")).strip() for component in components if component.get("ref_prefix")})
    if refs:
        evidence.append("ref_prefix=" + ",".join(refs))
    component_names = sorted({str(component.get("name", "")).strip() for component in components if component.get("name")})
    if component_names:
        evidence.append("components=" + ",".join(component_names))
    pads = _as_list(footprint.get("pads"))
    if pads:
        evidence.append(f"pads={len(pads)}")
    if pad_types:
        evidence.append("pad_types=" + ",".join(sorted(pad_types)))
    return evidence


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in [None, ""]:
        return []
    return [value]
