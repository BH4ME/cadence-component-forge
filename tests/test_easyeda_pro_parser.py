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
from jlc2cadence.extractors.easyeda_pro import looks_like_easyeda_pro_jsonl, parse_easyeda_pro_design_files
from jlc2cadence.ir.normalize import (
    attach_models,
    ensure_components_for_footprints,
    model_match_candidates,
)
from jlc2cadence.ir.schema import Footprint, LibraryIR, Model3D
from jlc2cadence.extractors.models import collect_model_assets
from jlc2cadence.reports.repair_kit import _linked_alias_keys
from jlc2cadence.validators.library_checks import validate_library


FIXTURE = ROOT / "tests" / "fixtures" / "easyeda_pro_v3"


class EasyEDAProParserTests(unittest.TestCase):
    def test_parse_easyeda_pro_v3_device_symbol_and_footprint(self) -> None:
        paths = find_design_files(prepare_input_files(FIXTURE))
        self.assertEqual({path.suffix.lower() for path in paths}, {".epru"})
        self.assertTrue(looks_like_easyeda_pro_jsonl(paths[0]))

        library = parse_easyeda_pro_design_files(paths)
        attach_models(library, collect_model_assets(FIXTURE, None))
        ensure_components_for_footprints(library)
        report = validate_library(library)

        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(len(library.components), 1)
        self.assertEqual(len(library.footprints), 1)
        component = library.components[0]
        footprint = library.footprints[0]
        self.assertEqual(component.name, "PRO_DEVICE")
        self.assertEqual(component.ref_prefix, "U")
        self.assertEqual(component.value, "pro-value")
        self.assertEqual(component.footprint, "SOT_23_3_PRO")
        self.assertEqual([(pin.number, pin.name, pin.type, pin.hidden, pin.side) for pin in component.pins], [
            ("1", "IN", "Input", False, "left"),
            ("2", "OUT", "Output", False, "right"),
            ("3", "VCC", "Power", True, "top"),
        ])
        self.assertEqual(len(footprint.pads), 4)
        self.assertEqual({pad.number for pad in footprint.pads if pad.is_electrical()}, {"1", "2", "3"})
        npth = [pad for pad in footprint.pads if pad.role == "npth"]
        self.assertEqual(len(npth), 1)
        self.assertEqual(npth[0].number, "NPTH1")
        self.assertEqual(npth[0].properties["plated"], "false")
        self.assertEqual(footprint.model3d.format, "step")
        self.assertEqual(footprint.model3d.transform_source, "easyeda_pro_v3")
        self.assertAlmostEqual(footprint.model3d.target_size[0], 3.0, delta=1e-5)
        self.assertAlmostEqual(footprint.model3d.target_size[1], 1.8, delta=1e-5)
        self.assertAlmostEqual(footprint.model3d.target_size[2], 0.0, delta=1e-8)
        self.assertEqual(footprint.model3d.rotation, (0.0, 0.0, 90.0))
        self.assertAlmostEqual(footprint.model3d.offset[0], 0.00254, places=7)
        self.assertAlmostEqual(footprint.model3d.offset[1], -0.00508, places=7)
        self.assertAlmostEqual(footprint.model3d.offset[2], 0.00762, places=7)
        self.assertEqual(footprint.properties["Model3D"], "SOT_23_3_PRO")
        self.assertEqual(footprint.properties["EasyEDA_3D_Model_Id"], "model-pro")
        self.assertEqual(footprint.properties["EasyEDA_3D_Model_Transform"], "118.11,70.866,0,90,0,0,0.1,-0.2,0.3")
        self.assertEqual(footprint.properties["EasyEDA_3D_Model_Transform_Status"], "parsed")

    def test_attach_models_by_easyeda_model_id_without_title(self) -> None:
        paths = find_design_files(prepare_input_files(FIXTURE))
        library = parse_easyeda_pro_design_files(paths)
        footprint = library.footprints[0]
        footprint.properties.pop("Model3D", None)
        footprint.properties.pop("EasyEDA_3D_Model_Title", None)
        model = Model3D(
            file="model-pro.step",
            source_file="model-pro.step",
            format="step",
            key="model-pro",
        )

        attach_models(library, [model])

        self.assertIsNotNone(footprint.model3d)
        self.assertEqual(footprint.model3d.file, "model-pro.step")
        self.assertEqual(footprint.model3d.transform_source, "easyeda_pro_v3")

    def test_attach_models_by_easyeda_composite_model_id_part(self) -> None:
        paths = find_design_files(prepare_input_files(FIXTURE))
        library = parse_easyeda_pro_design_files(paths)
        footprint = library.footprints[0]
        footprint.properties["EasyEDA_3D_Model_Id"] = "model-pro|workspace-456"
        footprint.properties.pop("Model3D", None)
        footprint.properties.pop("EasyEDA_3D_Model_Title", None)
        model = Model3D(
            file="model-pro.step",
            source_file="model-pro.step",
            format="step",
            key="model-pro",
        )

        attach_models(library, [model])

        self.assertIsNotNone(footprint.model3d)
        self.assertEqual(footprint.model3d.file, "model-pro.step")
        candidates = model_match_candidates(footprint)
        self.assertIn("modelpro", [candidate["key"] for candidate in candidates])
        self.assertNotIn("workspace456", [candidate["key"] for candidate in candidates])
        self.assertIn(
            "EasyEDA_3D_Model_Id:part",
            [candidate["source"] for candidate in candidates],
        )

    def test_workspace_part_alias_does_not_cross_bind_composite_models(self) -> None:
        library = LibraryIR(
            footprints=[
                Footprint(
                    name="TARGET_PACKAGE",
                    properties={
                        "EasyEDA_3D_Model_Id": "target-model|workspace-456",
                    },
                )
            ]
        )
        unrelated = Model3D(
            file="unrelated.step",
            source_file="unrelated.step",
            format="step",
            key="unrelated",
        )

        attach_models(library, [unrelated], {"workspace456": "unrelated.step"})

        self.assertIsNone(library.footprints[0].model3d)

    def test_repair_kit_alias_keys_exclude_workspace_only_composite_part(self) -> None:
        keys = _linked_alias_keys(
            {
                "alias_key": "target-model|workspace-456",
                "footprint": "TARGET_PACKAGE",
                "easyeda_model_id": "target-model|workspace-456",
                "candidate_model_ids": [
                    "target-model|workspace-456",
                    "target-model",
                    "workspace-456",
                ],
                "model_titles": [],
            }
        )

        self.assertIn("target-model|workspace-456", keys)
        self.assertIn("target-model", keys)
        self.assertNotIn("workspace-456", keys)

    def test_attach_models_by_explicit_alias(self) -> None:
        paths = find_design_files(prepare_input_files(FIXTURE))
        library = parse_easyeda_pro_design_files(paths)
        footprint = library.footprints[0]
        model = Model3D(
            file="vendor-weird-name.step",
            source_file="vendor-weird-name.step",
            format="step",
            key="vendor-weird-name",
        )

        attach_models(library, [model], {"sot233pro": "vendor-weird-name.step"})

        self.assertIsNotNone(footprint.model3d)
        self.assertEqual(footprint.model3d.file, "vendor-weird-name.step")
        self.assertEqual(footprint.model3d.transform_source, "easyeda_pro_v3")
        candidates = model_match_candidates(footprint, {"sot233pro": "vendor-weird-name.step"})
        self.assertEqual(candidates[0]["source"], "alias:footprint")
        self.assertEqual(candidates[0]["key"], "vendorweirdname")

    def test_convert_easyeda_pro_v3_fixture_outputs_assets(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_easyeda_pro_test_"))
        try:
            code = main(["convert", "--input", str(FIXTURE), "--models", str(FIXTURE), "--out", str(out_dir)])
            self.assertEqual(code, 0)
            ir = json.loads((out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8"))
            self.assertEqual(ir["components"][0]["name"], "PRO_DEVICE")
            self.assertEqual(ir["components"][0]["pins"][2]["hidden"], True)
            self.assertEqual(ir["footprints"][0]["name"], "SOT_23_3_PRO")
            self.assertEqual(ir["footprints"][0]["model3d"]["format"], "step")
            self.assertEqual(ir["footprints"][0]["model3d"]["transform_source"], "easyeda_pro_v3")
            self.assertEqual(ir["footprints"][0]["properties"]["EasyEDA_3D_Model_Id"], "model-pro")
            self.assertAlmostEqual(ir["footprints"][0]["model3d"]["target_size"][0], 3.0, delta=1e-5)
            self.assertAlmostEqual(ir["footprints"][0]["model3d"]["target_size"][1], 1.8, delta=1e-5)
            self.assertAlmostEqual(ir["footprints"][0]["model3d"]["target_size"][2], 0.0, delta=1e-8)
            self.assertEqual(sum(1 for pad in ir["footprints"][0]["pads"] if pad["role"] == "npth"), 1)
            output_validation = json.loads((out_dir / "reports" / "output_validation.json").read_text(encoding="utf-8"))
            self.assertEqual(output_validation["status"], "script_ready")
            self.assertEqual(output_validation["errors"], [])
            self.assertTrue((out_dir / "capture" / "library.olb.xml").exists())
            self.assertTrue((out_dir / "allegro" / "symbols" / "build_scripts" / "SOT_23_3_PRO.il").exists())
            self.assertTrue((out_dir / "3d" / "cadence_step_package.map").exists())
            transform_path = out_dir / "3d" / "easyeda_3d_transforms.json"
            self.assertTrue(transform_path.exists())
            transform_report = json.loads(transform_path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(transform_report["footprints"][0]["target_size"][0], 3.0, delta=1e-5)
            self.assertAlmostEqual(transform_report["footprints"][0]["target_size"][1], 1.8, delta=1e-5)
            self.assertAlmostEqual(transform_report["footprints"][0]["target_size"][2], 0.0, delta=1e-8)
            self.assertEqual(transform_report["footprints"][0]["rotation"], [0.0, 0.0, 90.0])
            self.assertEqual(transform_report["footprints"][0]["model_id"], "model-pro")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_easyeda_pro_zip_suffix_is_extracted_and_inspected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="jlc2cadence_easyeda_pro_zip_test_"))
        try:
            archive = tmp / "library.epro2"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(FIXTURE / "PRO_DEVICE.epru", "PRO_DEVICE.epru")
                zf.write(FIXTURE / "SOT_23_3_PRO.step", "SOT_23_3_PRO.step")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["inspect", "--input", str(archive), "--models", str(archive)])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["components"], 1)
            self.assertEqual(report["footprints"], 1)
            self.assertEqual(report["models"], 1)
            self.assertEqual(report["errors"], 0)
            self.assertEqual(report["warnings"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_convert_easyeda_pro_zip_binds_id_named_step_model(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="jlc2cadence_easyeda_pro_model_id_zip_test_"))
        out_dir = tmp / "out"
        try:
            archive = tmp / "library.epro2"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(FIXTURE / "PRO_DEVICE.epru", "library/PRO_DEVICE.epru")
                zf.write(FIXTURE / "SOT_23_3_PRO.step", "models/model-pro.step")

            code = main(["convert", "--input", str(archive), "--out", str(out_dir)])
            self.assertEqual(code, 0)
            ir = json.loads((out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8"))
            footprint = ir["footprints"][0]
            self.assertEqual(footprint["properties"]["EasyEDA_3D_Model_Id"], "model-pro")
            self.assertEqual(Path(footprint["model3d"]["source_file"]).name, "model-pro.step")
            self.assertEqual(Path(footprint["model3d"]["file"]).name, "model_pro.step")
            self.assertTrue((out_dir / "3d" / "model_pro.step").exists())
            self.assertEqual(
                json.loads((out_dir / "reports" / "3d_validation.json").read_text(encoding="utf-8"))["errors"],
                [],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
