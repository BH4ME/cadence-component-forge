from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.cli import main
from jlc2cadence.extractors.archive import find_design_files, prepare_input_files
from jlc2cadence.extractors.pads_ascii import looks_like_pads_ascii, parse_pads_design_files
from jlc2cadence.ir.normalize import ensure_components_for_footprints
from jlc2cadence.validators.library_checks import validate_library


FIXTURE = ROOT / "tests" / "fixtures" / "pads_ascii"


class PadsParserTests(unittest.TestCase):
    def test_parse_pads_decal_and_part_type(self) -> None:
        paths = prepare_input_files(FIXTURE)
        design_files = find_design_files(paths)
        self.assertEqual({path.suffix.lower() for path in design_files}, {".d", ".p"})
        self.assertTrue(all(looks_like_pads_ascii(path) for path in design_files))

        library = parse_pads_design_files(design_files)
        ensure_components_for_footprints(library)
        report = validate_library(library)

        self.assertFalse(report.errors, report.errors)
        self.assertEqual(len(library.components), 1)
        self.assertEqual(len(library.footprints), 1)
        component = library.components[0]
        footprint = library.footprints[0]
        self.assertEqual(component.name, "R_DEMO")
        self.assertEqual(component.value, "10k")
        self.assertEqual(component.footprint, "RES_0603")
        self.assertEqual([pin.number for pin in component.pins], ["1", "2"])
        self.assertEqual(footprint.name, "RES_0603")
        self.assertEqual([(pad.number, pad.x, pad.y) for pad in footprint.pads], [("1", -1.0, 0.0), ("2", 1.0, 0.0)])
        self.assertEqual(footprint.pads[0].size, (1.05, 0.95))
        self.assertEqual(len(footprint.graphics), 1)

    def test_cli_convert_pads_fixture(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_pads_test_"))
        try:
            code = main(["convert", "--input", str(FIXTURE), "--models", str(FIXTURE), "--out", str(out_dir)])
            self.assertEqual(code, 0)
            ir = json.loads((out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8"))
            self.assertEqual(ir["components"][0]["name"], "R_DEMO")
            self.assertEqual(ir["components"][0]["footprint"], "RES_0603")
            self.assertEqual(ir["footprints"][0]["model3d"]["format"], "step")
            self.assertEqual(json.loads((out_dir / "reports" / "3d_validation.json").read_text(encoding="utf-8"))["errors"], [])
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
