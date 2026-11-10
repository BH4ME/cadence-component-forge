"""Recognize only confirmed native rejections of original source geometry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import re
from typing import Any

from .errors import UnsupportedGeometryError

_KNOWN_REJECTIONS = (
    ("SPMHA1-273", "Shape intersects with itself", "region"),
    ("SPMHDB-70", "Buffer contains overlapping segments", "line"),
)


def source_native_geometry_error(
    builds: Iterable[Mapping[str, Any]], results_path: Path,
) -> UnsupportedGeometryError | None:
    """Classify one strict source-exact native geometry rejection.

    Matching requires the Cadence diagnostic immediately followed by the
    source-exact creation failure in the same build output stream and row.
    Generic native failures remain generic so they are not misreported as an
    irregular shape.
    """
    for row in builds:
        if not isinstance(row, Mapping):
            continue
        for stream_name in ("buildStdout", "buildStderr"):
            text = row.get(stream_name, "")
            if not isinstance(text, str):
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for code, diagnostic, source_kind in _KNOWN_REJECTIONS:
                native_pattern = re.compile(
                    rf"(?:E-\s*|ERROR\s*)\({re.escape(code)}\):\s*"
                    rf"{re.escape(diagnostic)}\.?", re.IGNORECASE,
                )
                source_pattern = re.compile(
                    rf"E-\s*\*Error\*\s+Source-exact:\s*failed creating source "
                    rf"{source_kind} on (?P<layer>[^\r\n]+)", re.IGNORECASE,
                )
                for previous, current in zip(lines, lines[1:]):
                    if native_pattern.fullmatch(previous) is None:
                        continue
                    source_error = source_pattern.fullmatch(current)
                    if source_error is None:
                        continue
                    footprint = str(row.get("footprint", ""))
                    layer = source_error.group("layer")
                    error = UnsupportedGeometryError(
                        f"Allegro rejected the original source {source_kind} "
                        f"({code}: {diagnostic}) on {layer}; "
                        f"footprint {footprint or '(unknown)'}; "
                        f"inspect {results_path} [{stream_name}]"
                    )
                    error.native_error_code = code
                    error.native_source_kind = source_kind
                    error.native_results_path = str(results_path)
                    error.native_build_stream = stream_name
                    error.native_footprint = footprint
                    error.native_layer = layer
                    return error
    return None
