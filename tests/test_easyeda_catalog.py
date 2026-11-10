from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jlc2cadence.extractors.easyeda_catalog import CatalogError, EasyEDACatalog, component_to_library
from jlc2cadence.ir.normalize import merge_libraries


def resistor_data():
    return {
        "uuid": "b210af5a1436310a86e8b108e2e5a90b", "title": "10k resistor", "lcsc": {"number": "C25804"},
        "description": "10k 1%", "SMT": True,
        "dataStr": {"head": {"x": 20, "y": 0, "license": "Original source license", "c_para": {
            "name": "0603WAF1002T5E", "pre": "R?", "package": "R0603", "Manufacturer": "UNI-ROYAL", "Manufacturer Part": "0603WAF1002T5E",
        }}, "shape": [
            "P~show~1~99~40~0~0~pin2~0^^40~0^^M 40 0 h-10~#000^^0~25~3~0~2~end~~0^^1~39~-4~0~2~end~~0^^0~33~0^^0~",
            "P~show~1~98~0~0~180~pin1~0^^0~0^^M 0 0 h10~#000^^0~15~3~0~1~start~~0^^1~1~-4~0~1~start~~0^^0~7~0^^0~",
        ], "BBox": {"x": 0, "y": -5, "width": 40, "height": 10}},
        "packageDetail": {"title": "R0603", "dataStr": {"head": {"x": 4000, "y": 3000, "c_para": {"package": "R0603", "3DModel": "R0603"}}, "shape": [
            "PAD~RECT~4003~3000~3.2~3.4~1~~2~0~~0~pad2~0~~Y~0",
            "PAD~RECT~3997~3000~3.2~3.4~1~~1~0~~0~pad1~0~~Y~0",
            "TRACK~0.5~3~~3995 2997 4005 2997~track1~0",
            'SVGNODE~{"attrs":{"title":"R0603","uuid":"6bd5cd867e9542ebae21caaf5d2d4c4d","c_origin":"4000,3000","c_rotation":"0,0,90","z":"0"},"childNodes":[]}',
        ]}},
    }


class EasyEDACatalogTests(unittest.TestCase):
    def test_adapter_preserves_physical_numbers_units_and_model_transform(self):
        library = component_to_library(resistor_data(), "C25804", {"step": "6bd5cd867e9542ebae21caaf5d2d4c4d"})
        component, footprint = library.components[0], library.footprints[0]
        self.assertEqual(component.pin_numbers(), {"1", "2"})
        self.assertEqual(component.pins[0].number, "2")  # Not the SPICE ordering value 99.
        self.assertTrue(all(pin.type == "Input" for pin in component.pins))
        self.assertAlmostEqual(footprint.pads[0].x, 0.762)
        self.assertAlmostEqual(footprint.pads[0].size[0], 0.8128)
        self.assertEqual(component.footprint, footprint.name)
        self.assertEqual(json.loads(footprint.properties["Catalog_3D_Rotation_deg"]), [0, 0, 90])
        self.assertEqual(library.properties["Symbol_Source_License"], "Original source license")
        self.assertEqual(library.properties["Conversion_Policy"], "source_exact")
        self.assertEqual(component.pins[0].properties["Source_Pin_Electrical_Type"], "1")

    def test_reference_prefix_preserves_original_source_and_component_association(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["head"]["c_para"]["pre"] = "R?"
        library = component_to_library(raw, "C25804")
        component, footprint = library.components[0], library.footprints[0]
        self.assertEqual(component.ref_prefix, "R")
        for item in (component, footprint):
            self.assertEqual(item.properties["Source_Refdes_Prefix"], "R")
            self.assertEqual(item.properties["Source_Refdes_Prefix_Source"], "dataStr.head.c_para.pre")
            self.assertEqual(item.properties["Source_Refdes_Prefix_Raw"], "R?")
            self.assertEqual(item.properties["Source_Refdes_Prefix_Status"], "verified")
            self.assertEqual(item.properties["Source_Refdes_Component"], component.name)
            evidence = json.loads(item.properties["Source_Refdes_Prefix_Evidence_JSON"])
            self.assertEqual(evidence, [
                {"source": "dataStr.head.c_para.pre", "raw": "R?", "prefix": "R"},
                {"source": "packageDetail.dataStr.head.c_para.pre", "raw": "R?", "prefix": "R"},
            ])

    def test_symbol_prefix_takes_precedence_without_using_part_number_or_package_name(self):
        raw = resistor_data()
        raw["dataStr"]["head"]["c_para"]["pre"] = "U?"
        raw["packageDetail"]["dataStr"]["head"]["c_para"]["pre"] = "R?"
        library = component_to_library(raw, "C25804")
        self.assertEqual(library.components[0].ref_prefix, "U")
        self.assertEqual(library.footprints[0].properties["Source_Refdes_Prefix"], "U")
        self.assertTrue(any("takes precedence" in warning for warning in library.warnings))
        self.assertEqual(raw["dataStr"]["head"]["c_para"]["pre"], "U?")
        self.assertEqual(raw["packageDetail"]["dataStr"]["head"]["c_para"]["pre"], "R?")

    def test_explicit_source_reference_or_package_prefix_can_supply_missing_symbol_prefix(self):
        raw = resistor_data()
        del raw["dataStr"]["head"]["c_para"]["pre"]
        raw["dataStr"]["head"]["c_para"]["ref"] = "D17"
        footprint = component_to_library(raw, "C25804").footprints[0]
        self.assertEqual(footprint.properties["Source_Refdes_Prefix"], "D")
        self.assertEqual(footprint.properties["Source_Refdes_Prefix_Source"], "dataStr.head.c_para.ref")
        self.assertEqual(footprint.properties["Source_Refdes_Prefix_Raw"], "D17")
        del raw["dataStr"]["head"]["c_para"]["ref"]
        raw["packageDetail"]["dataStr"]["head"]["c_para"]["pre"] = "J?"
        library = component_to_library(raw, "C25804")
        self.assertEqual(library.components[0].ref_prefix, "J")
        self.assertEqual(library.footprints[0].properties["Source_Refdes_Prefix_Source"], "packageDetail.dataStr.head.c_para.pre")

    def test_missing_invalid_or_conflicting_reference_prefix_is_explicit_and_not_guessed(self):
        for value in (None, "?", "[set unsafe]", "C25804"):
            with self.subTest(value=value):
                raw = resistor_data()
                raw["dataStr"]["head"]["c_para"]["pre"] = value
                library = component_to_library(raw, "C25804")
                self.assertEqual(library.components[0].ref_prefix, "")
                self.assertEqual(library.footprints[0].properties["Source_Refdes_Prefix_Status"], "missing")
                self.assertNotIn("Source_Refdes_Prefix", library.footprints[0].properties)
                self.assertTrue(any("none was inferred from the LCSC part number" in warning for warning in library.warnings))
        raw = resistor_data()
        raw["dataStr"]["head"]["c_para"]["ref"] = "C12"
        library = component_to_library(raw, "C25804")
        self.assertEqual(library.components[0].ref_prefix, "")
        self.assertEqual(library.footprints[0].properties["Source_Refdes_Prefix_Status"], "conflict")
        self.assertNotIn("Source_Refdes_Prefix", library.footprints[0].properties)
        self.assertTrue(any("Conflicting original reference prefixes" in warning for warning in library.warnings))

    def test_reference_metadata_does_not_change_original_geometry_or_step_binding(self):
        raw = resistor_data()
        raw["dataStr"]["shape"].append("R~10~-4~~~20~8~#880000~1~0~none~body~0")
        original = component_to_library(raw, "C25804")
        del raw["dataStr"]["head"]["c_para"]["pre"]
        missing = component_to_library(raw, "C25804")
        self.assertEqual([pin.to_dict() for pin in original.components[0].pins], [pin.to_dict() for pin in missing.components[0].pins])
        self.assertEqual([graphic.to_dict() for graphic in original.components[0].graphics], [graphic.to_dict() for graphic in missing.components[0].graphics])
        self.assertEqual([pad.to_dict() for pad in original.footprints[0].pads], [pad.to_dict() for pad in missing.footprints[0].pads])
        self.assertEqual([graphic.to_dict() for graphic in original.footprints[0].graphics], [graphic.to_dict() for graphic in missing.footprints[0].graphics])
        for key in ("EasyEDA_3D_Model_Id", "Catalog_3D_Offset_mm", "Catalog_3D_Rotation_deg", "Source_3D_Attributes_JSON"):
            self.assertEqual(original.footprints[0].properties[key], missing.footprints[0].properties[key])

    def test_original_symbol_body_pin_path_and_label_positions_are_preserved(self):
        raw = resistor_data()
        raw["dataStr"]["shape"].extend([
            "R~10~-4~1~1~20~8~#880000~1~0~none~body~0",
            "E~12~-2~0.5~0.5~#880000~0.2~0~#880000~polarity~0",
        ])
        raw["dataStr"]["shape"][1] = "P~show~1~98~9~9~90~pin1~0^^0~0^^M 0 0 v10~#000^^1~3~5~90~A~start~~~#000^^0~-1~5~90~1~end~~~#000^^0~7~0^^0~"
        library = component_to_library(raw, "C25804")
        component = library.components[0]
        pin = component.pins[1]
        self.assertEqual((pin.x, pin.y), (-5.08, 0))
        self.assertEqual(float(pin.properties["Source_Pin_End_Y_mm"]), -2.54)
        self.assertEqual(float(pin.properties["Source_Pin_End_X_mm"]), -5.08)
        self.assertEqual(pin.length, 2.54)
        self.assertAlmostEqual(float(pin.properties["Source_Pin_Name_X_mm"]), -17 * 0.254)
        self.assertEqual(pin.properties["Source_Pin_Name_Visible"], "true")
        self.assertEqual(pin.properties["Source_Pin_Number_Visible"], "false")
        self.assertEqual(pin.properties["Source_Pin_Name_Font"], "Verdana")
        self.assertAlmostEqual(float(pin.properties["Source_Pin_Name_Font_mm"]), 7 * 25.4 / 72)
        body, polarity = component.graphics
        self.assertEqual(body.points, [(-2.54, 1.016), (2.54, -1.016)])
        self.assertEqual(float(body.properties["radius_x_mm"]), 0.254)
        self.assertEqual(body.properties["fill"], "none")
        self.assertEqual(polarity.properties["fill"], "#880000")
        self.assertEqual(component.to_dict()["graphics"][0]["properties"]["radius_x_mm"], "0.254")
        self.assertFalse(any("regenerated" in warning for warning in library.warnings))

    def test_original_footprint_precision_circles_arcs_and_region_roles(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = "PAD~OVAL~4003.1234~3000.5678~1.4331~6.8583~1~~2~0~~0~pad2~0~~Y~0~0.0000~0.2000"
        raw["packageDetail"]["dataStr"]["shape"].extend([
            "CIRCLE~4000~3000~2~0.5~3~circle~0",
            "ARC~0.25~3~~M 4000 3000 A 10 10 0 0 1 4010 3010~~arc~0",
            "SOLIDREGION~5~~M 4000 3000 L 4001 3000 L 4001 3001 Z~solid~paste~0",
            "SOLIDREGION~100~~M 4000 3000 L 4001 3000 L 4001 3001 Z~cutout~negative~0",
        ])
        library = component_to_library(raw, "C25804")
        footprint = library.footprints[0]
        pad = footprint.pads[0]
        self.assertAlmostEqual(pad.size[0], 0.3640074, places=12)
        self.assertAlmostEqual(pad.size[1], 1.7420082, places=12)
        self.assertAlmostEqual(pad.x, 3.1234 * 0.254, places=12)
        self.assertAlmostEqual(pad.y, -0.5678 * 0.254, places=12)
        circle = next(graphic for graphic in footprint.graphics if graphic.kind == "circle")
        self.assertEqual(circle.points, [(0, 0)])
        self.assertEqual(float(circle.properties["radius_mm"]), 0.508)
        arc = next(graphic for graphic in footprint.graphics if graphic.kind == "arc")
        self.assertEqual(arc.points, [(0, 0), (2.54, -2.54)])
        self.assertEqual((float(arc.properties["center_x_mm"]), float(arc.properties["center_y_mm"])), (0, -2.54))
        self.assertEqual(arc.properties["clockwise"], "true")
        paste, cutout = [graphic for graphic in footprint.graphics if graphic.kind == "region"]
        self.assertEqual((paste.layer, paste.width, paste.properties["filled"]), ("paste_top", 0, "true"))
        self.assertEqual((cutout.layer, cutout.properties["region_type"], cutout.properties["filled"]), ("source_lead_shape", "cutout", "false"))
        self.assertEqual(paste.points[0], paste.points[-1])
        self.assertNotIn("Source_Mask_Top_mm", pad.properties)
        self.assertNotIn("Source_Paste_Top_mm", pad.properties)
        self.assertTrue(any("Unknown per-pad apertures" in warning for warning in library.warnings))

    def test_source_text_glyphs_and_hidden_reference_metadata_are_preserved(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"].extend([
            "TEXT~N~4000~3000~0.2~0~none~3~~2~R1~M 4000 3000 L 4001 3000 M 4001 3000 L 4001 3001~~text1~0",
            "TEXT~N~4002~3000~0.2~0~none~3~~2~R2~M 4002 3000 L 4003 3000~none~text2~0",
        ])
        graphics = component_to_library(raw, "C25804").footprints[0].graphics
        glyphs = [graphic for graphic in graphics if graphic.properties.get("source_text_glyph") == "true"]
        self.assertEqual(len(glyphs), 2)
        texts = [graphic for graphic in graphics if graphic.kind == "text"]
        self.assertEqual([graphic.text for graphic in texts], ["R1", "R2"])
        self.assertEqual(texts[0].properties["source_geometry_exported"], "true")
        self.assertEqual(texts[0].properties["text_type"], "N")
        self.assertEqual(texts[1].properties["visible"], "false")

    def test_unsupported_source_geometry_is_rejected_instead_of_omitted_or_redrawn(self):
        for record, message in [
            ("SOLIDREGION~3~~M 4000 3000 C 4001 3001 4002 3002 4003 3003 Z~solid~curve~0", "CubicBezier"),
            ("ARC~1~3~~M 4000 3000 A 10 5 0 0 1 4010 3010~~ellipse~0", "elliptical arc"),
            ("ARC~1~3~~M 4000 3000 L 4001 3000 B 1 2~~bad~0", "unsupported commands"),
            ("TRACK~1~88~~4000 3000 4001 3000~layer~0", "Unsupported original footprint layer"),
        ]:
            with self.subTest(record=record):
                raw = resistor_data()
                raw["packageDetail"]["dataStr"]["shape"].append(record)
                with self.assertRaisesRegex(CatalogError, message):
                    component_to_library(raw, "C25804")

    def test_original_model_transform_is_not_adjusted_from_outline_or_scaled(self):
        raw = resistor_data()
        node = json.loads(raw["packageDetail"]["dataStr"]["shape"][-1].split("~", 1)[1])
        node["attrs"].update(c_origin="4001,3002", z="3", c_width="900", c_height="700")
        node["childNodes"] = [{"attrs": {"points": "5000 6000 9000 10000"}}]
        raw["packageDetail"]["dataStr"]["shape"][-1] = "SVGNODE~" + json.dumps(node)
        footprint = component_to_library(raw, "C25804").footprints[0]
        self.assertEqual(json.loads(footprint.properties["Catalog_3D_Offset_mm"]), [0.254, -0.508, 0.762])
        self.assertEqual(json.loads(footprint.properties["Catalog_3D_Rotation_deg"]), [0, 0, 90])
        self.assertEqual(footprint.properties["Source_3D_Transform_Complete"], "true")
        self.assertNotIn("Catalog_3D_Target_Size_mm", footprint.properties)

    def test_missing_or_unrelated_model_transform_is_reported_without_inference(self):
        raw = resistor_data()
        library = component_to_library(raw, "C25804", {"step": "00000000000000000000000000000000"})
        self.assertNotIn("Catalog_3D_Offset_mm", library.footprints[0].properties)
        self.assertTrue(any("unverified model" in warning for warning in library.warnings))
        node = json.loads(raw["packageDetail"]["dataStr"]["shape"][-1].split("~", 1)[1])
        del node["attrs"]["z"]
        raw["packageDetail"]["dataStr"]["shape"][-1] = "SVGNODE~" + json.dumps(node)
        library = component_to_library(raw, "C25804")
        self.assertNotIn("Catalog_3D_Offset_mm", library.footprints[0].properties)
        self.assertTrue(any("missing transform fields: z" in warning for warning in library.warnings))

    def test_source_record_is_unchanged_and_graphics_survive_library_merge(self):
        raw = resistor_data()
        raw["dataStr"]["shape"].append("R~10~-4~~~20~8~#880000~1~0~none~body~0")
        before = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        left = component_to_library(raw, "C25804")
        right = component_to_library(raw, "C25804")
        self.assertEqual(left.properties["Source_Record_JSON"], before)
        self.assertEqual(left.properties["Source_Record_SHA256"], hashlib.sha256(before.encode()).hexdigest())
        self.assertEqual(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")), before)
        merged = merge_libraries(left, right)
        self.assertEqual(len(merged.components[0].graphics), 2)

    def test_source_undefined_type_and_drill_only_holes_are_not_reinterpreted(self):
        raw = resistor_data()
        raw["dataStr"]["shape"][0] = raw["dataStr"]["shape"][0].replace("P~show~1~", "P~show~0~")
        raw["packageDetail"]["dataStr"]["shape"].append("HOLE~4000~3000~4~hole~0")
        library = component_to_library(raw, "C25804")
        self.assertEqual(library.components[0].pins[0].type, "Undefined")
        self.assertEqual(library.components[0].pins[0].properties["Source_Pin_Electrical_Type"], "0")
        hole = library.footprints[0].pads[-1]
        self.assertEqual((hole.role, hole.layers, hole.hole_diameter), ("npth", [], 1.016))
        self.assertEqual(hole.properties["Source_Copper_Absent"], "true")

    def test_source_multilayer_pad_preserves_all_layer_role(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = "PAD~OVAL~4003~3000~3.2~3.4~11~~2~0.5~~0~pad2~0~~Y~0"
        pad = component_to_library(raw, "C25804").footprints[0].pads[0]
        self.assertEqual(pad.layers, ["all"])
        self.assertEqual(pad.properties["Source_Pad_Layer"], "11")
        self.assertEqual(pad.kind, "thru_hole")

    def test_rejects_polygon_and_missing_number_mapping(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = raw["packageDetail"]["dataStr"]["shape"][0].replace("PAD~RECT", "PAD~POLYGON")
        with self.assertRaisesRegex(CatalogError, "custom/polygon"):
            component_to_library(raw, "C25804")
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = raw["packageDetail"]["dataStr"]["shape"][0].replace("~1~~2~", "~1~~3~")
        with self.assertRaisesRegex(CatalogError, "absent from the footprint"):
            component_to_library(raw, "C25804")

    def test_rejects_split_pads_and_copper_graphics(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"].append(raw["packageDetail"]["dataStr"]["shape"][0])
        with self.assertRaisesRegex(CatalogError, "Multiple physical pads"):
            component_to_library(raw, "C25804")
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][2] = "TRACK~0.5~1~~3995 2997 4005 2997~track1~0"
        with self.assertRaisesRegex(CatalogError, "copper graphics"):
            component_to_library(raw, "C25804")

    def test_rejects_invalid_coordinates_and_unrepresented_ellipses(self):
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = raw["packageDetail"]["dataStr"]["shape"][0].replace("~4003~", "~nan~")
        with self.assertRaisesRegex(CatalogError, "non-finite"):
            component_to_library(raw, "C25804")
        raw = resistor_data()
        raw["packageDetail"]["dataStr"]["shape"][0] = raw["packageDetail"]["dataStr"]["shape"][0].replace("PAD~RECT", "PAD~ELLIPSE")
        with self.assertRaisesRegex(CatalogError, "elliptical pad"):
            component_to_library(raw, "C25804")

    def test_keyword_search_and_exact_lookup_cache(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            if "selectSmtComponentList" in request.full_url:
                data = {"code": 200, "data": {"componentPageInfo": {"total": 1, "list": [{"componentCode": "C7593", "componentModelEn": "NE555DR", "componentBrandEn": "TI", "componentSpecificationEn": "SOIC-8", "stockCount": 30}]}}}
            elif request.full_url.endswith("searchByNumbers"):
                data = {"success": True, "result": [{"uuid": "b210af5a1436310a86e8b108e2e5a90b", "number": "C25804", "step": "6bd5cd867e9542ebae21caaf5d2d4c4d"}]}
            else:
                data = {"success": True, "result": resistor_data()}
            return io.BytesIO(json.dumps(data).encode())

        with tempfile.TemporaryDirectory() as folder:
            provider = EasyEDACatalog(Path(folder), urlopen=opener)
            rows = provider.search("NE555")
            self.assertEqual(rows[0].to_dict()["lcsc_id"], "C7593")
            self.assertEqual(rows[0].stock, 30)
            self.assertIsNone(rows[0].has_step)
            self.assertEqual(provider.last_total, 1)
            self.assertEqual(provider.search("c25804")[0].part_id, "C25804")
            self.assertEqual(len(provider.fetch_part("C25804").footprints), 1)
            # The exact lookup first tries the faster official JLCPCB search
            # row, then falls back to the EasyEDA CAD lookup because this
            # fixture deliberately returns a different row.
            self.assertEqual(len(calls), 4)
            self.assertIn("Original source license", (Path(folder) / "C25804.json").read_text())

    def test_exact_search_uses_fast_official_row_without_downloading_cad(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            assert "selectSmtComponentList" in request.full_url
            data = {"code": 200, "data": {"componentPageInfo": {"total": 1, "list": [{
                "componentCode": "C25804", "componentModelEn": "R0603", "componentBrandEn": "Yageo",
                "componentSpecificationEn": "0402", "stockCount": 99,
            }]}}}
            return io.BytesIO(json.dumps(data).encode())

        with tempfile.TemporaryDirectory() as folder:
            row = EasyEDACatalog(Path(folder), urlopen=opener).search("c25804")
        self.assertEqual(row[0].part_id, "C25804")
        self.assertEqual(row[0].stock, 99)
        self.assertEqual(len(calls), 1)

    def test_exact_search_reuses_cached_component_without_network(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise AssertionError("a cached exact component must not access the network")

        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "C25804.json"
            cache.write_text(json.dumps({
                "schema": 1, "lcsc_id": "C25804", "source": "JLCEDA/EasyEDA Official Library",
                "lookup": {"number": "C25804", "uuid": resistor_data()["uuid"], "step": "6bd5cd867e9542ebae21caaf5d2d4c4d"},
                "component": resistor_data(),
            }), encoding="utf-8")
            rows = EasyEDACatalog(Path(folder), urlopen=opener).search("c25804")
        self.assertEqual(rows[0].part_id, "C25804")
        self.assertEqual(rows[0].name, "0603WAF1002T5E")
        self.assertEqual(calls, [])

    def test_wrong_identity_and_invalid_input_never_fall_back_to_another_part(self):
        raw = resistor_data()
        with self.assertRaisesRegex(CatalogError, "while C2040 was requested"):
            component_to_library(raw, "C2040")
        with self.assertRaises(CatalogError):
            EasyEDACatalog().fetch_part("../../C25804")

    def test_unknown_search_error_is_not_reported_as_no_results(self):
        def opener(request, timeout):
            return io.BytesIO(b'{"code":403,"message":"unavailable"}')
        with self.assertRaisesRegex(CatalogError, "rejected"):
            EasyEDACatalog(urlopen=opener).search("NE555")


if __name__ == "__main__":
    unittest.main()
