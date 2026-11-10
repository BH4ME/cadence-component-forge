from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, LibraryIR


EASYEDA_COMPONENT_URL = "https://easyeda.com/api/products/{lcsc_id}/components"
LCEDA_STEP_BY_NUMBER_URL = "https://lceda.cn/api/components/searchByNumbers"
LCSC_ID_RE = re.compile(r"(?<![A-Za-z0-9])C\d{3,}(?![A-Za-z0-9])", re.IGNORECASE)
SUCCESS_STATUSES = {"resolved", "cached"}


@dataclass
class LCSC3DResolution:
    lcsc_id: str
    footprint_names: list[str] = field(default_factory=list)
    component_names: list[str] = field(default_factory=list)
    source: str = ""
    url: str = ""
    cache_file: str = ""
    status: str = ""
    model_id: str = ""
    model_title: str = ""
    package_title: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "lcsc_id": self.lcsc_id,
            "footprints": list(self.footprint_names),
            "components": list(self.component_names),
            "source": self.source,
            "url": self.url,
            "cache_file": self.cache_file,
            "status": self.status,
            "model_id": self.model_id,
            "model_title": self.model_title,
            "package_title": self.package_title,
            "error": self.error,
        }


@dataclass
class LCSC3DResolutionReport:
    cache_dir: str = ""
    timeout: float = 30.0
    resolutions: list[LCSC3DResolution] = field(default_factory=list)
    assigned_footprints: list[str] = field(default_factory=list)
    ambiguous_footprints: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[LCSC3DResolution]:
        return [
            row
            for row in self.resolutions
            if row.status not in SUCCESS_STATUSES
        ]

    @property
    def resolved_count(self) -> int:
        return sum(1 for row in self.resolutions if row.status in SUCCESS_STATUSES)

    @property
    def error_count(self) -> int:
        return len(self.errors) + len(self.ambiguous_footprints)

    @property
    def status(self) -> str:
        if not self.resolutions:
            return "no_lcsc_parts"
        if not self.errors and not self.ambiguous_footprints:
            return "complete"
        if self.resolved_count or self.assigned_footprints:
            return "partial"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        status_counts: dict[str, int] = {}
        for row in self.resolutions:
            status_counts[row.status] = status_counts.get(row.status, 0) + 1
        return {
            "status": self.status,
            "cache_dir": self.cache_dir,
            "timeout": self.timeout,
            "requested_count": len(self.resolutions),
            "resolved_count": self.resolved_count,
            "error_count": self.error_count,
            "assigned_footprint_count": len(self.assigned_footprints),
            "ambiguous_footprint_count": len(self.ambiguous_footprints),
            "status_counts": status_counts,
            "assigned_footprints": list(self.assigned_footprints),
            "ambiguous_footprints": list(self.ambiguous_footprints),
            "resolutions": [row.to_dict() for row in self.resolutions],
        }


UrlOpen = Callable[..., object]


@dataclass
class _LCEDAStepLookup:
    source: str = "lceda_search_by_numbers"
    url: str = LCEDA_STEP_BY_NUMBER_URL
    cache_file: str = ""
    status: str = ""
    model_id: str = ""
    error: str = ""


def resolve_lcsc_3d_models(
    library: LibraryIR,
    cache_dir: Path | None = None,
    timeout: float = 30.0,
    urlopen: UrlOpen | None = None,
) -> LCSC3DResolutionReport:
    """Resolve missing EasyEDA 3D IDs from LCSC component properties.

    This function only enriches footprints that do not already carry an
    explicit EasyEDA 3D model ID. It never fabricates a model file; the
    existing STEP/OBJ downloader performs the subsequent model acquisition.
    """

    cache_path = cache_dir.expanduser().resolve() if cache_dir is not None else None
    if cache_path is not None:
        cache_path.mkdir(parents=True, exist_ok=True)

    report = LCSC3DResolutionReport(
        cache_dir=str(cache_path) if cache_path is not None else "",
        timeout=float(timeout),
    )
    grouped = _group_lcsc_requests(library)
    opener = urlopen or urllib.request.urlopen
    resolved_by_lcsc: dict[str, LCSC3DResolution] = {}

    for lcsc_id, request in grouped.items():
        row = _resolve_one(
            lcsc_id=lcsc_id,
            footprint_names=request["footprint_names"],
            component_names=request["component_names"],
            cache_dir=cache_path,
            timeout=float(timeout),
            urlopen=opener,
        )
        report.resolutions.append(row)
        resolved_by_lcsc[lcsc_id] = row

    _assign_resolved_models(library, grouped, resolved_by_lcsc, report)
    return report


def write_lcsc_3d_resolution_report(
    report: LCSC3DResolutionReport,
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def extract_lcsc_ids(values: list[object]) -> list[str]:
    """Extract stable LCSC part numbers from arbitrary property values."""

    found: list[str] = []
    for value in values:
        text = str(value or "").strip()
        for match in LCSC_ID_RE.findall(text):
            normalized = match.upper()
            if normalized not in found:
                found.append(normalized)
    return found


def _group_lcsc_requests(library: LibraryIR) -> dict[str, dict[str, list[str]]]:
    components_by_footprint: dict[str, list[Component]] = {}
    for component in library.components:
        footprint_name = str(component.footprint or "").strip()
        if footprint_name:
            components_by_footprint.setdefault(footprint_name, []).append(component)

    grouped: dict[str, dict[str, list[str]]] = {}
    for footprint in library.footprints:
        if _has_explicit_model_id(footprint):
            continue
        components = components_by_footprint.get(footprint.name, [])
        component_values = [
            value
            for component in components
            for value in component.properties.values()
        ]
        footprint_values = list(footprint.properties.values())
        lcsc_ids = extract_lcsc_ids(component_values + footprint_values)
        for lcsc_id in lcsc_ids:
            request = grouped.setdefault(
                lcsc_id,
                {"footprint_names": [], "component_names": []},
            )
            if footprint.name not in request["footprint_names"]:
                request["footprint_names"].append(footprint.name)
            for component in components:
                if component.name not in request["component_names"]:
                    request["component_names"].append(component.name)
    return grouped


def _resolve_one(
    lcsc_id: str,
    footprint_names: list[str],
    component_names: list[str],
    cache_dir: Path | None,
    timeout: float,
    urlopen: UrlOpen,
) -> LCSC3DResolution:
    url = EASYEDA_COMPONENT_URL.format(lcsc_id=lcsc_id)
    cache_file = ""
    if cache_dir is not None:
        cache_file = str(cache_dir / f"{cadence_safe_name(lcsc_id, 'lcsc')}.json")
    row = LCSC3DResolution(
        lcsc_id=lcsc_id,
        footprint_names=list(footprint_names),
        component_names=list(component_names),
        source="easyeda_product_components",
        url=url,
        cache_file=cache_file,
    )

    lceda_step = _resolve_lceda_step_by_number(
        lcsc_id=lcsc_id,
        cache_dir=cache_dir,
        timeout=timeout,
        urlopen=urlopen,
    )
    if lceda_step.model_id:
        row.source = lceda_step.source
        row.url = lceda_step.url
        row.cache_file = lceda_step.cache_file or row.cache_file
        row.model_id = lceda_step.model_id
        row.model_title = lcsc_id
        row.status = lceda_step.status
        row.error = ""
        return row

    data: dict[str, Any] | None = None
    if cache_dir is not None:
        data = _read_json_cache(Path(cache_file))
        if data is not None:
            row.status = "cached"

    if data is None:
        try:
            data = _download_json(url, timeout, urlopen)
        except urllib.error.HTTPError as exc:
            row.status = "not_found" if exc.code == 404 else f"http_{exc.code}"
            row.error = str(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            row.status = "network_error"
            row.error = str(exc)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            row.status = "invalid_response"
            row.error = str(exc)
        if data is not None and cache_dir is not None:
            _write_json_cache(Path(cache_file), data)

    if data is not None:
        if not isinstance(data, dict):
            row.status = "invalid_response"
            row.error = "EasyEDA component response is not a JSON object."
        elif data.get("success") is False:
            row.status = "api_error"
            row.error = str(data.get("message") or data.get("code") or "EasyEDA API returned success=false.")
        else:
            result = data.get("result")
            if not isinstance(result, dict):
                row.status = "invalid_response"
                row.error = "EasyEDA component response has no result object."
            else:
                row.model_id = _find_uuid_3d(result)
                row.package_title = _package_title(result)
                row.model_title = _model_title(result)
                if row.model_id:
                    if not row.model_title:
                        row.model_title = row.package_title
                    if not row.status:
                        row.status = "resolved"
                    return row
                row.status = "no_3d_model"
                row.error = "EasyEDA component metadata contains no uuid_3d."

    if not row.model_id and lceda_step.status:
        row.source = lceda_step.source
        row.url = lceda_step.url
        row.cache_file = lceda_step.cache_file or row.cache_file
        row.status = lceda_step.status
        row.error = lceda_step.error
    return row


def _resolve_lceda_step_by_number(
    lcsc_id: str,
    cache_dir: Path | None,
    timeout: float,
    urlopen: UrlOpen,
) -> _LCEDAStepLookup:
    cache_file = ""
    if cache_dir is not None:
        cache_file = str(cache_dir / f"{cadence_safe_name(lcsc_id, 'lcsc')}_lceda_step.json")
    row = _LCEDAStepLookup(cache_file=cache_file)

    data: dict[str, Any] | None = None
    if cache_file:
        data = _read_json_cache(Path(cache_file))
        if data is not None:
            row.status = "cached"

    if data is None:
        body = urllib.parse.urlencode({"numbers": json.dumps([lcsc_id])}).encode("utf-8")
        try:
            data = _download_json(
                LCEDA_STEP_BY_NUMBER_URL,
                timeout,
                urlopen,
                method="POST",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": "https://item.szlcsc.com/",
                },
            )
        except urllib.error.HTTPError as exc:
            row.status = "not_found" if exc.code == 404 else f"http_{exc.code}"
            row.error = str(exc)
            return row
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            row.status = "network_error"
            row.error = str(exc)
            return row
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            row.status = "invalid_response"
            row.error = str(exc)
            return row
        if cache_file:
            _write_json_cache(Path(cache_file), data)

    if not isinstance(data, dict):
        row.status = "invalid_response"
        row.error = "LCEDA component response is not a JSON object."
        return row
    if data.get("success") is False:
        row.status = "api_error"
        row.error = str(data.get("message") or data.get("code") or "LCEDA API returned success=false.")
        return row

    result = data.get("result")
    if not isinstance(result, list):
        row.status = "invalid_response"
        row.error = "LCEDA component response has no result list."
        return row

    target = lcsc_id.upper()
    matches = [
        item
        for item in result
        if isinstance(item, dict) and str(item.get("number") or "").strip().upper() == target
    ]
    if not matches:
        row.status = "not_found"
        row.error = f"LCEDA component response has no row for {lcsc_id}."
        return row
    step_id = str(matches[0].get("step") or "").strip()
    if not step_id:
        row.status = "no_3d_model"
        row.error = "LCEDA component metadata contains no step UUID."
        return row
    row.model_id = step_id
    if not row.status:
        row.status = "resolved"
    return row


def _assign_resolved_models(
    library: LibraryIR,
    grouped: dict[str, dict[str, list[str]]],
    resolved_by_lcsc: dict[str, LCSC3DResolution],
    report: LCSC3DResolutionReport,
) -> None:
    footprints_by_name = {footprint.name: footprint for footprint in library.footprints}
    for footprint_name in sorted(
        {
            footprint_name
            for request in grouped.values()
            for footprint_name in request["footprint_names"]
        }
    ):
        footprint = footprints_by_name.get(footprint_name)
        if footprint is None or _has_explicit_model_id(footprint):
            continue
        candidates = [
            row
            for lcsc_id, request in grouped.items()
            if footprint_name in request["footprint_names"]
            for row in [resolved_by_lcsc[lcsc_id]]
            if row.status in SUCCESS_STATUSES and row.model_id
        ]
        model_ids = {row.model_id for row in candidates}
        if len(model_ids) > 1:
            report.ambiguous_footprints.append(footprint_name)
            for row in candidates:
                row.error = (
                    f"Footprint {footprint_name} maps to multiple resolved 3D model IDs: "
                    + ", ".join(sorted(model_ids))
                )
            continue
        if not candidates:
            continue
        row = candidates[0]
        footprint.properties["EasyEDA_3D_Model_Id"] = row.model_id
        if row.model_title and not footprint.properties.get("EasyEDA_3D_Model_Title"):
            footprint.properties["EasyEDA_3D_Model_Title"] = row.model_title
        if row.model_title and not footprint.properties.get("Model3D"):
            footprint.properties["Model3D"] = cadence_safe_name(row.model_title, "MODEL")
        footprint.properties["EasyEDA_3D_Resolved_From_LCSC"] = row.lcsc_id
        if row.source:
            footprint.properties["EasyEDA_3D_Resolution_Source"] = row.source
        footprint.properties["EasyEDA_3D_Resolution_Status"] = row.status
        report.assigned_footprints.append(footprint_name)


def _has_explicit_model_id(footprint: Footprint) -> bool:
    for key in (
        "EasyEDA_3D_Model_Id",
        "EasyEDA_3D_Model_ID",
        "3D_Model",
        "3D Model",
        "Model3D",
        "Model",
    ):
        if str(footprint.properties.get(key, "") or "").strip():
            return True
    return False


def _download_json(
    url: str,
    timeout: float,
    urlopen: UrlOpen,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "jlc2cadence/lcsc-3d-resolver",
        "Referer": "https://easyeda.com/",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=request_headers,
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
    if status != 200:
        raise urllib.error.HTTPError(url, status, f"HTTP status {status}", None, None)
    decoded = payload.decode("utf-8-sig")
    data = json.loads(decoded)
    if not isinstance(data, dict):
        raise ValueError("EasyEDA component response root must be an object.")
    return data


def _read_json_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _find_uuid_3d(result: dict[str, Any]) -> str:
    preferred_paths = [
        ("packageDetail", "dataStr", "head", "uuid_3d"),
        ("packageDetail", "dataStr", "head", "uuid3d"),
        ("packageDetail", "head", "uuid_3d"),
        ("uuid_3d",),
        ("uuid3d",),
    ]
    for path in preferred_paths:
        value: Any = result
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return _find_key_recursively(result, {"uuid_3d", "uuid3d"})


def _find_key_recursively(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in keys and str(nested or "").strip():
                return str(nested).strip()
            found = _find_key_recursively(nested, keys)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_key_recursively(nested, keys)
            if found:
                return found
    return ""


def _package_title(result: dict[str, Any]) -> str:
    package_detail = result.get("packageDetail")
    if isinstance(package_detail, dict):
        title = str(package_detail.get("title") or "").strip()
        if title:
            return title
        data_str = package_detail.get("dataStr")
        if isinstance(data_str, dict):
            head = data_str.get("head")
            if isinstance(head, dict):
                c_para = head.get("c_para")
                if isinstance(c_para, dict):
                    for key in ("package", "3DModel", "3dModel"):
                        text = str(c_para.get(key) or "").strip()
                        if text:
                            return text
    return ""


def _model_title(result: dict[str, Any]) -> str:
    package_detail = result.get("packageDetail")
    if isinstance(package_detail, dict):
        data_str = package_detail.get("dataStr")
        if isinstance(data_str, dict):
            head = data_str.get("head")
            if isinstance(head, dict):
                c_para = head.get("c_para")
                if isinstance(c_para, dict):
                    for key in ("3DModel", "3dModel", "3D Model", "3D Model Title"):
                        text = str(c_para.get(key) or "").strip()
                        if text:
                            return text
    for key in ("package", "packageName", "package_title"):
        text = str(result.get(key) or "").strip()
        if text:
            return text
    return ""
