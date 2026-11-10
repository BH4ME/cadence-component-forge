from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.ir.schema import Component, Footprint, LibraryIR, Pad, Pin
from jlc2cadence.validators.library_checks import validate_library


class ValidationTests(unittest.TestCase):
    def test_pin_pad_mismatch_is_error(self) -> None:
        library = LibraryIR(
            components=[
                Component(
                    name="U_DEMO",
                    footprint="PKG_DEMO",
                    pins=[Pin(number="1", name="A"), Pin(number="2", name="B")],
                )
            ],
            footprints=[
                Footprint(
                    name="PKG_DEMO",
                    pads=[Pad(number="1", x=0, y=0, size=(1, 1), padstack="SMD_RECT")],
                )
            ],
        )
        report = validate_library(library)
        self.assertTrue(any("missing pads" in error for error in report.errors), report.errors)

    def test_mechanical_pads_do_not_trigger_extra_pin_warning(self) -> None:
        library = LibraryIR(
            components=[
                Component(
                    name="J_USB",
                    footprint="USB_C",
                    pins=[Pin(number="A1", name="GND"), Pin(number="A4", name="VBUS")],
                )
            ],
            footprints=[
                Footprint(
                    name="USB_C",
                    pads=[
                        Pad(number="A1", x=0, y=0, size=(0.3, 0.7), padstack="SMD_RECT"),
                        Pad(number="A4", x=0.5, y=0, size=(0.3, 0.7), padstack="SMD_RECT"),
                        Pad(number="SHELL1", x=-1, y=0, size=(1.2, 1.5), padstack="SHIELD_TAB", role="shield"),
                        Pad(
                            number="NPTH1",
                            x=1,
                            y=0,
                            kind="thru_hole",
                            shape="circle",
                            size=(0.7, 0.7),
                            hole_diameter=0.7,
                            padstack="NPTH_HOLE",
                            role="npth",
                            properties={"plated": "false"},
                        ),
                    ],
                )
            ],
        )
        report = validate_library(library)
        self.assertEqual(report.errors, [])
        self.assertFalse(any("pads not present as pins" in warning for warning in report.warnings), report.warnings)

    def test_shield_pins_can_match_non_electrical_shield_pads(self) -> None:
        library = LibraryIR(
            components=[
                Component(
                    name="HR911130A",
                    footprint="RJ45_TH",
                    pins=[
                        Pin(number="1", name="TX+"),
                        Pin(number="2", name="TX-"),
                        Pin(number="SHIELD0", name="SHIELD"),
                        Pin(number="SHIELD1", name="SHIELD"),
                    ],
                )
            ],
            footprints=[
                Footprint(
                    name="RJ45_TH",
                    pads=[
                        Pad(number="1", x=0, y=0, size=(1, 1), hole_diameter=0.6, padstack="TH_ROUND", kind="thru_hole"),
                        Pad(number="2", x=2, y=0, size=(1, 1), hole_diameter=0.6, padstack="TH_ROUND", kind="thru_hole"),
                        Pad(
                            number="SHIELD0",
                            x=-5,
                            y=0,
                            size=(1.8, 1.8),
                            hole_diameter=1.2,
                            padstack="SHIELD_TH",
                            kind="thru_hole",
                            role="shield",
                        ),
                        Pad(
                            number="SHIELD1",
                            x=5,
                            y=0,
                            size=(1.8, 1.8),
                            hole_diameter=1.2,
                            padstack="SHIELD_TH",
                            kind="thru_hole",
                            role="shield",
                        ),
                    ],
                )
            ],
        )
        report = validate_library(library)
        self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
