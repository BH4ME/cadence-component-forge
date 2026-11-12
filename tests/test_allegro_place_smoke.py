from __future__ import annotations

import argparse
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "allegro_place_smoke", ROOT / "tools/acceptance/allegro_place_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class AllegroPlacementAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "units": "millimeters", "accuracy": 4, "step": '(step_name "original.step")',
            "pins": [{"number": "1", "x": -1.25, "y": 0.5, "padstack": "PTH"},
                     {"number": "2", "x": 1.25, "y": 0.5, "padstack": "PTH"}],
        }
        self.board = copy.deepcopy(self.source)
        for pin in self.board["pins"]:
            pin["x"] += 25
            pin["y"] += 25

    def test_accepts_translation_and_changed_readback_order(self):
        self.board["pins"].reverse()
        result = smoke.compare(self.source, self.board, 2)
        self.assertEqual(result["max_pin_center_error_mm"], 0)
        self.assertTrue(result["step_mapping_equal"])

    def test_rejects_changed_pin_identity_padstack_or_coordinate(self):
        for key, value in (("number", "3"), ("padstack", "OTHER"), ("x", 24), ("y", 26)):
            with self.subTest(key=key):
                board = copy.deepcopy(self.board)
                board["pins"][0][key] = value
                with self.assertRaises(ValueError):
                    smoke.compare(self.source, board, 2)

    def test_rejects_changed_pin_count_or_expected_count(self):
        self.board["pins"].pop()
        with self.assertRaisesRegex(ValueError, "Pin count"):
            smoke.compare(self.source, self.board, 2)
        with self.assertRaisesRegex(ValueError, "Pin count"):
            smoke.compare(self.source, self.source, 3)

    def test_rejects_changed_step_association(self):
        self.board["step"] = '(step_name "substituted.step")'
        with self.assertRaisesRegex(ValueError, "STEP"):
            smoke.compare(self.source, self.board, 2)

    def test_rejects_wrong_units_or_resolution(self):
        for key, value in (("units", "mils"), ("accuracy", 2)):
            with self.subTest(key=key):
                board = copy.deepcopy(self.board)
                board[key] = value
                with self.assertRaises(ValueError):
                    smoke.compare(self.source, board, 2)

    def test_reads_complete_native_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readback.txt"
            path.write_text('UNITS\tmillimeters\t4\nPIN_COUNT\t1\n'
                            'PIN\tA1\t-1.25\t2.5\tROUND\nSTEP\tnil\n', encoding="utf-8")
            result = smoke.readback(path)
        self.assertEqual(result["pins"], [{"number": "A1", "x": -1.25, "y": 2.5,
                                           "padstack": "ROUND"}])
        self.assertEqual(result["step"], "nil")

    def test_rejects_missing_or_malformed_native_data(self):
        complete = 'UNITS\tmillimeters\t4\nPIN_COUNT\t1\nPIN\t1\t0\t1\tPTH\nSTEP\tnil\n'
        cases = ["", complete.replace("PIN_COUNT\t1", "PIN_COUNT\t2"),
                 complete.replace("STEP\tnil\n", ""),
                 complete.replace("PIN\t1\t0", "PIN\t1\tinvalid"),
                 complete + "PIN\tcorrupt\n", complete + "PIN_COUNT\t1\n",
                 complete.replace("\t0\t1\t", "\t1e999\t1\t")]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readback.txt"
            for index, text in enumerate(cases):
                with self.subTest(case=index):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        smoke.readback(path)

    def test_quotes_paths_and_rejects_script_control_characters(self):
        self.assertEqual(smoke.quoted(r"D:\some folder\source.dra"), '"D:/some folder/source.dra"')
        for character in ('"', '\r', '\n', '$', '\t', '\x00'):
            with self.subTest(character=repr(character)):
                with self.assertRaises(ValueError):
                    smoke.quoted("D:/source" + character + ".dra")

    def test_uses_physical_psm_filename_case_for_native_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            symbols = Path(temporary)
            compiled = symbols / "c11255_resistor.psm"
            compiled.write_bytes(b"compiled")
            self.assertEqual(smoke.native_psm_path(symbols, "C11255_RESISTOR").name,
                             "c11255_resistor.psm")
            with self.assertRaisesRegex(ValueError, "compiled PSM"):
                smoke.native_psm_path(symbols, "missing")

    def test_rejects_existing_output_without_launching_allegro(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cadence = root / "cadence"
            library = root / "library"
            for file in (cadence / "tools/bin/allegro.exe", library / "allegro/symbols/test.dra",
                         library / "allegro/symbols/test.psm"):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"fixture")
            args = argparse.Namespace(cadence_root=cadence, library_root=library,
                                      product="OrCAD_PCB_Designer", footprint="test", output=root)
            with patch.object(smoke.os, "name", "nt"), patch.object(smoke, "run_phase") as launch:
                with self.assertRaisesRegex(ValueError, "new folder"):
                    smoke.run(args)
                launch.assert_not_called()

    def test_native_failure_stops_without_trying_another_product(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.native_arguments(root)
            def failed(command, **kwargs):
                return {"phase": kwargs["phase"], "command": command, "pid": 1234, "exit_code": 2,
                        "status": "exited", "timed_out": False, "stdout": "No licenses available", "stderr": ""}
            with patch.object(smoke.os, "name", "nt"), patch.object(smoke, "run_phase", side_effect=failed) as launch:
                result = smoke.run(args)
            self.assertFalse(result["ok"])
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(result["phases"][0]["command"][1:3], ["-product", "OrCAD_PCB_Designer"])
            self.assertIn("No licenses available", result["phases"][0]["stdout"])

    def test_zero_exit_without_saved_pcb_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.native_arguments(Path(temporary))
            succeeded = {"pid": 1234, "exit_code": 0, "status": "exited", "timed_out": False,
                         "stdout": "", "stderr": ""}
            with patch.object(smoke.os, "name", "nt"), \
                    patch.object(smoke, "run_phase", return_value=succeeded):
                result = smoke.run(args)
            self.assertFalse(result["ok"])
            self.assertIn("did not save a PCB", result["error"])

    def test_timeout_preserves_phase_and_native_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.native_arguments(Path(temporary))
            timeout = {"pid": 1234, "exit_code": 1, "status": "timed_out", "timed_out": True,
                       "stdout": "native stdout", "stderr": "native stderr"}
            with patch.object(smoke.os, "name", "nt"), \
                    patch.object(smoke, "run_phase", return_value=timeout):
                result = smoke.run(args)
            self.assertFalse(result["ok"])
            self.assertIn("source launched PID 1234 timed out", result["error"])
            self.assertTrue(result["phases"][0]["timed_out"])
            self.assertEqual(result["phases"][0]["stdout"], "native stdout")
            self.assertEqual(result["phases"][0]["stderr"], "native stderr")

    @staticmethod
    def native_arguments(root):
        cadence = root / "cadence"
        library = root / "library"
        for file in (cadence / "tools/bin/allegro.exe", library / "allegro/symbols/test.dra",
                     library / "allegro/symbols/test.psm"):
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"fixture")
        return argparse.Namespace(cadence_root=cadence, library_root=library,
                                  product="OrCAD_PCB_Designer", footprint="test", output=root / "result",
                                  timeout=1, expected_pins=2)


if __name__ == "__main__":
    unittest.main()
