from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, Graphic, LibraryIR, Pad, Pin
from ..ir.units import parse_length


COMPONENT_RECORDS = {"COMPONENT", "LIBCOMPONENT", "PART", "SCHCOMPONENT"}
PIN_RECORDS = {"PIN", "SCHPIN"}
PAD_RECORDS = {"PAD", "PCB_PAD"}
LINE_RECORDS = {"TRACK", "LINE", "POLYLINE"}
ARC_RECORDS = {"ARC"}
TEXT_RECORDS = {"TEXT", "STRING", "DESIGNATOR", "COMMENT"}
REGION_RECORDS = {"REGION", "POLYGON", "FILL"}


def parse_record_line(line: str) -> dict[str, str] | None:
    line = line.strip().lstrip("\ufeff")
    if not line or "=" not in line:
        return None
    if "|" not in line and not line.upper().startswith("RECORD="):
        return None
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    record: dict[str, str] = {}
    for raw_part in line.split("|"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        record[key] = value
    return record if record else None


def parse_altium_design_files(paths: Iterable[Path], default_unit: str = "mm") -> LibraryIR:
    parser = AltiumAsciiParser(default_unit=default_unit)
    for path in paths:
        parser.parse_file(path)
    return parser.library


class AltiumAsciiParser:
    def __init__(self, default_unit: str = "mm") -> None:
        self.default_unit = default_unit
        self.library = LibraryIR()
        self._current_component: Component | None = None
        self._current_footprint: Footprint | None = None

    def parse_file(self, path: Path) -> None:
        records = list(_iter_records(path))
        if not records:
            return

        suffix = path.suffix.lower()
        self.library.source_files.append(str(path))
        if suffix in {".pcbdoc", ".pcblib"}:
            self._parse_pcb_records(records, path)
        elif suffix in {".schdoc", ".schlib", ".libpkg"}:
            self._parse_schematic_records(records, path)
        else:
            self._parse_mixed_records(records, path)

    def _parse_mixed_records(self, records: list[dict[str, str]], path: Path) -> None:
        pcb_score = sum(1 for rec in records if _record_type(rec) in PAD_RECORDS)
        pin_score = sum(1 for rec in records if _record_type(rec) in PIN_RECORDS)
        if pcb_score > pin_score:
            self._parse_pcb_records(records, path)
        else:
            self._parse_schematic_records(records, path)

    def _parse_schematic_records(self, records: list[dict[str, str]], path: Path) -> None:
        self._current_component = None
        for rec in records:
            record_type = _record_type(rec)
            if record_type in COMPONENT_RECORDS:
                self._current_component = self._component_from_record(rec, path)
                self.library.upsert_component(self._current_component)
            elif record_type in PIN_RECORDS or _has_pin_fields(rec):
                pin = self._pin_from_record(rec)
                component = self._current_component
                if component is None:
                    component = Component(name=cadence_safe_name(path.stem), ref_prefix="U")
                    self._current_component = component
                    self.library.upsert_component(component)
                component.add_pin(pin)
            elif record_type in TEXT_RECORDS and self._current_component is not None:
                _apply_component_text(self._current_component, rec)

    def _parse_pcb_records(self, records: list[dict[str, str]], path: Path) -> None:
        self._current_footprint = None
        for rec in records:
            record_type = _record_type(rec)
            if record_type in COMPONENT_RECORDS:
                self._current_footprint = self._footprint_from_record(rec, path)
                self.library.upsert_footprint(self._current_footprint)
            elif record_type in PAD_RECORDS or _has_pad_fields(rec):
                pad = self._pad_from_record(rec)
                footprint = self._current_footprint
                if footprint is None:
                    footprint = Footprint(name=cadence_safe_name(path.stem))
                    self._current_footprint = footprint
                    self.library.upsert_footprint(footprint)
                footprint.add_pad(pad)
            elif record_type in LINE_RECORDS:
                self._add_graphic("line", rec)
            elif record_type in ARC_RECORDS:
                self._add_graphic("arc", rec)
            elif record_type in TEXT_RECORDS:
                self._add_graphic("text", rec)
            elif record_type in REGION_RECORDS:
                self._add_graphic("region", rec)

    def _component_from_record(self, rec: dict[str, str], path: Path) -> Component:
        name = _first(rec, "DESIGNITEMID", "LIBREFERENCE", "COMPONENTNAME", "NAME", "DESIGNATOR") or path.stem
        ref = _first(rec, "DESIGNATOR", "PREFIX", "REFDES", "PARTID") or "U?"
        ref_prefix = "".join(ch for ch in ref if ch.isalpha()) or "U"
        footprint = _first(rec, "FOOTPRINT", "PCBFOOTPRINT", "PATTERN", "PCB_PATTERN", "MODEL") or ""
        value = _first(rec, "COMMENT", "VALUE", "PARAM_VALUE", "DESCRIPTION") or ""
        description = _first(rec, "DESCRIPTION", "LIBDESCRIPTION") or ""
        component = Component(
            name=cadence_safe_name(name),
            ref_prefix=ref_prefix,
            value=value,
            footprint=cadence_safe_name(footprint) if footprint else "",
            description=description,
            source_file=str(path),
        )
        for key, value in rec.items():
            if key not in {"RECORD", "DESIGNITEMID", "LIBREFERENCE", "COMPONENTNAME", "NAME"}:
                component.properties[_display_key(key)] = value
        return component

    def _pin_from_record(self, rec: dict[str, str]) -> Pin:
        number = _first(rec, "PINNUMBER", "PIN_NUMBER", "DESIGNATOR", "NAME", "NUMBER") or "?"
        name = _first(rec, "PINNAME", "PIN_NAME", "NAME", "DESCRIPTION") or number
        raw_pin_type = _first(rec, "PINTYPE", "PIN_TYPE", "ELECTRICAL", "TYPE") or "Passive"
        pin_type = _normalize_pin_type(raw_pin_type)
        x = parse_length(_first(rec, "X", "LOCATION.X", "PINX"), self.default_unit)
        y = parse_length(_first(rec, "Y", "LOCATION.Y", "PINY"), self.default_unit)
        length = parse_length(_first(rec, "LENGTH", "PINLENGTH"), self.default_unit)
        side = _side_from_orientation(_first(rec, "ORIENTATION", "ROTATION", "SIDE"))
        unit = _first(rec, "PART", "OWNERPARTID", "UNIT") or "A"
        hidden = _pin_hidden_from_record(rec, raw_pin_type)
        return Pin(number=number, name=name, type=pin_type, unit=str(unit), hidden=hidden, x=x, y=y, side=side, length=length)

    def _footprint_from_record(self, rec: dict[str, str], path: Path) -> Footprint:
        name = _first(rec, "PATTERN", "FOOTPRINT", "PCBFOOTPRINT", "DESIGNITEMID", "LIBREFERENCE", "NAME") or path.stem
        description = _first(rec, "DESCRIPTION", "COMMENT") or ""
        footprint = Footprint(name=cadence_safe_name(name), description=description, source_file=str(path))
        for key, value in rec.items():
            if key not in {"RECORD", "PATTERN", "FOOTPRINT", "PCBFOOTPRINT", "DESIGNITEMID", "LIBREFERENCE", "NAME"}:
                footprint.properties[_display_key(key)] = value
        return footprint

    def _pad_from_record(self, rec: dict[str, str]) -> Pad:
        raw_number = _first(rec, "DESIGNATOR", "PADNAME", "PINNUMBER", "NAME", "NUMBER") or ""
        number = raw_number.strip() or "?"
        x = parse_length(_first(rec, "X", "LOCATION.X", "CENTERX"), self.default_unit)
        y = parse_length(_first(rec, "Y", "LOCATION.Y", "CENTERY"), self.default_unit)
        sx = parse_length(_first(rec, "XSIZE", "SIZE_X", "WIDTH", "X"), self.default_unit)
        sy = parse_length(_first(rec, "YSIZE", "SIZE_Y", "HEIGHT", "Y"), self.default_unit)
        if sx == 0:
            sx = parse_length(_first(rec, "SIZE", "DIAMETER"), self.default_unit)
        if sy == 0:
            sy = sx
        hole = parse_length(_first(rec, "HOLESIZE", "HOLERADIUS", "DRILL", "DRILLSIZE"), self.default_unit)
        if "HOLERADIUS" in rec and "HOLESIZE" not in rec:
            hole *= 2
        slot_size = _slot_size_from_record(rec, self.default_unit)
        if slot_size and hole <= 0:
            hole = min(slot_size)
        layer = _first(rec, "LAYER", "STARTLAYER") or "TopLayer"
        layers = _map_layers(layer)
        kind = "thru_hole" if hole > 0 or slot_size or "MULTI" in layer.upper() else "smd"
        shape = _normalize_pad_shape(_first(rec, "SHAPE", "PADSHAPE", "TOPSHAPE", "MIDSHAPE") or "rect")
        rotation = _parse_float(_first(rec, "ROTATION", "ANGLE") or "0")
        properties: dict[str, str] = {}
        plated = _first(rec, "PLATED", "PLATING", "PLATEDTHRU", "PLATED_THROUGH")
        if plated is not None:
            properties["plated"] = _normalize_bool_text(plated)
        role = _pad_role_from_record(rec, raw_number, layer, hole, slot_size, properties.get("plated", "true"))
        padstack = _padstack_name(kind, shape, sx, sy, hole, slot_size, role, properties.get("plated", "true"))
        return Pad(
            number=number,
            x=x,
            y=y,
            rotation=rotation,
            kind=kind,
            shape=shape,
            size=(sx, sy),
            hole_diameter=hole,
            slot_size=slot_size,
            layers=layers,
            padstack=padstack,
            role=role,
            properties=properties,
        )

    def _add_graphic(self, kind: str, rec: dict[str, str]) -> None:
        if self._current_footprint is None:
            return
        layer = _first(rec, "LAYER") or "TopOverlay"
        graphic = Graphic(
            kind=kind,
            layer=_map_graphic_layer(layer),
            width=parse_length(_first(rec, "WIDTH", "LINEWIDTH"), self.default_unit),
            x=parse_length(_first(rec, "X", "X1"), self.default_unit),
            y=parse_length(_first(rec, "Y", "Y1"), self.default_unit),
            rotation=_parse_float(_first(rec, "ROTATION", "ANGLE") or "0"),
            text=_first(rec, "TEXT", "STRING", "NAME", "VALUE") or "",
            points=_points_from_record(rec, self.default_unit),
        )
        self._current_footprint.graphics.append(graphic)


def _iter_records(path: Path) -> Iterable[dict[str, str]]:
    text = _read_text(path)
    for line in text.splitlines():
        rec = parse_record_line(line)
        if rec is not None:
            yield rec


def _read_text(path: Path) -> str:
    encodings = ["utf-8-sig", "utf-16", "gb18030", "latin-1"]
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return path.read_text()


def _record_type(rec: dict[str, str]) -> str:
    value = _first(rec, "RECORD", "RECORDTYPE", "TYPE") or ""
    return value.strip().upper().replace(" ", "")


def _has_pin_fields(rec: dict[str, str]) -> bool:
    return any(key in rec for key in ["PINNUMBER", "PIN_NUMBER", "PINNAME", "PIN_NAME"])


def _has_pad_fields(rec: dict[str, str]) -> bool:
    return any(key in rec for key in ["PADNAME", "HOLESIZE", "DRILLSIZE"]) or (
        "DESIGNATOR" in rec and ("XSIZE" in rec or "YSIZE" in rec)
    )


def _first(rec: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = rec.get(key)
        if value not in [None, ""]:
            return value
    return None


def _display_key(key: str) -> str:
    return key.lower().replace("_", " ").title().replace(" ", "_")


def _normalize_pin_type(pin_type: str) -> str:
    key = pin_type.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "passive": "Passive",
        "input": "Input",
        "output": "Output",
        "io": "Bidirectional",
        "bidirectional": "Bidirectional",
        "power": "Power",
        "power_pin": "Power",
        "hidden_power": "Power",
        "power_hidden": "Power",
        "hiddenpower": "Power",
        "open_collector": "OpenCollector",
        "open_emitter": "OpenEmitter",
        "not_connected": "NoConnect",
        "nc": "NoConnect",
        "analog": "Passive",
    }
    return mapping.get(key, pin_type or "Passive")


def _pin_hidden_from_record(rec: dict[str, str], raw_pin_type: str) -> bool:
    hidden_fields = ("HIDDEN", "ISHIDDEN", "PINHIDDEN", "HIDEPIN", "HIDE")
    visible_fields = ("VISIBLE", "PINVISIBLE", "PIN_VISIBLE", "SHOWPIN", "SHOW_PIN", "DISPLAYPIN", "DISPLAY_PIN")
    for key in hidden_fields:
        value = rec.get(key)
        if value not in [None, ""]:
            return _parse_hidden_bool(value)
    for key in visible_fields:
        value = rec.get(key)
        if value not in [None, ""]:
            return not _parse_visible_bool(value)
    type_key = raw_pin_type.strip().lower().replace("-", "_").replace(" ", "_")
    return type_key in {"hidden_power", "power_hidden", "hiddenpower"}


def _parse_hidden_bool(value: str) -> bool:
    text = str(value).strip().lower()
    if text in {"1", "yes", "y", "true", "t", "on", "hidden", "hide", "invisible"}:
        return True
    if text in {"0", "no", "n", "false", "f", "off", "visible", "show", "shown"}:
        return False
    return bool(text)


def _parse_visible_bool(value: str) -> bool:
    text = str(value).strip().lower()
    if text in {"1", "yes", "y", "true", "t", "on", "visible", "show", "shown"}:
        return True
    if text in {"0", "no", "n", "false", "f", "off", "hidden", "hide", "invisible"}:
        return False
    return bool(text)


def _side_from_orientation(value: str | None) -> str:
    if not value:
        return "left"
    text = value.strip().lower()
    mapping = {
        "0": "left",
        "180": "right",
        "90": "bottom",
        "270": "top",
        "left": "left",
        "right": "right",
        "top": "top",
        "bottom": "bottom",
    }
    return mapping.get(text, "left")


def _normalize_pad_shape(shape: str) -> str:
    key = shape.strip().lower().replace(" ", "_")
    mapping = {
        "rectangle": "rect",
        "rectangular": "rect",
        "roundrect": "roundrect",
        "roundedrectangle": "roundrect",
        "rounded_rectangle": "roundrect",
        "circle": "circle",
        "round": "circle",
        "oval": "oval",
        "octagonal": "octagon",
        "octagon": "octagon",
    }
    return mapping.get(key, key or "rect")


def _slot_size_from_record(rec: dict[str, str], default_unit: str) -> tuple[float, float] | None:
    pair_value = _first(rec, "SLOTSIZE", "SLOT_SIZE", "SLOTDIMENSIONS", "HOLESLOTSIZE")
    pair = _parse_length_pair(pair_value, default_unit) if pair_value else None
    if pair:
        return pair

    width = parse_length(
        _first(rec, "SLOTWIDTH", "SLOT_WIDTH", "HOLEWIDTH", "HOLE_WIDTH", "DRILLSIZEWIDTH"),
        default_unit,
    )
    length = parse_length(
        _first(rec, "SLOTLENGTH", "SLOT_LENGTH", "HOLELENGTH", "HOLE_LENGTH", "DRILLSIZEHEIGHT"),
        default_unit,
    )
    if width > 0 and length > 0:
        return (width, length)

    hole_type = (_first(rec, "HOLETYPE", "DRILLTYPE", "DRILLKIND") or "").upper()
    if "SLOT" not in hole_type:
        return None

    fallback_hole = parse_length(_first(rec, "HOLESIZE", "DRILL", "DRILLSIZE"), default_unit)
    if width <= 0:
        width = fallback_hole
    if length <= 0:
        length = fallback_hole
    if width > 0 and length > 0:
        return (width, length)
    return None


def _parse_length_pair(value: str, default_unit: str) -> tuple[float, float] | None:
    matches = re.findall(
        r"[-+]?\d+(?:\.\d+)?\s*(?:mm|millimeter|millimeters|mil|mils|in|inch|inches)?",
        str(value),
        flags=re.IGNORECASE,
    )
    if len(matches) < 2:
        return None
    first = parse_length(matches[0], default_unit)
    second = parse_length(matches[1], default_unit)
    if first <= 0 or second <= 0:
        return None
    return (first, second)


def _padstack_name(
    kind: str,
    shape: str,
    sx: float,
    sy: float,
    hole: float,
    slot_size: tuple[float, float] | None,
    role: str = "electrical",
    plated: str = "true",
) -> str:
    drill_part = f"slot_{slot_size[0]:.3f}x{slot_size[1]:.3f}" if slot_size else f"{hole:.3f}"
    qualifiers: list[str] = []
    if role != "electrical":
        qualifiers.append(role)
    if kind == "thru_hole" and str(plated).strip().lower() == "false" and "np" not in role:
        qualifiers.append("nonplated")
    prefix = "_".join(qualifiers + [kind])
    return cadence_safe_name(f"{prefix}_{shape}_{sx:.3f}x{sy:.3f}_{drill_part}")


def _pad_role_from_record(
    rec: dict[str, str],
    raw_number: str,
    layer: str,
    hole: float,
    slot_size: tuple[float, float] | None,
    plated: str,
) -> str:
    bool_fields = [
        ("SHIELD", "shield"),
        ("SHIELDPAD", "shield"),
        ("SHELL", "shield"),
        ("MECHANICAL", "mechanical"),
        ("ISMECHANICAL", "mechanical"),
        ("MECHANICALPAD", "mechanical"),
        ("MOUNTING", "mounting"),
        ("MOUNTINGHOLE", "mounting"),
        ("ISMOUNTINGHOLE", "mounting"),
        ("TOOLING", "mounting"),
        ("TOOLINGHOLE", "mounting"),
        ("NPTH", "npth"),
        ("NONPLATED", "npth"),
        ("NON_PLATED", "npth"),
    ]
    for key, role in bool_fields:
        value = rec.get(key)
        if value not in [None, ""] and _bool_field_is_true(value):
            return role

    explicit = _first(
        rec,
        "PADROLE",
        "PAD_ROLE",
        "ROLE",
        "PADTYPE",
        "PAD_TYPE",
        "PADKIND",
        "PAD_KIND",
        "FUNCTION",
        "PADFUNCTION",
        "PINFUNCTION",
        "USE",
        "PADUSE",
        "HOLETYPE",
        "DRILLTYPE",
        "DRILLKIND",
    )
    explicit_role = _normalize_pad_role_text(explicit or "")
    if explicit_role != "electrical":
        return explicit_role

    number = raw_number.strip().upper().replace("-", "_")
    if number in {"", "?", "0"} and (hole > 0 or slot_size):
        return "npth" if str(plated).strip().lower() == "false" else "mechanical"
    if "MECHANICAL" in layer.upper():
        return "mechanical"
    if re.match(r"^(NPTH|MOUNT|MNT|MH|MP|TOOL|LOC|LOCK)[A-Z0-9_]*$", number):
        return "npth" if str(plated).strip().lower() == "false" else "mounting"
    if re.match(r"^(SHELL|SHIELD)[A-Z0-9_]*$", number):
        return "shield"
    return "electrical"


def _normalize_pad_role_text(value: str) -> str:
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return "electrical"
    if any(token in text for token in ["shield", "shell"]):
        return "shield"
    if any(token in text for token in ["mount", "tool", "locating", "locator"]):
        return "mounting"
    if any(token in text for token in ["npth", "non_plated", "nonplated", "unplated"]):
        return "npth"
    if "mech" in text:
        return "mechanical"
    return "electrical"


def _bool_field_is_true(value: str) -> bool:
    text = str(value).strip().lower()
    if text in {"0", "no", "n", "false", "f", "off", "none", "electrical"}:
        return False
    return bool(text)


def _normalize_bool_text(value: str) -> str:
    text = str(value).strip().lower()
    if text in {"1", "yes", "y", "true", "t", "plated", "plate"}:
        return "true"
    if text in {"0", "no", "n", "false", "f", "nonplated", "non_plated", "unplated"}:
        return "false"
    return text or "true"


def _map_layers(layer: str) -> list[str]:
    text = layer.upper()
    if "MULTI" in text:
        return ["top", "bottom"]
    if "BOTTOM" in text:
        return ["bottom"]
    return ["top"]


def _map_graphic_layer(layer: str) -> str:
    text = layer.upper()
    if "SILK" in text or "OVERLAY" in text:
        return "silkscreen_top" if "BOTTOM" not in text else "silkscreen_bottom"
    if "ASSEMBLY" in text:
        return "assembly_top" if "BOTTOM" not in text else "assembly_bottom"
    if "COURTYARD" in text or "BOUND" in text:
        return "place_bound_top" if "BOTTOM" not in text else "place_bound_bottom"
    return layer.lower()


def _points_from_record(rec: dict[str, str], default_unit: str) -> list[tuple[float, float]]:
    pairs = [("X1", "Y1"), ("X2", "Y2"), ("X3", "Y3"), ("X4", "Y4")]
    points: list[tuple[float, float]] = []
    for x_key, y_key in pairs:
        if x_key in rec and y_key in rec:
            points.append((parse_length(rec[x_key], default_unit), parse_length(rec[y_key], default_unit)))
    if not points and "X" in rec and "Y" in rec:
        points.append((parse_length(rec["X"], default_unit), parse_length(rec["Y"], default_unit)))
    return points


def _apply_component_text(component: Component, rec: dict[str, str]) -> None:
    name = _first(rec, "NAME", "PARAMETER", "TEXT")
    value = _first(rec, "VALUE", "STRING")
    if name and value:
        component.properties[_display_key(name.upper())] = value


def _parse_float(value: str) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return 0.0
