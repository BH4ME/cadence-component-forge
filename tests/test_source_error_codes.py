from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.extractors.easyeda_catalog import CatalogError, EasyEDACatalog, _validate_pad_shapes, component_to_library
from jlc2cadence.generators.allegro_skill import write_footprint_build_skill
from jlc2cadence.ir.schema import Graphic
from tests.test_allegro_source_geometry import source_footprint, source_pad
from tests import test_capture_source_geometry as capture_source_fixtures
from tests.test_easyeda_catalog import resistor_data


def response_opener(payload):
    def open_response(request, timeout):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))
    return open_response


class SourceErrorCodeTests(unittest.TestCase):
    def assert_code(self, expected, call, exception=ValueError):
        with self.assertRaises(exception) as captured:
            call()
        self.assertEqual(getattr(captured.exception, "error_code", ""), expected)
        return captured.exception

    def convert_with_footprint_record(self, record):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"].append(record)
        return component_to_library(raw, "C25804")

    def test_explicit_geometry_error_retains_value_error_compatibility(self):
        error_type = importlib.import_module("jlc2cadence.errors").UnsupportedGeometryError
        error = error_type("Original curve cannot be represented")
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.error_code, "unsupported_geometry")
        self.assertIn("Original curve", str(error))

    def test_catalog_error_preserves_message_and_explicit_code(self):
        error = CatalogError("Confirmed empty lookup", error_code="no_cad_library")
        self.assertEqual(str(error), "Confirmed empty lookup")
        self.assertEqual(error.error_code, "no_cad_library")
        self.assertEqual(getattr(CatalogError("Unexpected response"), "error_code", ""), "")

    def test_confirmed_lookup_without_matching_part_has_no_cad_code(self):
        for rows in ([], [{"number": "C1", "uuid": "0" * 32}]):
            with self.subTest(rows=rows):
                provider = EasyEDACatalog(urlopen=response_opener({"success": True, "result": rows}))
                self.assert_code("no_cad_library", lambda: provider.fetch_part("C25804"), CatalogError)

    def test_protocol_uuid_and_network_failures_are_not_missing_cad_or_geometry(self):
        payloads = [
            {"success": False, "result": []},
            {"success": True, "result": "not a list"},
            {"success": True, "result": [{"number": "C25804", "uuid": "invalid"}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                provider = EasyEDACatalog(urlopen=response_opener(payload))
                self.assert_code("", lambda: provider.fetch_part("C25804"), CatalogError)

        def disconnected(request, timeout):
            raise URLError("offline fixture")

        self.assert_code("", lambda: EasyEDACatalog(urlopen=disconnected).fetch_part("C25804"), CatalogError)

    def test_known_curve_and_ellipse_limits_have_geometry_code(self):
        for record in [
            "SOLIDREGION~3~~M 4000 3000 C 4001 3001 4002 3002 4003 3003 Z~solid~curve~0",
            "SOLIDREGION~3~~M 4000 3000 Q 4001 3001 4003 3003 Z~solid~curve~0",
            "ARC~1~3~~M 4000 3000 A 10 5 0 0 1 4010 3010~~ellipse~0",
            "SOLIDREGION~3~~M 4000 3000 A 10 10 0 0 1 4010 3010 Z~solid~curve~0",
        ]:
            with self.subTest(record=record):
                self.assert_code("unsupported_geometry", lambda: self.convert_with_footprint_record(record), CatalogError)

    def test_complete_polygon_pad_is_a_geometry_limit(self):
        record = resistor_data()["packageDetail"]["dataStr"]["shape"][0].replace("PAD~RECT", "PAD~POLYGON")
        self.assertGreaterEqual(len(record.split("~")), 17)
        self.assert_code("unsupported_geometry", lambda: _validate_pad_shapes([record]), CatalogError)

    def test_short_pad_is_incomplete_even_when_its_shape_says_polygon(self):
        original = resistor_data()["packageDetail"]["dataStr"]["shape"][0]
        for record in ("~".join(original.split("~")[:16]), "PAD~POLYGON"):
            with self.subTest(record=record):
                error = self.assert_code("", lambda: _validate_pad_shapes([record]), CatalogError)
                self.assertNotIn("custom/polygon", str(error))

    def test_unknown_pad_shape_is_not_a_confirmed_geometry_limitation(self):
        record = resistor_data()["packageDetail"]["dataStr"]["shape"][0].replace("PAD~RECT", "PAD~UNKNOWN")
        self.assert_code("", lambda: _validate_pad_shapes([record]), CatalogError)

    def test_duplicate_physical_pad_and_local_via_have_geometry_code(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"].append(raw["packageDetail"]["dataStr"]["shape"][0])
        self.assert_code("unsupported_geometry", lambda: component_to_library(raw, "C25804"), CatalogError)
        self.assert_code("unsupported_geometry", lambda: self.convert_with_footprint_record(
            "VIA~4000~3000~1~~0.5~via~0"), CatalogError)

    def test_rect_semantics_missing_origin_and_electrical_type_have_no_geometry_code(self):
        self.assert_code("", lambda: self.convert_with_footprint_record("RECT~4000~3000~2~2~3~0"), CatalogError)
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["head"].pop("x")
        self.assert_code("", lambda: component_to_library(raw, "C25804"), CatalogError)
        raw = resistor_data()
        raw["dataStr"]["shape"][0] = raw["dataStr"]["shape"][0].replace("P~show~1~", "P~show~5~")
        self.assert_code("", lambda: component_to_library(raw, "C25804"), CatalogError)

    def test_invalid_numeric_pad_data_is_not_reported_as_a_geometry_limit(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = raw["packageDetail"]["dataStr"]["shape"][0].replace("~4003~", "~nan~")
        self.assert_code("", lambda: component_to_library(raw, "C25804"), CatalogError)

    def test_capture_filled_rounded_rectangle_is_a_known_geometry_limit(self):
        fixture = capture_source_fixtures.CaptureSourceGeometryTests()
        component = fixture.source_component(graphics=[Graphic("rectangle", "symbol",
            [(-2.54, -2.54), (2.54, 2.54)], properties={"radius_x_mm": "0.508",
                "radius_y_mm": "0.508", "fill": "#880000"})])
        self.assert_code("unsupported_geometry", lambda: fixture.render(component))

    def test_capture_missing_unknown_and_font_precision_failures_have_no_geometry_code(self):
        fixture = capture_source_fixtures.CaptureSourceGeometryTests()
        components = [fixture.source_component(),
            fixture.source_component(graphics=[Graphic("unknown", "symbol")]),
            fixture.source_component(graphics=[Graphic("text", "symbol", text="A",
                properties={"font_size_mm": "0.001"})]),
            fixture.source_component(graphics=[Graphic("text", "symbol", text="A",
                properties={"font_size_mm": "2.54", "font_style": "oblique"})])]
        for component in components:
            with self.subTest(graphics=component.graphics):
                self.assert_code("", lambda: fixture.render(component))

    def build_footprint(self, pad):
        with tempfile.TemporaryDirectory() as temporary:
            return write_footprint_build_skill(source_footprint([pad]), Path(temporary) / "source.il")

    def test_allegro_valid_nonzero_drill_offset_is_geometry_but_malformed_data_is_not(self):
        for offset, code in [("[0.1,0]", "unsupported_geometry"), ("not JSON", "")]:
            with self.subTest(offset=offset):
                pad = source_pad(kind="thru_hole", hole_diameter=0.5, layers=["all"],
                    properties={"plated": "true", "Source_Drill_Offset_mm": offset})
                self.assert_code(code, lambda: self.build_footprint(pad))

    def test_allegro_known_slot_angle_limit_differs_from_missing_angle_data(self):
        for angle, code in [("0", "unsupported_geometry"), (None, "")]:
            with self.subTest(angle=angle):
                properties = {"plated": "true", "Source_Slot_Shape": "oval"}
                if angle is not None:
                    properties["Source_Drill_Rotation_deg"] = angle
                pad = source_pad(kind="thru_hole", rotation=0, hole_diameter=0.5,
                    slot_size=(0.5, 1.0), layers=["all"], properties=properties)
                self.assert_code(code, lambda: self.build_footprint(pad))


if __name__ == "__main__":
    unittest.main()
