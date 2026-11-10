from __future__ import annotations

from html import escape
from math import atan2, cos, hypot, pi, sin
from pathlib import Path

from ..ir.geometry import footprint_bounds
from ..ir.schema import Component, Footprint, LibraryIR, Pad


def write_svg_previews(library: LibraryIR, reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_footprint_preview(library.footprints[0] if library.footprints else None, reports_dir / "footprint_preview.svg")
    write_symbol_preview(library.components[0] if library.components else None, reports_dir / "symbol_preview.svg")


def write_footprint_preview(footprint: Footprint | None, path: Path) -> None:
    if footprint is None:
        path.write_text(_empty_svg("No footprint"), encoding="utf-8")
        return
    if footprint.properties.get("Conversion_Policy") == "source_exact":
        _write_source_preview(footprint.graphics, [], footprint.pads, path)
        return
    min_x, min_y, max_x, max_y = footprint_bounds(footprint)
    margin = 1.0
    scale = 40
    width = max(160, int((max_x - min_x + 2 * margin) * scale))
    height = max(120, int((max_y - min_y + 2 * margin) * scale))

    def tx(x: float) -> float:
        return (x - min_x + margin) * scale

    def ty(y: float) -> float:
        return height - (y - min_y + margin) * scale

    lines = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
        "<rect width='100%' height='100%' fill='#ffffff'/>",
        f"<text x='8' y='18' font-size='12' fill='#202124'>{escape(footprint.name)}</text>",
    ]
    for graphic in footprint.graphics:
        if len(graphic.points) >= 2:
            points = " ".join(f"{tx(x):.2f},{ty(y):.2f}" for x, y in graphic.points)
            lines.append(f"<polyline points='{points}' fill='none' stroke='#4b5563' stroke-width='1'/>")
        elif graphic.text:
            lines.append(f"<text x='{tx(graphic.x):.2f}' y='{ty(graphic.y):.2f}' font-size='10'>{escape(graphic.text)}</text>")
    for pad in footprint.pads:
        lines.append(_pad_svg(pad, tx, ty, scale))
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_symbol_preview(component: Component | None, path: Path) -> None:
    if component is None:
        path.write_text(_empty_svg("No symbol"), encoding="utf-8")
        return
    if component.properties.get("Conversion_Policy") == "source_exact":
        _write_source_preview(component.graphics, component.pins, [], path)
        return
    rows = max(2, len(component.pins) // 2 + len(component.pins) % 2)
    height = max(120, rows * 24 + 48)
    width = 320
    box_top = 32
    box_bottom = height - 24
    lines = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
        "<rect width='100%' height='100%' fill='#ffffff'/>",
        f"<rect x='90' y='{box_top}' width='140' height='{box_bottom - box_top}' fill='none' stroke='#202124'/>",
        f"<text x='96' y='22' font-size='13' fill='#202124'>{escape(component.name)}</text>",
    ]
    left_pins = component.pins[0::2]
    right_pins = component.pins[1::2]
    for idx, pin in enumerate(left_pins):
        y = box_top + 18 + idx * 24
        lines.append(f"<line x1='50' y1='{y}' x2='90' y2='{y}' stroke='#202124'/>")
        lines.append(f"<text x='54' y='{y - 3}' font-size='10'>{escape(pin.number)}</text>")
        lines.append(f"<text x='96' y='{y + 4}' font-size='10'>{escape(pin.name)}</text>")
    for idx, pin in enumerate(right_pins):
        y = box_top + 18 + idx * 24
        lines.append(f"<line x1='230' y1='{y}' x2='270' y2='{y}' stroke='#202124'/>")
        lines.append(f"<text x='248' y='{y - 3}' font-size='10'>{escape(pin.number)}</text>")
        lines.append(f"<text x='176' y='{y + 4}' font-size='10'>{escape(pin.name)}</text>")
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def _pad_svg(pad: Pad, tx, ty, scale: int) -> str:
    x = tx(pad.x)
    y = ty(pad.y)
    sx = max(pad.size[0] * scale, 4)
    sy = max(pad.size[1] * scale, 4)
    fill = "#d97706" if pad.kind == "smd" else "#2563eb"
    if pad.shape in {"circle", "oval"}:
        return (
            f"<ellipse cx='{x:.2f}' cy='{y:.2f}' rx='{sx / 2:.2f}' ry='{sy / 2:.2f}' "
            f"fill='{fill}' opacity='0.85'><title>{escape(pad.number)}</title></ellipse>"
        )
    radius = 3 if pad.shape == "roundrect" else 0
    return (
        f"<rect x='{x - sx / 2:.2f}' y='{y - sy / 2:.2f}' width='{sx:.2f}' height='{sy:.2f}' "
        f"rx='{radius}' fill='{fill}' opacity='0.85'><title>{escape(pad.number)}</title></rect>"
    )


def _empty_svg(text: str) -> str:
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='240' height='120' viewBox='0 0 240 120'>"
        "<rect width='100%' height='100%' fill='#fff'/>"
        f"<text x='16' y='64' font-size='14'>{escape(text)}</text></svg>"
    )


def _write_source_preview(graphics, pins, pads, path: Path) -> None:
    points = [point for graphic in graphics for point in graphic.points]
    for graphic in graphics:
        props = graphic.properties
        if graphic.kind in {"circle", "arc"}:
            cx = float(props.get("center_x_mm", graphic.points[0][0] if graphic.kind == "circle" and graphic.points else graphic.x))
            cy = float(props.get("center_y_mm", graphic.points[0][1] if graphic.kind == "circle" and graphic.points else graphic.y))
            radius = float(props.get("radius_mm", hypot(graphic.points[0][0] - cx, graphic.points[0][1] - cy) if graphic.points else 0))
            points.extend([(cx - radius, cy - radius), (cx + radius, cy + radius)])
        elif graphic.kind == "text":
            points.append((graphic.x, graphic.y))
    for pin in pins:
        points.extend([(pin.x, pin.y), (float(pin.properties["Source_Pin_End_X_mm"]), float(pin.properties["Source_Pin_End_Y_mm"]))])
    for pad in pads:
        angle = pad.rotation * pi / 180
        points.extend((pad.x + x * cos(angle) - y * sin(angle), pad.y + x * sin(angle) + y * cos(angle))
                      for x in (-pad.size[0] / 2, pad.size[0] / 2) for y in (-pad.size[1] / 2, pad.size[1] / 2))
    if not points:
        path.write_text(_empty_svg("No source geometry"), encoding="utf-8")
        return
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    margin = max(1, max(x1 - x0, y1 - y0) * .12)
    view = f"{x0-margin:.10g} {-y1-margin:.10g} {x1-x0+2*margin:.10g} {y1-y0+2*margin:.10g}"
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" viewBox="{view}">',
             '<rect x="-100000" y="-100000" width="200000" height="200000" fill="white"/>']
    for graphic in graphics:
        props, pts = graphic.properties, graphic.points
        if props.get("visible") == "false":
            continue
        fill = "white" if props.get("region_type") == "cutout" else "#35596b" if props.get("filled") == "true" else "none"
        style = f'fill="{fill}" stroke="#35596b" stroke-width="{graphic.width:.10g}"'
        if graphic.kind == "text":
            lines.append(f'<text x="{graphic.x:.10g}" y="{-graphic.y:.10g}" font-family="{escape(props.get("font", "Verdana"), quote=True)}" font-size="{float(props.get("font_size_mm", 1)):.10g}" text-anchor="{escape(props.get("anchor", "start"), quote=True)}" transform="rotate({-graphic.rotation:.10g} {graphic.x:.10g} {-graphic.y:.10g})">{escape(graphic.text)}</text>')
        elif graphic.kind in {"circle", "arc"}:
            cx = float(props.get("center_x_mm", pts[0][0] if graphic.kind == "circle" and pts else graphic.x))
            cy = float(props.get("center_y_mm", pts[0][1] if graphic.kind == "circle" and pts else graphic.y))
            radius = float(props.get("radius_mm", hypot(pts[0][0] - cx, pts[0][1] - cy) if pts else 0))
            if graphic.kind == "circle":
                lines.append(f'<circle cx="{cx:.10g}" cy="{-cy:.10g}" r="{radius:.10g}" {style}/>')
            elif len(pts) == 2:
                clockwise = props.get("clockwise") == "true"
                angle = (atan2(pts[1][1] - cy, pts[1][0] - cx) - atan2(pts[0][1] - cy, pts[0][0] - cx)) % (2 * pi)
                if clockwise:
                    angle = (2 * pi - angle) % (2 * pi)
                lines.append(f'<path d="M {pts[0][0]:.10g} {-pts[0][1]:.10g} A {radius:.10g} {radius:.10g} 0 {int(angle > pi)} {int(clockwise)} {pts[1][0]:.10g} {-pts[1][1]:.10g}" {style}/>')
        elif graphic.kind in {"rectangle", "rect", "ellipse"} and len(pts) == 2:
            left, top, width, height = min(p[0] for p in pts), -max(p[1] for p in pts), abs(pts[1][0] - pts[0][0]), abs(pts[1][1] - pts[0][1])
            if graphic.kind == "ellipse":
                lines.append(f'<ellipse cx="{left+width/2:.10g}" cy="{top+height/2:.10g}" rx="{width/2:.10g}" ry="{height/2:.10g}" {style}/>')
            else:
                lines.append(f'<rect x="{left:.10g}" y="{top:.10g}" width="{width:.10g}" height="{height:.10g}" rx="{float(props.get("radius_x_mm", 0)):.10g}" ry="{float(props.get("radius_y_mm", 0)):.10g}" {style}/>')
        elif pts:
            tag = "polygon" if graphic.kind in {"polygon", "region"} else "polyline"
            positions = " ".join(f"{x:.10g},{-y:.10g}" for x, y in pts)
            lines.append(f'<{tag} points="{positions}" {style}/>')
    for pin in pins:
        if pin.hidden:
            continue
        ex, ey = float(pin.properties["Source_Pin_End_X_mm"]), float(pin.properties["Source_Pin_End_Y_mm"])
        lines.append(f'<line x1="{pin.x:.10g}" y1="{-pin.y:.10g}" x2="{ex:.10g}" y2="{-ey:.10g}" stroke="#27a779" stroke-width="0.05"/>')
        for label, text in (("Name", pin.name), ("Number", pin.number)):
            prefix = f"Source_Pin_{label}_"
            if pin.properties.get(prefix + "Visible") != "true":
                continue
            x, y = float(pin.properties[prefix + "X_mm"]), -float(pin.properties[prefix + "Y_mm"])
            lines.append(f'<text x="{x:.10g}" y="{y:.10g}" font-family="{escape(pin.properties.get(prefix + "Font", "Verdana"), quote=True)}" font-size="{float(pin.properties[prefix + "Font_mm"]):.10g}" text-anchor="{escape(pin.properties.get(prefix + "Anchor", "start"), quote=True)}">{escape(text)}</text>')
    for pad in pads:
        width, height = pad.size
        angle = -pad.rotation
        style = f'fill="#27a779" transform="translate({pad.x:.10g} {-pad.y:.10g}) rotate({angle:.10g})"'
        if pad.shape == "circle":
            lines.append(f'<ellipse cx="0" cy="0" rx="{width/2:.10g}" ry="{height/2:.10g}" {style}/>')
        else:
            radius = min(width, height) / 2 if pad.shape == "oval" else 0
            lines.append(f'<rect x="{-width/2:.10g}" y="{-height/2:.10g}" width="{width:.10g}" height="{height:.10g}" rx="{radius:.10g}" {style}/>')
        if pad.hole_diameter:
            lines.append(f'<circle cx="{pad.x:.10g}" cy="{-pad.y:.10g}" r="{pad.hole_diameter/2:.10g}" fill="white"/>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")
