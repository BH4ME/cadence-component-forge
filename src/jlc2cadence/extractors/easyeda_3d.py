from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.normalize import model_id_candidates
from ..ir.schema import Component, Footprint, LibraryIR, Model3D


EASYEDA_STEP_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{model_id}"
EASYEDA_OBJ_URL = "https://modules.easyeda.com/3dmodel/{model_id}"
SUPPORTED_FORMATS = {"step", "obj"}
SUCCESS_STATUSES = {"downloaded", "cached", "existing"}
DEFAULT_MODEL_TEMPLATES = {
    "step": [EASYEDA_STEP_URL],
    "obj": [EASYEDA_OBJ_URL],
}
MODEL_SOURCE_KEYS = ("step", "obj")


@dataclass
class EasyEDA3DDownload:
    footprint_names: list[str]
    model_id: str
    model_title: str
    format: str
    url: str
    request_model_id: str = ""
    attempted_model_ids: list[str] = field(default_factory=list)
    attempted_urls: list[str] = field(default_factory=list)
    source: str = ""
    file: str = ""
    status: str = ""
    bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "footprints": list(self.footprint_names),
            "model_id": self.model_id,
            "model_title": self.model_title,
            "format": self.format,
            "url": self.url,
            "request_model_id": self.request_model_id,
            "attempted_model_ids": list(self.attempted_model_ids),
            "attempted_urls": list(self.attempted_urls),
            "source": self.source,
            "file": self.file,
            "status": self.status,
            "bytes": self.bytes,
            "error": self.error,
        }


@dataclass
class EasyEDA3DFetchReport:
    output_dir: str
    formats: list[str]
    model_sources: dict[str, Any] = field(default_factory=dict)
    downloads: list[EasyEDA3DDownload] = field(default_factory=list)
    models: list[Model3D] = field(default_factory=list)

    @property
    def errors(self) -> list[EasyEDA3DDownload]:
        return [row for row in self.downloads if row.status not in SUCCESS_STATUSES]

    @property
    def status(self) -> str:
        if not self.downloads:
            return "no_model_ids"
        if not self.errors:
            return "complete"
        if any(row.status in SUCCESS_STATUSES for row in self.downloads):
            return "partial"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        status_counts: dict[str, int] = {}
        for row in self.downloads:
            status_counts[row.status] = status_counts.get(row.status, 0) + 1
        return {
            "status": self.status,
            "output_dir": self.output_dir,
            "formats": list(self.formats),
            "model_sources": dict(self.model_sources),
            "download_count": len(self.downloads),
            "model_count": len(self.models),
            "error_count": len(self.errors),
            "status_counts": status_counts,
            "downloads": [row.to_dict() for row in self.downloads],
        }


UrlOpen = Callable[..., object]


@dataclass
class ModelSourceSet:
    """Configurable URL sources used by the EasyEDA 3D downloader."""

    path: str = ""
    include_defaults: bool = True
    templates: dict[str, list[str]] = field(default_factory=dict)
    models: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "include_defaults": self.include_defaults,
            "template_count": sum(len(values) for values in self.templates.values()),
            "model_override_count": len(self.models),
        }

    def urls_for(
        self,
        model_id: str,
        model_title: str,
        footprints: list[Footprint],
        model_format: str,
        request_model_id: str | None = None,
        components: list[Component] | None = None,
    ) -> list[tuple[str, str]]:
        request_model_id = request_model_id or model_id
        values = _template_values(
            request_model_id,
            model_title,
            footprints,
            model_format,
            source_model_id=model_id,
            components=components,
        )
        templates: list[tuple[str, str]] = []
        override_urls: list[str] = []
        lookup_keys = _source_lookup_keys(model_id, model_title, footprints, components=components)
        lookup_keys.extend(_source_lookup_keys(request_model_id, "", [], components=components))
        for key in lookup_keys:
            override_urls.extend(self.models.get(key, {}).get(model_format, []))
        templates = [("custom_model", template) for template in override_urls] + templates
        templates.extend(
            ("custom_template", template)
            for template in self.templates.get(model_format, [])
        )
        if self.include_defaults:
            templates.extend(("official", template) for template in DEFAULT_MODEL_TEMPLATES[model_format])
        urls: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source, template in templates:
            try:
                url = template.format(**values)
            except KeyError as exc:
                raise ValueError(
                    f"Unknown model source placeholder {exc.args[0]!r} in {template!r}"
                ) from exc
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append((source, url))
        return urls


def load_model_sources(path: Path | None) -> ModelSourceSet:
    if path is None:
        return ModelSourceSet()
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Model sources file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Model sources file root must be a JSON object.")
    source_set = ModelSourceSet(
        path=str(path),
        include_defaults=bool(data.get("include_defaults", True)),
    )
    source_set.templates = _parse_source_templates(data.get("templates", data))
    source_set.models = _parse_model_overrides(data.get("models", data.get("overrides", {})))
    return source_set


def fetch_easyeda_3d_models(
    library: LibraryIR,
    output_dir: Path,
    formats: Iterable[str] = ("step",),
    cache_dir: Path | None = None,
    timeout: float = 30.0,
    urlopen: UrlOpen | None = None,
    model_sources: ModelSourceSet | None = None,
) -> EasyEDA3DFetchReport:
    normalized_formats = _normalize_formats(formats)
    model_sources = model_sources or ModelSourceSet()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir is not None:
        cache_dir = cache_dir.expanduser().resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)

    report = EasyEDA3DFetchReport(
        output_dir=str(output_dir),
        formats=normalized_formats,
        model_sources=model_sources.summary(),
    )
    grouped = _group_model_ids(library.footprints)
    components_by_footprint = _components_by_footprint(library.components)
    opener = urlopen or urllib.request.urlopen

    for model_id, footprints in grouped.items():
        components = _components_for_footprints(footprints, components_by_footprint)
        model_title = footprints[0].properties.get("EasyEDA_3D_Model_Title", "")
        for model_format in normalized_formats:
            row = _fetch_one(
                model_id=model_id,
                model_title=str(model_title or ""),
                footprints=footprints,
                model_format=model_format,
                output_dir=output_dir,
                cache_dir=cache_dir,
                timeout=timeout,
                urlopen=opener,
                model_sources=model_sources,
                components=components,
            )
            report.downloads.append(row)
            if row.status in SUCCESS_STATUSES and row.file:
                report.models.append(
                    Model3D(
                        file=row.file,
                        source_file=row.file,
                        format=model_format,
                        key=normalized_key(Path(row.file).stem),
                    )
                )

    return report


def write_easyeda_3d_fetch_report(
    report: EasyEDA3DFetchReport,
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _group_model_ids(footprints: list[Footprint]) -> dict[str, list[Footprint]]:
    grouped: dict[str, list[Footprint]] = {}
    for footprint in footprints:
        model_id = _model_id(footprint)
        if not model_id:
            continue
        grouped.setdefault(model_id, []).append(footprint)
    return grouped


def _model_id(footprint: Footprint) -> str:
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


def _fetch_one(
    model_id: str,
    model_title: str,
    footprints: list[Footprint],
    model_format: str,
    output_dir: Path,
    cache_dir: Path | None,
    timeout: float,
    urlopen: UrlOpen,
    model_sources: ModelSourceSet,
    components: list[Component] | None = None,
) -> EasyEDA3DDownload:
    row = EasyEDA3DDownload(
        footprint_names=[footprint.name for footprint in footprints],
        model_id=model_id,
        model_title=model_title,
        format=model_format,
        url="",
    )
    for request_model_id in _download_model_id_candidates(model_id):
        try:
            request_urls = model_sources.urls_for(
                model_id,
                model_title,
                footprints,
                model_format,
                request_model_id=request_model_id,
                components=components,
            )
        except ValueError as exc:
            row.status = "invalid_source"
            row.error = str(exc)
            return row
        for source, url in request_urls:
            target = output_dir / f"{cadence_safe_name(request_model_id, 'model')}.{model_format}"
            cache_target = (
                cache_dir / f"{cadence_safe_name(request_model_id, 'model')}.{model_format}"
                if cache_dir is not None
                else None
            )
            row.request_model_id = request_model_id
            row.url = url
            row.file = str(target)
            row.source = source
            row.attempted_model_ids.append(request_model_id)
            row.attempted_urls.append(url)

            existing = _read_valid_model(target, model_format)
            if existing is not None:
                row.status = "existing"
                row.bytes = len(existing)
                return row

            if cache_target is not None:
                cached = _read_valid_model(cache_target, model_format)
                if cached is not None:
                    target.write_bytes(cached)
                    row.status = "cached"
                    row.bytes = len(cached)
                    return row

            try:
                payload, status = _download(url, timeout, urlopen)
            except urllib.error.HTTPError as exc:
                row.status = "not_found" if exc.code == 404 else f"http_{exc.code}"
                row.error = str(exc)
                row.file = ""
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                row.status = "network_error"
                row.error = str(exc)
                row.file = ""
                continue

            if status != 200:
                row.status = "not_found" if status == 404 else f"http_{status}"
                row.error = f"HTTP status {status}"
                row.file = ""
                continue
            if not _valid_model_payload(payload, model_format):
                row.status = "invalid_model"
                row.error = f"Response does not look like a valid {model_format.upper()} model."
                row.file = ""
                continue

            target.write_bytes(payload)
            if cache_target is not None and cache_target.resolve() != target.resolve():
                temporary = cache_target.with_name(cache_target.name + "." + uuid4().hex + ".tmp")
                try:
                    temporary.write_bytes(payload)
                    temporary.replace(cache_target)
                finally:
                    temporary.unlink(missing_ok=True)
            row.status = "downloaded"
            row.bytes = len(payload)
            return row

    if not row.attempted_model_ids:
        row.status = "not_found"
        row.error = "No usable EasyEDA 3D model ID candidate."
    return row


def _download(url: str, timeout: float, urlopen: UrlOpen) -> tuple[bytes, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": "jlc2cadence/3d-fetch",
        },
    )
    try:
        response = urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    except TypeError:
        response = urlopen(request, timeout=timeout)
    with response as handle:
        payload = handle.read()
        status = int(getattr(handle, "status", getattr(handle, "code", 200)) or 200)
    return bytes(payload), status


def _read_valid_model(path: Path, model_format: str) -> bytes | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return payload if _valid_model_payload(payload, model_format) else None


def _valid_model_payload(payload: bytes, model_format: str) -> bool:
    if not payload:
        return False
    sample = payload.lstrip()
    if model_format == "step":
        return sample.startswith(b"ISO-10303-21;")
    if model_format == "obj":
        return any(
            line.startswith((b"v ", b"vn ", b"f ", b"o ", b"g "))
            for line in sample.splitlines()[:200]
        )
    return False


def _template_values(
    model_id: str,
    model_title: str,
    footprints: list[Footprint],
    model_format: str,
    source_model_id: str | None = None,
    components: list[Component] | None = None,
) -> dict[str, str]:
    candidates = model_id_candidates(model_id)
    model_id_part = candidates[0] if candidates else str(model_id or "")
    workspace_id = ""
    source_model_id = source_model_id or model_id
    if "|" in str(source_model_id or ""):
        workspace_id = str(source_model_id).split("|", 1)[1]
    footprint = footprints[0].name if footprints else ""
    raw_values = {
        "model_id_raw": str(model_id or ""),
        "model_id_part_raw": str(model_id_part or ""),
        "workspace_id_raw": workspace_id,
        "model_title_raw": str(model_title or ""),
        "footprint_raw": footprint,
    }
    encoded_values = {
        "model_id": urllib.parse.quote(raw_values["model_id_raw"], safe=""),
        "model_id_part": urllib.parse.quote(raw_values["model_id_part_raw"], safe=""),
        "workspace_id": urllib.parse.quote(workspace_id, safe=""),
        "model_title": urllib.parse.quote(raw_values["model_title_raw"], safe=""),
        "footprint": urllib.parse.quote(footprint, safe=""),
    }
    return {
        **raw_values,
        **encoded_values,
        **_component_template_values(components or []),
        "format": model_format,
    }


def _source_lookup_keys(
    model_id: str,
    model_title: str,
    footprints: list[Footprint],
    components: list[Component] | None = None,
) -> list[str]:
    values = []
    for candidate in model_id_candidates(model_id):
        values.extend([candidate, normalized_key(candidate)])
    for value in [model_title, *(footprint.name for footprint in footprints)]:
        text = str(value or "").strip()
        if text:
            values.extend([text, normalized_key(text)])
    for value in _component_lookup_values(components or []):
        text = str(value or "").strip()
        if text:
            values.extend([text, normalized_key(text)])
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _components_by_footprint(components: list[Component]) -> dict[str, list[Component]]:
    grouped: dict[str, list[Component]] = {}
    for component in components:
        footprint = str(component.footprint or "").strip()
        if not footprint:
            continue
        for key in {footprint, normalized_key(footprint)}:
            if key:
                grouped.setdefault(key, []).append(component)
    return grouped


def _components_for_footprints(
    footprints: list[Footprint],
    components_by_footprint: dict[str, list[Component]],
) -> list[Component]:
    result: list[Component] = []
    seen: set[int] = set()
    for footprint in footprints:
        for key in (footprint.name, normalized_key(footprint.name)):
            for component in components_by_footprint.get(key, []):
                marker = id(component)
                if marker in seen:
                    continue
                seen.add(marker)
                result.append(component)
    return result


def _component_template_values(components: list[Component]) -> dict[str, str]:
    raw_values: dict[str, str] = {}

    def add_value(key: str, value: object) -> None:
        text = str(value or "").strip()
        if text and key not in raw_values:
            raw_values[key] = text

    for component in components:
        add_value("component_name", component.name)
        add_value("component_value", component.value)
        add_value("component_description", component.description)
        add_value("component_ref_prefix", component.ref_prefix)
        for prop_key, prop_value in component.properties.items():
            template_key = _template_property_key(prop_key)
            if template_key:
                add_value(f"component_{template_key}", prop_value)

    values: dict[str, str] = {}
    for key, value in raw_values.items():
        values[key] = urllib.parse.quote(value, safe="")
        values[f"{key}_raw"] = value
    return values


def _component_lookup_values(components: list[Component]) -> list[str]:
    values: list[str] = []
    for component in components:
        values.extend([component.name, component.value, component.description])
        values.extend(str(value or "") for value in component.properties.values())
    return values


def _template_property_key(value: object) -> str:
    text = str(value or "").strip().lower()
    chars: list[str] = []
    prev_was_sep = False
    for char in text:
        if char.isalnum():
            chars.append(char)
            prev_was_sep = False
        elif chars and not prev_was_sep:
            chars.append("_")
            prev_was_sep = True
    return "".join(chars).strip("_")


def _parse_source_templates(data: Any) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for model_format in MODEL_SOURCE_KEYS:
        value = data.get(model_format)
        urls = _as_url_list(value)
        if urls:
            parsed[model_format] = urls
    return parsed


def _parse_model_overrides(data: Any) -> dict[str, dict[str, list[str]]]:
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, dict[str, list[str]]] = {}
    for candidate, value in data.items():
        key = str(candidate or "").strip()
        if not key:
            continue
        if isinstance(value, str):
            by_format = {model_format: [value] for model_format in MODEL_SOURCE_KEYS}
        elif isinstance(value, dict):
            common = _as_url_list(value.get("url", value.get("urls")))
            by_format = {}
            for model_format in MODEL_SOURCE_KEYS:
                urls = _as_url_list(value.get(model_format)) or common
                if urls:
                    by_format[model_format] = urls
        else:
            continue
        for lookup_key in [key, normalized_key(key)]:
            if lookup_key:
                parsed[lookup_key] = by_format
    return parsed


def _as_url_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _download_model_id_candidates(model_id: str) -> list[str]:
    candidates = model_id_candidates(model_id)
    if "|" in str(model_id or "") and len(candidates) > 1:
        return candidates[1:]
    return candidates


def _normalize_formats(formats: Iterable[str]) -> list[str]:
    values = []
    for value in formats:
        text = str(value or "").strip().lower().lstrip(".")
        if text == "both":
            values.extend(["step", "obj"])
        elif text:
            values.append(text)
    normalized = []
    for value in values:
        if value not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported EasyEDA 3D format: {value}")
        if value not in normalized:
            normalized.append(value)
    return normalized or ["step"]
