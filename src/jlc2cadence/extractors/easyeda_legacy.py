from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, Graphic, LibraryIR, Pad, Pin


LEGACY_EASYEDA_SUFFIXES = {".esym", ".efoo", ".esch", ".epcb"}
LEGACY_EASYEDA_UNIT_MM = 0.0254


@dataclass
class _LegacyDoc:
    doc_type: str
    version: str
    path: Path
    rows: list[list[Any]] = field(default_factory=list)


@dataclass
class _LegacySymbol:
    uuid: str
    title: str
    pins: list[Pin]
    source_file: str


def looks_like_easyeda_legacy(path: Path) -> bool:
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            return isinstance(row, list) and len(row) >= 2 and row[0] == "DOCTYPE"
    except (OSError, json.JSONDecodeError):
        return False
    return False


def parse_easyeda_legacy_design_files(paths: Iterable[Path], default_unit: str = "mm") -> LibraryIR:
    parser = EasyEDAProLegacyParser(default_unit=default_unit)
    for path in paths:
        parser.parse_file(path)
    return parser.to_library()


class EasyEDAProLegacyParser:
    def __init__(self, default_unit: str = "mm") -> None:
        self.default_unit = default_unit
        self.docs: list[_LegacyDoc] = []
        self.warnings: list[str] = []
        self.project: dict[str, Any] = {}

    def parse_file(self, path: Path) -> None:
        suffix = path.suffix.lower()
        if path.name.lower() == "project.json":
            self._parse_project(path)
            return
        if suffix not in LEGACY_EASYEDA_SUFFIXES and not looks_like_easyeda_legacy(path):
            return

        sections: list[_LegacyDoc] = []
        current: _LegacyDoc | None = None
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError as exc:
            self.warnings.append(f"{path}: could not read legacy EasyEDA file: {exc}")
            return
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                self.warnings.append(f"{path}:{line_number}: invalid legacy EasyEDA JSON row.")
                continue
            if not isinstance(row, list) or not row:
                continue
            record_type = str(row[0] or "").strip().upper()
            if record_type == "DOCTYPE":
                doc_type = str(row[1] if len(row) > 1 else "").strip().upper()
                version = str(row[2] if len(row) > 2 else "").strip()
                current = _LegacyDoc(doc_type=doc_type, version=version, path=path)
                sections.append(current)
                continue
            if current is not None:
                current.rows.append(row)
        self.docs.extend(sections)

    def _parse_project(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            self.warnings.append(f"{path}: could not parse project.json: {exc}")
            return
        if isinstance(data, dict):
            self.project.update(data)

    def to_library(self) -> LibraryIR:
        library = LibraryIR()
        library.warnings.extend(self.warnings)
        if not self.docs:
            return library

        library.source_files.extend(sorted({str(doc.path) for doc in self.docs}))
        library.properties["EasyEDA_Pro_Format"] = "legacy_json_array"
        library.properties["EasyEDA_Pro_Legacy_Doc_Count"] = str(len(self.docs))

        symbols = self._parse_symbols()
        footprints, footprint_names = self._parse_footprints()
        self._apply_project_model_metadata(footprints)
        components = self._parse_components(symbols, footprint_names)

        for footprint in footprints.values():
            library.upsert_footprint(footprint)
        for component in components:
            library.upsert_component(component)
        return library

    def _parse_symbols(self) -> dict[str, _LegacySymbol]:
        symbols: dict[str, _LegacySymbol] = {}
        metadata = self.project.get("symbols")
        metadata = metadata if isinstance(metadata, dict) else {}
        used_names: set[str] = set()
        for doc in self.docs:
            if doc.doc_type != "SYMBOL":
                continue
            symbol_uuid = doc.path.stem.lower()
            meta = metadata.get(symbol_uuid, {})
            meta = meta if isinstance(meta, dict) else {}
            title = str(meta.get("title") or "").strip()
            rows = doc.rows
            part_name = ""
            attrs_by_parent: dict[str, dict[str, Any]] = defaultdict(dict)
            for row in rows:
                kind = _row_type(row)
                if kind == "PART" and len(row) > 1:
                    part_name = str(row[1] or "")
                elif kind == "ATTR" and len(row) >= 5:
                    parent_id = str(row[2] or "")
                    key = str(row[3] or "").strip()
                    attrs_by_parent[parent_id][key] = row[4]
                    if not title and not parent_id and key.lower() == "symbol":
                        title = str(row[4] or "")
            if not title:
                title = part_name.rsplit(".", 1)[0] if part_name else symbol_uuid
            metadata_uuid, metadata_entry = _metadata_by_title(metadata, title)
            if metadata_uuid and not meta:
                meta = metadata_entry
            if meta.get("title"):
                title = str(meta["title"]).strip()
            title = _unique_name(title, used_names, symbol_uuid, "SYMBOL")
            pins: list[Pin] = []
            for row in rows:
                if _row_type(row) != "PIN":
                    continue
                pin_id = str(row[1] if len(row) > 1 else "")
                pin_attrs = attrs_by_parent.get(pin_id, {})
                number = _first_attr(pin_attrs, "NUMBER", "Number") or str(row[1] or "?")
                name = _first_attr(pin_attrs, "NAME", "Name") or number
                raw_type = _first_attr(pin_attrs, "Pin Type", "PINTYPE", "TYPE") or "Passive"
                x = _mil_to_mm(_number_at(row, 4))
                y = _mil_to_mm(_number_at(row, 5))
                length = _mil_to_mm(_number_at(row, 6))
                rotation = _number_at(row, 7)
                pins.append(
                    Pin(
                        number=str(number),
                        name=str(name),
                        type=_normalize_pin_type(raw_type),
                        hidden=_legacy_pin_hidden(pin_attrs),
                        x=x,
                        y=y,
                        side=_side_from_rotation(rotation),
                        length=length,
                        properties={"EasyEDA_Legacy_Pin_Id": pin_id},
                    )
                )
            symbol = _LegacySymbol(
                uuid=symbol_uuid,
                title=title,
                pins=pins,
                source_file=str(doc.path),
            )
            for alias in {symbol_uuid, metadata_uuid}:
                if alias:
                    symbols[str(alias).lower()] = symbol
        return symbols

    def _parse_footprints(self) -> tuple[dict[str, Footprint], dict[str, str]]:
        metadata = self.project.get("footprints")
        metadata = metadata if isinstance(metadata, dict) else {}
        footprints: dict[str, Footprint] = {}
        names_by_uuid: dict[str, str] = {}
        used_names: set[str] = set()
        has_library_footprints = any(
            doc.doc_type == "FOOTPRINT" and doc.path.suffix.lower() == ".efoo"
            for doc in self.docs
        )
        for doc in self.docs:
            if doc.doc_type != "FOOTPRINT":
                continue
            if has_library_footprints and doc.path.suffix.lower() == ".epcb":
                continue
            uuid = _doc_uuid(doc)
            meta = metadata.get(uuid, {})
            meta = meta if isinstance(meta, dict) else {}
            attr_map = _footprint_attrs(doc.rows)
            title = str(meta.get("title") or "").strip() or str(attr_map.get("Footprint") or "").strip() or str(
                _head_value(doc.rows, "title") or uuid
            )
            metadata_uuid, metadata_entry = _metadata_by_title(metadata, title)
            if metadata_uuid and not meta:
                meta = metadata_entry
            if meta.get("title"):
                title = str(meta["title"]).strip()
            description = str(meta.get("desc") or meta.get("description") or "")
            name = _unique_name(title, used_names, uuid, "FOOTPRINT")
            footprint = Footprint(name=name, description=description, source_file=str(doc.path))
            footprint.properties["EasyEDA_Legacy_UUID"] = uuid
            footprint.properties["EasyEDA_Legacy_Title"] = title
            if metadata_uuid:
                footprint.properties["EasyEDA_Legacy_Project_UUID"] = metadata_uuid
            for key, value in attr_map.items():
                footprint.properties[_display_key(key)] = _string_value(value)
            for row in doc.rows:
                kind = _row_type(row)
                if kind == "PAD":
                    pad = _legacy_pad_from_row(row)
                    if pad is not None:
                        footprint.add_pad(pad)
                elif kind == "POLY":
                    graphic = _legacy_poly_graphic(row)
                    if graphic is not None:
                        footprint.graphics.append(graphic)
            if footprint.pads:
                aliases = {uuid, metadata_uuid}
                for alias in aliases:
                    if alias:
                        footprints[str(alias).lower()] = footprint
                        names_by_uuid[str(alias).lower()] = name
        return footprints, names_by_uuid

    def _apply_project_model_metadata(self, footprints: dict[str, Footprint]) -> None:
        devices = self.project.get("devices")
        devices = devices if isinstance(devices, dict) else {}
        for device in devices.values():
            if not isinstance(device, dict):
                continue
            attrs = device.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            footprint_uuid = str(attrs.get("Footprint") or "").strip().lower()
            footprint = footprints.get(footprint_uuid)
            if footprint is None:
                continue
            model_id = _first_present(attrs, "3D Model", "3DModel", "3D Model ID", "3DModelID", "Model3D", "Model")
            model_title = _first_present(attrs, "3D Model Title", "3DModelTitle", "Model Title", "ModelTitle")
            transform = _first_present(attrs, "3D Model Transform", "3DModelTransform")
            if model_id:
                footprint.properties["EasyEDA_3D_Model_Id"] = str(model_id)
            if model_title:
                footprint.properties["Model3D"] = cadence_safe_name(str(model_title), "MODEL")
                footprint.properties["EasyEDA_3D_Model_Title"] = str(model_title)
            elif model_id:
                footprint.properties["Model3D"] = cadence_safe_name(str(model_id), "MODEL")
            if transform:
                footprint.properties["EasyEDA_3D_Model_Transform"] = str(transform)

    def _parse_components(
        self,
        symbols: dict[str, _LegacySymbol],
        footprint_names: dict[str, str],
    ) -> list[Component]:
        instance_attrs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in self.docs:
            if doc.doc_type != "SCH":
                continue
            attrs_by_parent = _schematic_attrs(doc.rows)
            for component_id, attrs in attrs_by_parent.items():
                if attrs.get("Device"):
                    instance_attrs[str(attrs["Device"])].append(attrs)

        devices = self.project.get("devices")
        devices = devices if isinstance(devices, dict) else {}
        used_ids = set(instance_attrs)
        if devices:
            used_ids &= set(str(key) for key in devices)
        if not used_ids:
            used_ids = set(str(key) for key in devices)

        components: list[Component] = []
        used_names: set[str] = set()
        for device_id in sorted(used_ids):
            device = devices.get(device_id, {})
            device = device if isinstance(device, dict) else {}
            project_attrs = device.get("attributes")
            project_attrs = project_attrs if isinstance(project_attrs, dict) else {}
            instances = instance_attrs.get(device_id, [])
            instance = instances[0] if instances else {}
            attrs = dict(project_attrs)
            attrs.update({key: value for key, value in instance.items() if value not in [None, ""]})

            symbol_uuid = _most_common_value(instances, "Symbol") or str(project_attrs.get("Symbol") or "").strip()
            symbol_uuid = symbol_uuid.lower()
            symbol = symbols.get(symbol_uuid)
            if symbol is None:
                continue
            footprint_uuid = str(project_attrs.get("Footprint") or instance.get("Footprint") or "").strip().lower()
            footprint_name = footprint_names.get(footprint_uuid, "")
            title = str(device.get("title") or symbol.title or device_id)
            name = _unique_name(title, used_names, device_id, "COMPONENT")
            designator = str(instance.get("Designator") or project_attrs.get("Designator") or "").strip()
            ref_prefix = "".join(ch for ch in designator if ch.isalpha()) or _guess_ref_prefix(title)
            value = _legacy_component_value(attrs, title)
            description = str(device.get("description") or project_attrs.get("Description") or "")
            component = Component(
                name=name,
                ref_prefix=ref_prefix,
                value=value,
                footprint=footprint_name,
                description=description,
                pins=[_copy_pin(pin) for pin in symbol.pins],
                source_file=symbol.source_file,
            )
            component.properties["EasyEDA_Legacy_Device"] = device_id
            component.properties["EasyEDA_Legacy_Symbol"] = symbol_uuid
            if footprint_uuid:
                component.properties["EasyEDA_Legacy_Footprint"] = footprint_uuid
            for key, value_obj in attrs.items():
                component.properties[_display_key(str(key))] = _string_value(value_obj)
            components.append(component)
        return components


def _doc_uuid(doc: _LegacyDoc) -> str:
    head = _head_row(doc.rows)
    if head and len(head) > 1 and isinstance(head[1], dict):
        value = head[1].get("uuid")
        if value:
            return str(value).split("_", 1)[0].lower()
    return doc.path.stem.lower()


def _head_row(rows: list[list[Any]]) -> list[Any] | None:
    for row in rows:
        if _row_type(row) == "HEAD":
            return row
    return None


def _head_value(rows: list[list[Any]], key: str) -> Any:
    head = _head_row(rows)
    if not head or len(head) < 2 or not isinstance(head[1], dict):
        return None
    return head[1].get(key)


def _row_type(row: list[Any]) -> str:
    return str(row[0] if row else "").strip().upper()


def _footprint_attrs(rows: list[list[Any]]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for row in rows:
        if _row_type(row) != "ATTR" or len(row) < 9:
            continue
        key = str(row[7] or "").strip()
        if key:
            attrs[key] = row[8]
    return attrs


def _schematic_attrs(rows: list[list[Any]]) -> dict[str, dict[str, Any]]:
    attrs_by_parent: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        if _row_type(row) != "ATTR" or len(row) < 5:
            continue
        parent_id = str(row[2] or "")
        key = str(row[3] or "").strip()
        if parent_id and key:
            attrs_by_parent[parent_id][key] = row[4]
    for row in rows:
        if _row_type(row) != "COMPONENT" or len(row) < 2:
            continue
        attrs_by_parent.setdefault(str(row[1]), {})
    return attrs_by_parent


def _legacy_pad_from_row(row: list[Any]) -> Pad | None:
    if len(row) < 11:
        return None
    number = str(row[5] if row[5] not in [None, ""] else "?")
    x = _mil_to_mm(_number_at(row, 6))
    y = _mil_to_mm(_number_at(row, 7))
    rotation = _number_at(row, 8)
    layer_id = str(row[4] if row[4] is not None else "")
    hole_geometry = row[9] if isinstance(row[9], list) else None
    pad_geometry = row[10] if isinstance(row[10], list) else hole_geometry
    sx, sy, shape = _legacy_geometry(pad_geometry)
    hole_diameter = 0.0
    slot_size: tuple[float, float] | None = None
    if hole_geometry is not None and layer_id in {"12", "13", "MULTI"}:
        hx, hy, _ = _legacy_geometry(hole_geometry)
        hole_diameter = min(hx, hy) if hx > 0 and hy > 0 else max(hx, hy)
        if abs(hx - hy) > 0.01:
            slot_size = (min(hx, hy), max(hx, hy))
    kind = "thru_hole" if hole_diameter > 0 or layer_id in {"12", "13", "MULTI"} else "smd"
    if sx <= 0:
        sx = hole_diameter or 0.5
    if sy <= 0:
        sy = sx
    role = _legacy_pad_role(number, kind, hole_diameter)
    plated = "true" if kind == "thru_hole" and role != "npth" else "false" if role == "npth" else "true"
    properties = {
        "EasyEDA_Legacy_Pad_Id": str(row[1] if len(row) > 1 else ""),
        "EasyEDA_Legacy_Layer": layer_id,
        "plated": plated,
    }
    padstack = _padstack_name(kind, shape, sx, sy, hole_diameter, slot_size, role, plated)
    return Pad(
        number=number,
        x=x,
        y=y,
        rotation=rotation,
        kind=kind,
        shape=shape,
        size=(sx, sy),
        hole_diameter=hole_diameter,
        slot_size=slot_size,
        layers=_legacy_layers(layer_id),
        padstack=padstack,
        role=role,
        properties=properties,
    )


def _legacy_geometry(value: Any) -> tuple[float, float, str]:
    if not isinstance(value, list) or not value:
        return 0.0, 0.0, "rect"
    shape_text = str(value[0] or "").strip().upper()
    width = _mil_to_mm(_number_at(value, 1))
    height = _mil_to_mm(_number_at(value, 2))
    if height <= 0:
        height = width
    if shape_text in {"ROUND", "CIRCLE"}:
        shape = "circle" if abs(width - height) < 0.001 else "oval"
    elif shape_text in {"OVAL", "ELLIPSE"}:
        shape = "oval"
    elif shape_text in {"ROUNDRECT", "ROUNDED_RECTANGLE"}:
        shape = "roundrect"
    elif shape_text in {"OCTAGON", "OCTAGONAL"}:
        shape = "octagon"
    else:
        shape = "rect"
    return width, height, shape


def _legacy_poly_graphic(row: list[Any]) -> Graphic | None:
    if len(row) < 7 or not isinstance(row[6], list):
        return None
    numbers = [_number_at(row[6], index) for index in range(len(row[6]))]
    points: list[tuple[float, float]] = []
    pending: list[float] = []
    for value, token in zip(numbers, row[6]):
        if isinstance(token, (int, float)):
            pending.append(_mil_to_mm(value))
            if len(pending) == 2:
                points.append((pending[0], pending[1]))
                pending = []
    if len(points) < 2:
        return None
    return Graphic(
        kind="line",
        layer=_legacy_graphic_layer(row[4] if len(row) > 4 else ""),
        points=points,
        width=_mil_to_mm(_number_at(row, 5)),
    )


def _legacy_pin_hidden(attrs: dict[str, Any]) -> bool:
    for key in ("Hidden", "HIDDEN", "Hide", "Visible", "VISIBLE"):
        if key not in attrs:
            continue
        value = str(attrs[key]).strip().lower()
        if key.lower() == "visible":
            return value in {"0", "false", "no", "hidden"}
        return value in {"1", "true", "yes", "hidden", "hide"}
    return False


def _legacy_pad_role(number: str, kind: str, hole: float) -> str:
    text = number.strip().upper().replace("-", "_")
    if re.match(r"^(NPTH|MOUNT|MNT|MH|MP|TOOL|LOC|LOCK)[A-Z0-9_]*$", text):
        return "npth" if kind == "thru_hole" else "mounting"
    if re.match(r"^(SHELL|SHIELD)[A-Z0-9_]*$", text):
        return "shield"
    if not text and hole > 0:
        return "npth"
    return "electrical"


def _legacy_layers(layer_id: str) -> list[str]:
    text = layer_id.strip().upper()
    if text in {"12", "13", "MULTI"}:
        return ["top", "bottom"]
    if text in {"2", "BOTTOM", "BOT"}:
        return ["bottom"]
    return ["top"]


def _legacy_graphic_layer(layer_id: Any) -> str:
    text = str(layer_id).strip().upper()
    if text in {"3", "TOP_SILK", "TOPSILK"}:
        return "silkscreen_top"
    if text in {"4", "BOTTOM_SILK", "BOTTOMSILK"}:
        return "silkscreen_bottom"
    if text in {"48", "49", "50", "51"}:
        return "assembly_top"
    return f"easyeda_legacy_layer_{text.lower()}" if text else "easyeda_legacy_layer"


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
    qualifiers = [] if role == "electrical" else [role]
    if kind == "thru_hole" and plated == "false" and role != "npth":
        qualifiers.append("nonplated")
    prefix = "_".join(qualifiers + [kind])
    return cadence_safe_name(f"{prefix}_{shape}_{sx:.3f}x{sy:.3f}_{drill_part}")


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


def _normalize_pin_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "undefined": "Passive",
        "passive": "Passive",
        "in": "Input",
        "input": "Input",
        "out": "Output",
        "output": "Output",
        "io": "Bidirectional",
        "bidirectional": "Bidirectional",
        "power": "Power",
        "power_pin": "Power",
        "nc": "NoConnect",
        "not_connected": "NoConnect",
    }
    return mapping.get(text, str(value or "Passive"))


def _side_from_rotation(value: float) -> str:
    rotation = int(round(value)) % 360
    return {0: "left", 90: "bottom", 180: "right", 270: "top"}.get(rotation, "left")


def _legacy_component_value(attrs: dict[str, Any], fallback: str) -> str:
    for key in ("Value", "value", "Manufacturer Part", "LCSC Part Name"):
        value = attrs.get(key)
        if value not in [None, ""]:
            return str(value)
    name = attrs.get("Name")
    if name not in [None, ""]:
        text = str(name)
        match = re.fullmatch(r"=\{([^}]+)\}", text)
        if match and attrs.get(match.group(1)) not in [None, ""]:
            return str(attrs[match.group(1)])
        if not text.startswith("={"):
            return text
    return fallback


def _guess_ref_prefix(name: str) -> str:
    upper = name.upper()
    if upper.startswith(("R", "RES")):
        return "R"
    if upper.startswith(("C", "CAP")):
        return "C"
    if "CONN" in upper or "HEADER" in upper or "USB" in upper:
        return "J"
    if upper.startswith(("D", "LED")):
        return "D"
    return "U"


def _first_attr(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in attrs and attrs[key] not in [None, ""]:
            return str(attrs[key])
    return None


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in [None, ""]:
            return mapping[key]
    return None


def _metadata_by_title(metadata: dict[str, Any], title: str) -> tuple[str, dict[str, Any]]:
    target = str(title or "").strip().casefold()
    if not target:
        return "", {}
    for key, value in metadata.items():
        if not isinstance(value, dict):
            continue
        candidate = str(value.get("title") or "").strip().casefold()
        if candidate and candidate == target:
            return str(key).strip().lower(), value
    return "", {}


def _most_common_value(rows: list[dict[str, Any]], key: str) -> str:
    values = [str(row.get(key) or "").strip() for row in rows if str(row.get(key) or "").strip()]
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def _unique_name(value: str, used: set[str], suffix: str, fallback: str) -> str:
    base = cadence_safe_name(value, fallback)
    name = base
    if name in used:
        name = cadence_safe_name(f"{base}_{suffix[-8:]}", fallback)
        counter = 2
        while name in used:
            name = cadence_safe_name(f"{base}_{suffix[-8:]}_{counter}", fallback)
            counter += 1
    used.add(name)
    return name


def _display_key(key: str) -> str:
    return str(key).strip().replace("-", "_").replace(" ", "_")


def _string_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _number_at(value: Any, index: int) -> float:
    if not isinstance(value, list) or index >= len(value):
        return 0.0
    try:
        return float(str(value[index]).strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _mil_to_mm(value: float) -> float:
    return value * LEGACY_EASYEDA_UNIT_MM
