from __future__ import annotations

from html import escape
from pathlib import Path

from ..ir.schema import LibraryIR
from ..validators.library_checks import ValidationReport


def write_html_report(library: LibraryIR, validation: ValidationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    html = [
        "<!doctype html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>jlc2cadence summary</title>",
        "<style>",
        "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#202124}",
        "table{border-collapse:collapse;width:100%;margin:12px 0}",
        "td,th{border:1px solid #d0d7de;padding:6px 8px;text-align:left}",
        "th{background:#f6f8fa}",
        ".error{color:#b42318}.warn{color:#8a5a00}.ok{color:#067647}",
        "code{background:#f6f8fa;padding:1px 4px;border-radius:3px}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>jlc2cadence conversion summary</h1>",
        f"<p>Components: <b>{len(library.components)}</b> | Footprints: <b>{len(library.footprints)}</b></p>",
        f"<p class='{ 'error' if validation.errors else 'ok' }'>Errors: {len(validation.errors)} | Warnings: {len(validation.warnings)}</p>",
    ]
    html.extend(_messages("Errors", validation.errors, "error"))
    html.extend(_messages("Warnings", validation.warnings, "warn"))
    html.append("<h2>Components</h2>")
    html.append("<table><tr><th>Name</th><th>Ref</th><th>Footprint</th><th>Pins</th></tr>")
    for component in library.components:
        html.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(component.name),
                escape(component.ref_prefix),
                escape(component.footprint),
                len(component.pins),
            )
        )
    html.append("</table>")
    html.append("<h2>Footprints</h2>")
    html.append("<table><tr><th>Name</th><th>Pads</th><th>Graphics</th><th>3D</th></tr>")
    for footprint in library.footprints:
        model = footprint.model3d.file if footprint.model3d else ""
        html.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(footprint.name),
                len(footprint.pads),
                len(footprint.graphics),
                escape(model),
            )
        )
    html.append("</table>")
    html.append("</body></html>")
    path.write_text("\n".join(html), encoding="utf-8")
    return path


def _messages(title: str, messages: list[str], css_class: str) -> list[str]:
    if not messages:
        return []
    lines = [f"<h2>{escape(title)}</h2>", f"<ul class='{css_class}'>"]
    for message in messages:
        lines.append(f"<li>{escape(message)}</li>")
    lines.append("</ul>")
    return lines

