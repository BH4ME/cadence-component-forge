from __future__ import annotations

import json
from pathlib import Path
import unittest

from jlc2cadence.capture_bridge import failure_response
from jlc2cadence.errors import UnsupportedGeometryError
from jlc2cadence.native_geometry_errors import source_native_geometry_error


class NativeGeometryErrorTests(unittest.TestCase):
    def test_confirmed_source_region_and_line_errors_map_to_user_message(self):
        cases = (
            ("SPMHA1-273", "Shape intersects with itself", "region", "PACKAGE GEOMETRY/JLC_COMPONENT_SHAPE"),
            ("SPMHDB-70", "Buffer contains overlapping segments", "line", "PACKAGE GEOMETRY/SILKSCREEN_TOP"),
        )
        for code, diagnostic, kind, layer in cases:
            with self.subTest(code=code):
                results = Path("D:/independent/create_symbols.results.json")
                row = {"footprint": "C_TEST", "buildStdout":
                       f"E- ({code}): {diagnostic}.\n"
                       f"E- *Error* Source-exact: failed creating source {kind} on {layer}"}
                error = source_native_geometry_error([row], results)
                self.assertIsInstance(error, UnsupportedGeometryError)
                self.assertEqual(error.native_error_code, code)
                response = failure_response(error)
                self.assertEqual(response["error_code"], "unsupported_geometry")
                self.assertEqual(response["user_message"], "由于是异形结构，当前无法生成。")
                self.assertIn(str(results), response["error"])

    def test_generic_or_mismatched_native_failures_remain_unclassified(self):
        rows = (
            "E- (SPMHGE-7): Error(s) occurred, check logfile.",
            "E- (SPMHA1-273): Shape intersects with itself.\n"
            "E- *Error* Source-exact: failed creating source line on PACKAGE GEOMETRY/X",
            "License checkout failed; FLEXnet error -15.",
        )
        for text in rows:
            self.assertIsNone(source_native_geometry_error([{"buildStdout": text}], Path("x")))


if __name__ == "__main__":
    unittest.main()
