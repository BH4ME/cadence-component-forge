"""File-based worker for the Capture Tcl/Tk component browser."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPException
from math import hypot
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from . import __version__
from .native_geometry_errors import source_native_geometry_error
from .extractors.easyeda_3d import EasyEDA3DFetchReport, fetch_easyeda_3d_models, write_easyeda_3d_fetch_report
from .generators.allegro_skill import _padstack_name, source_refdes_report, write_allegro_outputs
from .generators.capture import write_capture_outputs
from .generators.model3d import write_model_map
from .ir.geometry import cadence_safe_name
from .ir.normalize import attach_models
from .ir.schema import LibraryIR
from .reports.svg_preview import write_svg_previews
from .step_preview import cached_step_preview_error, is_current_step_glb
from .validators.cadence_runner import (
    CadenceTools, compile_existing_package_symbols, run_step_mapping, run_symbol_build_plan,
)
from .validators.library_checks import validate_library


BRIDGE_BUILD_REVISION = 7
MAX_PREVIEW_GLB_BYTES = 6 * 1024 * 1024


def _ensure_worker_stdio() -> None:
    """Keep pythonw workers usable when Windows gives them no console streams."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def failure_response(exc: Exception) -> dict[str, Any]:
    error = str(exc)
    code = getattr(exc, "error_code", "") or "operation_failed"
    messages = {
        "no_cad_library": "\u6ca1\u6709 CAD \u6587\u4ef6\uff1a\u672a\u627e\u5230\u8be5\u5143\u5668\u4ef6\u7684\u7acb\u521b\u539f\u59cb CAD \u5e93\uff0c\u65e0\u6cd5\u5bfc\u5165\u3002",
        "unsupported_geometry": "\u7531\u4e8e\u662f\u5f02\u5f62\u7ed3\u6784\uff0c\u5f53\u524d\u65e0\u6cd5\u751f\u6210\u3002",
    }
    return {
        "ok": False, "error": error, "error_type": type(exc).__name__,
        "error_code": code, "user_message": messages.get(code, error),
    }


def write_response(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _catalog(root: Path, timeout: float):
    from .extractors.easyeda_catalog import EasyEDACatalog

    return EasyEDACatalog(cache_dir=root / "cache" / "catalog", timeout=timeout)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _owned_asset_path(value: str | Path, root: Path, parents: tuple[Path, ...]) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(root) or path.parent not in parents:
        raise ValueError("Cache asset is outside its managed directory")
    return path


def _known_cache_assets(root: Path, build: Path, *, published: bool) -> set[Path]:
    """Discover only generated assets in this build, without trusting JSON paths."""
    paths = {build / "capture" / "library.olb.xml", build / "reports" / "library.ir.json"}
    for relative, suffixes in (("3d", {".step", ".stp"}),
                               ("allegro/symbols", {".dra", ".psm"}),
                               ("allegro/padstacks", {".pad"})):
        directory = build / relative
        for path in directory.glob("*"):
            if path.suffix.lower() in suffixes:
                paths.add(path)
                if published:
                    paths.add(root / relative / path.name)
    # Check every resolved path before any content is read or copied, including
    # junctions/symlinks in otherwise familiar build directory names.
    return {_owned_asset_path(path, root, (path.parent,)) for path in paths}


def _cache_asset_paths(result: dict[str, Any], root: Path, build: Path) -> set[Path]:
    fixed = {"library_root": root, "build_dir": build,
             "capture_xml": build / "capture" / "library.olb.xml",
             "olb_path": build / "capture" / "library.olb",
             "source_provenance": build / "reports" / "source_provenance.json"}
    for key, expected in fixed.items():
        if Path(result[key]).resolve() != expected:
            raise ValueError(f"Cache identity mismatch: {key}")
    paths = _known_cache_assets(root, build, published=not result["prepared_only"])
    published_parents = (root / "3d", root / "allegro" / "symbols", root / "allegro" / "padstacks")
    for value in result["published_files"]:
        path = _owned_asset_path(value, root, published_parents)
        if path not in paths:
            raise ValueError("Published cache asset has no generated source")
    for value in result.get("step_files", []):
        if _owned_asset_path(value, root, (root / "3d", build / "3d")) not in paths:
            raise ValueError("STEP cache asset has no generated source")
    if result.get("footprint_path"):
        _owned_asset_path(result["footprint_path"], root, (root / "allegro" / "symbols", build / "allegro" / "symbols"))
    for value in result.get("previews", {}).values():
        _owned_asset_path(value, root, (build / "reports",))
    return paths


def _load_asset_cache(
    manifest: Path, root: Path, build: Path, part_id: str, target_root: str, *, prepared: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not manifest.is_file():
        return None, []
    proofs: dict[str, str] = {}
    paths = _known_cache_assets(root, build, published=not prepared)
    try:
        result = json.loads(manifest.read_text(encoding="utf-8"))
        if (result["part_id"] != part_id or result["version"] != __version__
                or result["build_revision"] != BRIDGE_BUILD_REVISION
                or result["cadence_target"] != target_root or result["prepared_only"] is not prepared
                or not isinstance(result["published_files"], list)
                or not isinstance(result["warnings"], list)):
            raise ValueError("Cache identity mismatch")
        paths = _cache_asset_paths(result, root, build)
        candidate = result["generated_file_sha256"]
        if not isinstance(candidate, dict) or set(candidate) != {str(path) for path in paths}:
            raise ValueError("Cache integrity proofs are missing or incomplete")
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in candidate.values()):
            raise ValueError("Cache integrity proof is invalid")
        proofs = candidate
        if any(not path.is_file() or not path.stat().st_size or _file_sha256(path) != proofs[str(path)] for path in paths):
            raise ValueError("Generated assets have changed or are missing")
        return result, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    # A manually edited managed file is valuable user data. Preserve its exact
    # bytes before rebuilding; if preservation fails, fail without overwriting.
    backups = []
    for path in sorted(paths):
        if path.is_file():
            digest = _file_sha256(path)
            if proofs.get(str(path)) == digest:
                continue
            backup = root / ".modified_assets" / build.name / path.relative_to(root) / f"{digest}.bak"
            if not backup.resolve().is_relative_to(root / ".modified_assets"):
                raise ValueError("Modified asset backup directory is outside the library")
            _publish_file(path, backup)
            backups.append(str(backup))
    warning = "缓存索引或生成文件校验失败，已从原始数据重新生成。"
    if backups:
        warning += f" 原有文件已备份至 {root / '.modified_assets' / build.name}。"
    return None, [warning]


def search_parts(
    query: str, root: Path, timeout: float = 20.0, *, page: int = 1, limit: int = 30,
) -> dict[str, Any]:
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError("Enter a part number or a keyword between 1 and 200 characters.")
    if (type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError("Search page must be positive and limit must be between 1 and 100.")
    catalog = _catalog(root, timeout)
    rows = []
    parts = catalog.search(query, limit=limit, page=page)
    # Exact IDs are a single result; the catalog lookup itself is not paginated.
    if page > 1 and re.fullmatch(r"C[0-9]+", query, re.IGNORECASE):
        parts = []
    for part in parts:
        data = part.to_dict()
        data["lcsc_id"] = data.get("lcsc_id", data.get("part_id", ""))
        data["title"] = data.get("title", data.get("name", ""))
        # Search metadata is not proof that a STEP can actually be downloaded.
        data.setdefault("has_step", None)
        rows.append(data)
    total = max(0, int(catalog.last_total))
    return {
        "ok": True, "results": rows, "query": query, "warnings": [],
        "total": total, "page": page, "page_size": limit, "has_more": page * limit < total,
    }


def _part_id(value: str) -> str:
    result = value.strip().upper()
    if not re.fullmatch(r"C[0-9]{3,}", result):
        raise ValueError("A valid LCSC part number such as C2040 is required.")
    return result


def _name(value: str) -> str:
    result = cadence_safe_name(value, "PART")
    if len(result) > 31:
        result = result[:22] + "_" + hashlib.sha256(result.encode()).hexdigest()[:8]
    return result


def namespace_part(library: LibraryIR, part_id: str) -> None:
    """Give unrelated catalog packages independent Allegro and Capture names."""
    def prefixed(value: str) -> str:
        return _name(value if value.upper().startswith(part_id + "_") else f"{part_id}_{value}")

    # Allegro writes package-symbol basenames in lowercase; its native symbol
    # placement API is case-sensitive even on Windows.
    names = {footprint.name: prefixed(footprint.name).lower() for footprint in library.footprints}
    for footprint in library.footprints:
        footprint.properties["Source_Footprint_Name"] = footprint.name
        footprint.name = names[footprint.name]
        for pad in footprint.pads:
            # Native PAD files contain build-specific metadata. Sharing a name
            # across parts would overwrite another part's verified file even
            # when the original pad geometry is identical.
            pad.padstack = prefixed(pad.padstack or _padstack_name(pad))
    for component in library.components:
        component.properties["Source_Symbol_Name"] = component.name
        component.name = prefixed(component.name)
        component.footprint = names.get(component.footprint, component.footprint)
        component.properties["LCSC Part #"] = part_id
        component.properties["PCB Footprint"] = component.footprint


@contextmanager
def _part_lock(root: Path, part_id: str) -> Iterator[None]:
    directory = root / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{part_id}.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"An import of {part_id} is already running.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"An import of {part_id} is already running.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _publish_file(source: Path, target: Path) -> None:
    import tempfile
    import time

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        for attempt in range(6):
            try:
                temporary.replace(target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                # Native readers and virus scanners can briefly deny replacement on Windows.
                time.sleep(0.05 * 2 ** attempt)
    finally:
        temporary.unlink(missing_ok=True)


def write_allegro_paths(root: Path) -> None:
    """Write an include file for Allegro's native environment-file syntax."""
    root = root.resolve()
    lines = ["# Generated by the JLC2Capture component browser."]
    for key, directory in (
        ("psmpath", root / "allegro" / "symbols"),
        ("padpath", root / "allegro" / "padstacks"),
        ("steppath", root / "3d"),
    ):
        value = directory.as_posix()
        if any(char in value for char in ('"', "\n", "\r", "$")):
            raise ValueError("The library path contains a character unsupported by Allegro env files.")
        lines.append(f'set {key} = ( "{value}" ${key} )')
    (root / "allegro_paths.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _capture_symbol_preview(path: Path, symbol_name: str) -> dict[str, Any]:
    document = ElementTree.parse(path).getroot()
    packages = document.findall("Package")
    package = next(
        (item for item in packages if item.find("Defn").get("name") == symbol_name),
        packages[0] if packages else None,
    )
    part = package.find("LibPart") if package is not None else None
    normal = part.find("NormalView") if part is not None else None
    preview: dict[str, Any] = {
        "name": symbol_name, "units": "capture", "source_path": str(path.resolve()),
        "pins": [], "graphics": [],
    }
    if normal is None:
        return preview
    physical = part.find("PhysicalPart")
    physical_numbers = {
        item.get("position", ""): item.get("number", "")
        for item in physical.findall("PinNumber/Defn")
    } if physical is not None else {}
    for rect in normal.findall("Rect/Defn"):
        x1, x2 = float(rect.get("x1", "0")), float(rect.get("x2", "0"))
        y1, y2 = -float(rect.get("y1", "0")), -float(rect.get("y2", "0"))
        preview["graphics"].append({
            "kind": "line", "width": float(rect.get("lineWidth", "0")),
            "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]],
        })
    for line in normal.findall("Line/Defn"):
        preview["graphics"].append({
            "kind": "line", "width": float(line.get("lineWidth", "0")),
            "points": [[float(line.get("x1", "0")), -float(line.get("y1", "0"))],
                       [float(line.get("x2", "0")), -float(line.get("y2", "0"))]],
        })
    for ellipse in normal.findall("Ellipse/Defn"):
        preview["graphics"].append({
            "kind": "ellipse", "width": float(ellipse.get("lineWidth", "0")),
            "points": [[float(ellipse.get("x1", "0")), -float(ellipse.get("y1", "0"))],
                       [float(ellipse.get("x2", "0")), -float(ellipse.get("y2", "0"))]],
        })
    for polyline in normal.findall("Polyline"):
        definition = polyline.find("Defn")
        preview["graphics"].append({
            "kind": "polyline", "width": float(definition.get("lineWidth", "0")) if definition is not None else 0,
            "points": [[float(point.get("x", "0")), -float(point.get("y", "0"))]
                       for point in polyline.findall("PolylinePoint/Defn")],
        })
    for arc in normal.findall("Arc/Defn"):
        cx = (float(arc.get("x1", "0")) + float(arc.get("x2", "0"))) / 2
        cy = -(float(arc.get("y1", "0")) + float(arc.get("y2", "0"))) / 2
        start = [float(arc.get("startX", "0")), -float(arc.get("startY", "0"))]
        end = [float(arc.get("endX", "0")), -float(arc.get("endY", "0"))]
        preview["graphics"].append({
            "kind": "arc", "width": float(arc.get("lineWidth", "0")), "points": [start, end],
            "properties": {"center_x_mm": str(cx), "center_y_mm": str(cy), "clockwise": "false"},
        })
    source_labels = bool(normal.findall("CommentText"))
    for comment in normal.findall("CommentText"):
        definition, font = comment.find("Defn"), comment.find("TextFont/Defn")
        if definition is None:
            continue
        preview["graphics"].append({
            "kind": "text", "text": definition.get("name", ""),
            "points": [[float(definition.get("x1", definition.get("locX", "0"))), -float(definition.get("y1", definition.get("locY", "0")))],
                       [float(definition.get("x2", definition.get("locX", "0"))), -float(definition.get("y2", definition.get("locY", "0")))]],
            "x": float(definition.get("locX", "0")), "y": -float(definition.get("locY", "0")),
            "rotation": float(font.get("escapement", "0")) / 10 if font is not None else 0,
            "properties": {"font_size_mm": str(abs(float(font.get("height", "0")))) if font is not None else "0",
                           "font": font.get("name", "") if font is not None else "",
                           "baseline": "top",
                           "anchor": {"1": "start", "2": "middle", "3": "end"}.get(definition.get("textJustification", "0"), "start")},
        })
    for scalar in normal.findall("SymbolPinScalar"):
        pin = scalar.find("Defn")
        if pin is None or pin.get("visible", "1") == "0":
            continue
        props = {item.get("name"): item.get("val", "") for item in scalar.findall("PinUserProp/Defn")}
        x, y = float(pin.get("hotptX", "0")), -float(pin.get("hotptY", "0"))
        end_x, end_y = float(pin.get("startX", "0")), -float(pin.get("startY", "0"))
        side = "left" if x < end_x else "right" if x > end_x else "top" if y > end_y else "bottom"
        preview["pins"].append({
            "number": props.get("Pin Number", physical_numbers.get(pin.get("position", ""), pin.get("name", ""))),
            "name": pin.get("name", ""), "x": x, "y": y,
            "end_x": end_x, "end_y": end_y, "side": side,
            "length": hypot(end_x - x, end_y - y),
            "labels_in_graphics": source_labels,
        })
    values = {item.get("name"): item.get("val", "") for item in normal.findall("SymbolUserProp/Defn")}
    value = normal.find("PartValue/Defn")
    values.update({"Name": symbol_name, "Value": value.get("name", "") if value is not None else ""})
    for display in normal.findall("SymbolDisplayProp/Defn"):
        text = values.get(display.get("name"), "")
        if text:
            preview["graphics"].append({
                "kind": "text", "text": text, "points": [],
                "x": float(display.get("locX", "0")), "y": -float(display.get("locY", "0")),
            })
    return preview


def _write_source_provenance(library: LibraryIR, models: Any, reports: Path) -> Path:
    raw = library.properties.get("Source_Record_JSON", "")
    source_path = reports / "source_record.json"
    if raw:
        source_path.write_text(raw, encoding="utf-8")
    downloads = []
    for row in models.downloads:
        data = row.to_dict()
        source = Path(row.file) if row.file else None
        data["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest() if source and source.is_file() else ""
        downloads.append(data)
    payload = {
        "conversion_policy": library.properties.get("Conversion_Policy", "legacy"),
        "source_url": list(library.source_files),
        "source_record": str(source_path) if raw else "",
        "source_record_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
        "original_record_preserved": bool(raw),
        "step_geometry_modified": False,
        "step_downloads": downloads,
        "capture_precision": {key: value for key, value in library.components[0].properties.items()
                              if key.startswith("Capture_")},
        "allegro_refdes": [{"footprint": footprint.name, **source_refdes_report(footprint)}
                           for footprint in library.footprints
                           if footprint.properties.get("Conversion_Policy") == "source_exact"],
        "model_mapping": [footprint.model3d.to_dict() if footprint.model3d else None for footprint in library.footprints],
        "warnings": list(library.warnings),
    }
    path = reports / "source_provenance.json"
    write_response(path, payload)
    return path


_MPS_PRODUCTS_RPC_WARNING = "MPS ERROR: MPSC mpsCall: failed to initiate remote function call (psGetProducts), failure closing funcall message for write"


def _source_build_environment_warnings(row: dict[str, Any]) -> list[str]:
    if (row.get("status") != "created_psm" or row.get("buildStatus") != "created_dra"
            or any(type(row.get(key)) is not int or row[key] != 0
                   for key in ("exitCode", "buildExitCode", "compileExitCode"))):
        return []
    for key in ("dra", "psm"):
        path = Path(row.get(key, ""))
        if not path.is_file() or not path.stat().st_size:
            return []
    marker = f"jlc2cadence finished package symbol {row.get('footprint', '')}"
    text = str(row.get("buildStdout", ""))
    return [_MPS_PRODUCTS_RPC_WARNING] if marker in text and _MPS_PRODUCTS_RPC_WARNING in text.splitlines() else []


def _source_build_finished(row: dict[str, Any]) -> bool:
    warnings = _source_build_environment_warnings(row)
    text = "\n".join(line for key in ("buildStdout", "buildStderr", "compileStdout", "compileStderr")
                     for line in str(row.get(key, "")).splitlines() if line not in warnings)
    marker = f"jlc2cadence finished package symbol {row.get('footprint', '')}"
    return (row.get("buildStatus") == "created_dra" and bool(row.get("footprint"))
            and marker in text and not re.search(r"\*Error\*|\bERROR\s*[:(]", text))


def _source_refdes_missing(builds: list[dict[str, Any]]) -> bool:
    if not builds:
        return False
    missing = False
    for row in builds:
        if row.get("status") == "created_psm":
            continue
        if row.get("status") != "failed_psm" or not _source_build_finished(row):
            return False
        missing = True
        dra = Path(row.get("dra", ""))
        if not dra.is_file():
            return False
        log = dra.with_suffix(".log")
        if not log.is_file():
            return False
        text = log.read_text(encoding="utf-8", errors="replace")
        errors = [line for line in text.splitlines() if re.search(r"\bERROR\s*\(", line)]
        if not any("SPMHCS-1)" in line and "missing a refdes" in line.lower() for line in errors):
            return False
        if any(not any(code in line for code in ("SPMHCS-1)", "SPMHA1-291)")) for line in errors):
            return False
    return missing


def _validate_viewer_base_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("The STEP viewer URL must use an HTTP server on localhost.")
    # Accessing port also rejects malformed or out-of-range port values.
    parsed.port
    return value


def _read_step_glb(step: Path, root: Path) -> dict[str, str] | None:
    sidecar = step.with_name(f".{step.name}.glb").resolve()
    sidecar.relative_to(root)
    if not sidecar.is_file():
        return None
    if (sidecar.stat().st_mtime_ns < step.stat().st_mtime_ns
            and not is_current_step_glb(step, root, MAX_PREVIEW_GLB_BYTES)):
        return None
    if sidecar.stat().st_size > MAX_PREVIEW_GLB_BYTES:
        return None
    with sidecar.open("rb") as stream:
        data = stream.read(MAX_PREVIEW_GLB_BYTES + 1)
    if (len(data) < 12 or len(data) > MAX_PREVIEW_GLB_BYTES or data[:4] != b"glTF"
            or int.from_bytes(data[4:8], "little") != 2
            or int.from_bytes(data[8:12], "little") != len(data)):
        return None
    return {
        "format": "glb", "data_base64": base64.b64encode(data).decode("ascii"),
        "path": str(sidecar), "source_step": str(step),
    }


def _viewer_json(request: Request, timeout: float) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("CAD Viewer response exceeds 1 MiB")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("CAD Viewer response must be a JSON object")
    return result


def _generate_local_step_preview(step: Path, root: Path) -> None:
    # Isolate OCCT crashes and bound preview work independently from native imports.
    cached_error = cached_step_preview_error(step, root, MAX_PREVIEW_GLB_BYTES)
    if cached_error:
        raise ValueError(cached_error)
    executable = sys.executable
    # A pythonw parent has ``sys.stdout is None`` and cannot return the preview
    # JSON through a captured pipe.  Use its paired console interpreter for this
    # short-lived child; CREATE_NO_WINDOW still keeps it invisible on Windows.
    if os.name == "nt" and Path(executable).name.casefold() == "pythonw.exe":
        candidate = Path(executable).with_name("python.exe")
        if candidate.is_file():
            executable = str(candidate)
    command = [executable, "-m", "jlc2cadence.step_preview", "--step", str(step),
               "--root", str(root), "--max-bytes", str(MAX_PREVIEW_GLB_BYTES)]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        response = json.loads(process.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("Local STEP preview worker did not return valid JSON") from exc
    if process.returncode or not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error", "Local STEP preview failed") if isinstance(response, dict) else "Invalid preview response"
        raise ValueError(str(detail))
    if not is_current_step_glb(step, root, MAX_PREVIEW_GLB_BYTES):
        raise ValueError("Local STEP preview does not match the original STEP file")


def _add_compact_step_preview(result: dict[str, Any], root: Path, viewer_base_url: str) -> None:
    result["preview_3d"] = {}
    result["warnings"] = [message for message in result.get("warnings", [])
                          if not str(message).startswith("Compact 3D preview unavailable:")]
    if not result.get("step_files"):
        return
    try:
        root = root.resolve()
        step = Path(result["step_files"][0]).resolve(strict=True)
        relative = step.relative_to(root)
        preview = _read_step_glb(step, root)
        if not viewer_base_url:
            if not is_current_step_glb(step, root, MAX_PREVIEW_GLB_BYTES):
                _generate_local_step_preview(step, root)
            preview = _read_step_glb(step, root)
            if preview is None:
                raise ValueError("Local STEP preview worker did not produce a valid GLB")
        if preview is None and viewer_base_url:
            parsed = urlsplit(viewer_base_url)
            metadata_url = urlunsplit((parsed.scheme, parsed.netloc, "/__cad/server", "", ""))
            metadata = _viewer_json(Request(metadata_url, headers={"Accept": "application/json"}), 1)
            api = metadata.get("serverApiVersion")
            if (metadata.get("app") != "cad-viewer" or metadata.get("dynamicRoot") is not True
                    or type(api) is not int or api < 2):
                raise ValueError("A compatible CAD Viewer service is unavailable")
            parameters = {"dir": root.as_posix(), "file": relative.as_posix()}
            if step.with_name(f".{step.name}.glb").exists():
                parameters["force"] = "1"
            query = urlencode(parameters)
            artifact_url = urlunsplit((parsed.scheme, parsed.netloc, "/__cad/step-artifact", query, ""))
            converted = _viewer_json(Request(artifact_url, method="POST", headers={"Accept": "application/json"}), 45)
            if converted.get("ok") is not True:
                raise ValueError(str(converted.get("error") or "CAD Viewer STEP conversion failed"))
            artifact = converted.get("result")
            if not isinstance(artifact, dict):
                raise ValueError("CAD Viewer did not return a STEP artifact")
            for key, expected in (("stepPath", step), ("glbPath", step.with_name(f".{step.name}.glb"))):
                if key in artifact and Path(artifact[key]).resolve() != expected:
                    raise ValueError("CAD Viewer returned an artifact for another STEP file")
            preview = _read_step_glb(step, root)
            if preview is None:
                raise ValueError("CAD Viewer did not produce a current GLB preview within 6 MiB")
        if preview is not None:
            result["preview_3d"] = preview
    except (OSError, ValueError, TypeError, KeyError, HTTPException, subprocess.SubprocessError) as exc:
        if viewer_base_url and isinstance(exc, (OSError, HTTPException)) and "relative" in locals():
            try:
                _generate_local_step_preview(step, root)
                preview = _read_step_glb(step, root)
                if preview is None:
                    raise ValueError("Local STEP preview worker did not produce a valid GLB")
                result["preview_3d"] = preview
                result["preview_renderer"] = "local_fallback"
                return
            except (OSError, ValueError, subprocess.SubprocessError) as fallback:
                exc = ValueError(f"{exc}; local fallback: {fallback}")
        warning = f"Compact 3D preview unavailable: {exc}"
        warnings = result.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)


def _enrich_asset_previews(result: dict[str, Any], root: Path, build: Path, viewer_base_url: str, *, load_3d: bool = True) -> None:
    ir_path = build / "reports" / "library.ir.json"
    library = json.loads(ir_path.read_text(encoding="utf-8"))
    footprints = library.get("footprints", [])
    footprint = next((item for item in footprints if item.get("name") == result["footprint_name"]), {})
    result["preview_data"] = {
        "symbol": _capture_symbol_preview(Path(result["capture_xml"]), result["symbol_name"]),
        "footprint": {
            **{key: footprint.get(key, default) for key, default in (
                ("name", ""), ("units", "mm"), ("origin", [0, 0]), ("pads", []), ("graphics", []),
            )},
            "source_path": str(ir_path.resolve()),
        },
    }
    candidates = [Path(item) for item in result.get("published_files", [])]
    candidates.extend(sorted((build / "3d").glob("*")))
    step_files: dict[str, str] = {}
    for path in candidates:
        if path.suffix.lower() in {".step", ".stp"} and path.is_file():
            step_files.setdefault(path.name.casefold(), str(path.resolve()))
    result["step_files"] = list(step_files.values())
    drawings = [Path(item) for item in result.get("published_files", [])]
    drawings.extend(sorted((build / "allegro" / "symbols").glob("*")))
    result["footprint_path"] = next((
        str(path.resolve()) for path in drawings if path.suffix.lower() == ".dra" and path.is_file()
        and path.stem.casefold() == result["footprint_name"].casefold()
    ), "")
    result["step_viewer_url"] = ""
    result["preview_3d"] = {}
    if load_3d:
        _add_compact_step_preview(result, root, viewer_base_url)
    if viewer_base_url and result["step_files"]:
        try:
            relative = Path(result["step_files"][0]).relative_to(root)
        except ValueError:
            return
        parsed = urlsplit(viewer_base_url)
        query = [(key, value) for key, value in parse_qsl(parsed.query) if key not in {"dir", "file"}]
        query.extend((("dir", root.as_posix()), ("file", relative.as_posix())))
        result["step_viewer_url"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), ""))


def import_part(
    part_id: str,
    root: Path,
    *,
    cadence_root: Path | None = None,
    timeout: float = 20.0,
    prepare_only: bool = False,
    viewer_base_url: str = "",
    defer_step: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    if defer_step and not prepare_only:
        raise ValueError("STEP deferral is only supported for previews; imports always fetch original STEP data.")
    part_id = _part_id(part_id)
    viewer_base_url = _validate_viewer_base_url(viewer_base_url)
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 64 * 1024 * 1024:
        raise OSError("元件库所在磁盘空间不足（需要至少 64 MiB 可用空间），请清理磁盘或更换元件库目录后重试。")
    with _part_lock(root, part_id):
        library = _catalog(root, timeout).fetch_part(part_id)
        timings = {"source_cad_seconds": round(time.perf_counter() - started, 3)}
        if len(library.components) != 1 or len(library.footprints) != 1:
            raise ValueError("A catalog part must contain exactly one symbol and one footprint.")
        namespace_part(library, part_id)
        validation = validate_library(library)
        if validation.errors:
            raise ValueError("Invalid component geometry: " + "; ".join(str(item) for item in validation.errors))
        target_root = str(cadence_root.expanduser().resolve()) if cadence_root else "auto"
        fingerprint = hashlib.sha256(
            (f"{__version__}:{BRIDGE_BUILD_REVISION}:{target_root.casefold()}:" + json.dumps(library.to_dict(), sort_keys=True)).encode("utf-8")
        ).hexdigest()[:16]
        # Content-addressed builds preserve already-loaded libraries and earlier imports.
        # Keep deferred 2D output separate from builds with real STEP bindings.
        # Their model maps must never overwrite each other.
        build = root / "parts" / part_id / (fingerprint + ("_2d" if defer_step else ""))
        build.mkdir(parents=True, exist_ok=True)
        reports = build / "reports"
        reports.mkdir(exist_ok=True)
        manifest_path = build / "capture_browser.json"
        preview_manifest = build / "preview_browser.json"
        cache_warnings = []
        if prepare_only and preview_manifest.is_file():
            existing, recovery_warnings = _load_asset_cache(
                preview_manifest, root, build, part_id, target_root, prepared=True,
            )
            cache_warnings.extend(recovery_warnings)
            if existing:
                fresh = defer_step or existing.get("step_status") == "available" or time.time() - preview_manifest.stat().st_mtime < 300
                if existing.get("ok") and fresh:
                    existing["cached"] = True
                    # Revalidate external files and current viewer settings; an
                    # embedded mesh from a previous response is not proof that
                    # its original STEP or the viewer configuration is current.
                    _enrich_asset_previews(existing, root, build, viewer_base_url, load_3d=not defer_step)
                    existing["timings"] = {**timings, "total_seconds": round(time.perf_counter() - started, 3)}
                    return existing
        # Preview requests use ``--prepare-only`` but may target a component that
        # was already fully imported.  Reuse the content-addressed manifest in
        # that case; rebuilding the same symbol/footprint on every row click was
        # the main source of the multi-second Capture delay.
        if manifest_path.exists():
            existing, recovery_warnings = _load_asset_cache(
                manifest_path, root, build, part_id, target_root, prepared=False,
            )
            cache_warnings.extend(recovery_warnings)
            if (existing and existing.get("ok") and existing.get("asset_status") == "complete"
                    and existing.get("step_status") == "available" and existing["published_files"]):
                existing["cached"] = True
                existing["warnings"] = list(dict.fromkeys([*existing["warnings"], *cache_warnings]))
                _enrich_asset_previews(existing, root, build, viewer_base_url)
                existing["timings"] = {**timings, "total_seconds": round(time.perf_counter() - started, 3)}
                return existing

        warnings = [str(item) for item in validation.warnings if "has no padstack name" not in str(item)
                    and "has no linked 3D model" not in str(item)]
        warnings = list(dict.fromkeys([*library.warnings, *warnings, *cache_warnings]))
        model_started = time.perf_counter()
        if defer_step:
            models = EasyEDA3DFetchReport(output_dir=str(build / "3d"), formats=["step"])
        else:
            print(f"Downloading models for {part_id}", flush=True)
            models = fetch_easyeda_3d_models(
                library, build / "3d", formats=("step",),
                cache_dir=root / "cache" / "3d", timeout=timeout,
            )
        timings["step_download_seconds"] = round(time.perf_counter() - model_started, 3)
        write_easyeda_3d_fetch_report(models, reports / "3d_fetch.json")
        attach_models(library, models.models)
        for footprint in library.footprints:
            if footprint.model3d and "Catalog_3D_Offset_mm" in footprint.properties:
                footprint.model3d = replace(
                    footprint.model3d,
                    offset=tuple(json.loads(footprint.properties["Catalog_3D_Offset_mm"])),
                    rotation=tuple(json.loads(footprint.properties["Catalog_3D_Rotation_deg"])),
                    transform_source="easyeda_standard",
                )
            elif footprint.model3d and footprint.properties.get("Conversion_Policy") == "source_exact":
                footprint.model3d = replace(footprint.model3d, transform_source="missing_original")
        step_status = "deferred" if defer_step else "available" if library.footprints[0].model3d else "missing"
        if step_status == "missing":
            warnings.append("No downloadable STEP model is available for this part.")
        mapping_ready = bool(library.footprints[0].model3d and
                             library.footprints[0].model3d.transform_source != "missing_original")
        if step_status == "available" and not mapping_ready:
            warnings.append("Original STEP placement transform is unavailable or unverified; STEP is downloaded without an inferred Allegro binding.")

        scripts = build / "scripts"
        write_capture_outputs(library, build / "capture", scripts)
        write_allegro_outputs(library, build / "allegro", scripts)
        write_model_map(library, models.models, build / "3d", scripts)
        write_svg_previews(library, reports)
        warnings[:] = list(dict.fromkeys([*warnings, *library.warnings]))
        provenance = _write_source_provenance(library, models, reports)
        write_response(reports / "library.ir.json", library.to_dict())
        plan = json.loads((build / "capture" / "capture_build_plan.json").read_text(encoding="utf-8"))
        component = plan["components"][0]
        result: dict[str, Any] = {
            "ok": True,
            "part_id": part_id,
            "lcsc_id": part_id,
            "version": __version__,
            "build_revision": BRIDGE_BUILD_REVISION,
            "cadence_target": target_root,
            "conversion_policy": library.properties.get("Conversion_Policy", "legacy"),
            "allegro_refdes": source_refdes_report(library.footprints[0])
            if library.properties.get("Conversion_Policy") == "source_exact" else {},
            "source_provenance": str(provenance),
            "olb_path": str(build / "capture" / "library.olb"),
            "capture_xml": str(build / "capture" / "library.olb.xml"),
            "symbol_name": component["capture_internal_name"],
            "footprint_name": library.footprints[0].name,
            "library_root": str(root),
            "build_dir": str(build),
            "step_status": step_status,
            "step_mapping_status": "deferred" if defer_step else "ready" if mapping_ready else "missing_source_transform" if step_status == "available" else "missing_step",
            "asset_status": "prepared" if prepare_only else "pending",
            "allegro_status": "not_built",
            "warnings": warnings,
            "cached": False,
            "prepared_only": prepare_only,
            "published_files": [],
            "previews": {
                "symbol": str(reports / "symbol_preview.svg"),
                "footprint": str(reports / "footprint_preview.svg"),
            },
        }
        if prepare_only:
            _enrich_asset_previews(result, root, build, viewer_base_url, load_3d=not defer_step)
            result["preview_step_sha256"] = {path: _file_sha256(Path(path))
                                             for path in result.get("step_files", [])}
            result["generated_file_sha256"] = {str(path): _file_sha256(path) for path in _cache_asset_paths(result, root, build)}
            result["timings"] = {**timings, "total_seconds": round(time.perf_counter() - started, 3)}
            write_response(preview_manifest, result)
            return result

        tools = CadenceTools.discover(cadence_root)
        if not tools.allegro or not tools.allegro_batch:
            raise RuntimeError("Allegro tools were not found. Configure the Cadence installation directory.")
        print(f"Building Allegro package {result['footprint_name']}", flush=True)
        builds = run_symbol_build_plan(tools, build / "allegro" / "symbols")
        environment_warnings = list(dict.fromkeys(warning for row in builds
                                                  for warning in _source_build_environment_warnings(row)))
        result["native_environment_warnings"] = environment_warnings
        for warning in environment_warnings:
            warnings.append("Native build completed with verified nonempty DRA/PSM and zero exit codes, but reported an MPS RPC environment warning; native geometry readback is still required: " + warning)
        if library.properties.get("Conversion_Policy") == "source_exact" and (
                not builds or any(not _source_build_finished(row) for row in builds)):
            geometry_error = source_native_geometry_error(
                builds, build / "allegro" / "symbols" / "create_symbols.results.json",
            )
            if geometry_error is not None:
                raise geometry_error
            raise RuntimeError(f"Incomplete native source geometry build; inspect {build / 'allegro' / 'symbols' / 'create_symbols.results.json'}")
        incomplete_refdes = (library.properties.get("Conversion_Policy") == "source_exact"
                             and _source_refdes_missing(builds))
        if not incomplete_refdes and (not builds or any(row.get("status") != "created_psm" for row in builds)):
            raise RuntimeError(f"Allegro footprint generation failed; inspect {build / 'allegro' / 'symbols' / 'create_symbols.results.json'}")
        result["allegro_status"] = "missing_source_refdes" if incomplete_refdes else "created_psm"
        result["asset_status"] = "incomplete" if incomplete_refdes or step_status == "missing" or not mapping_ready else "complete"
        if incomplete_refdes:
            warnings.insert(0, "Allegro PSM unavailable: the footprint has no native RefDes required by Cadence (SPMHCS-1), and no supported source label or verified component prefix could supply it. DRA/PAD/STEP are retained. See allegro_refdes for the missing or unsupported data. The package is incomplete for PCB placement.")
        if step_status == "available" and mapping_ready:
            print("Applying STEP mapping", flush=True)
            mapping = run_step_mapping(tools, build)
            if mapping.get("status") != "mapped":
                result["step_status"] = "mapping_failed"
                result["step_mapping_status"] = "failed"
                result["asset_status"] = "incomplete"
                warnings.append(f"STEP downloaded, but Allegro mapping failed; inspect {build / '3d' / 'apply_3d_mapping.results.json'}")
            elif not incomplete_refdes:
                result["step_mapping_status"] = "mapped"
                # STEP data was added to DRA after the first PSM compilation.
                compiled = compile_existing_package_symbols(tools, build / "allegro" / "symbols")
                if not compiled or any(row.get("status") != "created_psm" for row in compiled):
                    raise RuntimeError("Failed to compile the STEP-mapped footprint into its package symbol.")
            else:
                result["step_mapping_status"] = "mapped_dra_only"

        published = []
        for source_dir, target_dir, suffixes in (
            (build / "allegro" / "symbols", root / "allegro" / "symbols", {".dra", ".psm"}),
            (build / "allegro" / "padstacks", root / "allegro" / "padstacks", {".pad"}),
            (build / "3d", root / "3d", {".step", ".stp"}),
        ):
            for source in source_dir.iterdir():
                if source.is_file() and source.suffix.lower() in suffixes:
                    target = target_dir / source.name
                    _publish_file(source, target)
                    published.append(str(target))
        result["published_files"] = published
        provenance_data = json.loads(provenance.read_text(encoding="utf-8"))
        provenance_data.update({
            "warnings": warnings, "asset_status": result["asset_status"],
            "allegro_status": result["allegro_status"], "step_mapping_status": result["step_mapping_status"],
            "native_build_results": str(build / "allegro" / "symbols" / "create_symbols.results.json"),
            "native_environment_warnings": environment_warnings,
            "published_file_sha256": {path: _file_sha256(Path(path)) for path in published},
        })
        write_response(provenance, provenance_data)
        _enrich_asset_previews(result, root, build, viewer_base_url)
        result["generated_file_sha256"] = {str(path): _file_sha256(path) for path in _cache_asset_paths(result, root, build)}
        write_allegro_paths(root)
        result["timings"] = {**timings, "total_seconds": round(time.perf_counter() - started, 3)}
        write_response(manifest_path, result)
        write_response(root / "parts" / part_id / "latest.json", result)
        return result


def _register_library_paths(result: dict[str, Any], root: Path, cadence_root: Path | None) -> None:
    from .library_paths import register_allegro_paths

    try:
        result["path_registration"] = register_allegro_paths(root, cadence_root)
        status = result["path_registration"].get("status")
        if status in {"environment_unavailable", "missing_assets"}:
            message = ("无法找到 Allegro 用户配置目录，请在 PCB Editor 中检查元件库搜索路径。"
                       if status == "environment_unavailable" else "封装库文件不完整，未自动登记搜索路径，请查看导入结果中的缺失资源。")
            result.setdefault("warnings", []).append(message)
    except (OSError, ValueError, RuntimeError) as exc:
        result.setdefault("warnings", []).append(f"封装库已生成，但自动登记搜索路径失败：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("search", "import", "register-paths"):
        item = sub.add_parser(command)
        item.add_argument("--library-root", required=True, type=Path)
        item.add_argument("--output", required=True, type=Path)
        item.add_argument("--timeout", type=float, default=20.0)
        if command == "search":
            item.add_argument("--query", required=True)
            item.add_argument("--page", type=int, default=1)
            item.add_argument("--limit", type=int, default=30)
        elif command == "import":
            item.add_argument("--part", required=True)
            item.add_argument("--cadence-root", type=Path)
            item.add_argument("--prepare-only", action="store_true")
            item.add_argument("--defer-step", action="store_true")
            item.add_argument("--viewer-base-url", default="")
        else:
            item.add_argument("--cadence-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_worker_stdio()
    args = build_parser().parse_args(argv)
    try:
        if not 0 < args.timeout <= 120:
            raise ValueError("Timeout must be between 0 and 120 seconds.")
        if args.command == "search":
            result = search_parts(args.query, args.library_root.resolve(), args.timeout, page=args.page, limit=args.limit)
        elif args.command == "register-paths":
            from .library_paths import register_allegro_paths
            result = {"ok": True, "path_registration": register_allegro_paths(args.library_root, args.cadence_root)}
        else:
            result = import_part(
                args.part, args.library_root, cadence_root=args.cadence_root,
                timeout=args.timeout, prepare_only=args.prepare_only,
                viewer_base_url=args.viewer_base_url,
                defer_step=args.defer_step,
            )
            if not args.prepare_only:
                _register_library_paths(result, args.library_root, args.cadence_root)
    except Exception as exc:
        result = failure_response(exc)
    write_response(args.output, result)
    if not result["ok"]:
        print(result["error"], file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
