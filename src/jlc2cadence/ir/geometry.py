from __future__ import annotations

import re
from hashlib import sha1

from .schema import Footprint, Graphic, Pad


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def cadence_safe_name(value: str, fallback: str = "PART") -> str:
    text = value.strip() if value else fallback
    text = re.sub(r"[^A-Za-z0-9_$]+", "_", text)
    text = text.strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"_{text}"
    if len(text) > 60:
        digest = sha1(text.encode("utf-8")).hexdigest()[:8]
        text = f"{text[:51]}_{digest}"
    return text


def footprint_bounds(footprint: Footprint) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for pad in footprint.pads:
        px, py = _pad_extent(pad)
        xs.extend(px)
        ys.extend(py)
    for graphic in footprint.graphics:
        gx, gy = _graphic_extent(graphic)
        xs.extend(gx)
        ys.extend(gy)
    if not xs or not ys:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _pad_extent(pad: Pad) -> tuple[list[float], list[float]]:
    sx, sy = pad.size
    half_x = sx / 2 if sx else 0.25
    half_y = sy / 2 if sy else 0.25
    return [pad.x - half_x, pad.x + half_x], [pad.y - half_y, pad.y + half_y]


def _graphic_extent(graphic: Graphic) -> tuple[list[float], list[float]]:
    if graphic.points:
        return [point[0] for point in graphic.points], [point[1] for point in graphic.points]
    return [graphic.x], [graphic.y]

