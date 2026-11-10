from __future__ import annotations

import gzip
import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.normalize import model_id_candidates
from ..ir.schema import Footprint, LibraryIR, Model3D
from .easyeda_3d import _normalize_formats, _valid_model_payload


STEP_MARKER = b"ISO-10303-21;"
STEP_END_MARKER = b"END-ISO-10303-21;"
GZIP_MAGIC = b"\x1f\x8b"
MODEL_ID_RE = re.compile(r"[A-Fa-f0-9]{32}(?:\|[A-Fa-f0-9]{32})?")


@dataclass
class EasyEDACacheRoot:
    path: str
    exists: bool
    file_count: int = 0
    bytes_scanned: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "file_count": self.file_count,
            "bytes_scanned": self.bytes_scanned,
            "error": self.error,
        }


@dataclass
class EasyEDACachePayload:
    source_file: str
    output_file: str
    format: str
    key: str
    matched_model_id: str = ""
    matched_footprints: list[str] = field(default_factory=list)
    bytes: int = 0
    source_kind: str = "file"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "output_file": self.output_file,
            "format": self.format,
            "key": self.key,
            "matched_model_id": self.matched_model_id,
            "matched_footprints": list(self.matched_footprints),
            "bytes": self.bytes,
            "source_kind": self.source_kind,
        }


@dataclass
class EasyEDACacheReference:
    source_file: str
    model_id: str
    model_title: str = ""
    footprints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "model_id": self.model_id,
            "model_title": self.model_title,
            "footprints": list(self.footprints),
        }


@dataclass
class EasyEDACacheImportReport:
    output_dir: str
    formats: list[str]
    roots: list[EasyEDACacheRoot] = field(default_factory=list)
    requested_model_ids: list[str] = field(default_factory=list)
    payloads: list[EasyEDACachePayload] = field(default_factory=list)
    references: list[EasyEDACacheReference] = field(default_factory=list)
    models: list[Model3D] = field(default_factory=list)

    @property
    def matched_model_ids(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for payload in self.payloads:
            if payload.matched_model_id and payload.matched_model_id not in seen:
                seen.add(payload.matched_model_id)
                result.append(payload.matched_model_id)
        return result

    @property
    def missing_model_ids(self) -> list[str]:
        matched = set(self.matched_model_ids)
        return [model_id for model_id in self.requested_model_ids if model_id not in matched]

    @property
    def status(self) -> str:
        if not self.roots:
            return "no_cache_roots"
        if not any(root.exists for root in self.roots):
            return "no_existing_cache_roots"
        if not self.requested_model_ids:
            return "found_payloads" if self.payloads else "no_model_ids"
        if not self.payloads:
            return "no_model_payloads"
        if not self.missing_model_ids:
            return "complete"
        return "partial"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "output_dir": self.output_dir,
            "formats": list(self.formats),
            "roots": [root.to_dict() for root in self.roots],
            "requested_model_ids": list(self.requested_model_ids),
            "matched_model_ids": self.matched_model_ids,
            "missing_model_ids": self.missing_model_ids,
            "payload_count": len(self.payloads),
            "model_count": len(self.models),
            "reference_count": len(self.references),
            "payloads": [payload.to_dict() for payload in self.payloads],
            "references": [reference.to_dict() for reference in self.references],
        }


def default_easyeda_cache_roots() -> list[Path]:
    home = Path.home()
    return [
        home / "AppData" / "Local" / "LCEDA-Pro",
        home / "AppData" / "Roaming" / "LCEDA",
    ]


def import_easyeda_cached_3d_models(
    library: LibraryIR | None,
    cache_roots: Iterable[Path],
    output_dir: Path,
    formats: Iterable[str] = ("step",),
) -> EasyEDACacheImportReport:
    normalized_formats = _normalize_formats(formats)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = _requested_model_rows(library)
    requested_ids = [row["model_id"] for row in requested]
    requested_by_key = _requested_by_key(requested)
    report = EasyEDACacheImportReport(
        output_dir=str(output_dir),
        formats=normalized_formats,
        requested_model_ids=requested_ids,
    )
    seen_payloads: set[tuple[str, str]] = set()
    seen_references: set[tuple[str, str]] = set()

    for root_input in cache_roots:
        root = root_input.expanduser().resolve()
        root_row = EasyEDACacheRoot(path=str(root), exists=root.exists())
        report.roots.append(root_row)
        if not root.exists():
            continue
        try:
            files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        except OSError as exc:
            root_row.error = str(exc)
            continue
        root_row.file_count = len(files)
        for source in files:
            try:
                raw = source.read_bytes()
            except OSError:
                continue
            root_row.bytes_scanned += len(raw)
            payloads = _extract_payloads_from_file(source, raw, normalized_formats)
            references = _extract_references_from_text(source, raw, requested_by_key)
            for reference in references:
                ref_key = (reference.source_file, reference.model_id)
                if ref_key not in seen_references:
                    seen_references.add(ref_key)
                    report.references.append(reference)
            for model_format, payload, source_kind in payloads:
                keys = _payload_match_keys(source, raw, payload, requested_by_key)
                if not keys:
                    keys = [
                        {
                            "model_id": "",
                            "key": normalized_key(source.stem),
                            "footprints": [],
                        }
                    ]
                for key_row in keys:
                    key = str(key_row["key"])
                    if not key:
                        continue
                    cache_target_id = _cache_target_id(str(key_row["model_id"] or ""))
                    target_name = cadence_safe_name(
                        str(cache_target_id or key or source.stem),
                        "model",
                    )
                    target = output_dir / f"{target_name}.{model_format}"
                    dedupe_key = (str(target), key)
                    if dedupe_key in seen_payloads:
                        continue
                    seen_payloads.add(dedupe_key)
                    if not target.exists() or target.read_bytes() != payload:
                        target.write_bytes(payload)
                    payload_row = EasyEDACachePayload(
                        source_file=str(source),
                        output_file=str(target),
                        format=model_format,
                        key=key,
                        matched_model_id=str(key_row["model_id"] or ""),
                        matched_footprints=list(key_row["footprints"]),
                        bytes=len(payload),
                        source_kind=source_kind,
                    )
                    report.payloads.append(payload_row)
                    report.models.append(
                        Model3D(
                            file=str(target),
                            source_file=str(source),
                            format=model_format,
                            key=key,
                        )
                    )

    return report


def write_easyeda_cache_import_report(
    report: EasyEDACacheImportReport,
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _requested_model_rows(library: LibraryIR | None) -> list[dict[str, object]]:
    if library is None:
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for footprint in library.footprints:
        model_id = _footprint_model_id(footprint)
        if not model_id:
            continue
        if model_id not in seen:
            rows.append(
                {
                    "model_id": model_id,
                    "title": str(footprint.properties.get("EasyEDA_3D_Model_Title", "") or ""),
                    "footprints": [footprint.name],
                }
            )
            seen.add(model_id)
        else:
            for row in rows:
                if row["model_id"] == model_id:
                    footprints = row["footprints"]
                    assert isinstance(footprints, list)
                    footprints.append(footprint.name)
                    break
    return rows


def _footprint_model_id(footprint: Footprint) -> str:
    for key in (
        "EasyEDA_3D_Model_Id",
        "EasyEDA_3D_Model_ID",
        "3D_Model",
        "3D Model",
        "Model3D",
    ):
        value = str(footprint.properties.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _requested_by_key(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        model_id = str(row.get("model_id", "") or "")
        values = model_id_candidates(model_id)
        title = str(row.get("title", "") or "")
        if title:
            values.append(title)
        for value in values:
            key = normalized_key(value)
            if key:
                by_key[key] = row
    return by_key


def _extract_payloads_from_file(
    source: Path,
    raw: bytes,
    formats: list[str],
) -> list[tuple[str, bytes, str]]:
    payloads: list[tuple[str, bytes, str]] = []
    candidate_blobs = [(raw, "file")]
    candidate_blobs.extend(_embedded_gzip_blobs(raw))
    if _looks_like_zip(raw):
        payloads.extend(_extract_payloads_from_zip(source, formats))
    else:
        payloads.extend(_extract_payloads_from_embedded_zips(raw, formats))
    for blob, source_kind in candidate_blobs:
        if "step" in formats:
            step = _extract_step_payload(blob)
            if step is not None:
                payloads.append(("step", step, source_kind))
        if "obj" in formats:
            obj = _extract_obj_payload(blob)
            if obj is not None:
                payloads.append(("obj", obj, source_kind))
    return _dedupe_payloads(payloads)


def _embedded_gzip_blobs(raw: bytes) -> list[tuple[bytes, str]]:
    """Return valid gzip members found at the file start or inside a cache record."""
    blobs: list[tuple[bytes, str]] = []
    seen: set[bytes] = set()
    offsets: list[int] = [0] if raw.startswith(GZIP_MAGIC) else []
    cursor = 0
    while True:
        offset = raw.find(GZIP_MAGIC, cursor)
        if offset < 0:
            break
        if offset not in offsets:
            offsets.append(offset)
        cursor = offset + 1
    for offset in offsets:
        decompressed = _try_gzip(raw[offset:])
        if decompressed is None or decompressed in seen:
            continue
        seen.add(decompressed)
        source_kind = "gzip" if offset == 0 else "gzip_embedded"
        blobs.append((decompressed, source_kind))
    return blobs


def _extract_payloads_from_embedded_zips(
    raw: bytes,
    formats: list[str],
) -> list[tuple[str, bytes, str]]:
    payloads: list[tuple[str, bytes, str]] = []
    cursor = 0
    seen_offsets: set[int] = set()
    while True:
        offset = raw.find(b"PK\x03\x04", cursor)
        if offset < 0:
            break
        cursor = offset + 1
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        try:
            with zipfile.ZipFile(io.BytesIO(raw[offset:])) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    suffix = Path(member.filename).suffix.lower().lstrip(".")
                    if suffix == "stp":
                        suffix = "step"
                    if suffix not in formats:
                        continue
                    try:
                        payload = archive.read(member)
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        continue
                    if _valid_model_payload(payload, suffix):
                        payloads.append(
                            (
                                suffix,
                                payload,
                                f"zip_embedded:{member.filename}",
                            )
                        )
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return payloads


def _extract_payloads_from_zip(
    source: Path,
    formats: list[str],
) -> list[tuple[str, bytes, str]]:
    payloads: list[tuple[str, bytes, str]] = []
    if not zipfile.is_zipfile(source):
        return payloads
    try:
        with zipfile.ZipFile(source) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                suffix = Path(member.filename).suffix.lower().lstrip(".")
                if suffix == "stp":
                    suffix = "step"
                if suffix not in formats:
                    continue
                try:
                    payload = zf.read(member)
                except (OSError, zipfile.BadZipFile):
                    continue
                if _valid_model_payload(payload, suffix):
                    payloads.append((suffix, payload, f"zip:{member.filename}"))
    except (OSError, zipfile.BadZipFile):
        return []
    return payloads


def _extract_step_payload(raw: bytes) -> bytes | None:
    start = raw.find(STEP_MARKER)
    if start < 0:
        return None
    end = raw.find(STEP_END_MARKER, start)
    if end >= 0:
        end += len(STEP_END_MARKER)
        while end < len(raw) and raw[end] in b"\r\n \t":
            end += 1
    else:
        end = len(raw)
    payload = raw[start:end]
    return payload if _valid_model_payload(payload, "step") else None


def _extract_obj_payload(raw: bytes) -> bytes | None:
    text = raw.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    start_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("mtllib ", "o ", "g ", "v ")):
            start_index = index
            break
    if start_index is None:
        return None
    candidate = "\n".join(lines[start_index:]).encode("utf-8")
    if not _looks_like_obj_payload(candidate):
        return None
    return candidate


def _looks_like_obj_payload(payload: bytes) -> bool:
    if not _valid_model_payload(payload, "obj"):
        return False
    vertex_count = 0
    face_count = 0
    for line in payload.splitlines()[:5000]:
        stripped = line.lstrip()
        if stripped.startswith(b"v "):
            vertex_count += 1
        elif stripped.startswith(b"f "):
            face_count += 1
        if vertex_count >= 3 and face_count >= 1:
            return True
    return False


def _payload_match_keys(
    source: Path,
    raw: bytes,
    payload: bytes,
    requested_by_key: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    text = _searchable_text(source, raw, payload)
    matches: list[dict[str, object]] = []
    seen_model_ids: set[str] = set()
    for row in _unique_requested_rows(requested_by_key):
        model_id = str(row.get("model_id", "") or "")
        if not model_id or model_id in seen_model_ids:
            continue
        values = [str(row.get("model_id", "") or ""), str(row.get("title", "") or "")]
        values.extend(model_id_candidates(values[0]))
        if any(value and value in text for value in values):
            seen_model_ids.add(model_id)
            key = normalized_key(_cache_target_id(model_id))
            matches.append(
                {
                    "model_id": row.get("model_id", ""),
                    "key": key,
                    "footprints": list(row.get("footprints", [])),
                }
            )
    return matches


def _unique_requested_rows(
    requested_by_key: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in requested_by_key.values():
        model_id = str(row.get("model_id", "") or "")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        rows.append(row)
    return rows


def _cache_target_id(model_id: str) -> str:
    candidates = model_id_candidates(model_id)
    if "|" in str(model_id or "") and len(candidates) > 1:
        return candidates[1]
    return candidates[0] if candidates else ""


def _extract_references_from_text(
    source: Path,
    raw: bytes,
    requested_by_key: dict[str, dict[str, object]],
) -> list[EasyEDACacheReference]:
    text = _decode_for_search(raw)
    references: list[EasyEDACacheReference] = []
    seen_ids: set[str] = set()
    for match in MODEL_ID_RE.finditer(text):
        model_id = match.group(0)
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        row = _requested_row_for_model_id(model_id, requested_by_key)
        if requested_by_key and not row:
            continue
        references.append(
            EasyEDACacheReference(
                source_file=str(source),
                model_id=model_id,
                model_title=str(row.get("title", "") or ""),
                footprints=list(row.get("footprints", [])),
            )
        )
    return references


def _requested_row_for_model_id(
    model_id: str,
    requested_by_key: dict[str, dict[str, object]],
) -> dict[str, object]:
    for candidate in model_id_candidates(model_id):
        key = normalized_key(candidate)
        if key in requested_by_key:
            return requested_by_key[key]
    return {}


def _searchable_text(source: Path, raw: bytes, payload: bytes) -> str:
    values = [str(source), _decode_for_search(raw), _decode_for_search(payload)]
    return "\n".join(values)


def _decode_for_search(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="ignore")
    if len(text) < 4 and b"\x00" in raw[:64]:
        text = raw.decode("utf-16le", errors="ignore")
    return text


def _try_gzip(raw: bytes) -> bytes | None:
    if not raw.startswith(GZIP_MAGIC):
        return None
    try:
        return gzip.decompress(raw)
    except OSError:
        try:
            # Chromium cache records can append metadata after a gzip member.
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        except zlib.error:
            return None


def _looks_like_zip(raw: bytes) -> bool:
    return raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))


def _dedupe_payloads(payloads: list[tuple[str, bytes, str]]) -> list[tuple[str, bytes, str]]:
    seen: set[tuple[str, bytes]] = set()
    result: list[tuple[str, bytes, str]] = []
    for model_format, payload, source_kind in payloads:
        key = (model_format, payload)
        if key in seen:
            continue
        seen.add(key)
        result.append((model_format, payload, source_kind))
    return result
