from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, Graphic, LibraryIR, Pad, Pin
from ..ir.units import parse_easyeda_3d_transform
from .easyeda_legacy import looks_like_easyeda_legacy


EASYEDA_PRO_JSONL_SUFFIXES = {".epru", ".efoo", ".esym", ".edevice", ".esch", ".epcb"}
EASYEDA_PRO_ARCHIVE_SUFFIXES = {".epro", ".epro2", ".elibz"}
EASYEDA_PRO_UNIT_MM = 0.0254


@dataclass
class _ProRecord:
    record_type: str
    record_id: str
    ticket: int
    data: dict[str, Any]


@dataclass
class _ProDoc:
    doc_type: str
    uuid: str
    source_file: str
    records: dict[tuple[str, str], _ProRecord] = field(default_factory=dict)

    def add_record(self, record: _ProRecord) -> None:
        key = (record.record_type, record.record_id)
        existing = self.records.get(key)
        if existing is None or record.ticket >= existing.ticket:
            self.records[key] = record

    def records_of_type(self, record_type: str) -> list[_ProRecord]:
        return [record for record in self.records.values() if record.record_type == record_type]

    def meta(self) -> dict[str, Any]:
        metas = self.records_of_type("META")
        if not metas:
            return {}
        return metas[-1].data


def looks_like_easyeda_pro_jsonl(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8-sig", errors="ignore")[:8192]
    except OSError:
        return False
    return "||" in sample and '"DOCHEAD"' in sample and '"docType"' in sample


def parse_easyeda_pro_design_files(paths: Iterable[Path], default_unit: str = "mm") -> LibraryIR:
    parser = EasyEDAProParser(default_unit=default_unit)
    for path in paths:
        parser.parse_file(path)
    return parser.to_library()


class EasyEDAProParser:
    def __init__(self, default_unit: str = "mm") -> None:
        self.default_unit = default_unit
        self.docs: list[_ProDoc] = []
        self.warnings: list[str] = []

    def parse_file(self, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix not in EASYEDA_PRO_JSONL_SUFFIXES and not looks_like_easyeda_pro_jsonl(path):
            return

        current: _ProDoc | None = None
        parsed_records = 0
        for line_number, line in enumerate(_read_text(path).splitlines(), start=1):
            parsed = _parse_v3_jsonl_line(line)
            if parsed is None:
                continue
            outer, inner = parsed
            record_type = str(outer.get("type") or "").strip().upper()
            if not record_type:
                continue
            ticket = _int_value(outer.get("ticket"), 0)
            record_id = str(outer.get("id") or record_type)

            if record_type == "DOCHEAD":
                doc_type = str(inner.get("docType") or "").strip().upper()
                if not doc_type:
                    self.warnings.append(f"{path}:{line_number}: EasyEDA Pro DOCHEAD has no docType.")
                    current = None
                    continue
                current = _ProDoc(
                    doc_type=doc_type,
                    uuid=str(inner.get("uuid") or f"{path.stem}_{len(self.docs) + 1}"),
                    source_file=str(path),
                )
                self.docs.append(current)
                parsed_records += 1
                continue

            if current is None:
                continue
            if not isinstance(inner, dict):
                continue
            current.add_record(
                _ProRecord(
                    record_type=record_type,
                    record_id=record_id,
                    ticket=ticket,
                    data=inner,
                )
            )
            parsed_records += 1

        if parsed_records == 0 and suffix in EASYEDA_PRO_JSONL_SUFFIXES and not looks_like_easyeda_legacy(path):
            self.warnings.append(f"{path}: no EasyEDA Pro V3 JSONL records were parsed.")

    def to_library(self) -> LibraryIR:
        library = LibraryIR()
        library.warnings.extend(self.warnings)
        if not self.docs:
            return library

        library.source_files.extend(sorted({doc.source_file for doc in self.docs}))
        library.properties["EasyEDA_Pro_Doc_Count"] = str(len(self.docs))
        library.properties["EasyEDA_Pro_Format"] = "v3_jsonl"

        footprint_by_uuid: dict[str, Footprint] = {}
        used_footprint_names: set[str] = set()
        for doc in [doc for doc in self.docs if doc.doc_type == "FOOTPRINT"]:
            footprint = _footprint_from_doc(doc, used_footprint_names)
            if footprint is None:
                continue
            footprint_by_uuid[doc.uuid] = footprint
            library.upsert_footprint(footprint)

        symbol_pins_by_uuid = {
            doc.uuid: _symbol_pins_from_doc(doc)
            for doc in self.docs
            if doc.doc_type == "SYMBOL"
        }

        used_component_names: set[str] = set()
        device_components = 0
        for doc in [doc for doc in self.docs if doc.doc_type == "DEVICE"]:
            component = _component_from_device_doc(
                doc=doc,
                symbol_pins_by_uuid=symbol_pins_by_uuid,
                footprint_by_uuid=footprint_by_uuid,
                used_component_names=used_component_names,
            )
            if component is None:
                continue
            library.upsert_component(component)
            device_components += 1

        if device_components == 0:
            for doc in [doc for doc in self.docs if doc.doc_type == "SYMBOL"]:
                component = _component_from_symbol_doc(doc, used_component_names)
                if component:
                    library.upsert_component(component)

        return library


def _parse_v3_jsonl_line(line: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    text = line.strip().lstrip("\ufeff")
    if not text or "||" not in text:
        return None
    left, right = text.split("||", 1)
    right = right.strip()
    if right.endswith("|"):
        right = right[:-1]
    try:
        outer = json.loads(left)
        inner = json.loads(right)
    except json.JSONDecodeError:
        return None
    if not isinstance(outer, dict) or not isinstance(inner, dict):
        return None
    return outer, inner


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


def _footprint_from_doc(doc: _ProDoc, used_names: set[str]) -> Footprint | None:
    meta = doc.meta()
    title = _first_text(meta, "title", "name") or doc.uuid
    name = _unique_cadence_name(title, used_names, doc.uuid, "FOOTPRINT")
    footprint = Footprint(
        name=name,
        description=str(meta.get("description") or ""),
        source_file=doc.source_file,
    )
    footprint.properties["EasyEDA_Pro_UUID"] = doc.uuid
    footprint.properties["EasyEDA_Pro_Title"] = str(title)
    for key, value in _as_dict(meta.get("attributes")).items():
        footprint.properties[_display_key(key)] = _string_value(value)

    for record in doc.records_of_type("PAD"):
        pad = _pad_from_record(record)
        if pad:
            footprint.add_pad(pad)

    for record_type in ["POLY", "LINE", "RECT", "CIRCLE", "ARC", "TEXT", "STRING"]:
        for record in doc.records_of_type(record_type):
            graphic = _graphic_from_record(record_type, record.data)
            if graphic:
                footprint.graphics.append(graphic)

    return footprint if footprint.pads else None


def _pad_from_record(record: _ProRecord) -> Pad | None:
    data = record.data
    number = str(data.get("num") or data.get("number") or data.get("pinNumber") or record.record_id).strip() or "?"
    x = _to_mm(_first_present(data, "centerX", "x", "positionX"))
    y = _to_mm(_first_present(data, "centerY", "y", "positionY"))
    sx, sy, shape = _pad_ref_geometry(data.get("defaultPad"))
    hole_diameter, slot_size = _hole_geometry(data.get("hole"))
    if sx <= 0:
        sx = hole_diameter if hole_diameter > 0 else 0.5
    if sy <= 0:
        sy = sx
    layer_id = data.get("layerId")
    layers = _pad_layers(layer_id)
    kind = "thru_hole" if hole_diameter > 0 or slot_size is not None or "bottom" in layers and "top" in layers else "smd"
    plated = _normalize_bool_text(data.get("plated", True))
    role = _pad_role(number, kind, data, plated)
    if role == "npth":
        plated = "false"
    padstack = _padstack_name(kind, shape, sx, sy, hole_diameter, slot_size, role, plated)
    properties = {
        "EasyEDA_Pro_Id": record.record_id,
        "EasyEDA_Pro_LayerId": str(layer_id),
        "plated": plated,
    }
    if data.get("padType") not in [None, ""]:
        properties["EasyEDA_Pro_PadType"] = str(data.get("padType"))
    return Pad(
        number=number,
        x=x,
        y=y,
        rotation=_float_value(data.get("padAngle", data.get("rotation", 0.0))),
        kind=kind,
        shape=shape,
        size=(sx, sy),
        hole_diameter=hole_diameter,
        slot_size=slot_size,
        layers=layers,
        padstack=padstack,
        role=role,
        properties=properties,
    )


def _pad_ref_geometry(value: Any) -> tuple[float, float, str]:
    if isinstance(value, dict):
        pad_type = str(value.get("padType") or value.get("type") or "RECT")
        width = _to_mm(_first_present(value, "width", "sizeX", "diameter"))
        height = _to_mm(_first_present(value, "height", "sizeY", "diameter"))
    elif isinstance(value, list):
        pad_type = str(value[0] if value else "RECT")
        width = _to_mm(value[1] if len(value) > 1 else 0)
        height = _to_mm(value[2] if len(value) > 2 else width)
    else:
        pad_type = "RECT"
        width = 0.0
        height = 0.0
    if height <= 0:
        height = width
    shape = _pad_shape(pad_type, width, height)
    return width, height, shape


def _hole_geometry(value: Any) -> tuple[float, tuple[float, float] | None]:
    if not value:
        return 0.0, None
    if isinstance(value, dict):
        hole_type = str(value.get("holeType") or value.get("type") or "ROUND")
        width = _to_mm(_first_present(value, "width", "sizeX", "diameter"))
        height = _to_mm(_first_present(value, "height", "sizeY", "diameter"))
    elif isinstance(value, list):
        hole_type = str(value[0] if value else "ROUND")
        width = _to_mm(value[1] if len(value) > 1 else 0)
        height = _to_mm(value[2] if len(value) > 2 else width)
    else:
        return 0.0, None
    if height <= 0:
        height = width
    drill = min(width, height) if width > 0 and height > 0 else max(width, height)
    if "SLOT" in hole_type.upper() and width > 0 and height > 0 and abs(width - height) > 0.01:
        return drill, (min(width, height), max(width, height))
    return drill, None


def _pad_shape(pad_type: str, width: float, height: float) -> str:
    text = pad_type.strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"RECT", "RECTANGLE", "SQUARE"}:
        return "rect"
    if text in {"ROUNDRECT", "ROUND_RECT", "ROUNDED_RECTANGLE"}:
        return "roundrect"
    if text in {"ROUND", "CIRCLE", "ELLIPSE"}:
        return "circle" if abs(width - height) < 0.001 else "oval"
    if text in {"OVAL", "SLOT"}:
        return "oval"
    if text in {"OCTAGON", "NGON"}:
        return "octagon"
    return "rect"


def _pad_layers(layer_id: Any) -> list[str]:
    text = str(layer_id).strip().upper()
    if text in {"2", "BOTTOM", "BOT"}:
        return ["bottom"]
    if text in {"12", "MULTI", "MULTI_LAYER", "MULTILAYER"}:
        return ["top", "bottom"]
    return ["top"]


def _pad_role(number: str, kind: str, data: dict[str, Any], plated: str) -> str:
    explicit = str(data.get("padType") or data.get("type") or "").strip().lower()
    if any(token in explicit for token in ["fiducial", "mark", "tool", "mount"]):
        return "mounting"
    number_key = number.strip().upper().replace("-", "_")
    if number_key in {"", "0", "?", "NPTH"} and kind == "thru_hole":
        return "npth" if plated == "false" else "mechanical"
    if re.match(r"^(NPTH|MOUNT|MNT|MH|MP|TOOL|LOC|LOCK)[A-Z0-9_]*$", number_key):
        return "npth" if plated == "false" else "mounting"
    if re.match(r"^(SHELL|SHIELD)[A-Z0-9_]*$", number_key):
        return "shield"
    if kind == "thru_hole" and plated == "false":
        return "npth"
    return "electrical"


def _padstack_name(
    kind: str,
    shape: str,
    sx: float,
    sy: float,
    hole: float,
    slot_size: tuple[float, float] | None,
    role: str,
    plated: str,
) -> str:
    drill_part = f"slot_{slot_size[0]:.3f}x{slot_size[1]:.3f}" if slot_size else f"{hole:.3f}"
    qualifiers: list[str] = []
    if role != "electrical":
        qualifiers.append(role)
    if kind == "thru_hole" and plated == "false" and "np" not in role:
        qualifiers.append("nonplated")
    prefix = "_".join(qualifiers + [kind])
    return cadence_safe_name(f"{prefix}_{shape}_{sx:.3f}x{sy:.3f}_{drill_part}")


def _graphic_from_record(record_type: str, data: dict[str, Any]) -> Graphic | None:
    layer = _graphic_layer(data.get("layerId"))
    width = _to_mm(_first_present(data, "width", "strokeWidth", "lineWidth"))
    if record_type in {"POLY", "LINE"}:
        points = _points_from_path(data.get("path"))
        if not points:
            points = _points_from_xy(data)
        if len(points) >= 2:
            return Graphic(kind="line", layer=layer, points=points, width=width)
    if record_type == "RECT":
        x1 = _to_mm(_first_present(data, "dotX1", "x1", "startX"))
        y1 = _to_mm(_first_present(data, "dotY1", "y1", "startY"))
        x2 = _to_mm(_first_present(data, "dotX2", "x2", "endX"))
        y2 = _to_mm(_first_present(data, "dotY2", "y2", "endY"))
        return Graphic(kind="region", layer=layer, points=[(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)], width=width)
    if record_type in {"TEXT", "STRING"}:
        return Graphic(
            kind="text",
            layer=layer,
            x=_to_mm(_first_present(data, "x", "positionX")),
            y=_to_mm(_first_present(data, "y", "positionY")),
            text=str(_first_present(data, "text", "string", "value") or ""),
            rotation=_float_value(data.get("rotation", 0.0)),
        )
    return None


def _points_from_path(value: Any) -> list[tuple[float, float]]:
    if isinstance(value, list):
        numbers = [_float_value(item) for item in value if isinstance(item, (int, float))]
    elif isinstance(value, str):
        numbers = [float(match) for match in re.findall(r"[-+]?\d+(?:\.\d+)?", value)]
    else:
        return []
    points = []
    for idx in range(0, len(numbers) - 1, 2):
        points.append((_to_mm(numbers[idx]), _to_mm(numbers[idx + 1])))
    return points


def _points_from_xy(data: dict[str, Any]) -> list[tuple[float, float]]:
    pairs = [("x1", "y1"), ("startX", "startY"), ("x2", "y2"), ("endX", "endY")]
    points = []
    for x_key, y_key in pairs:
        if x_key in data and y_key in data:
            points.append((_to_mm(data[x_key]), _to_mm(data[y_key])))
    return points


def _graphic_layer(layer_id: Any) -> str:
    text = str(layer_id).strip().upper()
    mapping = {
        "3": "silkscreen_top",
        "4": "silkscreen_bottom",
        "9": "assembly_top",
        "10": "assembly_bottom",
        "11": "place_bound_top",
        "14": "mechanical",
        "TOP_SILK": "silkscreen_top",
        "BOT_SILK": "silkscreen_bottom",
        "TOP_ASSEMBLY": "assembly_top",
        "BOT_ASSEMBLY": "assembly_bottom",
        "OUTLINE": "place_bound_top",
    }
    return mapping.get(text, f"easyeda_layer_{text.lower()}" if text else "easyeda_layer")


def _symbol_pins_from_doc(doc: _ProDoc) -> list[Pin]:
    pin_records = doc.records_of_type("PIN")
    if not pin_records:
        return []
    attrs_by_parent = _attrs_by_parent(doc)
    part_ids = sorted(
        {
            str(record.data.get("partId") or "")
            for record in pin_records
            if str(record.data.get("partId") or "").strip()
        }
    )
    unit_map = {part_id: _unit_name(index) for index, part_id in enumerate(part_ids)}
    if len(unit_map) <= 1:
        unit_map = {part_id: "A" for part_id in unit_map}

    pins: list[Pin] = []
    for record in pin_records:
        attrs = attrs_by_parent.get(record.record_id, {})
        number = _first_attr(attrs, "pinnumber", "number") or str(record.data.get("number") or record.record_id)
        name = _first_attr(attrs, "pinname", "name") or number
        pin_type = _normalize_pin_type(_first_attr(attrs, "pintype", "type") or _electric_pin_type(record.data.get("electric")))
        part_id = str(record.data.get("partId") or "")
        hidden = _pin_hidden(record.data, attrs)
        pins.append(
            Pin(
                number=str(number),
                name=str(name),
                type=pin_type,
                unit=unit_map.get(part_id, "A"),
                hidden=hidden,
                x=_to_mm(_first_present(record.data, "x", "positionX")),
                y=_to_mm(_first_present(record.data, "y", "positionY")),
                side=_side_from_rotation(record.data.get("rotation")),
                length=_to_mm(record.data.get("length")),
                properties={"EasyEDA_Pro_Pin_Id": record.record_id},
            )
        )
    return pins


def _component_from_device_doc(
    doc: _ProDoc,
    symbol_pins_by_uuid: dict[str, list[Pin]],
    footprint_by_uuid: dict[str, Footprint],
    used_component_names: set[str],
) -> Component | None:
    meta = doc.meta()
    attrs = _as_dict(meta.get("attributes") or meta.get("attrs"))
    footprint_uuid = str(_first_present(attrs, "Footprint", "footprint") or "").strip()
    footprint_name = ""
    footprint = footprint_by_uuid.get(footprint_uuid)
    if footprint is not None:
        footprint_name = footprint.name
    else:
        footprint_title = _first_present(attrs, "Footprint Name", "FootprintName", "Supplier Footprint")
        if footprint_title:
            footprint_name = cadence_safe_name(str(footprint_title), "FOOTPRINT")
    if not footprint_name:
        return None

    symbol_uuid = str(_first_present(attrs, "Symbol", "symbol") or "").strip()
    pins = [_copy_pin(pin) for pin in symbol_pins_by_uuid.get(symbol_uuid, [])]
    title = _first_text(meta, "title", "name") or _first_present(attrs, "Name", "Manufacturer Part", "Supplier Part") or doc.uuid
    name = _unique_cadence_name(str(title), used_component_names, doc.uuid, "COMPONENT")
    ref = str(_first_present(attrs, "Designator", "designator", "RefDes", "Reference") or "U?")
    ref_prefix = "".join(ch for ch in ref if ch.isalpha()) or _guess_ref_prefix(name)
    value = _component_value(attrs, str(title))
    description = str(_first_present(meta, "description") or _first_present(attrs, "Description", "LCSC Part Name") or "")

    component = Component(
        name=name,
        ref_prefix=ref_prefix,
        value=value,
        footprint=footprint_name,
        description=description,
        pins=pins,
        source_file=doc.source_file,
    )
    component.properties["EasyEDA_Pro_UUID"] = doc.uuid
    component.properties["EasyEDA_Pro_Title"] = str(title)
    for key, value_obj in attrs.items():
        component.properties[_display_key(str(key))] = _string_value(value_obj)

    if footprint is not None:
        model_id = _first_present(
            attrs,
            "3D Model",
            "3DModel",
            "3D Model ID",
            "3DModelID",
            "Model3D",
            "Model",
        )
        model_title = _first_present(
            attrs,
            "3D Model Title",
            "3DModelTitle",
            "Model Title",
            "ModelTitle",
        )
        model_transform = _first_present(attrs, "3D Model Transform", "3DModelTransform")
        if model_id:
            footprint.properties["EasyEDA_3D_Model_Id"] = str(model_id)
        if model_title:
            footprint.properties["Model3D"] = cadence_safe_name(str(model_title), "MODEL")
            footprint.properties["EasyEDA_3D_Model_Title"] = str(model_title)
        elif model_id:
            footprint.properties["Model3D"] = cadence_safe_name(str(model_id), "MODEL")
        if model_transform:
            footprint.properties["EasyEDA_3D_Model_Transform"] = str(model_transform)
            if parse_easyeda_3d_transform(str(model_transform)) is None:
                footprint.properties["EasyEDA_3D_Model_Transform_Status"] = "invalid"
                footprint.properties["EasyEDA_3D_Model_Transform_Error"] = "expected 9 numeric fields"
            else:
                footprint.properties["EasyEDA_3D_Model_Transform_Status"] = "parsed"

    return component


def _component_from_symbol_doc(doc: _ProDoc, used_component_names: set[str]) -> Component | None:
    pins = _symbol_pins_from_doc(doc)
    if not pins:
        return None
    meta = doc.meta()
    title = _first_text(meta, "title", "name") or doc.uuid
    name = _unique_cadence_name(str(title), used_component_names, doc.uuid, "COMPONENT")
    return Component(
        name=name,
        ref_prefix=_guess_ref_prefix(name),
        value=name,
        pins=pins,
        description=str(meta.get("description") or ""),
        source_file=doc.source_file,
        properties={"EasyEDA_Pro_UUID": doc.uuid, "EasyEDA_Pro_Title": str(title)},
    )


def _attrs_by_parent(doc: _ProDoc) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in doc.records_of_type("ATTR"):
        parent_id = str(record.data.get("parentId") or "")
        if not parent_id:
            continue
        key = _attr_key(record.data.get("key"))
        if not key:
            continue
        grouped.setdefault(parent_id, {})[key] = record.data.get("value")
    return grouped


def _attr_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _first_attr(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value not in [None, ""]:
            return str(value)
    return None


def _pin_hidden(data: dict[str, Any], attrs: dict[str, Any]) -> bool:
    if data.get("display") is False:
        return True
    hidden = _first_attr(attrs, "hidden", "hidepin")
    if hidden is not None:
        return _hidden_bool_value(hidden)
    visible = _first_attr(attrs, "visible", "showpin")
    if visible is not None:
        return not _visible_bool_value(visible)
    return False


def _electric_pin_type(value: Any) -> str:
    mapping = {
        0: "Passive",
        1: "Input",
        2: "Output",
        3: "Bidirectional",
    }
    return mapping.get(_int_value(value, 0), "Passive")


def _normalize_pin_type(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "undefined": "Passive",
        "unknown": "Passive",
        "passive": "Passive",
        "input": "Input",
        "output": "Output",
        "io": "Bidirectional",
        "bidirectional": "Bidirectional",
        "bi": "Bidirectional",
        "power": "Power",
        "power_pin": "Power",
        "open_collector": "OpenCollector",
        "open_emitter": "OpenEmitter",
        "not_connected": "NoConnect",
        "no_connect": "NoConnect",
        "nc": "NoConnect",
    }
    return mapping.get(key, value or "Passive")


def _side_from_rotation(value: Any) -> str:
    rotation = int(round(_float_value(value))) % 360
    return {
        0: "left",
        90: "bottom",
        180: "right",
        270: "top",
    }.get(rotation, "left")


def _component_value(attrs: dict[str, Any], fallback: str) -> str:
    value = _first_present(attrs, "Value", "value")
    if value not in [None, ""]:
        return str(value)
    name = str(_first_present(attrs, "Name", "name") or "")
    if name and not name.startswith("={"):
        return name
    return fallback


def _unique_cadence_name(value: str, used: set[str], uuid: str, fallback: str) -> str:
    base = cadence_safe_name(value, fallback)
    name = base
    if name in used:
        suffix = cadence_safe_name(uuid[-8:] if uuid else "dup", "DUP")
        name = cadence_safe_name(f"{base}_{suffix}", fallback)
        counter = 2
        while name in used:
            name = cadence_safe_name(f"{base}_{suffix}_{counter}", fallback)
            counter += 1
    used.add(name)
    return name


def _copy_pin(pin: Pin) -> Pin:
    return Pin(
        number=pin.number,
        name=pin.name,
        type=pin.type,
        unit=pin.unit,
        hidden=pin.hidden,
        x=pin.x,
        y=pin.y,
        side=pin.side,
        length=pin.length,
        properties=dict(pin.properties),
    )


def _guess_ref_prefix(name: str) -> str:
    upper = name.upper()
    if upper.startswith(("R", "RES")):
        return "R"
    if upper.startswith(("C", "CAP")):
        return "C"
    if "CONN" in upper or "USB" in upper or "FPC" in upper:
        return "J"
    return "U"


def _display_key(key: str) -> str:
    return str(key).strip().replace("-", "_").replace(" ", "_")


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    value = _first_present(mapping, *keys)
    if value in [None, ""]:
        return None
    return str(value)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in [None, ""]:
            return mapping[key]
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _to_mm(value: Any) -> float:
    return _float_value(value) * EASYEDA_PRO_UNIT_MM


def _float_value(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"0", "no", "n", "false", "f", "off", "hidden", "hide", "invisible"}:
        return False
    if text in {"1", "yes", "y", "true", "t", "on", "visible", "show", "shown"}:
        return True
    return bool(text)


def _hidden_bool_value(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in {"0", "no", "n", "false", "f", "off", "visible", "show", "shown"}:
        return False
    if text in {"1", "yes", "y", "true", "t", "on", "hidden", "hide", "invisible"}:
        return True
    return bool(text)


def _visible_bool_value(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in {"0", "no", "n", "false", "f", "off", "hidden", "hide", "invisible"}:
        return False
    if text in {"1", "yes", "y", "true", "t", "on", "visible", "show", "shown"}:
        return True
    return bool(text)


def _normalize_bool_text(value: Any) -> str:
    return "true" if _bool_value(value) else "false"


def _string_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _unit_name(index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if index < len(alphabet):
        return alphabet[index]
    return f"U{index + 1}"
