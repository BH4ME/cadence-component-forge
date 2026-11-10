from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir.geometry import normalized_key
from .models import MODEL_SUFFIXES, collect_model_assets


@dataclass
class ModelAliasSet:
    path: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    model_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "count": len(self.rows),
            "mapping": dict(self.mapping),
            "rows": list(self.rows),
            "model_files": list(self.model_files),
        }


def load_model_aliases(path: Path | None) -> ModelAliasSet:
    if path is None:
        return ModelAliasSet()
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Model aliases file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    aliases = ModelAliasSet(path=str(path))
    for candidate, target in _iter_alias_pairs(data):
        candidate_text = str(candidate or "").strip()
        target_text = str(target or "").strip()
        candidate_key = normalized_key(candidate_text)
        target_key = _target_key(target_text)
        if not candidate_key or not target_key:
            continue
        target_path = _resolve_target_path(target_text, path.parent)
        target_is_file = Path(target_text).suffix.lower() in MODEL_SUFFIXES
        row = {
            "candidate": candidate_text,
            "candidate_key": candidate_key,
            "target": target_text,
            "target_key": target_key,
            "target_path": str(target_path) if target_path else "",
            "target_is_file": target_is_file,
            "target_exists": bool(target_path),
        }
        aliases.mapping[candidate_key] = target_key
        aliases.rows.append(row)
        if target_path and str(target_path) not in aliases.model_files:
            aliases.model_files.append(str(target_path))
    return aliases


def collect_model_alias_assets(aliases: ModelAliasSet, output_dir: Path | None) -> list:
    assets = []
    for file_name in aliases.model_files:
        assets.extend(collect_model_assets(Path(file_name), output_dir))
    return assets


def _iter_alias_pairs(data: Any):
    if isinstance(data, dict):
        for key in ["aliases", "model_aliases"]:
            if key in data:
                yield from _iter_alias_pairs(data[key])
                return
        for key in ["models", "footprints", "packages"]:
            if key in data and isinstance(data[key], dict):
                yield from data[key].items()
                return
        for candidate, target in data.items():
            if isinstance(target, str):
                yield candidate, target
            elif isinstance(target, dict):
                yield candidate, _target_from_object(target)
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_object(item)
            target = _target_from_object(item)
            if candidate and target:
                yield candidate, target


def _candidate_from_object(item: dict[str, Any]) -> str:
    for key in ["candidate", "footprint", "package", "model_id", "key", "from"]:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _target_from_object(item: dict[str, Any]) -> str:
    for key in ["target", "model", "model_key", "file", "step", "path", "to"]:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _target_key(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.suffix.lower() in MODEL_SUFFIXES:
        return normalized_key(path.stem)
    return normalized_key(value)


def _resolve_target_path(value: str, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if path.exists() and path.suffix.lower() in MODEL_SUFFIXES:
        return path
    return None
