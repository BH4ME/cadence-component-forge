from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.generators.capture import write_capture_olb_xml, write_capture_outputs
from jlc2cadence.ir.schema import Component, LibraryIR, Pin


class CapturePropertyLimitTests(unittest.TestCase):
    def render(self, component, directory):
        output = Path(directory) / "library.olb.xml"
        with patch("jlc2cadence.generators.capture._capture_text_metrics", return_value=(8, 10, 20)):
            write_capture_olb_xml(LibraryIR(components=[component]), output)
        return output, ElementTree.parse(output).getroot()

    def test_native_property_at_observed_limit_stays_inline(self):
        value = "x" * 32767
        component = Component(name="BOUNDARY", properties={"Original metadata": value})
        with tempfile.TemporaryDirectory() as directory:
            output, root = self.render(component, directory)
            native = root.find("Package/PackageUserProp/Defn[@name='Original metadata']")
            self.assertEqual(native.get("val"), value)
            self.assertFalse(output.with_name(output.name + ".properties.json").exists())

    def test_oversized_property_is_preserved_in_external_file_without_truncation(self):
        value = "x" * 32768
        component = Component(name="LARGE", properties={"Original metadata": value})
        with tempfile.TemporaryDirectory() as directory:
            output, root = self.render(component, directory)
            report_path = output.with_name(output.name + ".properties.json")
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            native = root.findall(".//PackageUserProp/Defn[@name='Original metadata']")
            native += root.findall(".//SymbolUserProp/Defn[@name='Original metadata']")
            self.assertEqual(len(native), 2)
            for node in native:
                reference = json.loads(node.get("val"))
                self.assertEqual(reference["format"], "jlc2cadence-capture-property-reference")
                self.assertEqual(reference["version"], 1)
                self.assertEqual(reference["file"], report_path.name)
                self.assertEqual(reference["path"], str(report_path.resolve()))
                entry = report["entries"][reference["entry"]]
                original_node = root.find(entry["xml_path"])
                self.assertEqual(original_node.tag, entry["scope"])
                self.assertEqual(original_node.find("Defn").get("name"), entry["name"])
                self.assertEqual(entry["value"], value)
                self.assertEqual(entry["sha256"], hashlib.sha256(value.encode()).hexdigest())
                self.assertLessEqual(len(node.get("val")), 32767)
            self.assertEqual(component.properties["Original metadata"], value)
            self.assertEqual(len({entry["xml_path"] for entry in report["entries"]}), 2)

    def test_outputs_keep_full_neutral_properties_and_list_external_file(self):
        value = "metadata" * 5000
        component = Component(name="LARGE", properties={"Original metadata": value})
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture"
            with patch("jlc2cadence.generators.capture._capture_text_metrics", return_value=(8, 10, 20)):
                write_capture_outputs(LibraryIR(components=[component]), capture, Path(directory) / "scripts")
            neutral = ElementTree.parse(capture / "library.capture.xml").getroot()
            original = neutral.find("components/component/properties/property[@name='Original metadata']")
            self.assertEqual(original.get("value"), value)
            plan = json.loads((capture / "capture_build_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["inputs"]["native_property_values"], "library.olb.xml.properties.json")

    def test_regeneration_without_large_values_removes_stale_external_file(self):
        component = Component(name="LARGE", properties={"Original metadata": "x" * 32768})
        with tempfile.TemporaryDirectory() as directory:
            output, _ = self.render(component, directory)
            report = output.with_name(output.name + ".properties.json")
            self.assertTrue(report.exists())
            component.properties["Original metadata"] = "small"
            self.render(component, directory)
            self.assertFalse(report.exists())

    def test_limit_counts_native_utf16_units_for_supplementary_characters(self):
        value = "\U00020000" * 16384
        component = Component(name="UNICODE", properties={"Original metadata": value})
        with tempfile.TemporaryDirectory() as directory:
            output, root = self.render(component, directory)
            report = json.loads(output.with_name(output.name + ".properties.json").read_text(encoding="utf-8"))
            self.assertEqual(report["entries"][0]["utf16_units"], 32768)
            self.assertEqual(report["entries"][0]["value"], value)
            for node in root.findall(".//SymbolUserProp/Defn") + root.findall(".//PackageUserProp/Defn"):
                self.assertLessEqual(len(node.get("val").encode("utf-16-le")) // 2, 32767)

    def test_large_source_symbol_keeps_all_pins_and_full_generated_reports(self):
        pins = []
        for index in range(64):
            y = index * 2.54
            pins.append(Pin(number=str(index + 1), name=f"IN{index}", type="Input", x=-5.08, y=y,
                length=2.54, properties={
                    "Source_Pin_Name_Visible": "true", "Source_Pin_Name_X_mm": "-2.54",
                    "Source_Pin_Name_Y_mm": str(y), "Source_Pin_Name_Font_mm": "2.54",
                    "Source_Pin_Number_Visible": "true", "Source_Pin_Number_X_mm": "-3.81",
                    "Source_Pin_Number_Y_mm": str(y), "Source_Pin_Number_Font_mm": "2.54",
                }))
        component = Component(name="SOURCE64", pins=pins, properties={"Conversion_Policy": "source_exact",
            "Source_Symbol_Units": "mm"})
        with tempfile.TemporaryDirectory() as directory:
            output, root = self.render(component, directory)
            self.assertEqual(len(root.findall(".//SymbolPinScalar")), 64)
            self.assertEqual(len(root.findall(".//CommentText")), 128)
            report_name = "Capture_Text_Anchor_Conversion_Report"
            full_report = component.properties[report_name]
            self.assertGreater(len(full_report), 32767)
            external = json.loads(output.with_name(output.name + ".properties.json").read_text(encoding="utf-8"))
            saved = [entry for entry in external["entries"] if entry["name"] == report_name]
            self.assertTrue(saved)
            self.assertTrue(all(entry["value"] == full_report for entry in saved))
            symbol_pins = root.findall(".//SymbolPinScalar/Defn")
            self.assertTrue(all(pin.get("hotptX") == "-20" and pin.get("startX") == "-10" for pin in symbol_pins))
            self.assertEqual([pin.y for pin in component.pins], [index * 2.54 for index in range(64)])


if __name__ == "__main__":
    unittest.main()
