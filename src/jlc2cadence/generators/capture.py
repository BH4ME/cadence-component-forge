from __future__ import annotations

import json
import math
import sys
from functools import lru_cache
from hashlib import sha1, sha256
from itertools import zip_longest
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, QName, SubElement, indent, register_namespace

from .. import __version__
from ..errors import UnsupportedGeometryError
from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Graphic, LibraryIR, Pin


XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION_ATTR = QName(XSI_NS, "noNamespaceSchemaLocation")
CAPTURE_GRID = 10
PIN_SPACING = 20
PIN_LENGTH = 10
SYMBOL_WIDTH = 80
# A full 9-unit Arial font height conservatively covers wide pin-name glyphs.
PIN_LABEL_MAX_ADVANCE = 9
CAPTURE_COORDINATE_MM = 0.254
CAPTURE_PROPERTY_UTF16_LIMIT = 32767


def write_capture_outputs(library: LibraryIR, capture_dir: Path, scripts_dir: Path) -> None:
    capture_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    root_dir = scripts_dir.parent
    write_capture_olb_xml(library, capture_dir / "library.olb.xml")
    write_capture_xml(library, capture_dir / "library.capture.xml")
    write_capture_build_plan(library, capture_dir / "capture_build_plan.json")
    write_capture_readme(capture_dir / "README.md")
    write_capture_tcl(library, scripts_dir / "create_capture_olb.tcl")
    write_capture_replay(scripts_dir / "run_capture_import.tcl")
    write_capture_replay(root_dir / "run_capture_import.tcl")
    write_capture_helper(library, scripts_dir / "generate_capture_library.ps1")


def write_capture_xml(library: LibraryIR, path: Path) -> Path:
    root = Element("jlc2cadence-capture-library", {"version": f"v{__version__}"})
    props = SubElement(root, "properties")
    for key, value in sorted(library.properties.items()):
        SubElement(props, "property", {"name": key, "value": value})
    components = SubElement(root, "components")
    for component in library.components:
        _component_xml(components, component)
    tree = ElementTree(root)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def write_capture_olb_xml(library: LibraryIR, path: Path) -> Path:
    register_namespace("xsi", XSI_NS)
    root = Element("Lib", {SCHEMA_LOCATION_ATTR: "olb.xsd"})
    SubElement(root, "Defn", {"name": "jlc2cadence"})
    _default_values_xml(root)
    package_names = _capture_internal_names(library)
    for component, package_name in zip(library.components, package_names):
        if _uses_source_geometry(component):
            component.properties["Capture_Quantization_Report"] = "[]"
            component.properties["Capture_Style_Conversion_Report"] = "[]"
            component.properties["Capture_Pin_Type_Conversion_Report"] = "[]"
            component.properties["Capture_Text_Anchor_Conversion_Report"] = "[]"
        _package_xml(root, component, package_name)
        if _uses_source_geometry(component):
            coordinate_error = float(component.properties["Capture_Max_Coordinate_Error_mm"])
            dimension_error = float(component.properties["Capture_Max_Dimension_Error_mm"])
            if coordinate_error or dimension_error:
                warning = (
                    f"{component.name}: Capture integer coordinates have {CAPTURE_COORDINATE_MM:g} mm "
                    f"resolution; maximum coordinate rounding {coordinate_error:g} mm, "
                    f"maximum dimension rounding {dimension_error:g} mm. Original source geometry "
                    "is retained in the IR; see Capture_Quantization_Report."
                )
                if warning not in library.warnings:
                    library.warnings.append(warning)
            if json.loads(component.properties["Capture_Style_Conversion_Report"]):
                warning = (
                    f"{component.name}: Capture stores stroke widths as thin/medium/wide styles, "
                    "not physical dimensions. Original numeric stroke widths remain in the source IR; "
                    "native thin strokes are used and recorded in Capture_Style_Conversion_Report."
                )
                if warning not in library.warnings:
                    library.warnings.append(warning)
            if json.loads(component.properties["Capture_Pin_Type_Conversion_Report"]):
                warning = (
                    f"{component.name}: Capture has no Undefined electrical pin type; source Undefined "
                    "pins use native Passive (4). Original source type 0 remains recorded per pin in "
                    "Source_Pin_Electrical_Type and Capture_Pin_Type_Conversion_Report."
                )
                if warning not in library.warnings:
                    library.warnings.append(warning)
    _externalize_native_properties(root, library, path)
    tree = ElementTree(root)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _externalize_native_properties(root: Element, library: LibraryIR, path: Path) -> None:
    report_path = path.with_name(path.name + ".properties.json")
    entries = []
    parents = {child: parent for parent in root.iter() for child in parent}
    xml_paths = {root: "."}
    for parent in root.iter():
        tag_counts = {}
        for child in parent:
            tag_counts[child.tag] = tag_counts.get(child.tag, 0) + 1
            xml_paths[child] = f"{xml_paths[parent]}/{child.tag}[{tag_counts[child.tag]}]"
    for node in root.iter():
        if not node.tag.endswith("UserProp"):
            continue
        definition = node.find("Defn")
        if definition is None or "val" not in definition.attrib:
            continue
        value = definition.get("val", "")
        units = len(value.encode("utf-16-le")) // 2
        if units <= CAPTURE_PROPERTY_UTF16_LIMIT:
            continue
        package = node
        while package.tag != "Package" and package in parents:
            package = parents[package]
        package_definition = package.find("Defn") if package.tag == "Package" else None
        digest = sha256(value.encode("utf-8")).hexdigest()
        entry = len(entries)
        entries.append({
            "package": package_definition.get("name", "") if package_definition is not None else "",
            "scope": node.tag,
            "xml_path": xml_paths[node],
            "name": definition.get("name", ""),
            "value": value,
            "sha256": digest,
            "utf16_units": units,
        })
        # Capture's native importer crashes above 32767 UTF-16 units per property.
        definition.set("val", json.dumps({
            "format": "jlc2cadence-capture-property-reference",
            "version": 1,
            "file": report_path.name,
            "path": str(report_path.resolve()),
            "entry": entry,
            "sha256": digest,
        }, ensure_ascii=False))
    if entries:
        report_path.write_text(json.dumps({
            "format": "jlc2cadence-capture-native-properties",
            "version": 1,
            "native_property_utf16_limit": CAPTURE_PROPERTY_UTF16_LIMIT,
            "entries": entries,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        warning = (
            f"Capture native property values exceeding {CAPTURE_PROPERTY_UTF16_LIMIT} UTF-16 units "
            f"are preserved without truncation in {report_path.name}. Native properties contain "
            "the adjacent filename, original absolute path, entry index and SHA256. Keep this file "
            "with library.olb; full values also remain in the IR and neutral Capture XML."
        )
        if warning not in library.warnings:
            library.warnings.append(warning)
    else:
        report_path.unlink(missing_ok=True)


def write_capture_build_plan(library: LibraryIR, path: Path) -> Path:
    package_names = _capture_internal_names(library)
    payload = {
        "tool": "OrCAD Capture XMATIC_XML2OLB",
        "version": f"v{__version__}",
        "inputs": {
            "neutral_xml": "library.capture.xml",
            "capture_olb_xml": "library.olb.xml",
        },
        "outputs": {
            "olb": "library.olb",
            "import_log": "library.olb.import.log",
            "import_tcl": "library.olb.import.tcl",
        },
        "scripts": {
            "tcl": "../scripts/create_capture_olb.tcl",
            "replay": "../run_capture_import.tcl",
            "legacy_replay": "../scripts/run_capture_import.tcl",
            "powershell": "../scripts/generate_capture_library.ps1",
        },
        "components": [
            {
                "name": component.name,
                "capture_internal_name": package_name,
                "ref_prefix": component.ref_prefix,
                "value": component.value,
                "pcb_footprint": component.footprint,
                "pins": [pin.number for pin in component.pins],
                "hidden_pins": [pin.number for pin in component.pins if pin.hidden],
                "units": [
                    {"unit": unit, "pins": [pin.number for pin in pins]}
                    for unit, pins in _component_unit_groups(component)
                ],
                "part_kind": _capture_part_kind(component),
                "homogeneous_units": len(_homogeneous_unit_groups(component)),
                "heterogeneous_units": len(_heterogeneous_unit_groups(component)),
                "status": "xml_ready",
            }
            for component, package_name in zip(library.components, package_names)
        ],
        "notes": [
            "The converter does not write Cadence binary .olb files directly.",
            "library.olb.xml follows Cadence Capture's Lib XML structure and is intended for XMATIC_XML2OLB.",
            "Use scripts/generate_capture_library.ps1 to launch Capture with scripts/create_capture_olb.tcl.",
        ],
    }
    report_path = path.parent / "library.olb.xml.properties.json"
    if report_path.is_file():
        payload["inputs"]["native_property_values"] = report_path.name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _component_xml(parent: Element, component: Component) -> None:
    attrs = {
        "name": component.name,
        "refPrefix": component.ref_prefix,
        "value": component.value,
        "footprint": component.footprint,
    }
    node = SubElement(parent, "component", attrs)
    if component.description:
        desc = SubElement(node, "description")
        desc.text = component.description
    properties = SubElement(node, "properties")
    for key, value in sorted(component.properties.items()):
        SubElement(properties, "property", {"name": key, "value": value})
    pins = SubElement(node, "pins")
    for pin in component.pins:
        SubElement(
            pins,
            "pin",
            {
                "number": pin.number,
                "name": pin.name,
                "type": pin.type,
                "unit": pin.unit,
                "hidden": "1" if pin.hidden else "0",
                "side": pin.side,
                "x": f"{pin.x:.6f}",
                "y": f"{pin.y:.6f}",
            },
        )


def _default_values_xml(root: Element) -> None:
    defaults = SubElement(root, "DefaultValues")
    SubElement(defaults, "Defn")
    fields = [
        "1ST PART FIELD",
        "2ND PART FIELD",
        "3RD PART FIELD",
        "4TH PART FIELD",
        "5TH PART FIELD",
        "6TH PART FIELD",
        "7TH PART FIELD",
        "PCB Footprint",
    ]
    for index, name in enumerate(fields, start=1):
        field = SubElement(defaults, "DefaultPartFieldMapping")
        SubElement(field, "Defn", {"index": str(index), "val": name})


def _package_xml(root: Element, component: Component, package_name: str) -> None:
    package = SubElement(root, "Package")
    ref_prefix = _capture_ref_prefix(component)
    footprint = cadence_safe_name(component.footprint, package_name) if component.footprint else ""
    homogeneous_units = _homogeneous_unit_groups(component)
    heterogeneous_units = _heterogeneous_unit_groups(component)
    is_homogeneous_multi = len(homogeneous_units) > 1
    is_heterogeneous_multi = len(heterogeneous_units) > 1
    SubElement(
        package,
        "Defn",
        {
            "alphabeticNumbering": "1" if is_homogeneous_multi or is_heterogeneous_multi else "0",
            "isHomogeneous": "0" if is_heterogeneous_multi else "1",
            "name": package_name,
            "refdesPrefix": ref_prefix,
            "pcbLib": "",
            "pcbFootprint": footprint,
        },
    )
    if is_homogeneous_multi:
        lib_part = SubElement(package, "LibPart")
        SubElement(lib_part, "Defn", {"CellName": package_name})
        _normal_view_xml(lib_part, component, pins=homogeneous_units[0][1], include_pin_number_props=False)
        for _unit, pins in homogeneous_units:
            _physical_part_xml(lib_part, pins)
    elif is_heterogeneous_multi:
        for unit, pins in heterogeneous_units:
            lib_part = SubElement(package, "LibPart")
            SubElement(lib_part, "Defn")
            _normal_view_xml(
                lib_part,
                component,
                pins=pins,
                unit=unit,
                heterogeneous=True,
            )
            _physical_part_xml(lib_part, pins)
    else:
        lib_part = SubElement(package, "LibPart")
        SubElement(lib_part, "Defn", {"CellName": package_name})
        _normal_view_xml(lib_part, component)
        _physical_part_xml(lib_part, component.pins)
    _package_user_props_xml(package, component, footprint)


def _normal_view_xml(
    parent: Element,
    component: Component,
    pins: list[Pin] | None = None,
    include_pin_number_props: bool = True,
    unit: str | None = None,
    heterogeneous: bool = False,
) -> None:
    if _uses_source_geometry(component):
        _source_normal_view_xml(parent, component, pins, include_pin_number_props, unit, heterogeneous)
        return
    pins = pins or component.pins or [Pin(number="1", name="1", type="Passive", side="left")]
    side_pins: dict[str, list[Pin]] = {side: [] for side in ("left", "right", "top", "bottom")}
    visible_layout_index = 0
    for pin in pins:
        if not pin.hidden:
            side_pins[_pin_side(pin, visible_layout_index)].append(pin)
            visible_layout_index += 1
    opposing_labels = max(
        (_pin_label_width(left) + _pin_label_width(right)
         for left, right in zip_longest(side_pins["left"], side_pins["right"])),
        default=0,
    )
    horizontal_labels = max(
        (_pin_label_width(pin) for side in ("top", "bottom") for pin in side_pins[side]), default=0,
    )
    horizontal_pitch = _grid_ceil(max(PIN_SPACING, horizontal_labels + CAPTURE_GRID))
    horizontal_count = max(len(side_pins["top"]), len(side_pins["bottom"]))
    horizontal_width = (
        (horizontal_count - 1) * horizontal_pitch + 2 * _grid_ceil(horizontal_labels + CAPTURE_GRID)
        if horizontal_count else 0
    )
    width = _grid_ceil(max(SYMBOL_WIDTH, opposing_labels + 3 * CAPTURE_GRID, horizontal_width))
    rows = max(len(side_pins["left"]), len(side_pins["right"]), 1)
    vertical_inset = 2 * CAPTURE_GRID if side_pins["top"] else CAPTURE_GRID
    height = max(40, rows * PIN_SPACING + 20) + vertical_inset - CAPTURE_GRID
    x1 = 0
    y1 = 0
    x2 = width
    y2 = height

    normal = SubElement(parent, "NormalView")
    SubElement(normal, "Defn", {"suffix": "Normal"})
    _simple_value_element(normal, "SymbolColor", "val", "48")
    bbox = SubElement(normal, "SymbolBBox")
    SubElement(bbox, "Defn", {"x1": str(x1), "x2": str(x2), "y1": str(y1), "y2": str(y2)})
    _simple_value_element(normal, "IsPinNumbersVisible", "val", "1")
    _simple_value_element(normal, "IsPinNamesRotated", "val", "0")
    _simple_value_element(normal, "IsPinNamesVisible", "val", "1")
    _simple_value_element(normal, "ContentsLibName", "name", "")
    _simple_value_element(normal, "ContentsViewName", "name", "")
    _simple_value_element(normal, "ContentsViewType", "type", "0")
    _simple_value_element(normal, "PartValue", "name", component.value or component.name)
    _simple_value_element(normal, "Reference", "name", f"{_capture_ref_prefix(component)}?")

    _symbol_body_xml(normal, x1, y1, x2, y2)
    visible_layout_index = 0
    side_rows = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for position, pin in enumerate(pins):
        side = _pin_side(pin, visible_layout_index)
        row = side_rows[side]
        horizontal_offset = CAPTURE_GRID * (
            (width - max(0, len(side_pins[side]) - 1) * horizontal_pitch) // (2 * CAPTURE_GRID)
        ) if side in {"top", "bottom"} else CAPTURE_GRID
        if not pin.hidden:
            side_rows[side] += 1
            visible_layout_index += 1
        _symbol_pin_xml(
            normal,
            pin,
            position,
            side,
            row,
            x1,
            x2,
            y2,
            include_pin_number_props=include_pin_number_props,
            horizontal_pitch=horizontal_pitch,
            horizontal_offset=horizontal_offset,
            vertical_inset=vertical_inset,
        )
    _symbol_user_props_xml(normal, component, unit=unit, heterogeneous=heterogeneous)
    reference_y = y2 + (PIN_LENGTH + 2 * CAPTURE_GRID if side_pins["top"] else CAPTURE_GRID)
    value_y = y1 - (PIN_LENGTH + 2 * CAPTURE_GRID if side_pins["bottom"] else CAPTURE_GRID)
    _display_prop_xml(normal, "Part Reference", x1 + CAPTURE_GRID, reference_y, _capture_ref_prefix(component))
    _display_prop_xml(normal, "Value", x1 + CAPTURE_GRID, value_y, component.value or component.name)


def _uses_source_geometry(component: Component) -> bool:
    return component.properties.get("Conversion_Policy") == "source_exact"


class _SourceCoordinates:
    def __init__(self) -> None:
        self.changes: list[dict[str, str | float]] = []
        self.styles: list[dict[str, str | float]] = []
        self.pin_types: list[dict[str, str]] = []
        self.text_anchors: list[dict] = []
        self.points: list[tuple[int, int]] = []

    def value(self, value_mm: float, label: str, dimension: bool = False) -> int:
        value = float(value_mm) / CAPTURE_COORDINATE_MM
        if not math.isfinite(value):
            raise ValueError(f"Non-finite source Capture coordinate: {label}")
        native = int(math.copysign(math.floor(abs(value) + 0.5), value))
        self.record(value_mm, native, label, dimension)
        return native

    def record(self, value_mm: float, native: int, label: str, dimension: bool = False) -> None:
        converted = native * CAPTURE_COORDINATE_MM
        error = abs(converted - value_mm)
        if error > 1e-9:
            self.changes.append({"element": label, "kind": "dimension" if dimension else "coordinate",
                                 "source_mm": value_mm, "capture_mm": round(converted, 12),
                                 "error_mm": round(error, 12)})

    def point(self, point: tuple[float, float], label: str) -> tuple[int, int]:
        # Capture uses Windows page coordinates (Y down); the neutral IR uses Y up.
        result = (self.value(point[0], label + ".x"), self.value(-point[1], label + ".y"))
        self.points.append(result)
        return result

    def bbox(self, points: list[tuple[float, float]], label: str) -> tuple[int, int, int, int]:
        if len(points) != 2:
            raise ValueError(f"{label}: source bounding box must contain two corners")
        x1, x2 = sorted((points[0][0], points[1][0]))
        y1, y2 = sorted((points[0][1], points[1][1]))
        left, top = self.point((x1, y2), label + ".origin")
        width = self.value(x2 - x1, label + ".width", True)
        height = self.value(y2 - y1, label + ".height", True)
        right, bottom = left + width, top + height
        self.record(x2, right, label + ".right")
        self.record(-y1, bottom, label + ".bottom")
        self.points.append((right, bottom))
        return left, top, right, bottom

    def save(self, component: Component) -> None:
        previous = json.loads(component.properties.get("Capture_Quantization_Report", "[]"))
        changes = previous + self.changes
        previous_styles = json.loads(component.properties.get("Capture_Style_Conversion_Report", "[]"))
        previous_pin_types = json.loads(component.properties.get("Capture_Pin_Type_Conversion_Report", "[]"))
        previous_text_anchors = json.loads(component.properties.get("Capture_Text_Anchor_Conversion_Report", "[]"))
        component.properties.update({
            "Capture_Coordinate_Unit_mm": str(CAPTURE_COORDINATE_MM),
            "Capture_Coordinate_Axes": "X-right,Y-down",
            "Capture_Source_Coordinate_Axes": "X-right,Y-up",
            "Capture_Source_Translation_mm": "[0, 0]",
            "Capture_Max_Coordinate_Error_mm": f"{max((float(c['error_mm']) for c in changes if c['kind'] == 'coordinate'), default=0):.12g}",
            "Capture_Max_Dimension_Error_mm": f"{max((float(c['error_mm']) for c in changes if c['kind'] == 'dimension'), default=0):.12g}",
            "Capture_Quantization_Report": json.dumps(changes, separators=(",", ":")),
            "Capture_Style_Conversion_Report": json.dumps(previous_styles + self.styles, separators=(",", ":")),
            "Capture_Pin_Type_Conversion_Report": json.dumps(previous_pin_types + self.pin_types, separators=(",", ":")),
            "Capture_Text_Anchor_Conversion_Report": json.dumps(previous_text_anchors + self.text_anchors, separators=(",", ":")),
        })


def _source_normal_view_xml(
    parent: Element, component: Component, pins: list[Pin] | None,
    include_pin_number_props: bool, unit: str | None, heterogeneous: bool,
) -> None:
    if component.properties.get("Source_Symbol_Units") != "mm":
        raise ValueError(f"{component.name}: source symbol units are missing or unsupported")
    source_pins = component.pins if pins is None else pins
    graphics = [graphic for graphic in component.graphics
                if unit is None or graphic.properties.get("unit", "A") == unit]
    if not source_pins and not graphics:
        raise ValueError(f"{component.name}: original symbol geometry is missing")
    coordinates = _SourceCoordinates()
    normal = SubElement(parent, "NormalView")
    SubElement(normal, "Defn", {"suffix": "Normal"})
    _simple_value_element(normal, "SymbolColor", "val", "48")
    bbox = SubElement(normal, "SymbolBBox")
    _simple_value_element(normal, "IsPinNumbersVisible", "val", "0")
    _simple_value_element(normal, "IsPinNamesRotated", "val", "0")
    _simple_value_element(normal, "IsPinNamesVisible", "val", "0")
    _simple_value_element(normal, "ContentsLibName", "name", "")
    _simple_value_element(normal, "ContentsViewName", "name", "")
    _simple_value_element(normal, "ContentsViewType", "type", "0")
    _simple_value_element(normal, "PartValue", "name", component.value or component.name)
    _simple_value_element(normal, "Reference", "name", f"{_capture_ref_prefix(component)}?")
    for index, graphic in enumerate(graphics):
        _source_graphic_xml(normal, graphic, coordinates, f"{unit or 'A'}.graphic[{index}]")
    for position, pin in enumerate(source_pins):
        _source_pin_xml(normal, pin, position, coordinates, include_pin_number_props)
    coordinates.save(component)
    _symbol_user_props_xml(normal, component, unit=unit, heterogeneous=heterogeneous)
    xs = [point[0] for point in coordinates.points]
    ys = [point[1] for point in coordinates.points]
    SubElement(bbox, "Defn", {"x1": str(min(xs, default=0)), "x2": str(max(xs, default=0)),
                              "y1": str(min(ys, default=0)), "y2": str(max(ys, default=0))})


def _source_style(graphic: Graphic, coordinates: _SourceCoordinates, label: str) -> dict[str, str]:
    style = graphic.properties.get("line_style", "solid")
    if style not in {"solid", "0", ""}:
        raise ValueError(f"{label}: unsupported original symbol line style {style!r}")
    coordinates.styles.append({"element": label, "source_stroke_width_mm": graphic.width,
                               "capture_line_width_enum": "0", "capture_style": "thin",
                               "status": "physical_width_not_representable"})
    return {"lineStyle": "0", "lineWidth": "0"}


def _source_bbox_attrs(bbox: tuple[int, int, int, int]) -> dict[str, str]:
    x1, y1, x2, y2 = bbox
    return {"x1": str(x1), "y1": str(y1), "x2": str(x2), "y2": str(y2)}


def _source_graphic_xml(parent: Element, graphic: Graphic, coordinates: _SourceCoordinates, label: str) -> None:
    if graphic.properties.get("visible", "true").lower() in {"0", "false", "no"}:
        return
    if graphic.kind == "text":
        _source_text_xml(parent, graphic.text, graphic.x, graphic.y, graphic.rotation,
                         graphic.properties, coordinates, label)
        return
    style = _source_style(graphic, coordinates, label)
    fill = graphic.properties.get("fill", "none")
    fill_style = "1" if fill.lower() in {"none", "", "transparent"} else "0"
    if graphic.kind in {"rectangle", "ellipse"}:
        bounds = coordinates.bbox(graphic.points, label)
        radius_x = float(graphic.properties.get("radius_x_mm", "0"))
        radius_y = float(graphic.properties.get("radius_y_mm", "0"))
        if graphic.kind == "rectangle" and (radius_x or radius_y):
            if fill_style != "1":
                raise UnsupportedGeometryError(f"{label}: filled rounded original rectangle cannot be converted losslessly")
            rx = coordinates.value(radius_x, label + ".radius_x", True)
            ry = coordinates.value(radius_y, label + ".radius_y", True)
            _source_rounded_rectangle_xml(parent, bounds, rx, ry, style, label)
        else:
            shape = SubElement(parent, "Rect" if graphic.kind == "rectangle" else "Ellipse")
            SubElement(shape, "Defn", {**style, **_source_bbox_attrs(bounds), "fillStyle": fill_style, "hatchStyle": "0"})
    elif graphic.kind in {"line", "polyline", "polygon", "region"}:
        if len(graphic.points) < 2:
            raise ValueError(f"{label}: original polyline has fewer than two points")
        points = [coordinates.point(point, f"{label}.point[{index}]") for index, point in enumerate(graphic.points)]
        if graphic.kind == "line" and len(points) == 2:
            shape = SubElement(parent, "Line")
            SubElement(shape, "Defn", {**style, "x1": str(points[0][0]), "y1": str(points[0][1]),
                                       "x2": str(points[1][0]), "y2": str(points[1][1])})
        else:
            polygon = graphic.kind in {"polygon", "region"}
            tag = "Polygon" if polygon else "Polyline"
            shape = SubElement(parent, tag)
            SubElement(shape, "Defn", {**style, **({"fillStyle": fill_style, "hatchStyle": "0"} if polygon else {})})
            for x, y in points:
                point = SubElement(shape, tag + "Point")
                SubElement(point, "Defn", {"x": str(x), "y": str(y)})
    elif graphic.kind == "arc":
        if len(graphic.points) != 2 or any(key not in graphic.properties for key in ("center_x_mm", "center_y_mm", "clockwise")):
            raise ValueError(f"{label}: original circular arc endpoints, center or direction are missing")
        cx = float(graphic.properties["center_x_mm"])
        cy = float(graphic.properties["center_y_mm"])
        radius = math.hypot(graphic.points[0][0] - cx, graphic.points[0][1] - cy)
        end_radius = math.hypot(graphic.points[1][0] - cx, graphic.points[1][1] - cy)
        if radius <= 0 or abs(radius - end_radius) > 1e-6:
            raise ValueError(f"{label}: original arc is not a supported circular arc")
        bounds = coordinates.bbox([(cx - radius, cy - radius), (cx + radius, cy + radius)], label)
        start, end = [coordinates.point(point, label + suffix) for point, suffix in zip(graphic.points, (".start", ".end"))]
        if graphic.properties["clockwise"].lower() in {"1", "true", "yes"}:
            start, end = end, start
        shape = SubElement(parent, "Arc")
        SubElement(shape, "Defn", {**style, **_source_bbox_attrs(bounds), "startX": str(start[0]),
                                   "startY": str(start[1]), "endX": str(end[0]), "endY": str(end[1])})
    else:
        raise ValueError(f"{label}: original symbol graphic {graphic.kind!r} is unsupported; no replacement was generated")


def _source_rounded_rectangle_xml(
    parent: Element, bounds: tuple[int, int, int, int], rx: int, ry: int,
    style: dict[str, str], label: str,
) -> None:
    x1, y1, x2, y2 = bounds
    if rx <= 0 or ry <= 0 or 2 * rx > x2 - x1 or 2 * ry > y2 - y1:
        raise ValueError(f"{label}: original rounded rectangle radii are invalid at Capture precision")
    for start, end in [((x1 + rx, y1), (x2 - rx, y1)), ((x2, y1 + ry), (x2, y2 - ry)),
                       ((x2 - rx, y2), (x1 + rx, y2)), ((x1, y2 - ry), (x1, y1 + ry))]:
        line = SubElement(parent, "Line")
        SubElement(line, "Defn", {**style, "x1": str(start[0]), "y1": str(start[1]),
                                  "x2": str(end[0]), "y2": str(end[1])})
    for cx, cy, start, end in [
        (x1 + rx, y1 + ry, (x1 + rx, y1), (x1, y1 + ry)),
        (x2 - rx, y1 + ry, (x2, y1 + ry), (x2 - rx, y1)),
        (x2 - rx, y2 - ry, (x2 - rx, y2), (x2, y2 - ry)),
        (x1 + rx, y2 - ry, (x1, y2 - ry), (x1 + rx, y2)),
    ]:
        arc = SubElement(parent, "Arc")
        SubElement(arc, "Defn", {**style, **_source_bbox_attrs((cx - rx, cy - ry, cx + rx, cy + ry)),
                                 "startX": str(start[0]), "startY": str(start[1]),
                                 "endX": str(end[0]), "endY": str(end[1])})


def _source_text_xml(
    parent: Element, text: str, x: float, y: float, rotation: float, properties: dict[str, str],
    coordinates: _SourceCoordinates, label: str,
) -> None:
    if not text:
        return
    if "font_size_mm" not in properties:
        raise ValueError(f"{label}: original text font size is missing")
    baseline_x, baseline_y = coordinates.point((x, y), label + ".source_baseline")
    height = coordinates.value(float(properties["font_size_mm"]), label + ".font_height", True)
    if height <= 0:
        raise ValueError(f"{label}: original font is too small for Capture integer coordinates")
    anchor = properties.get("anchor", "start")
    if anchor not in {"start", "middle", "end"}:
        raise ValueError(f"{label}: unsupported original text anchor {anchor!r}")
    source_weight = properties.get("font_weight", "").strip().lower()
    if source_weight in {"", "normal"}:
        weight = "400"
    elif source_weight == "bold":
        weight = "700"
    elif source_weight.isdigit() and 0 <= int(source_weight) <= 1000:
        weight = source_weight
    else:
        raise ValueError(f"{label}: unsupported original font weight {source_weight!r}")
    source_style = properties.get("font_style", "").strip().lower()
    if source_style not in {"", "normal", "italic"}:
        raise ValueError(f"{label}: unsupported original font style {source_style!r}")
    normalized_rotation = rotation % 360
    if normalized_rotation not in {0, 90, 180, 270}:
        raise ValueError(f"{label}: source text rotation requires an unsupported non-orthogonal font transform")
    font_name = properties.get("font", "Verdana")
    ascent, font_height, width = _capture_text_metrics(font_name, height, int(weight), source_style == "italic", text)
    anchor_offset = 0 if anchor == "start" else width / 2 if anchor == "middle" else width
    corners = [(-anchor_offset, -ascent), (width - anchor_offset, -ascent),
               (-anchor_offset, font_height - ascent), (width - anchor_offset, font_height - ascent)]
    rotations = {0: lambda px, py: (px, py), 90: lambda px, py: (py, -px),
                 180: lambda px, py: (-px, -py), 270: lambda px, py: (-py, px)}
    native_corners = [rotations[normalized_rotation](*corner) for corner in corners]
    offset_x = coordinates.value(min(point[0] for point in native_corners) * CAPTURE_COORDINATE_MM, label + ".anchor_offset_x")
    offset_y = coordinates.value(min(point[1] for point in native_corners) * CAPTURE_COORDINATE_MM, label + ".anchor_offset_y")
    loc_x, loc_y = baseline_x + offset_x, baseline_y + offset_y
    bbox_width, bbox_height = (width, font_height) if normalized_rotation in {0, 180} else (font_height, width)
    coordinates.points.extend([(loc_x, loc_y), (loc_x + bbox_width, loc_y + bbox_height)])
    coordinates.text_anchors.append({"element": label, "text": text, "source_anchor_mm": [x, y],
                                     "source_anchor": anchor, "source_baseline_native": [baseline_x, baseline_y],
                                     "native_top_left": [loc_x, loc_y], "native_font_ascent": ascent,
                                     "native_font_height": font_height, "native_text_width": width,
                                     "native_offset": [offset_x, offset_y], "rotation": normalized_rotation,
                                     "measurement": "Windows GDI GetTextMetricsW/GetTextExtentPoint32W"})
    node = SubElement(parent, "CommentText")
    SubElement(node, "Defn", {"locX": str(loc_x), "locY": str(loc_y), "name": text,
                              "textJustification": "1", "x1": str(loc_x), "x2": str(loc_x + bbox_width),
                              "y1": str(loc_y), "y2": str(loc_y + bbox_height)})
    angle = str(round(normalized_rotation * 10))
    font = SubElement(node, "TextFont")
    SubElement(font, "Defn", {"charset": "0", "escapement": angle, "height": str(-height),
                              "italic": "1" if source_style == "italic" else "0",
                              "name": font_name,
                              "orientation": angle, "weight": weight, "width": "0"})


@lru_cache(maxsize=2048)
def _capture_text_metrics(font_name: str, height: int, weight: int, italic: bool, text: str) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        raise ValueError("Native Windows GDI font metrics are required to convert original text baseline anchors")

    class TextMetric(ctypes.Structure):
        _fields_ = [(name, wintypes.LONG) for name in (
            "height", "ascent", "descent", "internal_leading", "external_leading", "average_width",
            "max_width", "weight", "overhang", "aspect_x", "aspect_y",
        )] + [(name, wintypes.WCHAR) for name in ("first_char", "last_char", "default_char", "break_char")] + [
            (name, wintypes.BYTE) for name in ("italic", "underlined", "struck_out", "pitch_and_family", "charset")
        ]

    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi.CreateFontW.restype = wintypes.HFONT
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.GetTextMetricsW.argtypes = [wintypes.HDC, ctypes.POINTER(TextMetric)]
    gdi.GetTextMetricsW.restype = wintypes.BOOL
    gdi.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
    gdi.GetTextExtentPoint32W.restype = wintypes.BOOL
    gdi.GetTextFaceW.argtypes = [wintypes.HDC, ctypes.c_int, wintypes.LPWSTR]
    gdi.GetTextFaceW.restype = ctypes.c_int
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteObject.restype = wintypes.BOOL
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.DeleteDC.restype = wintypes.BOOL
    dc = gdi.CreateCompatibleDC(None)
    font = gdi.CreateFontW(-height, 0, 0, 0, weight, int(italic), 0, 0, 1, 0, 0, 0, 0, font_name)
    if not dc or not font:
        if font:
            gdi.DeleteObject(font)
        if dc:
            gdi.DeleteDC(dc)
        raise ValueError(f"Could not create native source font {font_name!r}")
    previous = gdi.SelectObject(dc, font)
    try:
        face = ctypes.create_unicode_buffer(128)
        metric, extent = TextMetric(), wintypes.SIZE()
        if not gdi.GetTextFaceW(dc, len(face), face) or face.value.casefold() != font_name.casefold():
            raise ValueError(f"Original source font {font_name!r} is unavailable; Windows substituted {face.value!r}")
        if not gdi.GetTextMetricsW(dc, ctypes.byref(metric)) or not gdi.GetTextExtentPoint32W(dc, text, len(text.encode("utf-16-le")) // 2, ctypes.byref(extent)):
            raise ValueError(f"Could not measure native source text in font {font_name!r}")
        return metric.ascent, metric.height, extent.cx
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        gdi.DeleteObject(font)
        gdi.DeleteDC(dc)


def _source_pin_xml(
    parent: Element, pin: Pin, position: int, coordinates: _SourceCoordinates,
    include_pin_number_props: bool,
) -> None:
    label = f"{pin.unit}.pin[{pin.number}]"
    hot_x, hot_y = coordinates.point((pin.x, pin.y), label + ".hotpoint")
    end_properties = ("Source_Pin_End_X_mm", "Source_Pin_End_Y_mm")
    if all(key in pin.properties for key in end_properties):
        source_end = tuple(float(pin.properties[key]) for key in end_properties)
    elif pin.side in {"left", "right", "top", "bottom"}:
        dx, dy = {"left": (1, 0), "right": (-1, 0), "top": (0, -1), "bottom": (0, 1)}[pin.side]
        source_end = (pin.x + dx * pin.length, pin.y + dy * pin.length)
    else:
        raise ValueError(f"{label}: original pin endpoint is missing")
    start_x, start_y = coordinates.point(source_end, label + ".body_endpoint")
    source_length = math.hypot(source_end[0] - pin.x, source_end[1] - pin.y)
    path_start_properties = ("Source_Pin_Start_X_mm", "Source_Pin_Start_Y_mm")
    if all(key in pin.properties for key in path_start_properties):
        path_start = tuple(float(pin.properties[key]) for key in path_start_properties)
        if math.hypot(path_start[0] - pin.x, path_start[1] - pin.y) > 1e-9:
            raise UnsupportedGeometryError(f"{label}: original drawn pin start differs from its electrical hotpoint; "
                                           "Capture conversion of this pin decoration is unsupported")
    converted_length = math.hypot(start_x - hot_x, start_y - hot_y) * CAPTURE_COORDINATE_MM
    if abs(source_length - converted_length) > 1e-9:
        coordinates.changes.append({"element": label + ".length", "kind": "dimension", "source_mm": source_length,
                                    "capture_mm": round(converted_length, 12), "error_mm": round(abs(source_length - converted_length), 12)})
    symbol_pin = SubElement(parent, "SymbolPinScalar")
    if pin.type == "Undefined":
        coordinates.pin_types.append({"number": pin.number, "unit": pin.unit, "source_type": pin.type,
                                      "source_electrical_type": pin.properties.get("Source_Pin_Electrical_Type", "0"),
                                      "capture_type": "Passive", "capture_type_enum": "4"})
    SubElement(symbol_pin, "Defn", {"hotptX": str(hot_x), "hotptY": str(hot_y), "name": pin.name,
                                    "position": str(position), "startX": str(start_x), "startY": str(start_y),
                                    "type": str(_capture_pin_type(pin.type)), "visible": "0" if pin.hidden else "1"})
    for name, value in [("IsLong", "1"), ("IsClock", "0"), ("IsDot", "0"),
                        ("IsLeftPointing", "1" if pin.side == "left" else "0"),
                        ("IsRightPointing", "1" if pin.side == "right" else "0"), ("IsNetStyle", "0"),
                        ("IsNoConnect", "1" if _capture_pin_type(pin.type) == 8 else "0"),
                        ("IsGlobal", "1" if pin.properties.get("Global", "").lower() in {"true", "1"} else "0"),
                        ("IsNumberVisible", "0")]:
        _simple_value_element(symbol_pin, name, "val", value)
    _pin_user_props_xml(symbol_pin, pin, include_pin_number_prop=include_pin_number_props)
    for kind, text in [("Name", pin.name), ("Number", pin.number)]:
        prefix = f"Source_Pin_{kind}_"
        visible = pin.properties.get(prefix + "Visible")
        if visible is None:
            raise ValueError(f"{label}: original {kind.lower()} visibility is missing")
        if pin.hidden or visible.lower() in {"0", "false", "no"}:
            continue
        required = [prefix + field for field in ("X_mm", "Y_mm", "Font_mm")]
        if any(key not in pin.properties for key in required):
            raise ValueError(f"{label}: original {kind.lower()} text position/font size is missing")
        properties = {"font_size_mm": pin.properties[prefix + "Font_mm"],
                      "anchor": pin.properties.get(prefix + "Anchor", "start"),
                      "font": pin.properties.get(prefix + "Font", "Verdana")}
        _source_text_xml(parent, text, float(pin.properties[prefix + "X_mm"]),
                         float(pin.properties[prefix + "Y_mm"]),
                         float(pin.properties.get(prefix + "Rotation", "0")), properties,
                         coordinates, label + "." + kind.lower())


def _grid_ceil(value: int) -> int:
    return (value + CAPTURE_GRID - 1) // CAPTURE_GRID * CAPTURE_GRID


def _pin_label_width(pin: Pin | None) -> int:
    return len(pin.name or pin.number) * PIN_LABEL_MAX_ADVANCE if pin is not None else 0


def _physical_part_xml(parent: Element, pins: list[Pin]) -> None:
    physical = SubElement(parent, "PhysicalPart")
    SubElement(physical, "Defn")
    for index, pin in enumerate(pins):
        pin_number = SubElement(physical, "PinNumber")
        SubElement(pin_number, "Defn", {"number": pin.number, "position": str(index)})


def _symbol_body_xml(parent: Element, x1: int, y1: int, x2: int, y2: int) -> None:
    rect = SubElement(parent, "Rect")
    SubElement(
        rect,
        "Defn",
        {
            "fillStyle": "1",
            "hatchStyle": "0",
            "lineStyle": "0",
            "lineWidth": "0",
            "x1": str(x1),
            "x2": str(x2),
            "y1": str(y1),
            "y2": str(y2),
        },
    )


def _symbol_pin_xml(
    parent: Element,
    pin: Pin,
    position: int,
    side: str,
    row: int,
    left_x: int,
    right_x: int,
    top_y: int,
    include_pin_number_props: bool = True,
    horizontal_pitch: int = PIN_SPACING,
    horizontal_offset: int = CAPTURE_GRID,
    vertical_inset: int = CAPTURE_GRID,
) -> None:
    y = max(CAPTURE_GRID, top_y - vertical_inset - row * PIN_SPACING)
    if side == "right":
        start_x = right_x
        hot_x = right_x + PIN_LENGTH
    elif side == "top":
        start_x = left_x + horizontal_offset + row * horizontal_pitch
        y = top_y
        hot_x = start_x
    elif side == "bottom":
        start_x = left_x + horizontal_offset + row * horizontal_pitch
        y = 0
        hot_x = start_x
    else:
        start_x = left_x
        hot_x = left_x - PIN_LENGTH
    start_y = y
    hot_y = y + PIN_LENGTH if side == "top" else y - PIN_LENGTH if side == "bottom" else y
    if pin.hidden:
        hot_x = start_x
        hot_y = start_y
    symbol_pin = SubElement(parent, "SymbolPinScalar")
    SubElement(
        symbol_pin,
        "Defn",
        {
            "hotptX": str(hot_x),
            "hotptY": str(hot_y),
            "name": pin.name or pin.number,
            "position": str(position),
            "startX": str(start_x),
            "startY": str(start_y),
            "type": str(_capture_pin_type(pin.type, hidden=pin.hidden)),
            "visible": "0" if pin.hidden else "1",
        },
    )
    for name, val in [
        ("IsLong", "1"),
        ("IsClock", "0"),
        ("IsDot", "0"),
        ("IsLeftPointing", "1" if side == "left" else "0"),
        ("IsRightPointing", "1" if side == "right" else "0"),
        ("IsNetStyle", "0"),
        ("IsNoConnect", "1" if pin.type == "NoConnect" else "0"),
        ("IsGlobal", "1" if _is_global_pin(pin) else "0"),
        ("IsNumberVisible", "1"),
    ]:
        _simple_value_element(symbol_pin, name, "val", val)
    _pin_user_props_xml(symbol_pin, pin, include_pin_number_prop=include_pin_number_props)


def _pin_side(pin: Pin, default_index: int) -> str:
    if pin.side in {"left", "right", "top", "bottom"}:
        return pin.side
    return "left" if default_index % 2 == 0 else "right"


def _symbol_user_props_xml(
    parent: Element,
    component: Component,
    unit: str | None = None,
    heterogeneous: bool = False,
) -> None:
    props = {
        "JLC2CadenceSource": "jlc2cadence",
        "Part Value": component.value or component.name,
        "PCB Footprint": component.footprint,
        "Source Package": component.name,
    }
    if unit:
        props["Unit"] = unit
    if heterogeneous:
        props["Heterogeneous"] = "true"
        props["PACKAGE"] = "1"
    props.update(component.properties)
    if component.description:
        props["Description"] = component.description
    for key, value in sorted((key, value) for key, value in props.items() if value):
        prop = SubElement(parent, "SymbolUserProp")
        SubElement(prop, "Defn", {"name": key, "val": str(value)})


def _pin_user_props_xml(parent: Element, pin: Pin, include_pin_number_prop: bool = True) -> None:
    props = {"Pin Type": pin.type, "Unit": pin.unit}
    if pin.hidden:
        props["Hidden"] = "true"
    if include_pin_number_prop:
        props["Pin Number"] = pin.number
    props.update(pin.properties)
    for key, value in sorted((key, value) for key, value in props.items() if value):
        prop = SubElement(parent, "PinUserProp")
        SubElement(prop, "Defn", {"name": key, "val": str(value)})


def _package_user_props_xml(parent: Element, component: Component, footprint: str) -> None:
    props = {
        "Part Value": component.value or component.name,
        "PCB Footprint": footprint,
        "Source Package": component.name,
    }
    props.update(component.properties)
    if component.description:
        props["Description"] = component.description
    for key, value in sorted((key, value) for key, value in props.items() if value):
        prop = SubElement(parent, "PackageUserProp")
        SubElement(prop, "Defn", {"name": key, "val": str(value)})


def _component_unit_groups(component: Component) -> list[tuple[str, list[Pin]]]:
    groups: list[tuple[str, list[Pin]]] = []
    by_unit: dict[str, list[Pin]] = {}
    for pin in component.pins:
        unit = str(pin.unit or "A").strip() or "A"
        if unit not in by_unit:
            by_unit[unit] = []
            groups.append((unit, by_unit[unit]))
        by_unit[unit].append(pin)
    if _uses_source_geometry(component):
        for graphic in component.graphics:
            unit = str(graphic.properties.get("unit", "A")).strip() or "A"
            if unit not in by_unit:
                by_unit[unit] = []
                groups.append((unit, by_unit[unit]))
    return groups or [("A", [])]


def _homogeneous_unit_groups(component: Component) -> list[tuple[str, list[Pin]]]:
    if _uses_source_geometry(component):
        return []
    groups = _component_unit_groups(component)
    if len(groups) <= 1:
        return []

    template_unit, template_pins = groups[0]
    template_keys = [_logical_pin_key(pin) for pin in template_pins]
    if not template_keys or len(set(template_keys)) != len(template_keys):
        return []

    ordered_groups: list[tuple[str, list[Pin]]] = [(template_unit, list(template_pins))]
    for unit, pins in groups[1:]:
        if len(pins) != len(template_pins):
            return []
        by_key: dict[tuple[str, str, str, str], Pin] = {}
        for pin in pins:
            key = _logical_pin_key(pin)
            if key in by_key:
                return []
            by_key[key] = pin
        ordered = []
        for key in template_keys:
            matched = by_key.get(key)
            if matched is None:
                return []
            ordered.append(matched)
        ordered_groups.append((unit, ordered))
    return ordered_groups


def _heterogeneous_unit_groups(component: Component) -> list[tuple[str, list[Pin]]]:
    groups = _component_unit_groups(component)
    if len(groups) <= 1 or _homogeneous_unit_groups(component):
        return []
    return groups


def _capture_part_kind(component: Component) -> str:
    if len(_homogeneous_unit_groups(component)) > 1:
        return "homogeneous_multi_unit"
    if len(_heterogeneous_unit_groups(component)) > 1:
        return "heterogeneous_multi_unit"
    return "single_unit"


def _logical_pin_key(pin: Pin) -> tuple[str, str, str, str]:
    return (
        (pin.name or pin.number).strip().upper(),
        pin.type.strip().lower(),
        pin.side.strip().lower(),
        "hidden" if pin.hidden else "visible",
    )


def _display_prop_xml(parent: Element, name: str, x: int, y: int, value: str) -> None:
    if not value:
        return
    display = SubElement(parent, "SymbolDisplayProp")
    SubElement(
        display,
        "Defn",
        {"locX": str(x), "locY": str(y), "name": name, "rotation": "0", "textJustification": "0"},
    )
    _font_xml(display, "PropFont")
    _simple_value_element(display, "PropColor", "val", "48")
    disp = SubElement(display, "PropDispType")
    SubElement(disp, "Defn", {"val": "1", "ValueIfValueExist": "0"})


def _font_xml(parent: Element, name: str) -> None:
    font = SubElement(parent, name)
    SubElement(
        font,
        "Defn",
        {
            "charset": "0",
            "escapement": "0",
            "height": "-9",
            "italic": "0",
            "name": "Arial",
            "orientation": "0",
            "weight": "400",
            "width": "4",
        },
    )


def _simple_value_element(parent: Element, element_name: str, attr_name: str, value: str) -> None:
    element = SubElement(parent, element_name)
    SubElement(element, "Defn", {attr_name: value})


def write_capture_readme(path: Path) -> None:
    path.write_text(
        """# Capture output

This directory contains two Capture symbol representations:

- `library.capture.xml`: neutral jlc2cadence XML for review and debugging.
- `library.olb.xml`: Cadence Capture `Lib` XML intended for `XMATIC_XML2OLB`.
- `capture_build_plan.json`: generated `.olb` build plan and component list.

The converter intentionally does not write binary `.olb` data directly. Run
`../scripts/generate_capture_library.ps1` on a Cadence 17.4 workstation to launch
Capture and import `library.olb.xml` into `library.olb`.
""",
        encoding="utf-8",
    )


def write_capture_tcl(library: LibraryIR, path: Path) -> Path:
    lines = [
        f"# Generated by jlc2cadence v{__version__}",
        "# Run inside OrCAD Capture 17.4, or launch with generate_capture_library.ps1.",
        "set here [file dirname [file normalize [info script]]]",
        "set root [file dirname $here]",
        "set xmlPath [file normalize [file join $root capture library.olb.xml]]",
        "set olbPath [file normalize [file join $root capture library.olb]]",
        "set logPath [file normalize [file join $root capture library.olb.import.log]]",
        "set tclPath [file normalize [file join $root capture library.olb.import.tcl]]",
        "puts \"jlc2cadence Capture components: %d\"" % len(library.components),
        "puts \"XML: $xmlPath\"",
        "puts \"OLB: $olbPath\"",
        "set ::env(CAPTURE_WRITE_TCL_ONXMLIMPORT) 1",
        "if {[file exists $olbPath]} { file delete -force $olbPath }",
        "",
        "set status [catch {XMATIC_XML2OLB $xmlPath $olbPath false $logPath $tclPath true} err]",
        "if {$status != 0} {",
        "  puts \"XMATIC_XML2OLB six-argument import failed: $err\"",
        "  set status [catch {XMATIC_XML2OLB $xmlPath $olbPath false $logPath $tclPath} err]",
        "}",
        "if {$status != 0} {",
        "  puts \"XMATIC_XML2OLB five-argument import failed: $err\"",
        "  set status [catch {XMATIC_XML2OLB $xmlPath $olbPath} err]",
        "}",
        "if {$status != 0 || ![file exists $olbPath]} {",
        "  set fh [open $logPath a]",
        "  puts $fh \"ERROR: XMATIC_XML2OLB failed: $err\"",
        "  close $fh",
        "  error \"Unable to generate $olbPath from $xmlPath\"",
        "}",
        "puts \"Created $olbPath\"",
        "if {[info exists ::env(JLC2CADENCE_CAPTURE_BATCH)] && $::env(JLC2CADENCE_CAPTURE_BATCH) == \"1\"} {",
        "  catch {Menu \"File::Exit\"}",
        "}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_capture_replay(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Capture command replay generated by jlc2cadence.",
                "# Capture command-line replay executes command-window commands; source the Tcl importer.",
                "source {scripts/create_capture_olb.tcl}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_capture_helper(library: LibraryIR, path: Path) -> None:
    lines = [
        f"# Generated by jlc2cadence v{__version__}",
        "param(",
        "  [string]$CaptureExe,",
        "  [string]$Product = 'OrCAD Capture',",
        "  [switch]$ManualOnly",
        ")",
        "$ErrorActionPreference = 'Stop'",
        "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path",
        "$Root = Split-Path -Parent $Here",
        "$CaptureXml = Join-Path $Root 'capture\\library.olb.xml'",
        "$NeutralXml = Join-Path $Root 'capture\\library.capture.xml'",
        "$BuildPlan = Join-Path $Root 'capture\\capture_build_plan.json'",
        "$Tcl = Join-Path $Here 'create_capture_olb.tcl'",
        "$Replay = Join-Path $Root 'run_capture_import.tcl'",
        "$ReplayName = Split-Path -Leaf $Replay",
        "$Olb = Join-Path $Root 'capture\\library.olb'",
        "Write-Host \"Capture OLB XML: $CaptureXml\"",
        "Write-Host \"Neutral XML: $NeutralXml\"",
        "Write-Host \"Build plan: $BuildPlan\"",
        "Write-Host \"Components: %d\"" % len(library.components),
        "if ($ManualOnly) {",
        "  Write-Host \"Manual Capture command: source {$Tcl}\"",
        "  return",
        "}",
        "if (-not (Test-Path -LiteralPath $Tcl)) { throw \"Missing Tcl script: $Tcl\" }",
        "if (-not (Test-Path -LiteralPath $Replay)) { throw \"Missing Capture replay script: $Replay\" }",
        "if (-not (Test-Path -LiteralPath $CaptureXml)) { throw \"Missing Capture XML: $CaptureXml\" }",
        "if (-not $CaptureExe) {",
        "  $Candidates = @()",
        "  if ($env:CDSROOT) {",
        "    $Candidates += (Join-Path $env:CDSROOT 'tools\\bin\\Capture.exe')",
        "  }",
        "  $Candidates += @(",
        "    'D:\\Cadence_17_4\\Cadence\\SPB_17.4\\tools\\bin\\Capture.exe',",
        "    'Capture.exe'",
        "  )",
        "  foreach ($Candidate in $Candidates) {",
        "    if ($Candidate -and (Get-Command $Candidate -ErrorAction SilentlyContinue)) {",
        "      $CaptureExe = $Candidate",
        "      break",
        "    }",
        "    if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {",
        "      $CaptureExe = $Candidate",
        "      break",
        "    }",
        "  }",
        "}",
        "if (-not $CaptureExe) { throw 'Unable to locate Capture.exe. Pass -CaptureExe explicitly.' }",
        "$env:CAPTURE_WRITE_TCL_ONXMLIMPORT = '1'",
        "$env:JLC2CADENCE_CAPTURE_BATCH = '1'",
        "$ExistingCapture = Get-CimInstance Win32_Process -Filter \"name = 'Capture.exe'\" -ErrorAction SilentlyContinue",
        "if ($ExistingCapture) {",
        "  throw 'Capture.exe is already running. Close existing Capture instances before running this batch import, otherwise Capture 17.4 may ignore the replay script.'",
        "}",
        "$Args = @(\"-product=`\"$Product`\"\", $ReplayName)",
        "$Proc = Start-Process -FilePath $CaptureExe -ArgumentList $Args -WorkingDirectory $Root -Wait -PassThru -WindowStyle Hidden",
        "if ($Proc.ExitCode -ne 0) { throw \"Capture exited with code $($Proc.ExitCode)\" }",
        "if (-not (Test-Path -LiteralPath $Olb)) { throw \"Capture did not create expected OLB: $Olb\" }",
        "Write-Host \"Created OLB: $Olb\"",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _capture_ref_prefix(component: Component) -> str:
    ref = (component.ref_prefix or "U").strip().rstrip("?*")
    return cadence_safe_name(ref, "U")


def _capture_internal_names(library: LibraryIR) -> list[str]:
    used: set[str] = set()
    names: list[str] = []
    for component in library.components:
        base_name = _capture_internal_name(component.name, "PART")
        candidate = base_name
        suffix_index = 2
        while candidate.casefold() in used:
            suffix = f"_{suffix_index}"
            candidate = f"{base_name[:31 - len(suffix)]}{suffix}"
            suffix_index += 1
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def _capture_internal_name(value: str, fallback: str = "PART") -> str:
    name = cadence_safe_name(value, fallback)
    if len(name) <= 31:
        return name
    digest = sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{name[:24]}_{digest}"


def _capture_pin_type(pin_type: str, hidden: bool = False) -> int:
    key = pin_type.strip().lower().replace("-", "_").replace(" ", "_")
    if hidden and key in {"power", "power_pin"}:
        return 7
    mapping = {
        "input": 0,
        "output": 1,
        "bidirectional": 2,
        "bidir": 2,
        "tri_state": 3,
        "passive": 4,
        "power": 5,
        "power_pin": 5,
        "open_collector": 6,
        "open_emitter": 7,
        "no_connect": 8,
        "noconnect": 8,
        "nc": 8,
    }
    return mapping.get(key, 4)


def _is_global_pin(pin: Pin) -> bool:
    if pin.hidden and pin.type.strip().lower().replace("-", "_").replace(" ", "_") in {"power", "power_pin"}:
        return True
    value = pin.properties.get("Global") or pin.properties.get("global")
    return str(value).strip().lower() in {"1", "yes", "true", "on"}
