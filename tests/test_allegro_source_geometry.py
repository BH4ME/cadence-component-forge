from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.generators.allegro_skill import (
    _padstack_name,
    source_refdes_report,
    write_allegro_outputs,
    write_footprint_build_skill,
)
from jlc2cadence.ir.schema import Footprint, Graphic, LibraryIR, Pad


def source_pad(**kwargs) -> Pad:
    properties = {"Conversion_Policy": "source_exact"}
    properties.update(kwargs.pop("properties", {}))
    values = {"number": "1", "x": -2.870962, "y": 1.3, "size": (0.364007, 1.742008),
              "shape": "oval", "rotation": 90.0, "properties": properties}
    values.update(kwargs)
    return Pad(**values)


def source_footprint(pads: list[Pad] | None = None, graphics: list[Graphic] | None = None) -> Footprint:
    return Footprint("SOURCE_IC", pads=[source_pad()] if pads is None else pads,
                     graphics=graphics or [], properties={"Conversion_Policy": "source_exact"})


def sourced_prefix(footprint: Footprint, prefix: str = "U", raw: str | None = None) -> Footprint:
    footprint.properties.update({
        "Source_Refdes_Prefix": prefix,
        "Source_Refdes_Prefix_Source": "dataStr.head.c_para.pre",
        "Source_Refdes_Prefix_Raw": raw if raw is not None else prefix + "?",
        "Source_Refdes_Component": "SOURCE_COMPONENT",
        "Source_Refdes_Prefix_Status": "verified",
    })
    return footprint


def source_refdes_graphic(**changes) -> Graphic:
    values = {"kind": "text", "layer": "silkscreen_top", "text": "U?", "x": 0.0, "y": 3.0,
              "properties": {"text_type": "N", "width_mm": "0.4", "height_mm": "0.8",
                             "stroke_width_mm": "0.1", "line_spacing_mm": "0", "char_spacing_mm": "0",
                             "font": "cadence_stroke", "visible": "true", "mirrored": "false", "justify": "center"}}
    values.update(changes)
    return Graphic(**values)


class AllegroSourceGeometryTests(unittest.TestCase):
    def build(self, footprint: Footprint) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.il"
            write_footprint_build_skill(footprint, path)
            return path.read_text(encoding="utf-8")

    def test_source_pad_dimensions_position_rotation_are_not_recalculated(self):
        output = self.build(source_footprint())
        self.assertIn("?figure 'OBLONG_Y", output)
        self.assertIn("?figureSize 0.364007:1.742008", output)
        self.assertIn(' -2.870962:1.3 pinText 90.0)', output)
        self.assertNotIn("Conservative package place bound", output)
        self.assertNotIn("Standard reference/value helper text", output)
        self.assertNotIn('"PACKAGE GEOMETRY/PLACE_BOUND_TOP"', output)
        self.assertNotIn("axlDBCreateText(", output)

    def test_mask_paste_use_source_dimensions_and_explicit_absence(self):
        pad = source_pad(properties={
            "Source_Mask_Top_mm": "[0.464007,1.842008]",
            "Source_Paste_Top_mm": "[0.344007,1.722008]",
            "Source_Mask_Bottom_mm": "null", "Source_Paste_Bottom_mm": "null",
        })
        output = self.build(source_footprint([pad]))
        self.assertIn('?layer "SOLDERMASK_TOP"', output)
        self.assertIn("?figureSize 0.464007:1.842008", output)
        self.assertIn("?figureSize 0.344007:1.722008", output)
        self.assertNotIn('?layer "SOLDERMASK_BOTTOM"', output)
        self.assertNotIn('?layer "PASTEMASK_BOTTOM"', output)
        self.assertNotIn("source aperture data unavailable", output)

    def test_unknown_mask_paste_are_omitted_and_reported(self):
        library = LibraryIR(footprints=[source_footprint()])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_allegro_outputs(library, root / "allegro", root / "scripts")
            output = (root / "allegro/symbols/build_scripts/SOURCE_IC.il").read_text(encoding="utf-8")
        self.assertNotIn('?layer "SOLDERMASK_TOP"', output)
        self.assertNotIn('?layer "PASTEMASK_TOP"', output)
        self.assertTrue(any("SOLDERMASK_TOP source aperture data unavailable" in warning for warning in library.warnings))
        self.assertTrue(any("PASTEMASK_TOP source aperture data unavailable" in warning for warning in library.warnings))
        self.assertTrue(any("four decimals" in warning for warning in library.warnings))

    def test_generic_conversion_keeps_existing_generated_helpers(self):
        footprint = Footprint("GENERIC", pads=[Pad("1", 0.0, 0.0, size=(0.4, 1.8))])
        output = self.build(footprint)
        self.assertIn('?layer "SOLDERMASK_TOP"', output)
        self.assertIn('?layer "PASTEMASK_TOP"', output)
        self.assertIn("Conservative package place bound", output)
        self.assertIn("Standard reference/value helper text", output)
        self.assertIn('axlDBCreateText("U*"', output)

    def test_verified_catalog_prefix_adds_only_dynamic_native_refdes(self):
        for prefix, raw in [("U", "U?"), ("R", "R?"), ("R", "R12")]:
            with self.subTest(raw=raw):
                footprint = sourced_prefix(source_footprint(), prefix, raw)
                before = footprint.to_dict()
                output = self.build(footprint)
                report = source_refdes_report(footprint)
                self.assertEqual(before, footprint.to_dict())
                self.assertEqual(output.count("axlDBCreateText("), 1)
                self.assertIn(f'axlDBCreateText("{prefix}*"', output)
                self.assertNotIn('axlDBCreateText("R12"', output)
                self.assertIn('"REF DES/ASSEMBLY_TOP" nil)', output)
                self.assertNotIn("Standard reference/value helper text", output)
                self.assertNotIn('"PACKAGE GEOMETRY/PLACE_BOUND_TOP"', output)
                self.assertEqual(report["status"], "derived_from_component")
                self.assertEqual(report["source"]["raw"], raw)
                self.assertEqual(report["source"]["field"], "dataStr.head.c_para.pre")
                self.assertEqual(report["target"]["text"], prefix + "*")
                self.assertEqual(report["target"]["role"], "cadence_required_dynamic_refdes")

    def test_unsourced_or_conflicting_prefix_never_uses_default_u(self):
        for changes, status in [
            ({"Source_Refdes_Prefix_Status": "missing"}, "missing_source_prefix"),
            ({"Source_Refdes_Prefix_Status": "conflict"}, "conflicting_source_prefix"),
            ({"Source_Refdes_Prefix": "U1"}, "invalid_source_prefix"),
            ({"Source_Refdes_Prefix_Raw": "R?"}, "invalid_source_prefix"),
            ({"Source_Refdes_Prefix_Source": ""}, "invalid_source_prefix"),
            ({"Source_Refdes_Component": ""}, "invalid_source_prefix"),
        ]:
            with self.subTest(changes=changes):
                footprint = sourced_prefix(source_footprint())
                footprint.properties.update(changes)
                output = self.build(footprint)
                self.assertNotIn("axlDBCreateText(", output)
                self.assertEqual(source_refdes_report(footprint)["status"], status)
                self.assertIn("no helper text added; Cadence package compilation may fail", output)

    def test_mixed_case_catalog_prefix_records_native_uppercase_without_source_changes(self):
        for prefix in ("DSub", "Card", "U"):
            with self.subTest(prefix=prefix):
                footprint = sourced_prefix(source_footprint(), prefix)
                before = footprint.to_dict()
                report = source_refdes_report(footprint)
                output = self.build(footprint)
                self.assertEqual(footprint.to_dict(), before)
                self.assertEqual(report["source"]["prefix"], prefix)
                self.assertEqual(report["source"]["raw"], prefix + "?")
                self.assertEqual(report["target"]["text"], prefix.upper() + "*")
                self.assertIn(f'axlDBCreateText("{prefix.upper()}*"', output)
                self.assertIn(f'sourceRefdes->text == "{prefix.upper()}*"', output)
                changes = report["native_case_adaptations"]
                if prefix == prefix.upper():
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(len(changes), 1)
                    self.assertEqual(changes[0]["source_text"], prefix + "*")
                    self.assertEqual(changes[0]["native_text"], prefix.upper() + "*")
                    self.assertTrue(changes[0]["documentation"])

    def test_original_refdes_retains_text_but_checks_native_ascii_case(self):
        footprint = sourced_prefix(source_footprint(graphics=[source_refdes_graphic(text="Card?")]))
        before = footprint.to_dict()
        output = self.build(footprint)
        report = source_refdes_report(footprint)
        self.assertEqual(footprint.to_dict(), before)
        self.assertEqual(report["status"], "source_preserved")
        self.assertIn('axlDBCreateText("Card?"', output)
        self.assertIn('sourceRefdes->text == "CARD?"', output)
        self.assertEqual(report["native_case_adaptations"][0]["source_text"], "Card?")
        self.assertEqual(report["native_case_adaptations"][0]["native_text"], "CARD?")

    def test_original_refdes_remains_at_source_position_without_duplicate(self):
        footprint = sourced_prefix(source_footprint(graphics=[source_refdes_graphic()]))
        output = self.build(footprint)
        self.assertEqual(output.count("axlDBCreateText("), 1)
        self.assertIn('axlDBCreateText("U?" 0.0:3.0 textOrient "REF DES/SILKSCREEN_TOP" nil)', output)
        self.assertNotIn('axlDBCreateText("U*"', output)
        self.assertEqual(source_refdes_report(footprint)["status"], "source_preserved")

    def test_unavailable_original_refdes_is_reported_without_replacement(self):
        for props in [{"text_type": "N", "visible": "false"},
                      {"text_type": "N", "visible": "true"},
                      {"text_type": "N", "source_geometry_exported": "true"}]:
            with self.subTest(props=props):
                footprint = sourced_prefix(source_footprint(graphics=[source_refdes_graphic(properties=props)]))
                output = self.build(footprint)
                self.assertNotIn("axlDBCreateText(", output)
                self.assertEqual(source_refdes_report(footprint)["status"], "source_refdes_unavailable")

    def test_generated_refdes_position_encloses_rotated_pads_and_source_circles(self):
        footprint = sourced_prefix(source_footprint(
            pads=[source_pad(x=0, y=0, size=(4, 1), rotation=90)],
            graphics=[Graphic("circle", "assembly_top", points=[(0, 5)], width=0.2,
                              properties={"radius_mm": "3"})]))
        report = source_refdes_report(footprint)
        self.assertAlmostEqual(report["source_geometry_bounds_mm"][1], -2)
        self.assertAlmostEqual(report["source_geometry_bounds_mm"][3], 8.1)
        self.assertEqual(report["target"]["position_mm"], [0, 8.6])
        self.assertIn('axlDBCreateText("U*" 0.0:8.6', self.build(footprint))

    def test_refdes_creation_checks_native_object_and_design_membership(self):
        output = self.build(sourced_prefix(source_footprint()))
        self.assertIn("result && car(result) && length(car(result)) == 1", output)
        self.assertIn("sourceRefdes = caar(result)", output)
        self.assertIn('sourceRefdes->objType == "text"', output)
        self.assertIn('sourceRefdes->layer == "REF DES/ASSEMBLY_TOP" && sourceRefdes->text == "U*"', output)
        self.assertIn("foreach(sourceNativeText axlDBGetDesign()->text", output)
        self.assertIn('error("Source-exact: native reference-designator is absent from design text', output)
        self.assertLess(output.index("sourceRefdesVerified = nil"), output.index("axlSaveDesign("))

    def test_build_plan_records_dynamic_refdes_origin_and_placement(self):
        library = LibraryIR(footprints=[sourced_prefix(source_footprint(), "R")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_allegro_outputs(library, root / "allegro", root / "scripts")
            plan = json.loads((root / "allegro/symbols/symbol_build_plan.json").read_text(encoding="utf-8"))
            stored = json.loads((root / "allegro/symbols/footprints.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["symbols"][0]["refdes"], source_refdes_report(library.footprints[0]))
        self.assertEqual(stored["footprints"][0]["graphics"], [])
        self.assertTrue(any("dynamic reference text R* added" in warning for warning in library.warnings))

    def test_circles_and_arcs_use_native_exact_curves(self):
        graphics = [
            Graphic("circle", "silkscreen_top", points=[(0.5, -0.25)], width=0.12,
                    properties={"radius_mm": "0.35"}),
            Graphic("arc", "silkscreen_top", points=[(1.0, 0.0), (0.0, 1.0)], width=0.08,
                    properties={"center_x_mm": "0", "center_y_mm": "0", "clockwise": "false"}),
        ]
        output = self.build(source_footprint(graphics=graphics))
        self.assertIn('axlDBCreateCircle(list(0.5:-0.25 0.35) 0.12 "PACKAGE GEOMETRY/SILKSCREEN_TOP")', output)
        self.assertIn("axlPathArcCenter(sourcePath 0.08 0.0:1.0 nil 0.0:0.0)", output)
        self.assertIn('axlDBCreatePath(sourcePath "PACKAGE GEOMETRY/SILKSCREEN_TOP")', output)

    def test_source_regions_keep_fill_and_zero_stroke_without_extra_vertices(self):
        points = [(0, 0), (2, 0), (0, 1)]
        output = self.build(source_footprint(graphics=[
            Graphic("region", "assembly_top", points=points, width=0, properties={"filled": "true"}),
            Graphic("region", "document_top", points=points, width=0, properties={"filled": "false"}),
        ]))
        self.assertIn("axlPathStart(list(0.0:0.0 2.0:0.0 0.0:1.0) 0.0)", output)
        self.assertIn('axlDBCreateShape(sourcePath t "PACKAGE GEOMETRY/ASSEMBLY_TOP")', output)
        self.assertIn('axlDBCreateShape(sourcePath nil "PACKAGE GEOMETRY/JLC_DOCUMENT")', output)
        self.assertNotIn("0.01", output)

    def test_source_layers_keep_distinct_semantic_roles(self):
        output = self.build(source_footprint(graphics=[
            Graphic("line", layer, points=[(0, 0), (1, 0)], width=0.1)
            for layer in ["document_top", "mechanical", "source_component_shape",
                          "source_lead_shape", "source_component_polarity", "paste_top", "mask_bottom"]
        ]))
        for subclass in ["JLC_DOCUMENT", "JLC_MECHANICAL", "JLC_COMPONENT_SHAPE", "JLC_LEAD_SHAPE",
                         "JLC_COMPONENT_POLARITY", "PASTEMASK_TOP", "SOLDERMASK_BOTTOM"]:
            self.assertIn(f'axlLayerCreateNonConductor("PACKAGE GEOMETRY/{subclass}")', output)
        self.assertNotIn('"PACKAGE GEOMETRY/ASSEMBLY_TOP"', output)

    def test_negative_annotation_has_diagnostic_boundary_and_explicit_warning(self):
        graphic = Graphic("region", "source_lead_shape", points=[(0, 0), (1, 0), (0, 1)],
                          properties={"region_type": "cutout", "filled": "false"})
        output = self.build(source_footprint(graphics=[graphic]))
        self.assertIn('axlDBCreateShape(sourcePath nil "PACKAGE GEOMETRY/JLC_LEAD_SHAPE_CUTOUT")', output)
        self.assertIn("negative fill semantics are not converted", output)
        self.assertNotIn('axlDBCreateShape(sourcePath t ', output)

    def test_missing_source_text_metrics_omits_native_text_and_reports_reason(self):
        output = self.build(source_footprint(graphics=[
            Graphic("text", "silkscreen_top", text="U?", x=0.0, y=3.0,
                    properties={"text_type": "N", "height_mm": "0.8", "visible": "true"}),
        ]))
        self.assertIn("width_mm unavailable; native text omitted", output)
        self.assertNotIn("axlDBCreateText(", output)
        self.assertNotIn("axlDBTextBlockCreate(", output)

    def test_original_glyph_text_is_not_displayed_twice(self):
        output = self.build(source_footprint(graphics=[
            Graphic("text", "silkscreen_top", text="SOURCE", properties={"source_geometry_exported": "true"}),
            Graphic("line", "silkscreen_top", points=[(1.0, 1.0), (1.0, 2.0)], width=0.12,
                    properties={"source_text_glyph": "true"}),
        ]))
        self.assertIn('axlDBCreateLine(list(1.0:1.0 1.0:2.0) 0.12', output)
        self.assertNotIn("axlDBCreateText(", output)

    def test_padstack_identity_includes_full_size_aperture_absence_and_layer(self):
        original = source_pad(size=(0.364007, 1.742008))
        changed = [
            source_pad(size=(0.364008, 1.742008)),
            source_pad(properties={"Source_Mask_Top_mm": "[0.4,1.8]"}),
            source_pad(properties={"Source_Mask_Top_mm": "null"}),
            source_pad(layers=["bottom"]),
        ]
        names = {_padstack_name(pad) for pad in [original, *changed]}
        self.assertEqual(len(names), 5)
        self.assertEqual(_padstack_name(original), _padstack_name(source_pad(x=100, y=-200, number="99")))

    def test_explicit_padstack_name_cannot_hide_different_definitions(self):
        with self.assertRaisesRegex(ValueError, "conflicting padstack definitions"):
            self.build(source_footprint([source_pad(padstack="shared"),
                                         source_pad(number="2", padstack="shared", size=(0.4, 1.8))]))

    def test_source_roundrect_radius_is_required(self):
        with self.assertRaisesRegex(ValueError, "corner_radius_mm is missing"):
            self.build(source_footprint([source_pad(shape="roundrect")]))
        output = self.build(source_footprint([source_pad(shape="roundrect", properties={"corner_radius_mm": "0.07"})]))
        self.assertIn('?radius 0.07 ?corners "UL-UR-LR-LL"', output)

    def test_unknown_or_invalid_electrical_geometry_rejects(self):
        for pad in [source_pad(shape="polygon"), source_pad(layers=["unknown"]),
                    source_pad(shape="circle", size=(0.4, 0.8)), source_pad(size=(0.4, 0))]:
            with self.subTest(pad=pad), self.assertRaises(ValueError):
                self.build(source_footprint([pad]))
        with self.assertRaisesRegex(ValueError, "source copper cutout"):
            self.build(source_footprint(graphics=[Graphic("region", "top", points=[(0, 0), (1, 0), (0, 1)],
                                                         properties={"filled": "false", "region_type": "cutout"})]))
        with self.assertRaisesRegex(ValueError, "non-plated drill region"):
            self.build(source_footprint(graphics=[Graphic("region", "mechanical", points=[(0, 0), (1, 0), (0, 1)],
                                                         properties={"filled": "false", "region_type": "npth"})]))

    def test_unknown_annotation_is_not_mapped_to_an_unrelated_layer(self):
        output = self.build(source_footprint(graphics=[
            Graphic("line", "unknown_layer_200", points=[(0, 0), (1, 1)], width=0.1),
        ]))
        self.assertIn("unsupported source layer", output)
        self.assertNotIn("axlDBCreateLine(", output)

    def test_through_hole_copper_layers_and_slot_shape_are_not_inferred(self):
        pad = source_pad(kind="thru_hole", shape="rect", size=(1.6, 1.6), hole_diameter=0.8,
                         layers=["top", "bottom"], properties={"plated": "true"})
        output = self.build(source_footprint([pad]))
        self.assertNotIn("DEFAULT_INTERNAL", output)
        with self.assertRaisesRegex(ValueError, "source slot shape"):
            self.build(source_footprint([source_pad(kind="thru_hole", slot_size=(0.5, 1.0),
                                                    properties={"plated": "true"})]))

    def test_invalid_aperture_dimensions_do_not_fall_back_to_copper(self):
        for value in ["[0,1]", "[0.1]", '"unknown"', "[true,1]"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(source_footprint([source_pad(properties={"Source_Mask_Top_mm": value})]))

    def test_source_mounting_hole_does_not_acquire_copper_pads(self):
        pad = source_pad(number="MH1", role="npth", kind="thru_hole", shape="circle", size=(0.8, 0.8),
                         hole_diameter=0.8, layers=[], properties={"plated": "false", "Source_Copper_Absent": "true"})
        output = self.build(source_footprint([pad]))
        self.assertIn("?drillDiameter 0.8 ?plating 'NON_PLATED", output)
        self.assertNotIn("make_axlPadStackPad(", output)
        self.assertIn("nil 90.0)", output)

    def test_offset_drill_and_missing_slot_orientation_cannot_be_lost(self):
        with self.assertRaisesRegex(ValueError, "offset drill"):
            self.build(source_footprint([source_pad(properties={"Source_Drill_Offset_mm": "[0.1,0]"})]))
        pad = source_pad(kind="thru_hole", hole_diameter=0.5, slot_size=(0.5, 1.0),
                         properties={"plated": "true", "Source_Slot_Shape": "oval"})
        with self.assertRaisesRegex(ValueError, "slot orientation is missing"):
            self.build(source_footprint([pad]))

    def test_native_padstack_pin_graphic_and_save_failures_abort_before_publication(self):
        output = self.build(source_footprint(graphics=[
            Graphic("line", "silkscreen_top", points=[(0, 0), (1, 0)], width=0.1),
        ]))
        self.assertIn('error("ERROR: failed to create padstack', output)
        self.assertIn('error("Source-exact: could not write padstack', output)
        self.assertIn('error("ERROR: failed to create pin', output)
        self.assertIn('error("Source-exact: failed creating source', output)
        self.assertIn('error("ERROR: axlSaveDesign failed', output)
        self.assertIn('error("Source-exact: native millimeter units/accuracy', output)


if __name__ == "__main__":
    unittest.main()
