from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ir.schema import Component, Footprint, LibraryIR, Pin


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    component_count: int = 0
    footprint_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_count": self.component_count,
            "footprint_count": self.footprint_count,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_library(library: LibraryIR) -> ValidationReport:
    report = ValidationReport(
        component_count=len(library.components),
        footprint_count=len(library.footprints),
    )
    if not library.components:
        report.errors.append("No components were parsed.")
    if not library.footprints:
        report.warnings.append("No footprints were parsed.")

    _check_duplicate_names("component", [component.name for component in library.components], report.errors)
    _check_duplicate_names("footprint", [footprint.name for footprint in library.footprints], report.errors)

    footprints = {footprint.name: footprint for footprint in library.footprints}
    for component in library.components:
        _validate_component(component, footprints, report)
    for footprint in library.footprints:
        _validate_footprint(footprint, report)

    report.warnings.extend(library.warnings)
    return report


def _validate_component(component: Component, footprints: dict[str, Footprint], report: ValidationReport) -> None:
    if not component.pins:
        report.warnings.append(f"Component {component.name} has no pins.")
    _check_duplicate_names(
        f"pin/unit in {component.name}",
        [f"{pin.unit}:{pin.number}" for pin in component.pins],
        report.errors,
    )
    if component.footprint:
        footprint = footprints.get(component.footprint)
        if footprint is None:
            report.errors.append(f"Component {component.name} references missing footprint {component.footprint}.")
        else:
            pin_numbers = component.pin_numbers()
            pad_numbers = footprint.pad_numbers(electrical_only=True)
            all_pad_numbers = footprint.pad_numbers()
            missing_pads = sorted(
                pin.number
                for pin in component.pins
                if pin.number not in pad_numbers and not _pin_can_use_non_electrical_pad(pin, all_pad_numbers)
            )
            extra_pads = sorted(pad_numbers - pin_numbers)
            if missing_pads:
                report.errors.append(f"Component {component.name} pins missing pads in {footprint.name}: {', '.join(missing_pads)}.")
            if extra_pads:
                report.warnings.append(f"Footprint {footprint.name} has pads not present as pins on {component.name}: {', '.join(extra_pads)}.")
    else:
        report.warnings.append(f"Component {component.name} has no PCB footprint property.")


def _validate_footprint(footprint: Footprint, report: ValidationReport) -> None:
    if not footprint.pads:
        report.errors.append(f"Footprint {footprint.name} has no pads.")
    _check_duplicate_names(f"electrical pad in {footprint.name}", [pad.number for pad in footprint.pads if pad.is_electrical()], report.errors)
    for pad in footprint.pads:
        if pad.size[0] <= 0 or pad.size[1] <= 0:
            report.errors.append(f"Pad {footprint.name}.{pad.number} has invalid size {pad.size}.")
        if pad.kind == "thru_hole" and pad.hole_diameter <= 0 and pad.slot_size is None:
            report.errors.append(f"Through-hole pad {footprint.name}.{pad.number} has no drill or slot size.")
        if pad.slot_size is not None and (pad.slot_size[0] <= 0 or pad.slot_size[1] <= 0):
            report.errors.append(f"Slot pad {footprint.name}.{pad.number} has invalid slot size {pad.slot_size}.")
        if not pad.padstack:
            report.warnings.append(f"Pad {footprint.name}.{pad.number} has no padstack name.")
    if footprint.model3d is None:
        report.warnings.append(f"Footprint {footprint.name} has no linked 3D model.")


def _check_duplicate_names(kind: str, names: list[str], errors: list[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    for name in sorted(duplicates):
        errors.append(f"Duplicate {kind} name/number: {name}.")


def _pin_can_use_non_electrical_pad(pin: Pin, all_pad_numbers: set[str]) -> bool:
    number = str(pin.number or "").strip()
    if number not in all_pad_numbers:
        return False
    key = number.upper().replace("-", "_")
    return key.startswith(("SHELL", "SHIELD"))
