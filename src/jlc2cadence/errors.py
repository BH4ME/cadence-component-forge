"""Structured conversion failures shared by native-format generators."""


class UnsupportedGeometryError(ValueError):
    """Known original geometry cannot be represented by the current converter."""

    error_code = "unsupported_geometry"
