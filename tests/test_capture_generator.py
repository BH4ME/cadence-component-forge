from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.generators.capture import write_capture_olb_xml
from jlc2cadence.ir.schema import Component, LibraryIR, Pin


class CaptureGeneratorTests(unittest.TestCase):
    def normal_view(self, pins: list[Pin]) -> ElementTree.Element:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "library.xml"
            write_capture_olb_xml(LibraryIR(components=[Component(name="MCU", pins=pins)]), output)
            return ElementTree.parse(output).getroot().find("Package/LibPart/NormalView")

    def test_symbol_uses_unfilled_body_and_instance_reference_property(self) -> None:
        normal = self.normal_view([Pin(number="1", name="A")])
        self.assertEqual(normal.find("Rect/Defn").get("fillStyle"), "1")
        display_names = [item.get("name") for item in normal.findall("SymbolDisplayProp/Defn")]
        self.assertEqual(display_names, ["Part Reference", "Value"])
        self.assertEqual(normal.find("Reference/Defn").get("name"), "U?")
        self.assertEqual(normal.find("Defn").get("suffix"), "Normal")

    def test_opposing_long_pin_labels_have_space_and_grid_aligned_connections(self) -> None:
        pins = [
            Pin(number="1", name="PF0-OSC_IN", side="left", type="Input"),
            Pin(number="2", name="PA14-SWCLK", side="right", type="Input"),
            Pin(number="3", name="WWWWWWWWWWWW", side="left", type="Output"),
            Pin(number="4", name="ADC_EXT_TRIGGER", side="right", type="Input"),
        ]
        normal = self.normal_view(pins)
        box = normal.find("SymbolBBox/Defn")
        width = int(box.get("x2")) - int(box.get("x1"))
        self.assertGreaterEqual(width, (12 + 15) * 9 + 30)
        symbol_pins = normal.findall("SymbolPinScalar/Defn")
        self.assertEqual([pin.get("name") for pin in symbol_pins], [pin.name for pin in pins])
        self.assertEqual([pin.get("type") for pin in symbol_pins], ["0", "0", "1", "0"])
        self.assertEqual([pin.get("startX") for pin in symbol_pins], ["0", str(width), "0", str(width)])
        for pin in symbol_pins:
            for key in ("startX", "startY", "hotptX", "hotptY"):
                self.assertEqual(int(pin.get(key)) % 10, 0)

    def test_top_and_bottom_tracks_fit_labels_and_keep_properties_clear_of_pins(self) -> None:
        pins = [
            Pin(number=str(index + 1 + side_index * 16), name=f"OSC_CLOCK_{index}", side=side)
            for side_index, side in enumerate(("top", "bottom")) for index in range(16)
        ]
        normal = self.normal_view(pins)
        box = normal.find("SymbolBBox/Defn")
        width, height = int(box.get("x2")), int(box.get("y2"))
        symbol_pins = normal.findall("SymbolPinScalar/Defn")
        for side_pins in (symbol_pins[:16], symbol_pins[16:]):
            previous = None
            for pin in side_pins:
                x = int(pin.get("startX"))
                self.assertEqual(x % 10, 0)
                self.assertGreaterEqual(x, len(pin.get("name")) * 9)
                self.assertGreaterEqual(width - x, len(pin.get("name")) * 9)
                if previous is not None:
                    self.assertGreaterEqual(x - previous, len(pin.get("name")) * 9 + 10)
                previous = x
        properties = {item.get("name"): int(item.get("locY"))
                      for item in normal.findall("SymbolDisplayProp/Defn")}
        self.assertGreater(properties["Part Reference"], height + 10)
        self.assertLess(properties["Value"], -10)

    def test_hidden_pin_names_do_not_expand_visible_symbol_and_default_sides_are_measured(self) -> None:
        visible = [Pin(number="1", name="LONG_LEFT_LABEL", side=""),
                   Pin(number="2", name="LONG_RIGHT_LABEL", side="")]
        normal = self.normal_view(visible)
        hidden = self.normal_view([Pin(number="99", name="W" * 200, hidden=True)] + visible)
        self.assertEqual(normal.find("SymbolBBox/Defn").attrib, hidden.find("SymbolBBox/Defn").attrib)
        width = int(normal.find("SymbolBBox/Defn").get("x2"))
        self.assertGreaterEqual(width, (15 + 16) * 9 + 30)
        symbol_pins = hidden.findall("SymbolPinScalar/Defn")
        self.assertEqual([pin.get("visible") for pin in symbol_pins], ["0", "1", "1"])
        self.assertEqual(symbol_pins[1].get("startX"), "0")
        self.assertEqual(symbol_pins[2].get("startX"), str(width))


if __name__ == "__main__":
    unittest.main()
