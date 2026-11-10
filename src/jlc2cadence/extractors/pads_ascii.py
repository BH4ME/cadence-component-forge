from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, Graphic, LibraryIR, Pad, Pin
from ..ir.units import parse_length


PADS_HEADERS = {
    "*PADS-LIBRARY-PCB-DECALS-V9*",
    "*PADS-LIBRARY-PART-TYPES-V9*",
    "*PADS-LIBRARY-SCH-DECALS-V9*",
    "*PADS-LIBRARY-LINE-ITEMS-V9*",
}


@dataclass
class _PadStackSpec:
    pin: str
    layers: int
    plated: str
    drill: float
    unit: str = "mm"
    slot_length: float = 0.0
    layer_rows: list[list[str]] = field(default_factory=list)


def looks_like_pads_ascii(path: Path) -> bool:
    try:
        sample = _read_text(path)[:8192].upper()
    except OSError:
        return False
    return any(header in sample for header in PADS_HEADERS)


def parse_pads_design_files(paths: Iterable[Path], default_unit: str = "mm") -> LibraryIR:
    parser = PadsAsciiParser(default_unit=default_unit)
    for path in paths:
        parser.parse_file(path)
    parser.infer_non_schematic_pad_roles()
    return parser.library


class PadsAsciiParser:
    def __init__(self, default_unit: str = "mm") -> None:
        self.default_unit = default_unit
        self.library = LibraryIR()

    def parse_file(self, path: Path) -> None:
        lines = _clean_lines(path)
        if not any(line.upper() in PADS_HEADERS for line in lines):
            return
        self.library.source_files.append(str(path))
        idx = 0
        while idx < len(lines):
            header = lines[idx].upper()
            if header == "*PADS-LIBRARY-PCB-DECALS-V9*":
                idx = self._parse_pcb_decals(lines, idx + 1, path)
            elif header == "*PADS-LIBRARY-PART-TYPES-V9*":
                idx = self._parse_part_types(lines, idx + 1, path)
            else:
                idx += 1

    def infer_non_schematic_pad_roles(self) -> None:
        footprints = {footprint.name: footprint for footprint in self.library.footprints}
        for component in self.library.components:
            footprint = footprints.get(component.footprint)
            if footprint is None or not _looks_like_connector_component(component):
                continue
            pin_numbers = component.pin_numbers()
            for pad in footprint.pads:
                if not pad.is_electrical() or pad.number in pin_numbers:
                    continue
                pad.role = "shield" if pad.kind == "smd" else _through_hole_retention_role(pad)
                pad.properties.setdefault("inferred_role", "connector_retention")
                pad.padstack = _padstack_name(pad.kind, pad.shape, pad.size[0], pad.size[1], pad.hole_diameter, pad.slot_size, pad.role, pad.properties.get("plated", "true"))

    def _parse_pcb_decals(self, lines: list[str], idx: int, path: Path) -> int:
        while idx < len(lines):
            line = lines[idx]
            upper = line.upper()
            if upper in PADS_HEADERS:
                return idx
            if upper == "*END*":
                return idx + 1
            tokens = _split_tokens(line)
            if len(tokens) < 2:
                idx += 1
                continue

            footprint, consumed = self._footprint_from_pcb_decal(lines, idx, path)
            self.library.upsert_footprint(footprint)
            idx = consumed
        return idx

    def _footprint_from_pcb_decal(self, lines: list[str], start: int, path: Path) -> tuple[Footprint, int]:
        header = _split_tokens(lines[start])
        name = cadence_safe_name(header[0] if header else path.stem)
        unit = _pads_unit(header[1] if len(header) > 1 else self.default_unit)
        footprint = Footprint(name=name, units=unit, source_file=str(path))
        footprint.properties["PADS_Source"] = "PCB Decal"

        idx = start + 1
        terminals: list[tuple[str, float, float, float]] = []
        padstacks: dict[str, _PadStackSpec] = {}
        default_padstack: _PadStackSpec | None = None
        while idx < len(lines):
            line = lines[idx]
            upper = line.upper()
            if upper in PADS_HEADERS or upper == "*END*":
                break
            tokens = _split_tokens(line)
            if not tokens:
                idx += 1
                continue
            keyword = tokens[0].upper()
            if keyword == "TIMESTAMP":
                footprint.properties["PADS_Timestamp"] = " ".join(tokens[1:])
                idx += 1
            elif keyword == "T" or keyword.startswith("T"):
                terminal = _terminal_from_tokens(tokens, unit)
                if terminal:
                    terminals.append(terminal)
                idx += 1
            elif keyword == "PAD":
                spec, idx = _padstack_from_lines(lines, idx, unit)
                if spec.pin == "0":
                    default_padstack = spec
                else:
                    padstacks[spec.pin] = spec
            elif keyword in {"OPEN", "CLOSED"}:
                graphic, idx = _piece_from_lines(tokens, lines, idx, unit)
                if graphic:
                    footprint.graphics.append(graphic)
            else:
                idx += 1

        for pin, x, y, rotation in terminals:
            spec = padstacks.get(pin) or default_padstack or _default_padstack(pin)
            footprint.add_pad(_pad_from_terminal(pin, x, y, rotation, spec))

        return footprint, idx

    def _parse_part_types(self, lines: list[str], idx: int, path: Path) -> int:
        while idx < len(lines):
            line = lines[idx]
            upper = line.upper()
            if upper in PADS_HEADERS:
                return idx
            if upper == "*END*":
                return idx + 1
            tokens = _split_tokens(line)
            if len(tokens) < 2:
                idx += 1
                continue

            component, consumed = self._component_from_part_type(lines, idx, path)
            self.library.upsert_component(component)
            idx = consumed
        return idx

    def _component_from_part_type(self, lines: list[str], start: int, path: Path) -> tuple[Component, int]:
        header = _split_tokens(lines[start])
        name = cadence_safe_name(header[0] if header else path.stem)
        footprint = cadence_safe_name((header[1] if len(header) > 1 else "").split(":")[0])
        unit = _pads_unit(header[2] if len(header) > 2 else self.default_unit)
        attr_count = _int_at(header, 4)
        gate_count = _int_at(header, 5)
        sigpin_count = _int_at(header, 6)
        pinmap_count = _int_at(header, 7)
        component = Component(
            name=name,
            ref_prefix=_guess_ref_prefix(name),
            value=name,
            footprint=footprint,
            source_file=str(path),
        )
        component.properties["PADS_Source"] = "Part Type"
        component.properties["PADS_Unit"] = unit

        idx = start + 1
        if idx < len(lines) and lines[idx].upper().startswith("TIMESTAMP"):
            component.properties["PADS_Timestamp"] = " ".join(_split_tokens(lines[idx])[1:])
            idx += 1

        for _ in range(attr_count):
            if idx >= len(lines):
                break
            tokens = _split_tokens(lines[idx])
            if len(tokens) >= 2:
                key = tokens[0].strip('"')
                value = " ".join(tokens[1:])
                component.properties[_display_key(key)] = value
                if key.upper() in {"VALUE", "PART VALUE", "PART_VALUE"}:
                    component.value = value
                if key.upper() in {"DESCRIPTION", "DESC"}:
                    component.description = value
            idx += 1

        for _ in range(gate_count):
            if idx >= len(lines):
                break
            tokens = _split_tokens(lines[idx])
            if not tokens or tokens[0].upper() not in {"GATE", "CONN"}:
                break
            idx = self._read_gate(lines, idx, component)

        for _ in range(sigpin_count):
            if idx >= len(lines):
                break
            tokens = _split_tokens(lines[idx])
            if tokens and tokens[0].upper() == "SIGPIN" and len(tokens) >= 3:
                component.properties[f"Standard_Net_{tokens[1]}"] = tokens[2]
            idx += 1

        if pinmap_count > 0 and idx < len(lines):
            mapped = _split_tokens(lines[idx])
            for position, pin_number in enumerate(mapped, start=1):
                component.properties[f"PADS_Pin_Map_{position}"] = pin_number
            idx += 1

        return component, idx

    def _read_gate(self, lines: list[str], idx: int, component: Component) -> int:
        header = _split_tokens(lines[idx])
        keyword = header[0].upper()
        decal_count = _int_at(header, 1)
        pin_count = _int_at(header, 2)
        idx += 1

        gate_decals: list[str] = []
        for _ in range(decal_count):
            if idx >= len(lines):
                break
            tokens = _split_tokens(lines[idx])
            if tokens:
                gate_decals.append(tokens[0])
            idx += 1
        if gate_decals:
            component.properties["PADS_Gate_Decals"] = ":".join(gate_decals)

        if keyword == "CONN":
            pin_type = _pads_pin_type(header[3] if len(header) > 3 else "U")
            for _ in range(pin_count):
                if idx >= len(lines):
                    break
                tokens = _split_tokens(lines[idx])
                if tokens:
                    number = tokens[0]
                    component.add_pin(Pin(number=number, name=number, type=pin_type))
                idx += 1
            return idx

        for _ in range(pin_count):
            if idx >= len(lines):
                break
            tokens = _split_tokens(lines[idx])
            if len(tokens) >= 3:
                number = tokens[0]
                pin_type = _pads_pin_type(tokens[2])
                name = tokens[3] if len(tokens) >= 4 else number
                component.add_pin(Pin(number=number, name=name, type=pin_type))
            idx += 1
        return idx


def _clean_lines(path: Path) -> list[str]:
    text = _read_text(path)
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("!") or line.startswith("#"):
            continue
        lines.append(line)
    return lines


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


def _split_tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _pads_unit(token: str) -> str:
    text = token.strip().upper()
    if text == "I":
        return "mil"
    if text == "M":
        return "mm"
    return text.lower() if text else "mm"


def _pads_length(value: str | float | int | None, unit: str) -> float:
    return parse_length(value, "mil" if unit == "mil" else "mm")


def _terminal_from_tokens(tokens: list[str], unit: str) -> tuple[str, float, float, float] | None:
    if not tokens:
        return None
    if tokens[0].upper() == "T":
        if len(tokens) < 6:
            return None
        x = _pads_length(tokens[1], unit)
        y = _pads_length(tokens[2], unit)
        rotation = _float_at(tokens, 3)
        pin = tokens[5]
    elif tokens[0].upper().startswith("T"):
        if len(tokens) < 5:
            return None
        x = _pads_length(tokens[1], unit)
        y = _pads_length(tokens[2], unit)
        rotation = 0.0
        pin = tokens[-1]
    else:
        return None
    return pin, x, y, rotation


def _padstack_from_lines(lines: list[str], idx: int, unit: str) -> tuple[_PadStackSpec, int]:
    header = _split_tokens(lines[idx])
    pin = header[1] if len(header) > 1 else "0"
    layers = _int_at(header, 2)
    plated = _normalize_plating(header[3] if len(header) > 3 else "N")
    drill = _pads_length(header[4] if len(header) > 4 else "0", unit)
    slot_length = _pads_length(header[6] if len(header) > 6 else "0", unit)
    spec = _PadStackSpec(
        pin=pin,
        layers=layers,
        plated=plated,
        drill=drill,
        unit=unit,
        slot_length=slot_length,
    )
    idx += 1
    for _ in range(max(layers, 0)):
        if idx >= len(lines):
            break
        tokens = _split_tokens(lines[idx])
        if tokens and tokens[0].upper() not in PADS_HEADERS and tokens[0].upper() != "*END*":
            spec.layer_rows.append(tokens)
            idx += 1
    return spec, idx


def _piece_from_lines(tokens: list[str], lines: list[str], idx: int, unit: str) -> tuple[Graphic | None, int]:
    point_count = _int_at(tokens, 1)
    width = _pads_length(tokens[2] if len(tokens) > 2 else "0", unit)
    layer = _map_pads_layer(tokens[3] if len(tokens) > 3 else "21")
    points: list[tuple[float, float]] = []
    idx += 1
    for _ in range(max(point_count, 0)):
        if idx >= len(lines):
            break
        row = _split_tokens(lines[idx])
        if len(row) >= 2:
            points.append((_pads_length(row[0], unit), _pads_length(row[1], unit)))
        idx += 1
    if len(points) < 2:
        return None, idx
    return Graphic(kind="line", layer=layer, points=points, width=width), idx


def _pad_from_terminal(pin: str, x: float, y: float, rotation: float, spec: _PadStackSpec) -> Pad:
    size_x, size_y, shape, layers = _pad_geometry_from_spec(spec)
    slot_size = None
    if spec.slot_length > 0 and spec.drill > 0:
        slot_size = (spec.drill, spec.slot_length)
    kind = "thru_hole" if spec.drill > 0 or slot_size else "smd"
    role = _pad_role_from_number(pin, spec)
    padstack = _padstack_name(kind, shape, size_x, size_y, spec.drill, slot_size, role, spec.plated)
    return Pad(
        number=pin,
        x=x,
        y=y,
        rotation=rotation,
        kind=kind,
        shape=shape,
        size=(size_x, size_y),
        hole_diameter=spec.drill,
        slot_size=slot_size,
        layers=layers,
        padstack=padstack,
        role=role,
        properties={"plated": spec.plated} if kind == "thru_hole" else {},
    )


def _padstack_name(
    kind: str,
    shape: str,
    size_x: float,
    size_y: float,
    drill: float,
    slot_size: tuple[float, float] | None,
    role: str = "electrical",
    plated: str = "true",
) -> str:
    drill_part = (
        f"slot_{slot_size[0]:.3f}x{slot_size[1]:.3f}"
        if slot_size
        else f"{drill:.3f}"
    )
    qualifiers: list[str] = []
    if role != "electrical":
        qualifiers.append(role)
    if kind == "thru_hole" and str(plated).strip().lower() == "false" and "np" not in role:
        qualifiers.append("nonplated")
    prefix = "_".join(qualifiers + [kind])
    return cadence_safe_name(f"{prefix}_{shape}_{size_x:.3f}x{size_y:.3f}_{drill_part}")


def _pad_role_from_number(pin: str, spec: _PadStackSpec) -> str:
    number = str(pin).strip().upper().replace("-", "_")
    if number in {"", "0", "?"} and spec.drill > 0:
        return "npth" if spec.plated == "false" else "mechanical"
    if number.startswith(("NPTH", "MOUNT", "MNT", "MH", "MP", "TOOL", "LOC", "LOCK")):
        return "npth" if spec.plated == "false" else "mounting"
    if number.startswith(("SHELL", "SHIELD")):
        return "shield"
    return "electrical"


def _through_hole_retention_role(pad: Pad) -> str:
    if str(pad.properties.get("plated", "true")).strip().lower() == "false":
        return "npth"
    return "mounting"


def _pad_geometry_from_spec(spec: _PadStackSpec) -> tuple[float, float, str, list[str]]:
    if not spec.layer_rows:
        diameter = spec.drill if spec.drill > 0 else 0.5
        return diameter, diameter, "circle", ["top", "bottom"] if spec.drill > 0 else ["top"]
    row = spec.layer_rows[0]
    layer = row[0] if row else "-2"
    width = _pads_length(row[1] if len(row) > 1 else "0.5", spec.unit)
    shape_code = row[2].upper() if len(row) > 2 else "R"
    shape = _pads_shape(shape_code)
    length = _pads_length(
        row[5] if len(row) > 5 else row[1] if len(row) > 1 else "0.5",
        spec.unit,
    )
    if shape in {"oval", "rect"} and length > width:
        size_x, size_y = length, width
    else:
        size_x = width
        size_y = width if shape == "circle" else length
    layers = _pads_layers(layer, spec)
    return size_x, size_y, shape, layers


def _default_padstack(pin: str) -> _PadStackSpec:
    return _PadStackSpec(
        pin=pin,
        layers=1,
        plated="false",
        unit="mm",
        drill=0.0,
        layer_rows=[["-2", "0.5", "S"]],
    )


def _pads_shape(shape_code: str) -> str:
    mapping = {
        "R": "circle",
        "S": "rect",
        "A": "circle",
        "OF": "oval",
        "RF": "rect",
        "RT": "circle",
        "ST": "rect",
    }
    return mapping.get(shape_code, "rect")


def _pads_layers(layer: str, spec: _PadStackSpec) -> list[str]:
    if spec.drill > 0:
        return ["top", "bottom"]
    if layer in {"0", "-0"}:
        return ["bottom"]
    return ["top"]


def _map_pads_layer(layer: str) -> str:
    text = str(layer).strip().upper()
    if text in {"21", "25", "TOP", "-2"}:
        return "silkscreen_top"
    if text in {"22", "26", "BOTTOM", "-0"}:
        return "silkscreen_bottom"
    if text in {"23", "ASSEMBLY"}:
        return "assembly_top"
    return f"pads_layer_{text.lower()}"


def _pads_pin_type(code: str) -> str:
    mapping = {
        "S": "Output",
        "B": "Bidirectional",
        "C": "OpenCollector",
        "O": "Output",
        "T": "TriState",
        "L": "Input",
        "Z": "Passive",
        "P": "Power",
        "G": "Power",
        "U": "Passive",
    }
    return mapping.get(code.strip().upper(), "Passive")


def _normalize_plating(value: str) -> str:
    return "true" if value.strip().upper().startswith("P") else "false"


def _guess_ref_prefix(name: str) -> str:
    upper = name.upper()
    if upper.startswith(("R", "RES")):
        return "R"
    if upper.startswith(("C", "CAP")):
        return "C"
    if "CONN" in upper or "USB" in upper or "FPC" in upper:
        return "J"
    return "U"


def _looks_like_connector_component(component: Component) -> bool:
    text = " ".join(
        [
            component.name,
            component.ref_prefix,
            component.footprint,
            component.description,
            *component.properties.values(),
        ]
    ).upper()
    return any(
        token in text
        for token in [
            "CONN",
            "CONNECTOR",
            "HEADER",
            "MOLEX",
            "JST",
            "USB",
            "FPC",
            "FFC",
            "SOCKET",
            "RECEPTACLE",
        ]
    )


def _display_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_").title().replace("_", "_")


def _int_at(tokens: list[str], index: int, default: int = 0) -> int:
    if index >= len(tokens):
        return default
    try:
        return int(float(tokens[index]))
    except ValueError:
        return default


def _float_at(tokens: list[str], index: int, default: float = 0.0) -> float:
    if index >= len(tokens):
        return default
    try:
        return float(tokens[index])
    except ValueError:
        return default
