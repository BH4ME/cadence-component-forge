from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..ir.geometry import cadence_safe_name
from ..ir.schema import Component, Footprint, Graphic, LibraryIR, Pad, Pin


SEARCH_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
LOOKUP_URL = "https://lceda.cn/api/components/searchByNumbers"
COMPONENT_URL = "https://lceda.cn/api/components/{uuid}"
SOURCE_NAME = "JLCEDA/EasyEDA Official Library"
LCSC_ID = re.compile(r"C[0-9]+", re.IGNORECASE)
UUID = re.compile(r"[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
EE_MM = 0.254
CONVERSION_POLICY = "source_exact"
SCHEMATIC_FORMAT_URL = "https://docs.easyeda.com/en/DocumentFormat/2-EasyEDA-Schematic-File-Format/"
SEARCH_CACHE_TTL_SECONDS = 300.0
SEARCH_STALE_TTL_SECONDS = 3600.0


class CatalogError(RuntimeError):
    """The catalog could not supply a usable, faithfully convertible component."""

    def __init__(self, message: str, *, error_code: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class CatalogPart:
    part_id: str
    name: str
    manufacturer: str = ""
    description: str = ""
    package: str = ""
    datasheet: str = ""
    source_url: str = ""
    has_3d: bool | None = None
    has_step: bool | None = None
    stock: int | None = None

    @property
    def lcsc_id(self) -> str:
        return self.part_id

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update(lcsc_id=self.part_id, title=self.name)
        return result


class EasyEDACatalog:
    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout: float = 20.0,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else None
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Catalog timeout must be positive and finite.")
        self.urlopen = urlopen or urllib.request.urlopen
        self.last_total = 0

    def search(self, query: str, limit: int = 20, page: int = 1) -> list[CatalogPart]:
        query = str(query).strip()
        if not query or len(query) > 200:
            raise CatalogError("Enter a part number or a keyword of at most 200 characters.")
        if not 1 <= limit <= 100 or page < 1:
            raise ValueError("Catalog page must be positive and limit must be between 1 and 100.")
        if LCSC_ID.fullmatch(query):
            lcsc_id = query.upper()
            # An exact part-number search only needs the catalog row.  The
            # EasyEDA component endpoint is required later for conversion, but
            # fetching it here doubled the first-search latency.  Page two of
            # an exact lookup is always empty and must not hit the network.
            self.last_total = 1
            if page > 1:
                return []
            # A component record is already sufficient to render an exact
            # search row.  Reusing it avoids a network round trip when the
            # user searches a part that was imported or previewed earlier;
            # this is the common Capture workflow and keeps the second search
            # below the process-startup cost of the Tcl worker.
            cached_component = self._cached_component(lcsc_id)
            if cached_component is not None:
                raw, lookup = cached_component
                return [_component_metadata(raw, lcsc_id, lookup)]
            cache = self._search_cache_path(query, limit, page)
            cached = self._read_search_cache(cache, SEARCH_CACHE_TTL_SECONDS)
            if cached is not None:
                self.last_total = int(cached.get("total") or 0)
                return self._parts_from_cached_rows(cached.get("results"))
            try:
                response = self._json(
                    SEARCH_URL,
                    json.dumps({"keyword": query, "currentPage": 1, "pageSize": limit}).encode("utf-8"),
                    "application/json",
                )
                info = response.get("data", {}).get("componentPageInfo", {}) if isinstance(response.get("data"), dict) else {}
                rows = info.get("list") if isinstance(info, dict) else None
                if response.get("code") in (200, "200") and isinstance(rows, list):
                    self.last_total = int(info.get("total") or 0)
                    exact = next((row for row in rows if isinstance(row, dict)
                                  and str(row.get("componentCode") or "").upper() == lcsc_id), None)
                    if exact is not None:
                        part = _catalog_part_from_search_row(exact, lcsc_id)
                        self._write_search_cache(cache, query, page, limit, self.last_total, [part])
                        return [part]
            except CatalogError:
                # Fall through to the EasyEDA lookup so a service-specific
                # search failure still reports the precise CAD-library error.
                pass
            raw, lookup = self._component(lcsc_id)
            self.last_total = 1
            return [_component_metadata(raw, lcsc_id, lookup)]
        # Keyword searches are the slowest operation in the Capture panel.  Keep a
        # short-lived on-disk cache so reopening the panel or paging back does not
        # repeat the same network request.  A bounded stale entry remains useful
        # during a transient catalog outage and is clearly marked by the caller via
        # the normal results payload (the source is still the official API).
        cache = self._search_cache_path(query, limit, page)
        cached = self._read_search_cache(cache, SEARCH_CACHE_TTL_SECONDS)
        if cached is not None:
            self.last_total = int(cached.get("total") or 0)
            return self._parts_from_cached_rows(cached.get("results"))
        try:
            response = self._json(
                SEARCH_URL,
                json.dumps({"keyword": query, "currentPage": page, "pageSize": limit}).encode("utf-8"),
                "application/json",
            )
        except CatalogError:
            stale = self._read_search_cache(cache, SEARCH_STALE_TTL_SECONDS)
            if stale is None:
                raise
            self.last_total = int(stale.get("total") or 0)
            return self._parts_from_cached_rows(stale.get("results"))
        if response.get("code") not in (200, "200"):
            raise CatalogError("JLCPCB search rejected the request: " + str(response.get("message", response.get("msg", "unknown error"))))
        data = response.get("data")
        info = data.get("componentPageInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            raise CatalogError("The catalog returned an unexpected search result format.")
        rows = info.get("list") or []
        if not isinstance(rows, list):
            raise CatalogError("The catalog returned an unexpected search result format.")
        self.last_total = int(info.get("total") or 0)
        results = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise CatalogError("The catalog returned an unexpected search item format.")
            part_id = str(row.get("componentCode") or "").upper()
            if not LCSC_ID.fullmatch(part_id):
                continue
            results.append(_catalog_part_from_search_row(row, part_id))
        self._write_search_cache(cache, query, page, limit, self.last_total, results)
        return results

    def _search_cache_path(self, query: str, limit: int, page: int) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(
            json.dumps({"query": query.casefold(), "limit": limit, "page": page}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        return self.cache_dir / f"search_{key}.json"

    @staticmethod
    def _parts_from_cached_rows(rows: object) -> list[CatalogPart]:
        if not isinstance(rows, list):
            raise CatalogError("The cached catalog search result is invalid.")
        result: list[CatalogPart] = []
        for row in rows:
            if not isinstance(row, dict) or not LCSC_ID.fullmatch(str(row.get("part_id") or "")):
                raise CatalogError("The cached catalog search result is invalid.")
            result.append(CatalogPart(
                part_id=str(row.get("part_id")).upper(),
                name=str(row.get("name") or ""),
                manufacturer=str(row.get("manufacturer") or ""),
                description=str(row.get("description") or ""),
                package=str(row.get("package") or ""),
                datasheet=str(row.get("datasheet") or ""),
                source_url=str(row.get("source_url") or ""),
                has_3d=row.get("has_3d"), has_step=row.get("has_step"),
                stock=_optional_int(row.get("stock")),
            ))
        return result

    @staticmethod
    def _read_search_cache(path: Path | None, ttl: float) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            if time.time() - path.stat().st_mtime > float(ttl):
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _write_search_cache(path: Path | None, query: str, page: int, limit: int,
                            total: int, rows: list[CatalogPart]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            payload = {
                "schema": 1, "query": query, "page": page, "limit": limit,
                "total": total, "results": [asdict(row) for row in rows],
            }
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # A read-only library folder must never make a successful search fail.
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

    def fetch_part(self, lcsc_id: str) -> LibraryIR:
        lcsc_id = _normalize_id(lcsc_id)
        raw, lookup = self._component(lcsc_id)
        return component_to_library(raw, lcsc_id, lookup)

    def _cached_component(self, lcsc_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Read a previously downloaded official component record, if valid."""
        if self.cache_dir is None:
            return None
        cache = self.cache_dir / f"{lcsc_id}.json"
        if not cache.is_file():
            return None
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            raw, lookup = stored["component"], stored["lookup"]
            if (stored.get("lcsc_id") == lcsc_id and isinstance(raw, dict)
                    and isinstance(raw.get("dataStr"), dict) and isinstance(lookup, dict)):
                _check_identity(raw, lcsc_id)
                return raw, lookup
        except (OSError, ValueError, TypeError, KeyError, AttributeError, CatalogError):
            return None
        return None

    def _component(self, lcsc_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        lcsc_id = _normalize_id(lcsc_id)
        cache = self.cache_dir / f"{lcsc_id}.json" if self.cache_dir else None
        cached = self._cached_component(lcsc_id)
        if cached is not None:
            return cached
        lookup_response = self._json(
            LOOKUP_URL,
            urllib.parse.urlencode({"numbers": json.dumps([lcsc_id])}).encode("utf-8"),
            "application/x-www-form-urlencoded; charset=UTF-8",
        )
        rows = lookup_response.get("result")
        if lookup_response.get("success") is not True or not isinstance(rows, list):
            raise CatalogError("The component lookup service returned an unexpected response.")
        lookup = next((r for r in rows if isinstance(r, dict) and str(r.get("number", "")).upper() == lcsc_id), None)
        if not lookup:
            raise CatalogError(f"No EasyEDA CAD library was found for {lcsc_id}.", error_code="no_cad_library")
        component_uuid = str(lookup.get("uuid") or "")
        if not UUID.fullmatch(component_uuid):
            raise CatalogError(f"No valid symbol library ID was returned for {lcsc_id}.")
        response = self._json(COMPONENT_URL.format(uuid=component_uuid))
        raw = response.get("result")
        if response.get("success") is not True or not isinstance(raw, dict):
            raise CatalogError(f"The symbol and footprint data for {lcsc_id} could not be retrieved.")
        _check_identity(raw, lcsc_id)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_name(cache.name + "." + uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps({
                    "schema": 1, "lcsc_id": lcsc_id, "source": SOURCE_NAME,
                    "lookup": lookup, "component": raw,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
        return raw, lookup

    def _json(self, url: str, data: bytes | None = None, content_type: str = "") -> dict[str, Any]:
        headers = {"User-Agent": "Mozilla/5.0 jlc2cadence", "Accept": "application/json", "Referer": "https://lceda.cn/"}
        if content_type:
            headers["Content-Type"] = content_type
        if url == SEARCH_URL:
            headers.update(Origin="https://jlcpcb.com", Referer="https://jlcpcb.com/parts")
        try:
            with self.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CatalogError("The catalog response exceeded the supported size.")
                if body.startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise CatalogError("The decompressed catalog response exceeded the supported size.")
                result = json.loads(body.decode("utf-8-sig"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
            raise CatalogError(f"Catalog request failed ({urllib.parse.urlparse(url).hostname}): {exc}") from exc
        if not isinstance(result, dict):
            raise CatalogError("The catalog response must be a JSON object.")
        return result


def component_to_library(raw: dict[str, Any], lcsc_id: str, lookup: dict[str, Any] | None = None) -> LibraryIR:
    """Convert original catalog records without regenerating their geometry."""
    lcsc_id = _normalize_id(lcsc_id)
    _check_identity(raw, lcsc_id)
    lookup = lookup or {}
    package = raw.get("packageDetail")
    if not isinstance(raw.get("dataStr"), dict) or not isinstance(package, dict) or not isinstance(package.get("dataStr"), dict):
        raise CatalogError(f"{lcsc_id} does not include both a symbol and a footprint.")
    _validate_pad_shapes(package["dataStr"].get("shape", []))
    metadata = _component_metadata(raw, lcsc_id, lookup)
    footprint_name = cadence_safe_name(f"{lcsc_id}_{package.get('title') or metadata.package}")
    source_url = COMPONENT_URL.format(uuid=raw.get("uuid", ""))
    shared = {
        "LCSC Part Number": lcsc_id,
        "Manufacturer": metadata.manufacturer,
        "Manufacturer Part Number": metadata.name,
        "Datasheet": metadata.datasheet,
        "Source": SOURCE_NAME,
        "Source URL": source_url,
        "Source Homepages": "https://lceda.cn/ https://easyeda.com",
        "Conversion_Policy": CONVERSION_POLICY,
    }
    original_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    library = LibraryIR(source_files=[source_url], properties={
        "Catalog": SOURCE_NAME, "Catalog_Part": lcsc_id,
        "Conversion_Policy": CONVERSION_POLICY,
        "Source_Record_JSON": original_json,
        "Source_Record_SHA256": hashlib.sha256(original_json.encode("utf-8")).hexdigest(),
    })
    for label, data in (("Symbol", raw["dataStr"]), ("Footprint", package["dataStr"])):
        license_text = data.get("head", {}).get("license")
        if license_text:
            library.properties[f"{label}_Source_License"] = str(license_text)
    component_name = cadence_safe_name(f"{lcsc_id}_{metadata.name}")
    refdes = _source_refdes_metadata(raw, component_name, library)
    component = Component(
        name=component_name,
        ref_prefix=refdes.get("Source_Refdes_Prefix", ""),
        value=metadata.name,
        footprint=footprint_name,
        description=metadata.description,
        properties={**shared, **refdes, "PCB Footprint": footprint_name, "Source_Symbol_Units": "mm"},
        source_file=source_url,
    )
    _adapt_symbol(raw, component, library)
    if not component.pins:
        raise CatalogError(f"{lcsc_id} has no electrical symbol pins.")
    footprint = Footprint(name=footprint_name, description=metadata.description, properties={**shared, **refdes}, source_file=source_url)
    source_footprint = package["dataStr"]
    _adapt_pads(source_footprint, footprint, library)
    _adapt_graphics(source_footprint, footprint, library)
    _adapt_source_model(source_footprint, footprint, library, lookup)
    missing = component.pin_numbers() - footprint.pad_numbers(electrical_only=True)
    if missing:
        raise CatalogError(f"{lcsc_id}: symbol pin numbers are absent from the footprint: {', '.join(sorted(missing))}.")
    extra = footprint.pad_numbers(electrical_only=True) - component.pin_numbers()
    if extra:
        raise CatalogError(f"{lcsc_id}: footprint pads have no symbol pin: {', '.join(sorted(extra))}.")
    library.components.append(component)
    library.footprints.append(footprint)
    return library


def _source_refdes_metadata(raw: dict[str, Any], component_name: str, library: LibraryIR) -> dict[str, str]:
    fields = ("pre", "ref_prefix", "refPrefix", "refdes_prefix", "RefDesPrefix", "ref")
    tiers = []
    evidence = []
    sources = (("dataStr.head", raw["dataStr"].get("head", {})),
               ("packageDetail.dataStr.head", raw["packageDetail"]["dataStr"].get("head", {})))
    for source, head in sources:
        if not isinstance(head, dict):
            continue
        for path, attributes in ((source + ".c_para", head.get("c_para", {})), (source, head)):
            if not isinstance(attributes, dict):
                continue
            candidates = []
            for field in fields:
                if field not in attributes:
                    continue
                original = attributes[field]
                value = original.strip() if isinstance(original, str) else ""
                pattern = r"([A-Za-z]+)(?:[0-9]+|[?*]+)?" if field == "ref" else r"([A-Za-z]+)[?*]*"
                match = re.fullmatch(pattern, value)
                candidate = {"source": path + "." + field, "raw": original, "prefix": match[1] if match else ""}
                evidence.append(candidate)
                if match:
                    candidates.append(candidate)
            tiers.append(candidates)
    result = {"Source_Refdes_Component": component_name,
              "Source_Refdes_Prefix_Status": "missing",
              "Source_Refdes_Prefix_Evidence_JSON": json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))}
    for candidates in tiers:
        if not candidates:
            continue
        prefixes = {candidate["prefix"].upper() for candidate in candidates}
        if len(prefixes) != 1:
            result["Source_Refdes_Prefix_Status"] = "conflict"
            _warning(library, "Conflicting original reference prefixes in " + ", ".join(candidate["source"] for candidate in candidates) + "; no reference prefix was selected or inferred.")
            return result
        chosen = candidates[0]
        result.update(Source_Refdes_Prefix=chosen["prefix"], Source_Refdes_Prefix_Source=chosen["source"],
                      Source_Refdes_Prefix_Raw=chosen["raw"], Source_Refdes_Prefix_Status="verified")
        conflicts = [item for item in evidence if item["prefix"] and item["prefix"].upper() not in prefixes]
        if conflicts:
            _warning(library, "The component reference prefix from " + chosen["source"] + " takes precedence over differing lower-priority source fields: " + ", ".join(item["source"] for item in conflicts) + ". Original values are retained in Source_Refdes_Prefix_Evidence_JSON.")
        return result
    _warning(library, "The original component and footprint properties have no unambiguous reference prefix; none was inferred from the LCSC part number, package name, or component description.")
    return result


def _adapt_symbol(raw: dict[str, Any], component: Component, library: LibraryIR) -> None:
    units = [raw["dataStr"], *[part.get("dataStr") for part in raw.get("subparts") or []]]
    active_units = [data for data in units if isinstance(data, dict) and any(str(shape).startswith("P~") for shape in data.get("shape") or [])]
    for data in units:
        if not isinstance(data, dict):
            raise CatalogError("An original symbol subpart has no shape data.")
        if data not in active_units and data.get("shape"):
            raise CatalogError("Original graphics outside the electrical symbol units require explicit shared-unit conversion; no source graphics were omitted.")
    for index, data in enumerate(active_units):
        origin = _source_origin(data, "symbol")
        unit = _unit_name(index)
        component.properties[f"Source_Symbol_Origin_EE_{unit}"] = json.dumps(origin)
        if index == 0:
            component.properties["Source_Symbol_Origin_EE"] = json.dumps(origin)
        for record in _source_shapes(data, "symbol"):
            fields = record.split("~")
            command = fields[0]
            if command == "P":
                _adapt_source_pin(record, origin, unit, component, library)
                continue
            props = {"unit": unit, "Conversion_Policy": CONVERSION_POLICY, "EasyEDA_Source_Record": record}
            point = lambda x, y: _source_point(x, y, origin)
            if command == "R":
                if len(fields) >= 13:
                    x, y, rx, ry, width, height = fields[1:7]
                    stroke = 7
                elif len(fields) >= 11:
                    x, y, width, height = fields[1:5]
                    rx = ry = ""
                    stroke = 5
                else:
                    raise CatalogError("The source symbol rectangle is incomplete.")
                x, y, width, height = [_finite_number(value, "symbol rectangle") for value in (x, y, width, height)]
                if width <= 0 or height <= 0:
                    raise CatalogError("The source symbol rectangle has invalid dimensions.")
                radius_x = _finite_number(rx or ry or 0, "symbol rectangle radius") * EE_MM
                radius_y = _finite_number(ry or rx or 0, "symbol rectangle radius") * EE_MM
                props.update(radius_x_mm=str(radius_x), radius_y_mm=str(radius_y))
                _source_style(props, fields, stroke)
                component.graphics.append(Graphic("rectangle", "symbol", [point(x, y), point(x + width, y + height)], _source_width(fields, stroke + 1), properties=props))
            elif command in {"E", "C"}:
                minimum = 11 if command == "E" else 10
                if len(fields) < minimum:
                    raise CatalogError("The source symbol ellipse is incomplete.")
                cx, cy = [_finite_number(value, "symbol ellipse") for value in fields[1:3]]
                rx = _finite_number(fields[3], "symbol ellipse radius")
                ry = _finite_number(fields[4], "symbol ellipse radius") if command == "E" else rx
                if min(rx, ry) <= 0:
                    raise CatalogError("The source symbol ellipse has invalid radii.")
                stroke = 5 if command == "E" else 4
                _source_style(props, fields, stroke)
                component.graphics.append(Graphic("ellipse", "symbol", [point(cx - rx, cy - ry), point(cx + rx, cy + ry)], _source_width(fields, stroke + 1), properties=props))
            elif command in {"PL", "PG"}:
                _source_style(props, fields, 2)
                points = _source_points(_field(fields, 1), origin, "symbol polyline")
                component.graphics.append(Graphic("polygon" if command == "PG" else "polyline", "symbol", points, _source_width(fields, 3), properties=props))
            elif command in {"A", "PT"}:
                stroke = 3 if command == "A" else 2
                _source_style(props, fields, stroke)
                graphics = _path_graphics(_field(fields, 1), origin, "symbol", _source_width(fields, stroke + 1), props, filled=props.get("fill", "none") != "none")
                component.graphics.extend(graphics)
            elif command == "T":
                if len(fields) < 16:
                    raise CatalogError("The source symbol text is incomplete.")
                x, y = point(fields[2], fields[3])
                font, font_mm, defaulted = _source_font(fields[6], fields[7], library)
                props.update(font=font, font_size_mm=str(font_mm), font_defaulted=str(defaulted).lower(), anchor=fields[14], visible=str(_source_visible(fields[13])).lower(), fill=fields[5], text_type=fields[1], font_weight=fields[8], font_style=fields[9], baseline=fields[10])
                component.graphics.append(Graphic("text", "symbol", text=fields[12], x=x, y=y, rotation=-_finite_number(fields[4] or 0, "symbol text rotation") % 360, properties=props))
            else:
                raise CatalogError(f"Unsupported source symbol record {command!r}; no substitute symbol was generated.")


def _adapt_source_pin(record: str, origin: tuple[float, float], unit: str, component: Component, library: LibraryIR) -> None:
    segments = [segment.split("~") for segment in record.split("^^")]
    if len(segments) < 5 or len(segments[0]) < 8 or len(segments[1]) < 2 or len(segments[4]) < 8:
        raise CatalogError("The original symbol has an incomplete electrical pin record.")
    settings, dot, path, name, number_label = segments[:5]
    number = number_label[4].strip()
    if not number:
        raise CatalogError("The original symbol pin has no physical pin number.")
    rotation = _finite_number(settings[6] or 0, "symbol pin rotation") % 360
    if rotation not in {0, 90, 180, 270}:
        raise CatalogError(f"The original symbol pin has an unsupported angle: {rotation} degrees.", error_code="unsupported_geometry")
    hotpoint = _source_point(dot[0], dot[1], origin)
    line = _path_graphics(path[0], origin, "symbol", 0, {})
    if len(line) != 1 or line[0].kind != "line" or len(line[0].points) != 2:
        raise CatalogError(f"Pin {number} does not have one straight source pin line; no replacement was generated.", error_code="unsupported_geometry")
    start, end = line[0].points
    if math.dist(end, hotpoint) < math.dist(start, hotpoint):
        start, end = end, start
    if abs(start[0] - end[0]) > 1e-10 and abs(start[1] - end[1]) > 1e-10:
        raise CatalogError(f"Pin {number} has a non-orthogonal source pin line.", error_code="unsupported_geometry")
    electrical_type = _source_integer(settings[2], "symbol electrical type")
    if electrical_type not in {0, 1, 2, 3, 4}:
        raise CatalogError(f"Pin {number} has an unsupported source electrical type {settings[2]!r}.")
    props = {
        "Conversion_Policy": CONVERSION_POLICY, "EasyEDA_Pin_Id": settings[7],
        "EasyEDA_Source_Record": record, "Source_Pin_Electrical_Type": str(electrical_type),
        "Source_Pin_Color": _field(path, 1),
        "Source_Pin_Start_X_mm": str(start[0]), "Source_Pin_Start_Y_mm": str(start[1]),
        "Source_Pin_End_X_mm": str(end[0]), "Source_Pin_End_Y_mm": str(end[1]),
        "Source_Pin_Settings_X_mm": str(_source_point(settings[4], settings[5], origin)[0]),
        "Source_Pin_Settings_Y_mm": str(_source_point(settings[4], settings[5], origin)[1]),
    }
    for label, source_label in (("Name", name), ("Number", number_label)):
        if len(source_label) < 8:
            raise CatalogError(f"Pin {number} has an incomplete source {label.lower()} label.")
        x, y = _source_point(source_label[1], source_label[2], origin)
        font, font_mm, defaulted = _source_font(source_label[6], source_label[7], library)
        prefix = f"Source_Pin_{label}_"
        props.update({prefix + "X_mm": str(x), prefix + "Y_mm": str(y), prefix + "Visible": str(_source_visible(source_label[0])).lower(), prefix + "Rotation": str(-_finite_number(source_label[3] or 0, "symbol pin label rotation") % 360), prefix + "Anchor": source_label[5], prefix + "Font_mm": str(font_mm), prefix + "Font": font, prefix + "Font_Defaulted": str(defaulted).lower()})
    decoration = {"unit": unit, "source_pin_number": number, "Conversion_Policy": CONVERSION_POLICY}
    dot_visible = len(segments) > 5 and _source_visible(_field(segments[5], 0))
    clock_visible = len(segments) > 6 and _source_visible(_field(segments[6], 0))
    props.update(Source_Pin_Dot=str(dot_visible).lower(), Source_Pin_Clock=str(clock_visible).lower())
    if dot_visible:
        dx, dy = [_finite_number(value, "source pin dot") for value in segments[5][1:3]]
        component.graphics.append(Graphic("ellipse", "symbol", [_source_point(dx - 3, dy - 3, origin), _source_point(dx + 3, dy + 3, origin)], 0, properties=dict(decoration, source_pin_decoration="dot", fill="none", stroke=_field(path, 1))))
    if clock_visible:
        component.graphics.extend(_path_graphics(_field(segments[6], 1), origin, "symbol", 0, dict(decoration, source_pin_decoration="clock", fill="none", stroke=_field(path, 1))))
    component.pins.append(Pin(number=number, name=name[4], type={0: "Undefined", 1: "Input", 2: "Output", 3: "Bidirectional", 4: "Power"}[electrical_type], unit=unit, hidden=not _source_visible(settings[1]), x=hotpoint[0], y=hotpoint[1], side={0: "right", 90: "top", 180: "left", 270: "bottom"}[rotation], length=math.dist(start, end), properties=props))


def _adapt_pads(source: dict[str, Any], footprint: Footprint, library: LibraryIR) -> None:
    origin = _source_origin(source, "footprint")
    footprint.properties["Source_Footprint_Origin_EE"] = json.dumps(origin)
    seen = set()
    holes = 0
    for record in _source_shapes(source, "footprint"):
        fields = record.split("~")
        if fields[0] == "VIA":
            raise CatalogError("Footprint-local vias require manual conversion and are not supported by this generator.", error_code="unsupported_geometry")
        if fields[0] == "HOLE":
            holes += 1
            x, y = _source_point(_field(fields, 1), _field(fields, 2), origin)
            diameter = _finite_number(_field(fields, 3), "source mounting hole") * EE_MM
            if diameter <= 0:
                raise CatalogError("A source mounting hole has an invalid diameter.")
            footprint.pads.append(Pad(number=f"MH{holes}", x=x, y=y, kind="thru_hole", shape="circle", size=(diameter, diameter), hole_diameter=diameter, layers=[], role="npth", properties={"plated": "false", "Source_Copper_Absent": "true", "Conversion_Policy": CONVERSION_POLICY, "EasyEDA_Source_Record": record}))
            continue
        if fields[0] != "PAD":
            continue
        number = fields[8].strip()
        if not number:
            raise CatalogError("A copper pad has no electrical number; this footprint requires manual review.")
        if number in seen:
            raise CatalogError(f"Multiple physical pads share pin {number}; this footprint requires a split-pad capable generator.", error_code="unsupported_geometry")
        seen.add(number)
        width = _finite_number(fields[4], "source pad width") * EE_MM
        height = _finite_number(fields[5], "source pad height") * EE_MM
        if fields[1].upper() == "ELLIPSE" and not math.isclose(width, height, abs_tol=1e-12):
            raise CatalogError("A non-circular elliptical pad cannot be represented faithfully by the current Cadence generator.", error_code="unsupported_geometry")
        shape = {"RECT": "rect", "ELLIPSE": "circle", "OVAL": "oval"}[fields[1].upper()]
        layer = _source_integer(fields[6], "source pad layer")
        if layer not in {1, 2, 11}:
            raise CatalogError(f"Unsupported electrical pad layer {layer}.")
        hole = _finite_number(fields[9], "source drill radius") * 2 * EE_MM
        if hole < 0:
            raise CatalogError(f"Pad {number} has a negative source drill diameter.")
        if layer == 11 and hole <= 0:
            raise CatalogError(f"Multilayer pad {number} has no supported drill definition.")
        if width <= 0 or height <= 0:
            raise CatalogError(f"Pad {number} has invalid dimensions.")
        slot = None
        hole_length = _finite_number(fields[13] or 0, "source drill length") * EE_MM
        if hole_length < 0:
            raise CatalogError(f"Pad {number} has a negative source drill length.")
        if hole > 0 and fields[15].upper() not in {"Y", "N"}:
            raise CatalogError(f"Pad {number} is missing its original drill plating definition.")
        if hole_length > hole:
            slot = (hole, hole_length)
        properties = {"plated": str(fields[15].upper() == "Y").lower(), "Source_Pad_Layer": str(layer), "EasyEDA_Pad_Id": fields[12], "Conversion_Policy": CONVERSION_POLICY, "EasyEDA_Source_Record": record, "Source_Pad_Outline_EE": fields[10], "Source_Pad_Trailing_Fields": json.dumps(fields[17:])}
        if slot:
            properties["Source_Slot_Shape"] = "oval"
        if fields[14]:
            drill_points = _source_points(fields[14], origin, "source drill points")
            properties["Source_Drill_Points_mm"] = json.dumps(drill_points)
            if len(drill_points) != 2:
                raise CatalogError(f"Pad {number} has unsupported source drill points.", error_code="unsupported_geometry" if len(drill_points) > 2 else "")
            center = ((drill_points[0][0] + drill_points[1][0]) / 2, (drill_points[0][1] + drill_points[1][1]) / 2)
            pad_center = _source_point(fields[2], fields[3], origin)
            properties["Source_Drill_Offset_mm"] = json.dumps([center[0] - pad_center[0], center[1] - pad_center[1]])
            properties["Source_Drill_Rotation_deg"] = str(math.degrees(math.atan2(drill_points[1][1] - drill_points[0][1], drill_points[1][0] - drill_points[0][0])) % 180)
        x, y = _source_point(fields[2], fields[3], origin)
        footprint.pads.append(Pad(
            number=number,
            x=x,
            y=y,
            rotation=(-_finite_number(fields[11], "source pad rotation")) % 360,
            kind="thru_hole" if hole > 0 or layer == 11 else "smd",
            shape=shape,
            size=(width, height),
            hole_diameter=hole,
            slot_size=slot,
            layers=["all"] if layer == 11 else ["top" if layer == 1 else "bottom"],
            properties=properties,
        ))
    if not seen:
        raise CatalogError("The selected footprint has no electrical pads.")
    library.warnings.append("The source PAD records do not provide documented solder-mask/paste aperture definitions. Unknown per-pad apertures are omitted; explicit mask/paste graphics are preserved. Undocumented trailing PAD fields are retained without guessing their meaning.")


def _adapt_graphics(source: dict[str, Any], footprint: Footprint, library: LibraryIR) -> None:
    origin = _source_origin(source, "footprint")
    layers = {3: "silkscreen_top", 4: "silkscreen_bottom", 5: "paste_top", 6: "paste_bottom", 7: "mask_top", 8: "mask_bottom", 10: "board_outline", 12: "document_top", 13: "assembly_top", 14: "assembly_bottom", 15: "mechanical", 99: "source_component_shape", 100: "source_lead_shape", 101: "source_component_polarity"}
    names = {}
    for layer in source.get("layers") or []:
        values = str(layer).split("~")
        if len(values) > 1:
            names[values[0]] = values[1]
    for record in _source_shapes(source, "footprint"):
        fields = record.split("~")
        command = fields[0]
        if command in {"PAD", "HOLE", "SVGNODE"}:
            continue
        layer_index = {"TRACK": 2, "CIRCLE": 5, "RECT": 5, "SOLIDREGION": 1, "ARC": 2, "TEXT": 7}.get(command)
        if layer_index is None:
            raise CatalogError(f"Unsupported source footprint record {command!r}; no substitute geometry was generated.")
        layer_id = _source_integer(_field(fields, layer_index), "footprint graphic layer")
        if layer_id in {1, 2, 11} or 21 <= layer_id <= 52:
            raise CatalogError("This footprint contains copper graphics outside its numbered pads; faithful conversion of these copper graphics is not yet supported.", error_code="unsupported_geometry")
        if layer_id not in layers:
            raise CatalogError(f"Unsupported original footprint layer {layer_id}; no layer was substituted or omitted.")
        target = layers[layer_id]
        props = {"Conversion_Policy": CONVERSION_POLICY, "EasyEDA_Source_Record": record, "source_layer_id": str(layer_id), "source_layer_name": names.get(str(layer_id), "")}
        if command == "TRACK":
            points = _source_points(_field(fields, 4), origin, "source track")
            footprint.graphics.append(Graphic("line", target, points, _source_width(fields, 1), properties=props))
        elif command == "CIRCLE":
            center = _source_point(_field(fields, 1), _field(fields, 2), origin)
            radius = _finite_number(_field(fields, 3), "source circle radius") * EE_MM
            if radius <= 0:
                raise CatalogError("A source footprint circle has an invalid radius.")
            props["radius_mm"] = str(radius)
            footprint.graphics.append(Graphic("circle", target, [center], _source_width(fields, 4), properties=props))
        elif command == "RECT":
            raise CatalogError("The original footprint RECT record has no unambiguous documented fill/stroke semantics supported by this converter; no filled rectangle or substitute outline was invented.")
        elif command == "SOLIDREGION":
            region_type = _field(fields, 4)
            if region_type not in {"solid", "cutout", "npth"}:
                raise CatalogError(f"Unsupported source region type {region_type!r}.")
            props.update(region_type=region_type, filled=str(region_type == "solid").lower())
            path = _field(fields, 3).strip()
            if not re.search(r"[A-DF-Za-df-z]", path):
                points = _source_points(path, origin, "source region")
                if points[0] != points[-1]:
                    points.append(points[0])
                graphics = [Graphic("region", target, points, 0, properties=props)]
            else:
                graphics = _path_graphics(path, origin, target, 0, props, filled=region_type == "solid", closed_region=True)
            footprint.graphics.extend(graphics)
            if region_type != "solid":
                _warning(library, f"Source layer {layer_id} contains {region_type} regions. Their original negative-region role is preserved and requires target-format support; they must not become positive filled shapes.")
        elif command == "ARC":
            footprint.graphics.extend(_path_graphics(_field(fields, 4), origin, target, _source_width(fields, 1), props))
        elif command == "TEXT":
            if len(fields) < 14:
                raise CatalogError("A source footprint text record is incomplete.")
            x, y = _source_point(fields[2], fields[3], origin)
            visible = fields[12].lower() != "none"
            props.update(text_type=fields[1], visible=str(visible).lower(), mirrored=str(fields[6].lower() not in {"", "none", "0", "false"}).lower(), height_mm=str(_finite_number(fields[9], "source text height") * EE_MM), font_size_mm=str(_finite_number(fields[9], "source text height") * EE_MM), stroke_width_mm=str(_source_width(fields, 4)), source_text_path=fields[11])
            if fields[11] and visible:
                glyphs = _path_graphics(fields[11], origin, target, _source_width(fields, 4), dict(props, source_text_glyph="true"))
                footprint.graphics.extend(glyphs)
                props["source_geometry_exported"] = "true"
            elif visible:
                _warning(library, "A source footprint text has no original glyph path or complete font metrics; no replacement font geometry was invented.")
            footprint.graphics.append(Graphic("text", target, text=fields[10], x=x, y=y, rotation=-_finite_number(fields[5] or 0, "source text rotation") % 360, width=_source_width(fields, 4), properties=props))


def _adapt_source_model(source: dict[str, Any], footprint: Footprint, library: LibraryIR, lookup: dict[str, Any]) -> None:
    nodes = []
    for record in _source_shapes(source, "footprint"):
        if record.startswith("SVGNODE~"):
            try:
                node = json.loads(record.split("~", 1)[1])
            except (ValueError, TypeError) as exc:
                raise CatalogError("The source 3D-model record is malformed.") from exc
            if not isinstance(node, dict) or not isinstance(node.get("attrs"), dict):
                raise CatalogError("The source 3D-model record has no attributes.")
            nodes.append(node)
    if len(nodes) > 1:
        raise CatalogError("Multiple original 3D-model bindings require explicit assembly conversion.")
    attrs = nodes[0]["attrs"] if nodes else {}
    source_model_id = str(attrs.get("uuid") or "")
    step_id = str(lookup.get("step") or source_model_id)
    if not step_id:
        _warning(library, "The original catalog record has no 3D-model/STEP binding; no replacement model was generated.")
        return
    footprint.properties.update(EasyEDA_3D_Model_Id=step_id, EasyEDA_3D_Model_Title=str(attrs.get("title") or ""), Source_3D_Model_Id=source_model_id, Source_3D_Attributes_JSON=json.dumps(attrs, ensure_ascii=False, sort_keys=True))
    footprint.properties["Source_3D_Transform_Complete"] = "false"
    if not source_model_id or source_model_id != step_id:
        _warning(library, "The STEP lookup and original 3D-model binding are missing or differ; the original transform was not applied to an unverified model.")
        return
    missing = [field for field in ("c_origin", "c_rotation", "z") if attrs.get(field) in (None, "")]
    if missing:
        _warning(library, "The original 3D-model binding is missing transform fields: " + ", ".join(missing) + "; no offsets or rotations were inferred.")
        return
    origin_values = str(attrs["c_origin"]).split(",")
    rotation_values = str(attrs["c_rotation"]).split(",")
    if len(origin_values) != 2 or len(rotation_values) != 3:
        raise CatalogError("The original 3D-model transform has malformed dimensions.")
    origin = _source_origin(source, "footprint")
    x, y = _source_point(*origin_values, origin)
    offset = [x, y, _finite_number(attrs["z"], "source 3D z") * EE_MM]
    rotation = [_finite_number(value, "source 3D rotation") for value in rotation_values]
    footprint.properties.update(Catalog_3D_Offset_mm=json.dumps(offset), Catalog_3D_Rotation_deg=json.dumps(rotation), Source_3D_Transform_Complete="true", Source_3D_Transform_Convention="EasyEDA SVGNODE c_origin relative to footprint head; Y reflected; original Euler rotation degrees; no outline correction or scale")


def _path_graphics(path: str, origin: tuple[float, float], layer: str, width: float, properties: dict[str, str], filled: bool = False, closed_region: bool = False) -> list[Graphic]:
    tokens = re.sub(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", "", path)
    if re.sub(r"[MmLlHhVvAaZzCcSsQqTt\s,]", "", tokens):
        raise CatalogError("An original SVG path contains unsupported commands or tokens; no partial path was generated.")
    try:
        from svgelements import Arc, Close, CubicBezier, Line, Move, Path as SvgPath, QuadraticBezier
    except ImportError as exc:
        raise CatalogError("Install the capture plugin dependency svgelements==1.9.6 to preserve original paths.") from exc
    try:
        segments = list(SvgPath(path))
    except (ValueError, TypeError, IndexError, ZeroDivisionError) as exc:
        raise CatalogError("An original SVG path is malformed; no replacement was generated.") from exc
    if not segments:
        raise CatalogError("An original SVG path is empty or unsupported.")
    output = []
    points = []
    subpaths = 0
    def point(value: Any) -> tuple[float, float]:
        return _source_point(value.x, value.y, origin)
    def flush() -> None:
        nonlocal points
        if len(points) >= 2:
            output.append(Graphic("line", layer, points, width, properties=dict(properties)))
        points = []
    for segment in segments:
        if isinstance(segment, Move):
            flush()
            subpaths += 1
            if (filled or closed_region) and subpaths > 1:
                raise CatalogError("A compound original filled region requires explicit hole/winding conversion; no substitute was generated.", error_code="unsupported_geometry")
            points = [point(segment.end)]
        elif isinstance(segment, (Line, Close)):
            if not points:
                points = [point(segment.start)]
            points.append(point(segment.end))
        elif isinstance(segment, Arc):
            if filled or closed_region:
                raise CatalogError("An original curved filled region cannot yet be represented faithfully; no polygon approximation was generated.", error_code="unsupported_geometry")
            flush()
            if not math.isclose(segment.rx, segment.ry, rel_tol=1e-10, abs_tol=1e-10):
                raise CatalogError("An original elliptical arc cannot yet be represented faithfully; no circular substitute was generated.", error_code="unsupported_geometry")
            center = point(segment.center)
            props = dict(properties, center_x_mm=str(center[0]), center_y_mm=str(center[1]), clockwise=str(segment.sweep > 0).lower())
            output.append(Graphic("arc", layer, [point(segment.start), point(segment.end)], width, properties=props))
        else:
            raise CatalogError(f"Unsupported original SVG {type(segment).__name__}; no approximation was generated.",
                               error_code="unsupported_geometry" if isinstance(segment, (CubicBezier, QuadraticBezier)) else "")
    flush()
    if not output:
        raise CatalogError("An original SVG path has no supported drawable geometry.")
    if filled or closed_region:
        if len(output) != 1 or len(output[0].points) < 3:
            raise CatalogError("The original filled region does not have one closed polygon.")
        result = output[0]
        result.kind = "region"
        if result.points[0] != result.points[-1]:
            result.points.append(result.points[0])
        result.properties["filled"] = str(filled).lower()
    return output


def _source_shapes(data: dict[str, Any], label: str) -> list[str]:
    shapes = data.get("shape")
    if not isinstance(shapes, list) or not all(isinstance(shape, str) for shape in shapes):
        raise CatalogError(f"The original {label} shape data must be a list of records.")
    return shapes


def _source_origin(data: dict[str, Any], label: str) -> tuple[float, float]:
    head = data.get("head")
    if not isinstance(head, dict) or head.get("x") is None or head.get("y") is None:
        raise CatalogError(f"The original {label} has no defined source origin.")
    return _finite_number(head["x"], f"{label} origin"), _finite_number(head["y"], f"{label} origin")


def _source_point(x: Any, y: Any, origin: tuple[float, float]) -> tuple[float, float]:
    return ((_finite_number(x, "source geometry") - origin[0]) * EE_MM, -(_finite_number(y, "source geometry") - origin[1]) * EE_MM)


def _source_points(text: str, origin: tuple[float, float], label: str) -> list[tuple[float, float]]:
    values = text.replace(",", " ").split()
    if len(values) < 4 or len(values) % 2:
        raise CatalogError(f"The {label} has malformed coordinate pairs.")
    return [_source_point(values[index], values[index + 1], origin) for index in range(0, len(values), 2)]


def _field(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def _source_width(fields: list[str], index: int) -> float:
    width = _finite_number(_field(fields, index), "source stroke width") * EE_MM
    if width < 0:
        raise CatalogError("A source stroke has a negative width.")
    return width


def _source_style(properties: dict[str, str], fields: list[str], index: int) -> None:
    properties.update(stroke=_field(fields, index), stroke_style=_field(fields, index + 2), fill=_field(fields, index + 3))


def _source_visible(value: str) -> bool:
    return str(value).lower() in {"show", "1", "true", "yes", "on"}


def _source_font(font: str, size: str, library: LibraryIR) -> tuple[str, float, bool]:
    defaulted = not font or not size
    if defaulted:
        _warning(library, "Blank source symbol label font fields resolve to the documented EasyEDA defaults Verdana and 7pt: " + SCHEMATIC_FORMAT_URL + "#pin")
    value = _finite_number((size or "7").removesuffix("pt"), "source font size")
    if value < 0:
        raise CatalogError("The source symbol label has a negative font size.")
    return font or "Verdana", value * 25.4 / 72, defaulted


def _warning(library: LibraryIR, message: str) -> None:
    if message not in library.warnings:
        library.warnings.append(message)


def _validate_pad_shapes(shapes: list[str]) -> None:
    if not isinstance(shapes, list) or not all(isinstance(shape, str) for shape in shapes):
        raise CatalogError("The footprint shape data must be a list of records.")
    for shape in shapes:
        fields = shape.split("~")
        if fields[0] == "PAD":
            if len(fields) < 17:
                raise CatalogError("The original footprint PAD record is incomplete.")
            if fields[1].upper() not in {"RECT", "ELLIPSE", "OVAL"}:
                raise CatalogError("A custom/polygon pad cannot be represented faithfully by the current Cadence generator.",
                                   error_code="unsupported_geometry" if fields[1].upper() == "POLYGON" else "")
            for index in (2, 3, 4, 5, 6, 9, 11):
                _finite_number(fields[index], "pad")
            if fields[13]:
                _finite_number(fields[13], "drill slot")
        if fields[0] not in {"PAD", "TRACK", "HOLE", "VIA", "CIRCLE", "ARC", "RECT", "TEXT", "SVGNODE", "SOLIDREGION"}:
            raise CatalogError(f"Unsupported footprint record {fields[0]!r}; the geometry requires manual review.")


def _finite_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise CatalogError(f"The {label} contains an invalid numeric coordinate.") from exc
    if not math.isfinite(result):
        raise CatalogError(f"The {label} contains a non-finite coordinate.")
    return result


def _source_integer(value: Any, label: str) -> int:
    number = _finite_number(value, label)
    if not number.is_integer():
        raise CatalogError(f"The {label} must be an integer; its source value was not rounded.")
    return int(number)


def _catalog_part_from_search_row(row: dict[str, Any], part_id: str | None = None) -> CatalogPart:
    """Convert one official JLCPCB search row without downloading CAD data."""
    part_id = str(part_id or row.get("componentCode") or "").upper()
    return CatalogPart(
        part_id=part_id,
        name=str(row.get("componentModelEn") or row.get("componentName") or part_id),
        manufacturer=str(row.get("componentBrandEn") or ""),
        description=str(row.get("describe") or ""),
        package=str(row.get("componentSpecificationEn") or ""),
        datasheet=str(row.get("dataManualOfficialLink") or row.get("dataManualUrl") or ""),
        source_url=str(row.get("lcscGoodsUrl") or f"https://www.lcsc.com/search?q={part_id}"),
        stock=_optional_int(row.get("stockCount")),
    )


def _component_metadata(raw: dict[str, Any], lcsc_id: str, lookup: dict[str, Any]) -> CatalogPart:
    params = raw.get("dataStr", {}).get("head", {}).get("c_para", {})
    package = raw.get("packageDetail") or {}
    package_data = package.get("dataStr") or {}
    package_params = package_data.get("head", {}).get("c_para", {})
    return CatalogPart(
        part_id=lcsc_id,
        name=str(params.get("Manufacturer Part") or params.get("BOM_Manufacturer Part") or raw.get("title") or lcsc_id),
        manufacturer=str(params.get("Manufacturer") or params.get("BOM_Manufacturer") or ""),
        description=str(raw.get("description") or ""),
        package=str(package.get("title") or params.get("package") or ""),
        datasheet=str(package_params.get("link") or (raw.get("lcsc") or {}).get("url") or ""),
        source_url=f"https://www.lcsc.com/search?q={lcsc_id}",
        has_3d=bool(lookup.get("step") or package_data.get("head", {}).get("uuid_3d") or any(str(s).startswith("SVGNODE~") for s in package_data.get("shape", []))),
        has_step=bool(lookup.get("step")),
    )


def _normalize_id(value: str) -> str:
    value = str(value).strip().upper()
    if not LCSC_ID.fullmatch(value):
        raise CatalogError("A valid LCSC part number such as C25804 is required.")
    return value


def _check_identity(raw: dict[str, Any], expected: str) -> None:
    actual = str((raw.get("lcsc") or raw.get("szlcsc") or {}).get("number") or "").upper()
    if actual and actual != expected:
        raise CatalogError(f"The catalog returned {actual} while {expected} was requested.")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _unit_name(index: int) -> str:
    value = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value
