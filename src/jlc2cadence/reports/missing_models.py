from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .. import __version__
from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.normalize import model_match_candidates
from ..ir.schema import Component, Footprint, LibraryIR, Model3D


_MAX_QUERY_BASE_LENGTH = 96
_MAX_DESCRIPTION_QUERY_TERMS = 2
_DESCRIPTION_PACKAGE_WORDS = {
    "bga",
    "capacitor",
    "connector",
    "crystal",
    "dfn",
    "diode",
    "header",
    "inductor",
    "led",
    "lga",
    "module",
    "package",
    "qfn",
    "qfp",
    "resistor",
    "socket",
    "sop",
    "sot",
    "switch",
    "transistor",
    "tssop",
}


def build_missing_3d_report(
    library: LibraryIR,
    model_assets: list[Model3D],
    model_aliases: dict[str, str] | None = None,
    input_path: Path | None = None,
    models_path: Path | None = None,
    alias_path: Path | None = None,
) -> dict[str, Any]:
    model_aliases = model_aliases or {}
    linked = [footprint for footprint in library.footprints if footprint.model3d is not None]
    missing = [footprint for footprint in library.footprints if footprint.model3d is None]
    matched_files = {
        str(footprint.model3d.file)
        for footprint in linked
        if footprint.model3d is not None
    }
    available_models = [
        _model_row(model, matched=str(model.file) in matched_files)
        for model in model_assets
    ]
    components_by_footprint = _components_by_footprint(library.components)

    missing_rows = []
    alias_template: dict[str, str] = {}
    alias_conflicts: list[dict[str, str]] = []
    for footprint in missing:
        row = _missing_footprint_row(
            footprint,
            model_aliases,
            available_models,
            components_by_footprint.get(footprint.name, []),
        )
        alias_key = _preferred_alias_key(footprint)
        target = f"REPLACE_WITH_{cadence_safe_name(footprint.name)}.step"
        if alias_key:
            if alias_key in alias_template and alias_template[alias_key] != target:
                alias_conflicts.append(
                    {
                        "key": alias_key,
                        "existing_target": alias_template[alias_key],
                        "conflicting_target": target,
                        "footprint": footprint.name,
                    }
                )
            else:
                alias_template[alias_key] = target
        row["alias_key"] = alias_key
        row["alias_target_template"] = target if alias_key else ""
        missing_rows.append(row)

    if not library.footprints:
        status = "no_footprints"
    elif not missing:
        status = "complete"
    elif linked:
        status = "partial"
    else:
        status = "missing"

    return {
        "version": f"v{__version__}",
        "status": status,
        "input": str(input_path.expanduser().resolve()) if input_path else "",
        "models": str(models_path.expanduser().resolve()) if models_path else "",
        "model_aliases": str(alias_path.expanduser().resolve()) if alias_path else "",
        "counts": {
            "components": len(library.components),
            "footprints": len(library.footprints),
            "linked_models": len(linked),
            "missing_models": len(missing),
            "model_assets": len(model_assets),
            "unmatched_model_assets": sum(1 for item in available_models if not item["matched"]),
        },
        "missing_footprints": missing_rows,
        "linked_footprints": [
            _linked_footprint_row(footprint, model_aliases)
            for footprint in linked
        ],
        "available_model_files": available_models,
        "model_aliases_template": alias_template,
        "model_aliases_template_conflicts": alias_conflicts,
        "library_errors": list(getattr(library, "errors", [])),
        "library_warnings": list(library.warnings),
    }


def write_missing_3d_markdown(report: dict[str, Any], path: Path) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_missing_3d_markdown(report), encoding="utf-8")
    return path


def render_missing_3d_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# jlc2cadence missing 3D model report",
        "",
        f"- Version: {report['version']}",
        f"- Status: {report['status']}",
        f"- Input: {report['input'] or 'not recorded'}",
        f"- Models: {report['models'] or 'not supplied'}",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Components | {counts['components']} |",
        f"| Footprints | {counts['footprints']} |",
        f"| Linked STEP/STP/OBJ models | {counts['linked_models']} |",
        f"| Missing 3D models | {counts['missing_models']} |",
        f"| Available model files | {counts['model_assets']} |",
        f"| Unmatched model files | {counts['unmatched_model_assets']} |",
        "",
        "## Missing Footprints",
        "",
    ]
    missing = report["missing_footprints"]
    if missing:
        lines.extend(
            [
                "| Footprint | EasyEDA model ID | Model/title | Component part candidates | Candidate keys | Candidate filenames | Sourcing queries | Alias key |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in missing:
            lines.append(
                "| {footprint} | {model_ids} | {model_titles} | {component_parts} | {candidate_keys} | {candidate_filenames} | {sourcing_queries} | {alias_key} |".format(
                    footprint=_md(row["footprint"]),
                    model_ids=_md(", ".join(row["candidate_model_ids"]) or "-"),
                    model_titles=_md(", ".join(row["model_titles"]) or "-"),
                    component_parts=_md(", ".join(row["component_part_candidates"]) or "-"),
                    candidate_keys=_md(", ".join(row["candidate_keys"]) or "-"),
                    candidate_filenames=_md(", ".join(row["candidate_filenames"]) or "-"),
                    sourcing_queries=_md(", ".join(row.get("sourcing_queries", [])) or "-"),
                    alias_key=_md(row["alias_key"] or "-"),
                )
            )
    else:
        lines.append("No footprint is missing a linked 3D model.")

    lines.extend(["", "## Available Model Files", ""])
    models = report["available_model_files"]
    if models:
        lines.extend(
            [
                "| Filename | Format | Key | Matched | Path |",
                "|---|---|---|---:|---|",
            ]
        )
        for model in models:
            lines.append(
                "| {filename} | {format} | {key} | {matched} | {path} |".format(
                    filename=_md(model["filename"]),
                    format=_md(model["format"]),
                    key=_md(model["key"]),
                    matched="yes" if model["matched"] else "no",
                    path=_md(model["path"]),
                )
            )
    else:
        lines.append("No STEP/STP/OBJ model files were found.")

    lines.extend(["", "## model_aliases.json Template", ""])
    if report["model_aliases_template"]:
        lines.extend(
            [
                "Edit the target filenames, save the JSON next to the model files, then rerun conversion with `--model-aliases`.",
                "",
                "```json",
                json.dumps(report["model_aliases_template"], indent=2, ensure_ascii=False),
                "```",
            ]
        )
    else:
        lines.append("No alias rows are needed.")

    if report["model_aliases_template_conflicts"]:
        lines.extend(["", "Template conflicts:", ""])
        lines.extend(
            f"- `{item['key']}` is used by `{item['footprint']}` but has more than one suggested target."
            for item in report["model_aliases_template_conflicts"]
        )

    if report["library_errors"] or report["library_warnings"]:
        lines.extend(["", "## Parser Diagnostics", ""])
        lines.extend(f"- Error: {error}" for error in report["library_errors"])
        lines.extend(f"- Warning: {warning}" for warning in report["library_warnings"])

    return "\n".join(lines) + "\n"


def _missing_footprint_row(
    footprint: Footprint,
    model_aliases: dict[str, str],
    available_models: list[dict[str, Any]],
    components: list[Component],
) -> dict[str, Any]:
    candidates = model_match_candidates(footprint, model_aliases=model_aliases)
    candidate_keys = _unique(str(item["key"]) for item in candidates if item.get("key"))
    candidate_values = _unique(
        str(item["value"])
        for item in candidates
        if item.get("source") in {"EasyEDA_3D_Model_Id", "EasyEDA_3D_Model_Id:part"}
        and item.get("value")
    )
    model_titles = _unique(
        str(value)
        for value in [
            footprint.properties.get("EasyEDA_3D_Model_Title", ""),
            footprint.properties.get("Model3D", ""),
            footprint.properties.get("Model", ""),
        ]
        if str(value).strip()
    )
    component_names = _unique(component.name for component in components)
    component_part_candidates = _component_part_candidates(components)
    candidate_keys = _unique(
        [
            *candidate_keys,
            *(normalized_key(value) for value in component_names),
            *(normalized_key(value) for value in component_part_candidates),
        ]
    )
    fuzzy_files = []
    for model in available_models:
        model_key = str(model.get("key", ""))
        if any(key and (key in model_key or model_key in key) for key in candidate_keys):
            fuzzy_files.append(model["filename"])
    sourcing_queries = _sourcing_queries(
        footprint,
        component_part_candidates=component_part_candidates,
        candidate_model_ids=candidate_values,
        model_titles=model_titles,
    )
    return {
        "footprint": footprint.name,
        "source_file": footprint.source_file,
        "description": footprint.description,
        "easyeda_model_id": footprint.properties.get("EasyEDA_3D_Model_Id", ""),
        "candidate_model_ids": candidate_values,
        "model_titles": model_titles,
        "component_names": component_names,
        "component_part_candidates": component_part_candidates,
        "candidate_keys": candidate_keys,
        "candidate_filenames": _unique(fuzzy_files),
        "available_model_filenames": _unique(str(item["filename"]) for item in available_models),
        "sourcing_queries": sourcing_queries,
        "sourcing_urls": [_search_url(query) for query in sourcing_queries],
    }


def _components_by_footprint(components: list[Component]) -> dict[str, list[Component]]:
    grouped: dict[str, list[Component]] = {}
    for component in components:
        footprint_name = str(component.footprint or "").strip()
        if footprint_name:
            grouped.setdefault(footprint_name, []).append(component)
    return grouped


def _component_part_candidates(components: list[Component]) -> list[str]:
    allowed_keys = {
        "lcscpart",
        "lcscpartnumber",
        "supplierpart",
        "supplierpartnumber",
        "supplierpartno",
        "manufacturerpart",
        "manufacturerpartnumber",
        "manufacturerpartno",
        "mpn",
        "partnumber",
        "partno",
    }
    values: list[str] = []
    for component in components:
        for key, value in component.properties.items():
            normalized_property = normalized_key(key)
            if normalized_property not in allowed_keys:
                continue
            text = str(value or "").strip()
            if text:
                values.append(text)
    return _unique(values)


def _sourcing_queries(
    footprint: Footprint,
    *,
    component_part_candidates: list[str],
    candidate_model_ids: list[str],
    model_titles: list[str],
) -> list[str]:
    values: list[str] = []
    values.extend(_query_with_suffix(value, "STEP 3D model") for value in component_part_candidates)
    values.extend(_query_with_suffix(value, "STEP 3D model") for value in model_titles)
    values.extend(_query_with_suffix(value, "EasyEDA 3D STEP model") for value in candidate_model_ids)
    values.append(_query_with_suffix(footprint.name, "STEP 3D model"))
    values.extend(
        _query_with_suffix(value, "STEP 3D model")
        for value in _description_query_terms(footprint.description)
    )
    return _unique(value for value in values if str(value or "").strip())[:5]


def _query_with_suffix(value: object, suffix: str) -> str:
    text = _clean_query_base(value)
    if not text:
        return ""
    return f"{text} {suffix}"


def _clean_query_base(value: object) -> str:
    text = " ".join(str(value or "").replace("\u00a0", " ").split()).strip(" ,;|")
    if not text or len(text) > _MAX_QUERY_BASE_LENGTH:
        return ""
    return text


def _description_query_terms(description: str) -> list[str]:
    text = " ".join(str(description or "").replace("\u00a0", " ").split()).strip(" ,;|")
    if not text:
        return []
    if len(text) <= _MAX_QUERY_BASE_LENGTH and ";" not in text and "|" not in text:
        return [text]
    terms = []
    for chunk in re.split(r"[;|\r\n]+", text):
        cleaned = _clean_query_base(chunk)
        if cleaned and _looks_like_sourcing_term(cleaned):
            terms.append(cleaned)
    return _unique(terms)[:_MAX_DESCRIPTION_QUERY_TERMS]


def _looks_like_sourcing_term(value: str) -> bool:
    text = value.strip()
    if len(text) < 3:
        return False
    if len(text.split()) > 8:
        return False
    if any(char.isdigit() for char in text):
        return True
    normalized = normalized_key(text)
    return any(word in normalized for word in _DESCRIPTION_PACKAGE_WORDS)


def _search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _linked_footprint_row(
    footprint: Footprint,
    model_aliases: dict[str, str],
) -> dict[str, Any]:
    candidates = model_match_candidates(footprint, model_aliases=model_aliases)
    candidate_keys = _unique(str(item["key"]) for item in candidates if item.get("key"))
    candidate_values = _unique(
        str(item["value"])
        for item in candidates
        if item.get("source") in {"EasyEDA_3D_Model_Id", "EasyEDA_3D_Model_Id:part"}
        and item.get("value")
    )
    model_titles = _unique(
        str(value)
        for value in [
            footprint.properties.get("EasyEDA_3D_Model_Title", ""),
            footprint.properties.get("Model3D", ""),
            footprint.properties.get("Model", ""),
        ]
        if str(value).strip()
    )
    return {
        "footprint": footprint.name,
        "source_file": footprint.source_file,
        "easyeda_model_id": footprint.properties.get("EasyEDA_3D_Model_Id", ""),
        "candidate_model_ids": candidate_values,
        "model_titles": model_titles,
        "candidate_keys": candidate_keys,
        "alias_key": _preferred_alias_key(footprint),
        "model_file": _model_display_name(footprint.model3d),
        "model_path": footprint.model3d.file if footprint.model3d else "",
        "model_format": footprint.model3d.format if footprint.model3d else "",
    }


def _preferred_alias_key(footprint: Footprint) -> str:
    for key in [
        "EasyEDA_3D_Model_Id",
        "Model3D",
        "Model",
        "EasyEDA_3D_Model_Title",
    ]:
        value = str(footprint.properties.get(key, "") or "").strip()
        if value:
            return value
    return footprint.name


def _model_row(model: Model3D, matched: bool) -> dict[str, Any]:
    source = Path(model.source_file or model.file)
    return {
        "filename": source.name,
        "path": str(source),
        "format": model.format,
        "key": normalized_key(model.key or source.stem),
        "matched": matched,
    }


def _model_display_name(model: Model3D | None) -> str:
    if model is None:
        return ""
    return Path(model.source_file or model.file).name


def _unique(values):
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
