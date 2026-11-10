from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.schema import Model3D


MODEL_SUFFIXES = {".step", ".stp", ".obj"}


def collect_model_assets(input_path: Path | None, output_dir: Path | None) -> list[Model3D]:
    if input_path is None:
        return []
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        return []

    files = _collect_model_files(input_path)
    assets: list[Model3D] = []
    seen_targets: set[Path] = set()
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    for source in files:
        target = source
        if output_dir is not None:
            safe_stem = cadence_safe_name(source.stem)
            target = output_dir / f"{safe_stem}{source.suffix.lower()}"
            if target.resolve() not in seen_targets:
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                seen_targets.add(target.resolve())
        assets.append(
            Model3D(
                file=str(target),
                source_file=str(source),
                format=source.suffix.lower().lstrip("."),
                key=normalized_key(source.stem),
            )
        )
    return assets


def _collect_model_files(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        return sorted(path for path in input_path.rglob("*") if path.suffix.lower() in MODEL_SUFFIXES)
    if zipfile.is_zipfile(input_path):
        extract_dir = input_path.parent / f".{input_path.stem}_models"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)
        with zipfile.ZipFile(input_path) as zf:
            _safe_extract_zip(zf, extract_dir)
        return sorted(path for path in extract_dir.rglob("*") if path.suffix.lower() in MODEL_SUFFIXES)
    return [input_path] if input_path.suffix.lower() in MODEL_SUFFIXES else []


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in zf.infolist():
        member_path = (target_dir / member.filename).resolve()
        if target_root not in [member_path, *member_path.parents]:
            raise ValueError(f"Unsafe zip path: {member.filename}")
    zf.extractall(target_dir)

