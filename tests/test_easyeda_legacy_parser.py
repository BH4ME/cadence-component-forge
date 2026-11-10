from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.cli import main
from jlc2cadence.extractors.archive import find_design_files, prepare_input_files
from jlc2cadence.extractors.easyeda_legacy import (
    looks_like_easyeda_legacy,
    parse_easyeda_legacy_design_files,
)
from jlc2cadence.extractors.models import collect_model_assets
from jlc2cadence.ir.normalize import attach_models
from jlc2cadence.validators.library_checks import validate_library


FIXTURE = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"


class EasyEDAProLegacyParserTests(unittest.TestCase):
    def test_parse_legacy_symbol_footprint_and_device(self) -> None:
        paths = prepare_input_files(FIXTURE)
        design_files = find_design_files(paths)
        self.assertTrue(any(path.suffix.lower() == ".esym" for path in design_files))
        self.assertTrue(any(path.suffix.lower() == ".efoo" for path in design_files))
        self.assertTrue(looks_like_easyeda_legacy(FIXTURE / "legacy.esym"))

        library = parse_easyeda_legacy_design_files(paths)
        attach_models(library, collect_model_assets(FIXTURE, None))
        report = validate_library(library)

        self.assertEqual(report.errors, [])
        self.assertEqual(len(library.components), 1)
        self.assertEqual(len(library.footprints), 1)
        component = library.components[0]
        footprint = library.footprints[0]
        self.assertEqual(component.name, "LEGACY_TEST_IC")
        self.assertEqual(component.value, "legacy-value")
        self.assertEqual(component.footprint, "SOT_LEGACY_TEST")
        self.assertEqual([(pin.number, pin.name, pin.type, pin.side) for pin in component.pins], [
            ("1", "IN", "Input", "left"),
            ("2", "OUT", "Output", "right"),
        ])
        self.assertEqual(len(footprint.pads), 2)
        self.assertEqual(footprint.pads[0].shape, "rect")
        self.assertAlmostEqual(footprint.pads[0].size[0], 0.762, places=6)
        self.assertEqual(footprint.model3d.format, "step")
        self.assertEqual(footprint.model3d.transform_source, "easyeda_pro_v3")
        self.assertAlmostEqual(footprint.model3d.target_size[0], 3.0, delta=1e-5)
        self.assertEqual(footprint.properties["EasyEDA_3D_Model_Id"], "model123")

    def test_legacy_zip_is_discovered_and_inspected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="jlc2cadence_legacy_zip_test_"))
        try:
            archive = tmp / "legacy.epro"
            with zipfile.ZipFile(archive, "w") as zf:
                for name in ["project.json", "legacy.esch", "legacy.esym", "legacy.efoo", "SOT_LEGACY_TEST.step"]:
                    zf.write(FIXTURE / name, name)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["inspect", "--input", str(archive), "--models", str(archive)])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["components"], 1)
            self.assertEqual(report["footprints"], 1)
            self.assertEqual(report["models"], 1)
            self.assertEqual(report["errors"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_legacy_cli_conversion_writes_ir(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_legacy_convert_test_"))
        try:
            code = main(
                [
                    "convert",
                    "--input",
                    str(FIXTURE),
                    "--models",
                    str(FIXTURE),
                    "--out",
                    str(out_dir),
                ]
            )
            self.assertEqual(code, 0)
            ir = json.loads((out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8"))
            self.assertEqual(ir["properties"]["EasyEDA_Pro_Format"], "legacy_json_array")
            self.assertEqual(ir["components"][0]["pins"][0]["type"], "Input")
            self.assertEqual(ir["footprints"][0]["name"], "SOT_LEGACY_TEST")
            self.assertTrue((out_dir / "capture" / "library.olb.xml").exists())
            self.assertTrue((out_dir / "allegro" / "symbols" / "build_scripts" / "SOT_LEGACY_TEST.il").exists())
            self.assertEqual(
                json.loads((out_dir / "reports" / "output_validation.json").read_text(encoding="utf-8"))["status"],
                "script_ready",
            )
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
