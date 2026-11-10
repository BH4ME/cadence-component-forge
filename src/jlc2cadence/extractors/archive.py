from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


DESIGN_SUFFIXES = {
    ".schdoc",
    ".schlib",
    ".pcbdoc",
    ".pcblib",
    ".prjpcb",
    ".libpkg",
    ".asc",
    ".d",
    ".p",
    ".txt",
    ".epru",
    ".efoo",
    ".esym",
    ".edevice",
    ".esch",
    ".epcb",
}


def prepare_input_files(input_path: Path, work_dir: Path | None = None) -> list[Path]:
    """Return all files represented by input_path.

    Directories are read in place. Zip archives are extracted to work_dir.
    A single plain file returns a one-item list.
    """

    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if input_path.is_dir():
        return sorted(p for p in input_path.rglob("*") if p.is_file())

    if zipfile.is_zipfile(input_path):
        if work_dir is None:
            raise ValueError("work_dir is required for zip input")
        work_dir.mkdir(parents=True, exist_ok=True)
        target_dir = work_dir / input_path.stem
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True)
        with zipfile.ZipFile(input_path) as zf:
            _safe_extract_zip(zf, target_dir)
        return sorted(p for p in target_dir.rglob("*") if p.is_file())

    return [input_path]


def find_design_files(paths: list[Path]) -> list[Path]:
    design = [path for path in paths if path.suffix.lower() in DESIGN_SUFFIXES]
    if design:
        return sorted(design)
    return sorted(path for path in paths if _looks_like_altium_ascii(path) or _looks_like_easyeda_pro_jsonl(path))


def _looks_like_altium_ascii(path: Path) -> bool:
    if path.suffix.lower() in {".step", ".stp", ".obj", ".zip", ".png", ".jpg", ".jpeg"}:
        return False
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    return "RECORD=" in sample and "|" in sample


def _looks_like_easyeda_pro_jsonl(path: Path) -> bool:
    if path.suffix.lower() in {".step", ".stp", ".obj", ".zip", ".png", ".jpg", ".jpeg"}:
        return False
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except OSError:
        return False
    return "||" in sample and '"DOCHEAD"' in sample and '"docType"' in sample


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in zf.infolist():
        member_path = (target_dir / member.filename).resolve()
        if target_root not in [member_path, *member_path.parents]:
            raise ValueError(f"Unsafe zip path: {member.filename}")
    zf.extractall(target_dir)
