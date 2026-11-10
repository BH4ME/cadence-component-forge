from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import __version__
from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.schema import LibraryIR


CADENCE_ASSET_SUFFIXES = {".dra", ".psm", ".pad", ".fsm", ".olb"}
FOOTPRINT_ASSET_SUFFIXES = {".dra", ".psm", ".pad", ".fsm"}


@dataclass(frozen=True)
class CadenceAsset:
    suffix: str
    stem: str
    key: str
    source: str
    member: str = ""
    path: Path | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "suffix": self.suffix,
            "stem": self.stem,
            "key": self.key,
            "source": self.source,
            "member": self.member,
        }


@dataclass
class CadenceAssetImportReport:
    version: str
    out_dir: str
    roots: list[str] = field(default_factory=list)
    status: str = "not_run"
    counts: dict[str, int] = field(default_factory=dict)
    matches: list[dict[str, Any]] = field(default_factory=list)
    library_assets: list[dict[str, Any]] = field(default_factory=list)
    deduplicated_library_assets: list[dict[str, Any]] = field(default_factory=list)
    unmatched_assets: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    reused_footprints: list[str] = field(default_factory=list)
    reuse_statuses: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "out_dir": self.out_dir,
            "roots": list(self.roots),
            "status": self.status,
            "counts": dict(self.counts),
            "matches": list(self.matches),
            "library_assets": list(self.library_assets),
            "deduplicated_library_assets": list(self.deduplicated_library_assets),
            "unmatched_assets": list(self.unmatched_assets),
            "conflicts": list(self.conflicts),
            "reused_footprints": list(self.reused_footprints),
            "reuse_statuses": dict(self.reuse_statuses),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def import_cadence_assets(
    library: LibraryIR,
    roots: Path | Iterable[Path] | None,
    out_dir: Path,
) -> CadenceAssetImportReport:
    root_paths = _as_paths(roots)
    report = CadenceAssetImportReport(
        version=f"v{__version__}",
        out_dir=str(out_dir.expanduser().resolve()),
        roots=[str(path) for path in root_paths],
    )
    assets: list[CadenceAsset] = []
    for root in root_paths:
        try:
            assets.extend(_scan_root(root))
        except Exception as exc:
            report.errors.append(f"Could not scan Cadence asset root {root}: {exc}")

    report.counts["scanned_assets"] = len(assets)
    if not assets:
        report.status = "no_assets" if not report.errors else "failed"
        report.counts.update(_empty_counts())
        return report

    footprint_keys = {
        normalized_key(footprint.name): footprint.name
        for footprint in library.footprints
        if normalized_key(footprint.name)
    }
    grouped: dict[tuple[str, str], list[CadenceAsset]] = {}
    library_grouped: dict[str, list[CadenceAsset]] = {}
    for asset in assets:
        if asset.suffix == ".olb":
            library_grouped.setdefault(_library_target_key(asset), []).append(asset)
            continue
        footprint_name = footprint_keys.get(asset.key)
        if not footprint_name:
            report.unmatched_assets.append(
                {
                    **asset.to_dict(),
                    "reason": "no_exact_footprint_name_match",
                }
            )
            continue
        grouped.setdefault((footprint_name, asset.suffix), []).append(asset)

    for candidates in sorted(library_grouped.values(), key=lambda items: _library_target_name(items[0]).casefold()):
        if len(candidates) != 1:
            digests = [_asset_digest(candidate) for candidate in candidates]
            if len(set(digests)) == 1:
                _copy_library_asset(candidates[0], out_dir, report)
                report.deduplicated_library_assets.append(
                    {
                        "target_name": _library_target_name(candidates[0]),
                        "sha256": digests[0],
                        "selected": candidates[0].to_dict(),
                        "duplicates": [candidate.to_dict() for candidate in candidates[1:]],
                        "action": "deduplicated_identical_library_assets",
                    }
                )
                report.warnings.append(
                    "Deduplicated identical Cadence .olb assets with basename "
                    f"{_library_target_name(candidates[0])}."
                )
            else:
                report.conflicts.append(
                    {
                        "kind": "library_asset_basename_collision",
                        "target_name": _library_target_name(candidates[0]),
                        "matches": [candidate.to_dict() for candidate in candidates],
                        "reason": "multiple_library_assets_same_basename",
                    }
                )
            continue
        _copy_library_asset(candidates[0], out_dir, report)

    reuse_statuses: dict[str, str] = {}
    for (footprint_name, suffix), candidates in sorted(grouped.items()):
        if len(candidates) != 1:
            report.conflicts.append(
                {
                    "footprint": footprint_name,
                    "suffix": suffix,
                    "matches": [candidate.to_dict() for candidate in candidates],
                    "reason": "multiple_exact_matches",
                }
            )
            continue
        asset = candidates[0]
        target = _target_path(out_dir, footprint_name, suffix)
        try:
            _copy_asset(asset, target)
        except Exception as exc:
            report.errors.append(
                f"Could not copy {asset.source or asset.member} to {target}: {exc}"
            )
            continue
        report.matches.append(
            {
                **asset.to_dict(),
                "footprint": footprint_name,
                "target": str(target),
                "action": "copied",
                "status": "reused",
            }
        )
        if suffix in {".dra", ".psm"}:
            reuse_statuses[footprint_name] = _reuse_status_for_footprint(
                out_dir,
                footprint_name,
            )

    report.reused_footprints = sorted(reuse_statuses)
    report.counts.update(
        {
            "matched_assets": len(report.matches),
            "library_assets": len(report.library_assets),
            "deduplicated_library_assets": len(report.deduplicated_library_assets),
            "unmatched_assets": len(report.unmatched_assets),
            "conflicts": len(report.conflicts),
            "library_conflicts": sum(
                1
                for conflict in report.conflicts
                if conflict.get("kind") == "library_asset_basename_collision"
            ),
            "reused_footprints": len(report.reused_footprints),
        }
    )
    if report.errors or report.conflicts:
        report.status = "failed"
    elif report.matches or report.library_assets:
        report.status = "complete" if not report.unmatched_assets else "partial"
    else:
        report.status = "no_matches"
    report.reuse_statuses = reuse_statuses
    return report


def write_cadence_asset_import_report(
    report: CadenceAssetImportReport,
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def cadence_asset_reuse_statuses(report: CadenceAssetImportReport) -> dict[str, str]:
    return dict(report.reuse_statuses)


def _as_paths(roots: Path | Iterable[Path] | None) -> list[Path]:
    if roots is None:
        return []
    if isinstance(roots, (str, Path)):
        roots = [roots]
    return [Path(root).expanduser().resolve() for root in roots if str(root).strip()]


def _scan_root(root: Path) -> list[CadenceAsset]:
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_dir():
        return [_asset_from_path(path) for path in sorted(root.rglob("*")) if _is_asset(path)]
    if zipfile.is_zipfile(root):
        assets = []
        with zipfile.ZipFile(root) as archive:
            for member in sorted(archive.infolist(), key=lambda item: item.filename.lower()):
                suffix = Path(member.filename).suffix.lower()
                if suffix not in CADENCE_ASSET_SUFFIXES or member.is_dir():
                    continue
                stem = Path(member.filename).stem
                assets.append(
                    CadenceAsset(
                        suffix=suffix,
                        stem=stem,
                        key=normalized_key(stem),
                        source=str(root),
                        member=member.filename,
                    )
                )
        return assets
    if _is_asset(root):
        return [_asset_from_path(root)]
    raise ValueError(f"Unsupported Cadence asset input: {root}")


def _asset_from_path(path: Path) -> CadenceAsset:
    return CadenceAsset(
        suffix=path.suffix.lower(),
        stem=path.stem,
        key=normalized_key(path.stem),
        source=str(path),
        path=path,
    )


def _is_asset(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in CADENCE_ASSET_SUFFIXES


def _copy_asset(asset: CadenceAsset, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if asset.path is not None:
        shutil.copy2(asset.path, target)
        return
    with zipfile.ZipFile(asset.source) as archive:
        with archive.open(asset.member) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)


def _asset_digest(asset: CadenceAsset) -> str:
    digest = hashlib.sha256()
    if asset.path is not None:
        with asset.path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    with zipfile.ZipFile(asset.source) as archive, archive.open(asset.member) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_library_asset(
    asset: CadenceAsset,
    out_dir: Path,
    report: CadenceAssetImportReport,
) -> None:
    target = out_dir / "cadence_assets" / "capture" / _library_target_name(asset)
    try:
        _copy_asset(asset, target)
    except Exception as exc:
        report.errors.append(
            f"Could not copy Cadence .olb asset {asset.source or asset.member}: {exc}"
        )
        return
    report.library_assets.append(
        {
            **asset.to_dict(),
            "target": str(target),
            "action": "copied_library_asset",
            "status": "supplied_not_merged",
        }
    )


def _library_target_name(asset: CadenceAsset) -> str:
    return Path(asset.member or asset.source).name


def _library_target_key(asset: CadenceAsset) -> str:
    return _library_target_name(asset).casefold()


def _target_path(out_dir: Path, footprint_name: str, suffix: str) -> Path:
    if suffix == ".pad":
        return out_dir / "allegro" / "padstacks" / f"{cadence_safe_name(footprint_name)}{suffix}"
    return out_dir / "allegro" / "symbols" / f"{cadence_safe_name(footprint_name)}{suffix}"


def _reuse_status_for_footprint(out_dir: Path, footprint_name: str) -> str:
    stem = cadence_safe_name(footprint_name)
    symbols_dir = out_dir / "allegro" / "symbols"
    if (symbols_dir / f"{stem}.dra").exists() and (symbols_dir / f"{stem}.psm").exists():
        return "reused_psm"
    if (symbols_dir / f"{stem}.dra").exists():
        return "reused_dra"
    if (symbols_dir / f"{stem}.psm").exists():
        return "reused_psm"
    return "reused_asset"


def _empty_counts() -> dict[str, int]:
    return {
        "matched_assets": 0,
        "library_assets": 0,
        "deduplicated_library_assets": 0,
        "unmatched_assets": 0,
        "conflicts": 0,
        "library_conflicts": 0,
        "reused_footprints": 0,
    }
