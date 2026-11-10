from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..ir.geometry import cadence_safe_name, footprint_bounds, normalized_key
from ..ir.schema import Footprint, LibraryIR, Model3D
from ..ir.units import parse_easyeda_3d_transform


PLACEHOLDER_MODEL_KIND = "placeholder_3d_envelope"


@dataclass
class Placeholder3DSpec:
    footprint: str
    file: str
    width: float
    depth: float
    height: float
    center: tuple[float, float]
    footprint_bounds: tuple[float, float, float, float]
    size_source: str
    height_source: str
    easyeda_model_id: str = ""
    easyeda_model_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "footprint": self.footprint,
            "file": self.file,
            "width": self.width,
            "depth": self.depth,
            "height": self.height,
            "center": list(self.center),
            "footprint_bounds": list(self.footprint_bounds),
            "size_source": self.size_source,
            "height_source": self.height_source,
            "easyeda_model_id": self.easyeda_model_id,
            "easyeda_model_title": self.easyeda_model_title,
            "kind": PLACEHOLDER_MODEL_KIND,
        }


@dataclass
class Placeholder3DReport:
    status: str = "unchecked"
    output_dir: str = ""
    generated: list[Placeholder3DSpec] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def generated_count(self) -> int:
        return len(self.generated)

    @property
    def models(self) -> list[Model3D]:
        models: list[Model3D] = []
        for spec in self.generated:
            models.append(
                Model3D(
                    file=spec.file,
                    source_file=spec.file,
                    format="step",
                    key=normalized_key(spec.footprint),
                    target_size=(spec.width, spec.depth, spec.height),
                    transform_source=PLACEHOLDER_MODEL_KIND,
                    units="mm",
                    properties={
                        "kind": PLACEHOLDER_MODEL_KIND,
                        "generated_by": f"jlc2cadence v{__version__}",
                        "source": "footprint_envelope",
                    },
                )
            )
        return models

    def finalize(self) -> None:
        if self.errors and self.generated:
            self.status = "partial"
        elif self.errors:
            self.status = "failed"
        elif self.generated:
            self.status = "generated"
        else:
            self.status = "nothing_to_generate"
        if self.generated and not any("placeholder STEP" in warning for warning in self.warnings):
            self.warnings.append(
                f"Generated {len(self.generated)} placeholder STEP envelope model(s); "
                "replace them with real vendor/JLC STEP files before mechanical signoff."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": f"v{__version__}",
            "status": self.status,
            "output_dir": self.output_dir,
            "generated_count": self.generated_count,
            "skipped_existing_count": len(self.skipped_existing),
            "generated": [spec.to_dict() for spec in self.generated],
            "skipped_existing": list(self.skipped_existing),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "notes": [
                "Placeholder models are simple STEP box envelopes inferred from footprint geometry and package-name heuristics.",
                "They are useful for Cadence 3D mapping workflow review but are not manufacturer-verified mechanical models.",
            ],
        }


def synthesize_missing_step_models(
    library: LibraryIR,
    output_dir: Path,
    *,
    overwrite: bool = True,
) -> Placeholder3DReport:
    """Create clearly marked STEP envelope placeholders for footprints with no linked model."""
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = Placeholder3DReport(output_dir=str(output_dir))
    for footprint in library.footprints:
        if footprint.model3d is not None:
            report.skipped_existing.append(footprint.name)
            continue
        target = _placeholder_path(output_dir, footprint.name)
        if target.exists() and not overwrite:
            report.errors.append(f"Placeholder STEP target already exists: {target}")
            continue
        spec = _placeholder_spec(footprint, target)
        try:
            _export_box_step(spec)
        except Exception as exc:  # pragma: no cover - depends on optional CAD runtime
            report.errors.append(f"Unable to generate placeholder STEP for {footprint.name}: {exc}")
            continue
        footprint.model3d = Model3D(
            file=spec.file,
            source_file=spec.file,
            format="step",
            key=normalized_key(footprint.name),
            target_size=(spec.width, spec.depth, spec.height),
            transform_source=PLACEHOLDER_MODEL_KIND,
            units="mm",
            properties={
                "kind": PLACEHOLDER_MODEL_KIND,
                "generated_by": f"jlc2cadence v{__version__}",
                "source": "footprint_envelope",
            },
        )
        report.generated.append(spec)
    report.finalize()
    return report


def write_placeholder_3d_report(report: Placeholder3DReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def is_placeholder_model(model: Model3D | dict[str, Any] | None) -> bool:
    if model is None:
        return False
    if isinstance(model, Model3D):
        return (
            model.transform_source == PLACEHOLDER_MODEL_KIND
            or model.properties.get("kind") == PLACEHOLDER_MODEL_KIND
        )
    properties = model.get("properties")
    return (
        str(model.get("transform_source", "")) == PLACEHOLDER_MODEL_KIND
        or (isinstance(properties, dict) and str(properties.get("kind", "")) == PLACEHOLDER_MODEL_KIND)
    )


def _placeholder_path(output_dir: Path, footprint_name: str) -> Path:
    safe_name = cadence_safe_name(footprint_name, "MODEL")
    return output_dir / f"{safe_name}.step"


def _placeholder_spec(footprint: Footprint, target: Path) -> Placeholder3DSpec:
    bounds = footprint_bounds(footprint)
    min_x, min_y, max_x, max_y = bounds
    bbox_width = max(max_x - min_x, 0.2)
    bbox_depth = max(max_y - min_y, 0.2)
    center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    target_size = _easyeda_target_size(footprint)
    name_size = _package_name_size(footprint.name)

    width, size_source = _choose_xy_size(target_size, name_size, bbox_width, axis=0)
    depth, depth_source = _choose_xy_size(target_size, name_size, bbox_depth, axis=1)
    size_source = "easyeda_target_size" if "easyeda" in {size_source, depth_source} else (
        "package_name" if "name" in {size_source, depth_source} else "footprint_bounds"
    )
    height, height_source = _choose_height(target_size, name_size, footprint.name)

    return Placeholder3DSpec(
        footprint=footprint.name,
        file=str(target),
        width=_round_mm(width),
        depth=_round_mm(depth),
        height=_round_mm(height),
        center=(_round_mm(center[0]), _round_mm(center[1])),
        footprint_bounds=tuple(_round_mm(value) for value in bounds),
        size_source=size_source,
        height_source=height_source,
        easyeda_model_id=footprint.properties.get("EasyEDA_3D_Model_Id", ""),
        easyeda_model_title=footprint.properties.get("EasyEDA_3D_Model_Title", ""),
    )


def _easyeda_target_size(footprint: Footprint) -> tuple[float, float, float] | None:
    parsed = parse_easyeda_3d_transform(footprint.properties.get("EasyEDA_3D_Model_Transform"))
    if parsed is None:
        return None
    target_size, _rotation, _offset = parsed
    if any(value > 0 for value in target_size):
        return target_size
    return None


def _choose_xy_size(
    target_size: tuple[float, float, float] | None,
    name_size: tuple[float, float, float] | None,
    bbox_value: float,
    *,
    axis: int,
) -> tuple[float, str]:
    if target_size is not None and target_size[axis] > 0:
        return max(float(target_size[axis]), 0.2), "easyeda"
    if name_size is not None and name_size[axis] > 0:
        return max(float(name_size[axis]), bbox_value, 0.2), "name"
    return max(bbox_value + 0.2, 0.5), "bounds"


def _choose_height(
    target_size: tuple[float, float, float] | None,
    name_size: tuple[float, float, float] | None,
    footprint_name: str,
) -> tuple[float, str]:
    if target_size is not None and target_size[2] > 0:
        return max(float(target_size[2]), 0.1), "easyeda_target_size"
    if name_size is not None and name_size[2] > 0:
        return max(float(name_size[2]), 0.1), "package_name"
    upper = footprint_name.upper()
    if any(token in upper for token in ["USB", "RJ", "CONN_TH", "HEADER", "HDR_TH", "BTB"]):
        return 4.0, "package_family"
    if any(token in upper for token in ["FPC", "FFC"]):
        return 2.0, "package_family"
    if any(token in upper for token in ["SOT", "SOP", "SOIC", "TSSOP", "MSOP"]):
        return 1.2, "package_family"
    if any(token in upper for token in ["QFN", "DFN", "LGA", "BGA"]):
        return 0.9, "package_family"
    if any(token in upper for token in ["CRYSTAL", "OSC", "XTAL"]):
        return 0.8, "package_family"
    return 1.0, "default"


def _package_name_size(name: str) -> tuple[float, float, float] | None:
    upper = name.upper()
    metric = re.search(r"(?<!\d)(0201|0402|0603|0805|1206|1210|1812)(?!\d)", upper)
    if metric:
        return {
            "0201": (0.6, 0.3, 0.3),
            "0402": (1.0, 0.5, 0.35),
            "0603": (1.6, 0.8, 0.45),
            "0805": (2.0, 1.25, 0.55),
            "1206": (3.2, 1.6, 0.65),
            "1210": (3.2, 2.5, 0.75),
            "1812": (4.5, 3.2, 0.8),
        }[metric.group(1)]
    imperial = re.search(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812)(?!\d)", upper)
    if imperial:
        return {
            "01005": (0.4, 0.2, 0.2),
            "0201": (0.6, 0.3, 0.3),
            "0402": (1.0, 0.5, 0.35),
            "0603": (1.6, 0.8, 0.45),
            "0805": (2.0, 1.25, 0.55),
            "1206": (3.2, 1.6, 0.65),
            "1210": (3.2, 2.5, 0.75),
            "1812": (4.5, 3.2, 0.8),
        }[imperial.group(1)]
    size_match = re.search(r"L(\d+(?:_\d+)?)_W(\d+(?:_\d+)?)", upper)
    if size_match:
        return (_number_token(size_match.group(1)), _number_token(size_match.group(2)), 1.0)
    return None


def _number_token(value: str) -> float:
    return float(value.replace("_", "."))


def _round_mm(value: float) -> float:
    return round(float(value), 6)


def _export_box_step(spec: Placeholder3DSpec) -> None:
    try:
        from build123d import Align, Box, Location, export_step
    except ImportError as exc:  # pragma: no cover - depends on optional CAD runtime
        raise RuntimeError(
            "build123d is required for --synthesize-missing-3d; install build123d or provide real STEP/STP files."
        ) from exc

    shape = Location((spec.center[0], spec.center[1], 0.0)) * Box(
        spec.width,
        spec.depth,
        spec.height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    shape.label = cadence_safe_name(f"{spec.footprint}_PLACEHOLDER_3D", "PLACEHOLDER_3D")
    export_step(shape, spec.file)
