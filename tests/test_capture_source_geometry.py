from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.generators.capture import write_capture_olb_xml
from jlc2cadence.ir.schema import Component, Graphic, LibraryIR, Pin


class CaptureSourceGeometryTests(unittest.TestCase):
    def source_pin(self, **kwargs) -> Pin:
        properties = {"Source_Pin_Name_Visible": "false", "Source_Pin_Number_Visible": "false"}
        properties.update(kwargs.pop("properties", {}))
        return Pin(number=kwargs.pop("number", "1"), name=kwargs.pop("name", "IN"),
                   properties=properties, **kwargs)

    def source_component(self, **kwargs) -> Component:
        return Component(name="SOURCE", properties={"Conversion_Policy": "source_exact",
                                                     "Source_Symbol_Units": "mm"}, **kwargs)

    def render(self, component: Component) -> tuple[ElementTree.Element, LibraryIR]:
        library = LibraryIR(components=[component])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "library.xml"
            with patch("jlc2cadence.generators.capture._capture_text_metrics", return_value=(8, 10, 20)):
                write_capture_olb_xml(library, output)
            root = ElementTree.parse(output).getroot()
        return root, library

    def test_original_body_pin_endpoint_and_electrical_type_are_retained(self) -> None:
        pin = self.source_pin(type="Input", x=-5.08, y=2.54, length=2.54, side="left")
        component = self.source_component(pins=[pin], graphics=[
            Graphic(kind="rectangle", layer="symbol", points=[(-2.54, -1.016), (2.54, 1.016)], width=.254),
        ])
        root, library = self.render(component)
        normal = root.find("Package/LibPart/NormalView")
        rect = normal.find("Rect/Defn")
        self.assertEqual([rect.get(key) for key in ("x1", "y1", "x2", "y2")], ["-10", "-4", "10", "4"])
        symbol_pin = normal.find("SymbolPinScalar/Defn")
        self.assertEqual([symbol_pin.get(key) for key in ("hotptX", "hotptY", "startX", "startY")], ["-20", "-10", "-10", "-10"])
        self.assertEqual(symbol_pin.get("type"), "0")
        self.assertEqual(normal.findall("SymbolDisplayProp"), [])
        self.assertEqual(normal.findall("CommentText"), [])
        self.assertEqual(component.properties["Capture_Max_Dimension_Error_mm"], "0")
        self.assertEqual(len(library.warnings), 1)
        self.assertIn("stroke widths", library.warnings[0])
        self.assertEqual(rect.get("lineWidth"), "0")
        self.assertEqual(json.loads(component.properties["Capture_Style_Conversion_Report"])[0]["source_stroke_width_mm"], .254)
        self.assertEqual(component.graphics[0].points, [(-2.54, -1.016), (2.54, 1.016)])

    def test_original_labels_use_their_source_anchors_without_native_reflow(self) -> None:
        pin = self.source_pin(x=-5.08, y=0, length=2.54, properties={
            "Source_Pin_Name_Visible": "true", "Source_Pin_Name_X_mm": "-1.27",
            "Source_Pin_Name_Y_mm": "0.762", "Source_Pin_Name_Font_mm": "2.54",
            "Source_Pin_Name_Font": "Verdana", "Source_Pin_Name_Rotation": "90",
            "Source_Pin_Name_Anchor": "end",
        })
        component = self.source_component(pins=[pin])
        root, _ = self.render(component)
        normal = root.find("Package/LibPart/NormalView")
        self.assertEqual(normal.find("IsPinNamesVisible/Defn").get("val"), "0")
        self.assertEqual(normal.find("IsPinNumbersVisible/Defn").get("val"), "0")
        text = normal.find("CommentText/Defn")
        self.assertEqual([text.get(key) for key in ("locX", "locY", "name", "textJustification")], ["-13", "-3", "IN", "1"])
        self.assertEqual([text.get(key) for key in ("x1", "y1", "x2", "y2")], ["-13", "-3", "-3", "17"])
        report = json.loads(component.properties["Capture_Text_Anchor_Conversion_Report"])
        self.assertEqual(report[0]["source_baseline_native"], [-5, -3])
        self.assertEqual(report[0]["native_font_ascent"], 8)
        font = normal.find("CommentText/TextFont/Defn")
        self.assertEqual((font.get("name"), font.get("height"), font.get("escapement")), ("Verdana", "-10", "900"))

    def test_rounded_source_body_uses_native_arcs_with_original_outer_dimensions(self) -> None:
        component = self.source_component(graphics=[
            Graphic(kind="rectangle", layer="symbol", points=[(-15.24, -15.24), (15.24, 12.7)],
                    width=.254, properties={"radius_x_mm": "0.508", "radius_y_mm": "0.508"}),
        ])
        root, _ = self.render(component)
        normal = root.find("Package/LibPart/NormalView")
        self.assertEqual(len(normal.findall("Line")), 4)
        self.assertEqual(len(normal.findall("Arc")), 4)
        self.assertEqual(normal.findall("Rect"), [])
        arcs = normal.findall("Arc/Defn")
        self.assertEqual(min(int(arc.get("x1")) for arc in arcs), -60)
        self.assertEqual(max(int(arc.get("x2")) for arc in arcs), 60)
        self.assertEqual(min(int(arc.get("y1")) for arc in arcs), -50)
        self.assertEqual(max(int(arc.get("y2")) for arc in arcs), 60)
        self.assertTrue(all(int(arc.get("x2")) - int(arc.get("x1")) == 4 for arc in arcs))

    def test_odd_diameter_is_preserved_and_unrepresentable_anchor_is_reported(self) -> None:
        component = self.source_component(graphics=[
            Graphic(kind="ellipse", layer="symbol", points=[(-14.351, 11.049), (-13.589, 11.811)],
                    properties={"fill": "#880000"}),
        ])
        root, library = self.render(component)
        ellipse = root.find("Package/LibPart/NormalView/Ellipse/Defn")
        self.assertEqual(int(ellipse.get("x2")) - int(ellipse.get("x1")), 3)
        self.assertEqual(int(ellipse.get("y2")) - int(ellipse.get("y1")), 3)
        self.assertEqual(ellipse.get("fillStyle"), "0")
        self.assertEqual(component.properties["Capture_Max_Dimension_Error_mm"], "0")
        self.assertEqual(component.properties["Capture_Max_Coordinate_Error_mm"], "0.127")
        self.assertTrue(json.loads(component.properties["Capture_Quantization_Report"]))
        self.assertIn("0.127 mm", library.warnings[0])
        self.assertEqual(component.graphics[0].points[0], (-14.351, 11.049))

    def test_missing_geometry_and_unsupported_shape_fail_without_substitution(self) -> None:
        with self.assertRaisesRegex(ValueError, "original symbol geometry is missing"):
            self.render(self.source_component())
        with self.assertRaisesRegex(ValueError, "unsupported; no replacement"):
            self.render(self.source_component(graphics=[Graphic(kind="bezier", layer="symbol")]))

    def test_original_circular_arc_retains_center_radius_and_sweep(self) -> None:
        component = self.source_component(graphics=[
            Graphic(kind="arc", layer="symbol", points=[(2.54, 0), (0, -2.54)],
                    properties={"center_x_mm": "0", "center_y_mm": "0", "clockwise": "true"}),
        ])
        root, _ = self.render(component)
        arc = root.find("Package/LibPart/NormalView/Arc/Defn")
        self.assertEqual([arc.get(key) for key in ("x1", "y1", "x2", "y2")], ["-10", "-10", "10", "10"])
        self.assertEqual([arc.get(key) for key in ("startX", "startY", "endX", "endY")], ["0", "10", "10", "0"])

    def test_missing_visible_label_position_is_an_explicit_failure(self) -> None:
        pin = self.source_pin(properties={"Source_Pin_Name_Visible": "true"})
        with self.assertRaisesRegex(ValueError, "original name text position/font size is missing"):
            self.render(self.source_component(pins=[pin]))

    def test_hidden_power_pin_keeps_source_type_and_is_not_made_global(self) -> None:
        pin = self.source_pin(type="Power", hidden=True, x=2.54, y=2.54, length=2.54)
        root, _ = self.render(self.source_component(pins=[pin]))
        symbol_pin = root.find("Package/LibPart/NormalView/SymbolPinScalar")
        self.assertEqual(symbol_pin.find("Defn").get("type"), "5")
        self.assertEqual(symbol_pin.find("Defn").get("startX"), "20")
        self.assertEqual(symbol_pin.find("IsGlobal/Defn").get("val"), "0")

    def test_undefined_source_electrical_type_mapping_is_reported(self) -> None:
        pin = self.source_pin(type="Undefined", length=2.54,
                              properties={"Source_Pin_Electrical_Type": "0"})
        component = self.source_component(pins=[pin])
        root, library = self.render(component)
        self.assertEqual(root.find("Package/LibPart/NormalView/SymbolPinScalar/Defn").get("type"), "4")
        self.assertEqual(json.loads(component.properties["Capture_Pin_Type_Conversion_Report"])[0]["source_type"], "Undefined")
        self.assertIn("no Undefined electrical pin type", library.warnings[0])

    def test_source_units_are_not_collapsed_by_matching_logical_pin_names(self) -> None:
        pins = [self.source_pin(number="1", unit="A", x=-2.54, length=2.54),
                self.source_pin(number="2", unit="B", x=-5.08, length=2.54)]
        root, _ = self.render(self.source_component(pins=pins))
        self.assertEqual(root.find("Package/Defn").get("isHomogeneous"), "0")
        views = root.findall("Package/LibPart/NormalView")
        self.assertEqual(len(views), 2)
        self.assertEqual([view.find("SymbolPinScalar/Defn").get("hotptX") for view in views], ["-10", "-20"])


if __name__ == "__main__":
    unittest.main()
