from __future__ import annotations

import re


MM_PER_MIL = 0.0254
MM_PER_INCH = 25.4


def parse_length(value: str | int | float | None, default_unit: str = "mm") -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return _scale(float(value), default_unit)
    text = str(value).strip().lower().replace(",", ".")
    if not text:
        return 0.0
    text = text.replace(" ", "")
    match = re.match(r"^([-+]?\d+(?:\.\d+)?)([a-z]*)$", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2) or default_unit
    return _scale(number, unit)


def _scale(number: float, unit: str) -> float:
    unit = unit.lower()
    if unit in {"mm", "millimeter", "millimeters"}:
        return number
    if unit in {"mil", "mils"}:
        return number * MM_PER_MIL
    if unit in {"in", "inch", "inches"}:
        return number * MM_PER_INCH
    return number


def fmt_mm(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def parse_easyeda_3d_transform(
    value: str | list[object] | tuple[object, ...] | None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]] | None:
    """Parse EasyEDA Pro V3's 9-field 3D transform into mm/degree tuples.

    V3 stores ``sizeX,sizeY,sizeZ,rotZ,rotX,rotY,offX,offY,offZ``.
    The dimensional values use the same mil-based coordinate convention as
    the surrounding V3 library records; rotations remain degrees. A zero
    ``sizeZ`` is preserved because V3 uses it to mean automatic height.
    """

    if value is None:
        return None
    if isinstance(value, str):
        tokens = [token for token in re.split(r"[,;\s]+", value.strip()) if token]
    elif isinstance(value, (list, tuple)):
        tokens = list(value)
    else:
        return None
    if len(tokens) != 9:
        return None
    try:
        raw = [float(str(token).strip().replace(",", ".")) for token in tokens]
    except (TypeError, ValueError):
        return None
    target_size = tuple(number * MM_PER_MIL for number in raw[0:3])
    rotation = (raw[4], raw[5], raw[3])
    offset = tuple(number * MM_PER_MIL for number in raw[6:9])
    return target_size, rotation, offset
