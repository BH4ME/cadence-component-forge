from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Pin:
    number: str
    name: str
    type: str = "Passive"
    unit: str = "A"
    hidden: bool = False
    x: float = 0.0
    y: float = 0.0
    side: str = "left"
    length: float = 0.0
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "type": self.type,
            "unit": self.unit,
            "hidden": self.hidden,
            "x": self.x,
            "y": self.y,
            "side": self.side,
            "length": self.length,
            "properties": dict(self.properties),
        }


@dataclass
class Component:
    name: str
    ref_prefix: str = "U"
    value: str = ""
    footprint: str = ""
    description: str = ""
    pins: list[Pin] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    graphics: list[Graphic] = field(default_factory=list)

    def add_pin(self, pin: Pin) -> None:
        for idx, existing in enumerate(self.pins):
            if existing.number == pin.number and existing.unit == pin.unit:
                self.pins[idx] = pin
                return
        self.pins.append(pin)

    def pin_numbers(self) -> set[str]:
        return {pin.number for pin in self.pins}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ref_prefix": self.ref_prefix,
            "value": self.value,
            "footprint": self.footprint,
            "description": self.description,
            "pins": [pin.to_dict() for pin in self.pins],
            "graphics": [graphic.to_dict() for graphic in self.graphics],
            "properties": dict(self.properties),
            "source_file": self.source_file,
        }


@dataclass
class Pad:
    number: str
    x: float
    y: float
    rotation: float = 0.0
    kind: str = "smd"
    shape: str = "rect"
    size: tuple[float, float] = (0.0, 0.0)
    hole_diameter: float = 0.0
    slot_size: tuple[float, float] | None = None
    layers: list[str] = field(default_factory=lambda: ["top"])
    padstack: str = ""
    role: str = "electrical"
    properties: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        role = str(self.role or "electrical").strip().lower()
        valid_roles = {"electrical", "mechanical", "mounting", "npth", "shield"}
        self.role = role if role in valid_roles else "electrical"

    def is_electrical(self) -> bool:
        return self.role == "electrical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "kind": self.kind,
            "shape": self.shape,
            "size": list(self.size),
            "hole_diameter": self.hole_diameter,
            "slot_size": list(self.slot_size) if self.slot_size else None,
            "layers": list(self.layers),
            "padstack": self.padstack,
            "role": self.role,
            "properties": dict(self.properties),
        }


@dataclass
class Graphic:
    kind: str
    layer: str
    points: list[tuple[float, float]] = field(default_factory=list)
    width: float = 0.0
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "layer": self.layer,
            "points": [list(point) for point in self.points],
            "width": self.width,
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "properties": dict(self.properties),
        }


@dataclass
class Model3D:
    file: str
    source_file: str = ""
    format: str = ""
    key: str = ""
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    target_size: tuple[float, float, float] | None = None
    transform_source: str = ""
    units: str = "mm"
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "source_file": self.source_file,
            "format": self.format,
            "key": self.key,
            "offset": list(self.offset),
            "rotation": list(self.rotation),
            "target_size": list(self.target_size) if self.target_size is not None else None,
            "transform_source": self.transform_source,
            "units": self.units,
            "properties": dict(self.properties),
        }


@dataclass
class Footprint:
    name: str
    units: str = "mm"
    origin: tuple[float, float] = (0.0, 0.0)
    pads: list[Pad] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    description: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    model3d: Model3D | None = None
    source_file: str = ""

    def add_pad(self, pad: Pad) -> None:
        if pad.is_electrical():
            for idx, existing in enumerate(self.pads):
                if existing.is_electrical() and existing.number == pad.number:
                    self.pads[idx] = pad
                    return
        self.pads.append(pad)

    def pad_numbers(self, electrical_only: bool = False) -> set[str]:
        return {pad.number for pad in self.pads if not electrical_only or pad.is_electrical()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "units": self.units,
            "origin": list(self.origin),
            "pads": [pad.to_dict() for pad in self.pads],
            "graphics": [graphic.to_dict() for graphic in self.graphics],
            "description": self.description,
            "properties": dict(self.properties),
            "model3d": self.model3d.to_dict() if self.model3d else None,
            "source_file": self.source_file,
        }


@dataclass
class LibraryIR:
    components: list[Component] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def upsert_component(self, component: Component) -> None:
        for idx, existing in enumerate(self.components):
            if existing.name == component.name:
                merged = _merge_component(existing, component)
                self.components[idx] = merged
                return
        self.components.append(component)

    def upsert_footprint(self, footprint: Footprint) -> None:
        for idx, existing in enumerate(self.footprints):
            if existing.name == footprint.name:
                merged = _merge_footprint(existing, footprint)
                self.footprints[idx] = merged
                return
        self.footprints.append(footprint)

    def footprint_by_name(self, name: str) -> Footprint | None:
        for footprint in self.footprints:
            if footprint.name == name:
                return footprint
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": [component.to_dict() for component in self.components],
            "footprints": [footprint.to_dict() for footprint in self.footprints],
            "source_files": list(dict.fromkeys(self.source_files)),
            "properties": dict(self.properties),
            "warnings": list(self.warnings),
        }


def _merge_component(left: Component, right: Component) -> Component:
    if right.ref_prefix:
        left.ref_prefix = right.ref_prefix
    if right.value:
        left.value = right.value
    if right.footprint:
        left.footprint = right.footprint
    if right.description:
        left.description = right.description
    if right.source_file:
        left.source_file = right.source_file
    left.properties.update(right.properties)
    for pin in right.pins:
        left.add_pin(pin)
    left.graphics.extend(right.graphics)
    return left


def _merge_footprint(left: Footprint, right: Footprint) -> Footprint:
    if right.description:
        left.description = right.description
    if right.source_file:
        left.source_file = right.source_file
    left.properties.update(right.properties)
    for pad in right.pads:
        left.add_pad(pad)
    left.graphics.extend(right.graphics)
    if right.model3d:
        left.model3d = right.model3d
    return left
