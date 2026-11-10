from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.extractors.altium_ascii import parse_altium_design_files, parse_record_line
from jlc2cadence.extractors.archive import find_design_files, prepare_input_files
from jlc2cadence.ir.normalize import ensure_components_for_footprints
from jlc2cadence.validators.library_checks import validate_library


FIXTURE = ROOT / "tests" / "fixtures" / "demo_altium"
SLOT_FIXTURE = ROOT / "tests" / "fixtures" / "through_hole_slot"


class AltiumParserTests(unittest.TestCase):
    def test_parse_record_line(self) -> None:
        record = parse_record_line("|RECORD=Pin|PinNumber=1|PinName=GND|PinType=Passive|")
        self.assertEqual(record["RECORD"], "Pin")
        self.assertEqual(record["PINNUMBER"], "1")
        self.assertEqual(record["PINNAME"], "GND")

    def test_parse_demo_library(self) -> None:
        paths = find_design_files(prepare_input_files(FIXTURE))
        library = parse_altium_design_files(paths)
        ensure_components_for_footprints(library)
        report = validate_library(library)
        self.assertFalse(report.errors, report.errors)
        self.assertEqual(len(library.components), 1)
        self.assertEqual(len(library.footprints), 1)
        component = library.components[0]
        footprint = library.footprints[0]
        self.assertEqual(component.footprint, "RES_0603")
        self.assertEqual(len(component.pins), 2)
        self.assertEqual(len(footprint.pads), 2)

    def test_parse_through_hole_slot_library(self) -> None:
        paths = find_design_files(prepare_input_files(SLOT_FIXTURE))
        library = parse_altium_design_files(paths)
        ensure_components_for_footprints(library)
        report = validate_library(library)
        self.assertFalse(report.errors, report.errors)
        self.assertEqual(len(library.components), 1)
        self.assertEqual(len(library.footprints), 1)
        component = library.components[0]
        footprint = library.footprints[0]
        self.assertEqual(component.footprint, "CONN_SLOT")
        self.assertEqual(len(component.pins), 2)
        self.assertEqual(len(footprint.pads), 2)
        pad1, pad2 = footprint.pads
        self.assertEqual(pad1.kind, "thru_hole")
        self.assertEqual(pad1.hole_diameter, 0.8)
        self.assertEqual(pad1.properties.get("plated"), "true")
        self.assertEqual(pad2.kind, "thru_hole")
        self.assertEqual(pad2.slot_size, (0.8, 1.8))
        self.assertEqual(pad2.properties.get("plated"), "false")


if __name__ == "__main__":
    unittest.main()
