from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jlc2cadence.capture_bridge import _capture_symbol_preview, _source_build_finished, _source_refdes_missing, _write_source_provenance
from jlc2cadence.extractors.easyeda_3d import EasyEDA3DDownload, EasyEDA3DFetchReport
from jlc2cadence.ir.schema import Component, LibraryIR
from jlc2cadence.generators.model3d import _step_map_entries
from jlc2cadence.ir.schema import Footprint, Model3D


class SourcePolicyBridgeTests(unittest.TestCase):
    def test_exact_mps_rpc_warning_requires_completed_native_files_and_zero_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            dra, psm = Path(directory) / "source.dra", Path(directory) / "source.psm"
            dra.write_bytes(b"native dra")
            psm.write_bytes(b"native psm")
            rpc = "MPS ERROR: MPSC mpsCall: failed to initiate remote function call (psGetProducts), failure closing funcall message for write"
            row = {"footprint": "source", "status": "created_psm", "buildStatus": "created_dra",
                   "dra": str(dra), "psm": str(psm), "exitCode": 0, "buildExitCode": 0, "compileExitCode": 0,
                   "buildStdout": rpc + "\njlc2cadence finished package symbol source"}
            original = dict(row)
            self.assertTrue(_source_build_finished(row))
            self.assertEqual(row, original)
            for changed in ({"buildExitCode": 1}, {"compileExitCode": True}, {"status": "failed_psm"},
                            {"buildStdout": rpc}, {"buildStderr": "*Error* source shape"},
                            {"compileStdout": "ERROR(SPMHA1-273): Shape intersects with itself"},
                            {"buildStdout": row["buildStdout"] + "\nMPS ERROR: unknown remote failure"}):
                with self.subTest(changed=changed):
                    self.assertFalse(_source_build_finished({**row, **changed}))
            psm.write_bytes(b"")
            self.assertFalse(_source_build_finished(row))
            psm.unlink()
            self.assertFalse(_source_build_finished(row))

    def test_missing_transform_does_not_produce_a_zero_offset_binding(self):
        model = Model3D("original.step", format="step", transform_source="missing_original")
        library = LibraryIR(footprints=[Footprint("part", model3d=model)])
        self.assertEqual(_step_map_entries(library), [])

    def test_only_confirmed_missing_refdes_is_a_partial_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            dra = Path(directory) / "source.dra"
            dra.write_bytes(b"native test artifact")
            log = dra.with_suffix(".log")
            row = {"status": "failed_psm", "buildStatus": "created_dra", "dra": str(dra),
                   "footprint": "source", "buildStdout": "jlc2cadence finished package symbol source"}
            log.write_text("ERROR(SPMHCS-1): Symbol is missing a refdes.\nERROR(SPMHA1-291): Create symbol has been aborted.")
            self.assertTrue(_source_refdes_missing([row]))
            self.assertFalse(_source_refdes_missing([{**row, "buildStdout": "*Error* failed original shape"}]))
            log.write_text(log.read_text() + "\nERROR(OTHER-1): Corrupt padstack")
            self.assertFalse(_source_refdes_missing([row]))
            self.assertFalse(_source_refdes_missing([{"status": "created_psm"}]))
            self.assertFalse(_source_refdes_missing([]))

    def test_original_record_and_downloaded_step_have_verifiable_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step = root / "original.step"
            original = b"ISO-10303-21;\noriginal geometry\nEND-ISO-10303-21;"
            step.write_bytes(original)
            raw = '{"uuid":"original","dataStr":{"shape":["original record"]}}'
            library = LibraryIR(components=[Component("test")], properties={
                "Conversion_Policy": "source_exact", "Source_Record_JSON": raw,
            })
            report = EasyEDA3DFetchReport(str(root), ["step"], downloads=[EasyEDA3DDownload(
                ["footprint"], "id", "original", "step", "https://modules.easyeda.com/original",
                file=str(step), status="downloaded",
            )])
            path = _write_source_provenance(library, report, root)
            provenance = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(Path(provenance["source_record"]).read_text(encoding="utf-8"), raw)
            self.assertEqual(provenance["source_record_sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual(provenance["step_downloads"][0]["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(step.read_bytes(), original)
            self.assertFalse(provenance["step_geometry_modified"])

    def test_native_source_preview_uses_graphics_and_explicit_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xml"
            path.write_text('''<Library><Package><Defn name="original"/><LibPart><NormalView>
                <Line><Defn x1="-7" y1="3" x2="9" y2="4"/></Line>
                <Ellipse><Defn x1="-2" y1="-2" x2="2" y2="2"/></Ellipse>
                <CommentText><Defn name="original label" locX="6" locY="-3"/>
                <TextFont><Defn name="Verdana" height="-4"/></TextFont></CommentText>
                <SymbolPinScalar><Defn name="original pin" hotptX="-10" hotptY="5" startX="-4" startY="5"/>
                </SymbolPinScalar></NormalView></LibPart></Package></Library>''', encoding="utf-8")
            preview = _capture_symbol_preview(path, "original")
            self.assertEqual(preview["graphics"][0]["points"], [[-7, -3], [9, -4]])
            self.assertEqual(preview["graphics"][1]["kind"], "ellipse")
            self.assertEqual(preview["graphics"][2]["text"], "original label")
            self.assertTrue(preview["pins"][0]["labels_in_graphics"])
            self.assertEqual(preview["pins"][0]["end_x"], -4)

    def test_required_refdes_adaptation_is_distinguished_from_original_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            footprint = Footprint("original", properties={
                "Conversion_Policy": "source_exact",
                "Source_Refdes_Prefix": "R",
                "Source_Refdes_Prefix_Source": "dataStr.head.c_para.pre",
                "Source_Refdes_Prefix_Raw": "R?",
                "Source_Refdes_Component": "resistor",
                "Source_Refdes_Prefix_Status": "verified",
            })
            library = LibraryIR(components=[Component("resistor", ref_prefix="R")],
                                footprints=[footprint], properties={"Conversion_Policy": "source_exact"})
            report = EasyEDA3DFetchReport(directory, ["step"])
            path = _write_source_provenance(library, report, Path(directory))
            adaptation = json.loads(path.read_text(encoding="utf-8"))["allegro_refdes"][0]
            self.assertEqual(adaptation["footprint"], "original")
            self.assertEqual(adaptation["status"], "derived_from_component")
            self.assertEqual(adaptation["source"]["raw"], "R?")
            self.assertEqual(adaptation["source"]["field"], "dataStr.head.c_para.pre")
            self.assertEqual(adaptation["target"]["text"], "R*")
            self.assertEqual(adaptation["target"]["layer"], "REF DES/ASSEMBLY_TOP")
            self.assertEqual(footprint.graphics, [])


if __name__ == "__main__":
    unittest.main()
