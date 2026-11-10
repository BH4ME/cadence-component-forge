from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Pattern

from .. import __version__
from ..extractors.model_aliases import load_model_aliases
from ..ir.geometry import cadence_safe_name, normalized_key
from ..ir.normalize import model_match_id_candidates


STEP_SUFFIXES = {".step", ".stp"}
MODEL_SUFFIXES = {".step", ".stp", ".obj"}
PLACEHOLDER_MODEL_KIND = "placeholder_3d_envelope"


@dataclass
class ModelRepairKit:
    directory: Path
    report_path: Path
    aliases_path: Path
    models_dir: Path
    input_path: Path | None
    report: dict[str, Any]


@dataclass(frozen=True)
class ModelRule:
    rule_id: str
    match_regex: str
    model: str
    source: str
    pattern: Pattern[str]

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.rule_id,
            "match_regex": self.match_regex,
            "model": self.model,
            "source": self.source,
        }


def write_model_repair_kit(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    """Write a folder that can be filled with exported STEP files and reused as-is."""
    output_dir = output_dir.expanduser().resolve()
    models_dir = output_dir / "models"
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    copied_models = _copy_linked_models(report, models_dir)
    alias_mapping = _repair_alias_mapping(report, copied_models)
    report_copy = dict(report)
    report_copy["repair_kit"] = str(output_dir)
    report_copy["repair_kit_prefilled_models"] = list(copied_models.values())

    report_path = output_dir / "missing_3d.json"
    alias_path = output_dir / "model_aliases.json"
    csv_path = output_dir / "models_todo.csv"
    todo_markdown_path = output_dir / "models_todo.md"
    readme_path = output_dir / "README.md"

    report_path.write_text(
        json.dumps(report_copy, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    alias_path.write_text(
        json.dumps(alias_mapping, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_todo_csv(report, csv_path, alias_mapping)
    _write_todo_markdown(report, todo_markdown_path, alias_mapping)
    readme_path.write_text(
        _render_repair_kit_readme(report_copy, alias_path, models_dir),
        encoding="utf-8",
    )
    return {
        "directory": str(output_dir),
        "models": str(models_dir),
        "report": str(report_path),
        "aliases": str(alias_path),
        "todo_csv": str(csv_path),
        "todo_markdown": str(todo_markdown_path),
        "readme": str(readme_path),
    }


def load_model_repair_kit(path: Path) -> ModelRepairKit:
    directory = path.expanduser().resolve()
    report_path = directory / "missing_3d.json"
    aliases_path = directory / "model_aliases.json"
    models_dir = directory / "models"
    report: dict[str, Any] = {}
    if report_path.exists():
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            report = data
    return ModelRepairKit(
        directory=directory,
        report_path=report_path,
        aliases_path=aliases_path,
        models_dir=models_dir,
        input_path=_input_from_report(report, directory),
        report=report,
    )


def check_model_repair_kit(path: Path) -> dict[str, Any]:
    kit = load_model_repair_kit(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not kit.directory.exists():
        errors.append(f"Repair kit directory does not exist: {kit.directory}")
    if not kit.report_path.exists():
        errors.append(f"Repair kit report is missing: {kit.report_path}")
    if not kit.aliases_path.exists():
        errors.append(f"Repair kit alias file is missing: {kit.aliases_path}")
    if not kit.models_dir.exists():
        errors.append(f"Repair kit models directory is missing: {kit.models_dir}")
    if kit.input_path is None:
        warnings.append("Repair kit report does not record an input export path.")
    elif not kit.input_path.exists():
        errors.append(f"Repair kit input export does not exist: {kit.input_path}")

    alias_rows = []
    placeholder_aliases = []
    missing_targets = []
    non_step_targets = []
    non_file_targets = []
    if kit.aliases_path.exists():
        try:
            aliases = load_model_aliases(kit.aliases_path)
        except Exception as exc:
            errors.append(f"Could not read repair kit aliases: {exc}")
            aliases = None
        if aliases is not None:
            for row in aliases.rows:
                target = str(row.get("target", "") or "")
                target_path = _resolve_alias_target(target, kit.aliases_path.parent)
                item = {
                    "candidate": row.get("candidate", ""),
                    "target": target,
                    "target_path": str(target_path) if target_path else "",
                }
                alias_rows.append(item)
                if "REPLACE_WITH_" in target.upper():
                    placeholder_aliases.append(item)
                if not row.get("target_is_file"):
                    non_file_targets.append(item)
                    continue
                suffix = Path(target).suffix.lower()
                if suffix not in STEP_SUFFIXES:
                    non_step_targets.append(item)
                if target_path is None or not target_path.exists():
                    missing_targets.append(item)

    model_files = []
    if kit.models_dir.exists():
        model_files = sorted(
            str(path)
            for path in kit.models_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES
        )

    missing_footprints = list(kit.report.get("missing_footprints", []) or [])
    if missing_footprints and not alias_rows:
        warnings.append("Repair kit has missing footprints but no alias rows.")
    if placeholder_aliases:
        warnings.append("Some alias targets still contain REPLACE_WITH placeholders.")
    if missing_targets:
        warnings.append("Some alias target files do not exist.")
    if non_file_targets:
        warnings.append("Some alias targets are model keys rather than file paths.")
    if non_step_targets:
        warnings.append("Some alias targets are not STEP/STP files; complete Cadence 3D mapping requires STEP/STP.")

    if errors:
        status = "failed"
    elif placeholder_aliases or missing_targets or non_file_targets or non_step_targets:
        status = "incomplete"
    else:
        status = "ready"

    return {
        "version": f"v{__version__}",
        "status": status,
        "repair_kit": str(kit.directory),
        "input": str(kit.input_path) if kit.input_path else "",
        "input_exists": bool(kit.input_path and kit.input_path.exists()),
        "models_dir": str(kit.models_dir),
        "models_dir_exists": kit.models_dir.exists(),
        "report": str(kit.report_path),
        "report_exists": kit.report_path.exists(),
        "aliases": str(kit.aliases_path),
        "aliases_exists": kit.aliases_path.exists(),
        "counts": {
            "missing_footprints": len(missing_footprints),
            "alias_rows": len(alias_rows),
            "placeholder_aliases": len(placeholder_aliases),
            "missing_alias_targets": len(missing_targets),
            "non_file_alias_targets": len(non_file_targets),
            "non_step_alias_targets": len(non_step_targets),
            "model_files": len(model_files),
        },
        "placeholder_aliases": placeholder_aliases,
        "missing_alias_targets": missing_targets,
        "non_file_alias_targets": non_file_targets,
        "non_step_alias_targets": non_step_targets,
        "model_files": model_files,
        "errors": errors,
        "warnings": warnings,
    }


def sync_model_repair_kit(path: Path, write: bool = True) -> dict[str, Any]:
    """Update placeholder repair-kit aliases from clearly named STEP/STP files."""
    kit = load_model_repair_kit(path)
    before = check_model_repair_kit(path)
    errors = list(before.get("errors", []) or [])
    warnings: list[str] = []
    if errors:
        return _sync_result(kit, write, before, before, [], [], [], [], errors, warnings)

    try:
        alias_data, alias_mapping = _load_repair_alias_data(kit.aliases_path)
    except Exception as exc:
        errors.append(f"Could not read repair kit aliases: {exc}")
        return _sync_result(kit, write, before, before, [], [], [], [], errors, warnings)

    model_files = _sync_candidate_model_files(kit.models_dir)
    model_index = _index_model_files(model_files)
    rows_by_alias = _missing_rows_by_alias(kit.report)
    updated: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []

    for alias_key, row_group in rows_by_alias.items():
        if alias_key not in alias_mapping:
            unresolved.append(
                {
                    "candidate": alias_key,
                    "footprints": row_group["footprints"],
                    "reason": "alias_row_missing",
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                }
            )
            continue
        current_target = _alias_target_text(alias_mapping[alias_key])
        if "REPLACE_WITH_" not in current_target.upper():
            preserved.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                }
            )
            continue
        matches = _matching_model_files(row_group["candidate_keys"], model_index)
        if not matches:
            unresolved.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                    "reason": "no_matching_step_or_stp_file",
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                }
            )
            continue
        if len(matches) > 1:
            ambiguous.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                    "matches": [
                        {
                            "file": str(item["path"]),
                            "relative_target": _relative_alias_target(item["path"], kit.directory),
                            "matched_keys": sorted(item["matched_keys"]),
                        }
                        for item in matches
                    ],
                }
            )
            continue
        match = matches[0]
        new_target = _relative_alias_target(match["path"], kit.directory)
        _set_alias_target(alias_mapping, alias_key, new_target)
        updated.append(
            {
                "candidate": alias_key,
                "old_target": current_target,
                "new_target": new_target,
                "model_file": str(match["path"]),
                "footprints": row_group["footprints"],
                "matched_keys": sorted(match["matched_keys"]),
            }
        )

    if ambiguous:
        warnings.append("Some placeholder aliases matched more than one STEP/STP file and were left unchanged.")
    if unresolved:
        warnings.append("Some placeholder aliases could not be matched to a STEP/STP filename.")
    if write and updated:
        kit.aliases_path.write_text(
            json.dumps(alias_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    after = check_model_repair_kit(path) if write else before
    return _sync_result(kit, write, before, after, updated, unresolved, ambiguous, preserved, errors, warnings)


def import_model_repair_kit_models(
    path: Path,
    model_roots: list[Path],
    write: bool = True,
    model_rules: Path | None = None,
) -> dict[str, Any]:
    """Copy clear STEP/STP matches from external roots into a repair kit."""
    kit = load_model_repair_kit(path)
    before = check_model_repair_kit(path)
    errors = list(before.get("errors", []) or [])
    warnings: list[str] = []
    roots = [root.expanduser().resolve() for root in model_roots]
    if not roots:
        errors.append("At least one model root is required.")
    for root in roots:
        if not root.exists():
            errors.append(f"Model root does not exist: {root}")
    if errors:
        return _import_result(
            kit,
            write,
            roots,
            before,
            before,
            [],
            [],
            [],
            [],
            [],
            errors,
            warnings,
            model_rules=model_rules,
        )

    try:
        alias_data, alias_mapping = _load_repair_alias_data(kit.aliases_path)
    except Exception as exc:
        errors.append(f"Could not read repair kit aliases: {exc}")
        return _import_result(
            kit,
            write,
            roots,
            before,
            before,
            [],
            [],
            [],
            [],
            [],
            errors,
            warnings,
            model_rules=model_rules,
        )

    model_files = _external_candidate_model_files(roots)
    rule_defs: list[ModelRule] = []
    if model_rules is not None:
        try:
            rule_defs = load_model_rules(model_rules)
        except Exception as exc:
            errors.append(f"Could not read model rules: {exc}")
            return _import_result(
                kit,
                write,
                roots,
                before,
                before,
                [],
                [],
                [],
                [],
                [],
                errors,
                warnings,
                model_rules=model_rules,
            )
    rule_base = model_rules.expanduser().resolve().parent if model_rules is not None else None
    excluded_placeholder_files = _known_placeholder_model_files(roots)
    model_index = _index_model_files(model_files)
    rule_model_files = {
        rule.rule_id: _rule_model_files(
            rule,
            roots,
            model_files,
            excluded_placeholder_files,
            rule_base,
        )
        for rule in rule_defs
    }
    rows_by_alias = _missing_rows_by_alias(kit.report)
    used_names = {model.name.lower() for model in kit.models_dir.iterdir() if model.is_file()}
    copied_by_source: dict[str, dict[str, Any]] = {}
    imported: list[dict[str, Any]] = []
    copied_models: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    rule_imports: list[dict[str, Any]] = []

    for alias_key, row_group in rows_by_alias.items():
        if alias_key not in alias_mapping:
            unresolved.append(
                {
                    "candidate": alias_key,
                    "footprints": row_group["footprints"],
                    "reason": "alias_row_missing",
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                }
            )
            continue
        current_target = _alias_target_text(alias_mapping[alias_key])
        if "REPLACE_WITH_" not in current_target.upper():
            preserved.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                }
            )
            continue
        matches = _matching_model_files(row_group["candidate_keys"], model_index)
        match_kind = "exact_filename"
        rule_match: ModelRule | None = None
        matched_rule_values: list[str] = []
        if not matches and rule_defs:
            rule_match, matches, matched_rule_values = _matching_model_rule(
                row_group,
                rule_defs,
                rule_model_files,
            )
            match_kind = "model_rule"
        if not matches:
            unresolved.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                    "reason": "no_matching_external_step_or_stp_file",
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                    "model_rules": [
                        rule.to_dict()
                        for rule in rule_defs
                        if _rule_matches_row(rule, row_group)
                    ],
                }
            )
            continue
        if len(matches) > 1:
            ambiguous.append(
                {
                    "candidate": alias_key,
                    "target": current_target,
                    "footprints": row_group["footprints"],
                    "candidate_keys": sorted(row_group["candidate_keys"]),
                    "matches": [
                        {
                            "file": str(item["path"]),
                            "matched_keys": sorted(item["matched_keys"]),
                            "rule_id": rule_match.rule_id if rule_match else "",
                        }
                        for item in matches
                    ],
                }
            )
            continue
        match = matches[0]
        import_info = _prepare_imported_model(
            match["path"],
            kit,
            used_names,
            copied_by_source,
        )
        if write and import_info["copy"] and not Path(import_info["target_path"]).exists():
            shutil.copy2(match["path"], import_info["target_path"])
        _set_alias_target(alias_mapping, alias_key, import_info["relative_target"])
        if rule_match is not None:
            rule_import = {
                "candidate": alias_key,
                "footprints": list(row_group["footprints"]),
                "rule_id": rule_match.rule_id,
                "match_regex": rule_match.match_regex,
                "rule_model": rule_match.model,
                "source_type": rule_match.source,
                "source_model": str(match["path"]),
                "matched_values": sorted(matched_rule_values),
                "target_model": import_info["target_path"],
                "relative_target": import_info["relative_target"],
            }
            rule_imports.append(rule_import)
            _set_alias_rule_metadata(
                alias_mapping,
                alias_key,
                target=import_info["relative_target"],
                metadata=rule_import,
            )
        if import_info["copy"] and import_info["source_key"] not in {item["source_key"] for item in copied_models}:
            copied_models.append(import_info)
        imported.append(
            {
                "candidate": alias_key,
                "old_target": current_target,
                "new_target": import_info["relative_target"],
                "target_model": import_info["target_path"],
                "copied": bool(import_info["copy"]),
                "footprints": row_group["footprints"],
                "matched_keys": sorted(match["matched_keys"]),
                "match_kind": match_kind,
                "rule_id": rule_match.rule_id if rule_match else "",
                "source_type": rule_match.source if rule_match else "",
                "source_model": str(match["path"]),
                "matched_rule_values": sorted(matched_rule_values),
            }
        )

    _annotate_shared_rule_models(rule_imports)
    for rule_import in rule_imports:
        _set_alias_rule_metadata(
            alias_mapping,
            str(rule_import["candidate"]),
            target=str(rule_import["relative_target"]),
            metadata=rule_import,
        )
        for imported_row in imported:
            if imported_row.get("candidate") == rule_import["candidate"]:
                imported_row.update(
                    {
                        "shared_model": bool(rule_import.get("shared_model", False)),
                        "shared_with": list(rule_import.get("shared_with", [])),
                    }
                )

    if ambiguous:
        warnings.append("Some placeholder aliases matched more than one external STEP/STP file and were left unchanged.")
    if unresolved:
        warnings.append("Some placeholder aliases could not be matched to an external STEP/STP filename.")
    if excluded_placeholder_files:
        warnings.append(
            f"Skipped {len(excluded_placeholder_files)} known placeholder STEP/STP model(s) "
            "from the external model roots."
        )
    if write and imported:
        kit.models_dir.mkdir(parents=True, exist_ok=True)
        kit.aliases_path.write_text(
            json.dumps(alias_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    after = check_model_repair_kit(path) if write else before
    return _import_result(
        kit,
        write,
        roots,
        before,
        after,
        imported,
        copied_models,
        unresolved,
        ambiguous,
        preserved,
        errors,
        warnings,
        model_rules=model_rules,
        rule_imports=rule_imports,
    )


def suggest_model_repair_kit_rules(
    path: Path,
    model_roots: list[Path],
    source: str = "suggested_standard_model_library",
) -> dict[str, Any]:
    """Suggest reviewable --model-rules entries for common shared package bodies."""
    kit = load_model_repair_kit(path)
    before = check_model_repair_kit(path)
    errors = list(before.get("errors", []) or [])
    warnings: list[str] = []
    roots = [root.expanduser().resolve() for root in model_roots]
    if not roots:
        errors.append("At least one model root is required.")
    for root in roots:
        if not root.exists():
            errors.append(f"Model root does not exist: {root}")
    if errors:
        return _suggest_rules_result(kit, roots, before, [], [], [], errors, warnings, source)

    try:
        _, alias_mapping = _load_repair_alias_data(kit.aliases_path)
    except Exception as exc:
        errors.append(f"Could not read repair kit aliases: {exc}")
        return _suggest_rules_result(kit, roots, before, [], [], [], errors, warnings, source)

    excluded_placeholder_files = {path.resolve() for path in _known_placeholder_model_files(roots)}
    package_models = _standard_package_model_files(_external_candidate_model_files(roots), excluded_placeholder_files)
    rows_by_alias = _missing_rows_by_alias(kit.report)
    suggestions: list[dict[str, Any]] = []
    ambiguous_models: list[dict[str, Any]] = []

    for token, model_paths in sorted(package_models.items()):
        if len(model_paths) != 1:
            ambiguous_models.append(
                {
                    "package": token,
                    "models": [str(path) for path in model_paths],
                }
            )
            continue
        matched_rows = []
        matched_values: set[str] = set()
        for alias_key, row_group in rows_by_alias.items():
            current_target = _alias_target_text(alias_mapping.get(alias_key))
            if "REPLACE_WITH_" not in current_target.upper():
                continue
            row_values = _standard_rule_row_values(row_group)
            matches = sorted(value for value in row_values if _value_matches_standard_token(value, token))
            if not matches:
                continue
            matched_values.update(matches)
            matched_rows.append(
                {
                    "candidate": alias_key,
                    "footprints": list(row_group.get("footprints", [])),
                    "matched_values": matches,
                }
            )
        if not matched_rows:
            continue
        model_path = model_paths[0]
        rule = {
            "id": f"suggested_{token}",
            "match_regex": _standard_model_rule_regex(token),
            "model": _model_rule_reference(model_path, roots),
            "source": source,
        }
        suggestions.append(
            {
                **rule,
                "source_model": str(model_path),
                "matched_aliases": matched_rows,
                "matched_alias_count": len(matched_rows),
                "matched_footprints": sorted(
                    {
                        footprint
                        for row in matched_rows
                        for footprint in row.get("footprints", [])
                    },
                    key=str.casefold,
                ),
                "matched_values": sorted(matched_values),
            }
        )

    if ambiguous_models:
        warnings.append("Some standard package model names appeared more than once and were not suggested.")
    if excluded_placeholder_files:
        warnings.append(
            f"Skipped {len(excluded_placeholder_files)} known placeholder STEP/STP model(s) "
            "from the external model roots."
        )
    if not suggestions and not warnings:
        warnings.append("No shared standard model-rule suggestions were found.")
    return _suggest_rules_result(
        kit,
        roots,
        before,
        suggestions,
        ambiguous_models,
        sorted(excluded_placeholder_files, key=lambda item: str(item).lower()),
        errors,
        warnings,
        source,
    )


def _copy_linked_models(report: dict[str, Any], models_dir: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    used_names = {path.name.lower() for path in models_dir.iterdir() if path.is_file()}
    for row in report.get("linked_footprints", []):
        source = _linked_model_source_path(row)
        if source is None:
            continue
        source_key = str(source.resolve())
        if source_key in copied:
            continue
        if _is_relative_to(source, models_dir):
            relative = source.relative_to(models_dir)
            copied[source_key] = f"models/{relative.as_posix()}"
            used_names.add(source.name.lower())
            continue
        target = _unique_model_target(models_dir, source.name, used_names)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        copied[source_key] = f"models/{target.name}"
    return copied


def _sync_result(
    kit: ModelRepairKit,
    write: bool,
    before: dict[str, Any],
    after: dict[str, Any],
    updated: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "version": f"v{__version__}",
        "status": "failed" if errors else str(after.get("status", "unknown")),
        "repair_kit": str(kit.directory),
        "write": write,
        "aliases": str(kit.aliases_path),
        "models_dir": str(kit.models_dir),
        "before": {
            "status": before.get("status", "unknown"),
            "counts": before.get("counts", {}),
        },
        "after": {
            "status": after.get("status", "unknown"),
            "counts": after.get("counts", {}),
        },
        "counts": {
            "model_files": len(_sync_candidate_model_files(kit.models_dir)),
            "updated_aliases": len(updated),
            "unresolved_aliases": len(unresolved),
            "ambiguous_aliases": len(ambiguous),
            "preserved_aliases": len(preserved),
        },
        "updated_aliases": updated,
        "unresolved_aliases": unresolved,
        "ambiguous_aliases": ambiguous,
        "preserved_aliases": preserved,
        "errors": errors,
        "warnings": warnings,
    }


def _import_result(
    kit: ModelRepairKit,
    write: bool,
    roots: list[Path],
    before: dict[str, Any],
    after: dict[str, Any],
    imported: list[dict[str, Any]],
    copied_models: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    model_rules: Path | None = None,
    rule_imports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rule_imports = rule_imports or []
    rule_defs: list[ModelRule] = []
    if model_rules is not None and model_rules.exists():
        try:
            rule_defs = load_model_rules(model_rules)
        except Exception:
            rule_defs = []
    excluded_placeholder_files = _known_placeholder_model_files(roots)
    return {
        "version": f"v{__version__}",
        "status": "failed" if errors else str(after.get("status", "unknown")),
        "repair_kit": str(kit.directory),
        "write": write,
        "model_roots": [str(root) for root in roots],
        "model_rules": str(model_rules.expanduser().resolve()) if model_rules else "",
        "model_rules_loaded": [rule.to_dict() for rule in rule_defs],
        "aliases": str(kit.aliases_path),
        "models_dir": str(kit.models_dir),
        "before": {
            "status": before.get("status", "unknown"),
            "counts": before.get("counts", {}),
        },
        "after": {
            "status": after.get("status", "unknown"),
            "counts": after.get("counts", {}),
        },
        "counts": {
            "scanned_model_files": len(_external_candidate_model_files(roots)),
            "excluded_placeholder_models": len(excluded_placeholder_files),
            "imported_aliases": len(imported),
            "copied_models": len(copied_models),
            "unresolved_aliases": len(unresolved),
            "ambiguous_aliases": len(ambiguous),
            "preserved_aliases": len(preserved),
            "rule_matched_aliases": len(rule_imports),
            "shared_rule_models": sum(
                1 for item in rule_imports if item.get("shared_model")
            ),
        },
        "imported_aliases": imported,
        "copied_models": copied_models,
        "unresolved_aliases": unresolved,
        "ambiguous_aliases": ambiguous,
        "preserved_aliases": preserved,
        "rule_imports": rule_imports,
        "excluded_placeholder_models": [str(path) for path in excluded_placeholder_files],
        "errors": errors,
        "warnings": warnings,
    }


def _suggest_rules_result(
    kit: ModelRepairKit,
    roots: list[Path],
    before: dict[str, Any],
    suggestions: list[dict[str, Any]],
    ambiguous_models: list[dict[str, Any]],
    excluded_placeholder_files: list[Path],
    errors: list[str],
    warnings: list[str],
    source: str,
) -> dict[str, Any]:
    return {
        "version": f"v{__version__}",
        "status": "failed" if errors else ("suggested" if suggestions else "no_suggestions"),
        "repair_kit": str(kit.directory),
        "model_roots": [str(root) for root in roots],
        "source": source,
        "aliases": str(kit.aliases_path),
        "models_dir": str(kit.models_dir),
        "before": {
            "status": before.get("status", "unknown"),
            "counts": before.get("counts", {}),
        },
        "counts": {
            "scanned_model_files": len(_external_candidate_model_files(roots)),
            "excluded_placeholder_models": len(excluded_placeholder_files),
            "suggested_rules": len(suggestions),
            "matched_aliases": sum(int(item.get("matched_alias_count", 0) or 0) for item in suggestions),
            "ambiguous_model_packages": len(ambiguous_models),
        },
        "rules": [
            {
                "id": item["id"],
                "match_regex": item["match_regex"],
                "model": item["model"],
                "source": item["source"],
            }
            for item in suggestions
        ],
        "suggestions": suggestions,
        "ambiguous_model_packages": ambiguous_models,
        "excluded_placeholder_models": [str(path) for path in excluded_placeholder_files],
        "errors": errors,
        "warnings": warnings,
    }


def _load_repair_alias_data(path: Path) -> tuple[Any, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Repair kit aliases must be a JSON object.")
    for key in ["aliases", "model_aliases", "models", "footprints", "packages"]:
        value = data.get(key)
        if isinstance(value, dict):
            return data, value
    return data, data


def load_model_rules(path: Path) -> list[ModelRule]:
    """Load deterministic footprint-to-standard-model rules from JSON."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Model rules file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(raw_rules, list):
        raise ValueError("Model rules JSON must contain a top-level 'rules' array.")

    rules: list[ModelRule] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"Model rule {index} must be a JSON object.")
        rule_id = str(raw_rule.get("id", "") or "").strip()
        match_regex = str(raw_rule.get("match_regex", "") or "").strip()
        model = str(raw_rule.get("model", "") or "").strip()
        source = str(raw_rule.get("source", "") or "").strip() or "model_rules"
        if not rule_id or not match_regex or not model:
            raise ValueError(
                f"Model rule {index} requires non-empty id, match_regex, and model fields."
            )
        if rule_id in seen_ids:
            raise ValueError(f"Duplicate model rule id: {rule_id}")
        if Path(model).suffix.lower() not in STEP_SUFFIXES:
            raise ValueError(
                f"Model rule {rule_id} must reference a STEP/STP model, got {model!r}."
            )
        try:
            pattern = re.compile(match_regex, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex for model rule {rule_id}: {exc}") from exc
        seen_ids.add(rule_id)
        rules.append(
            ModelRule(
                rule_id=rule_id,
                match_regex=match_regex,
                model=model,
                source=source,
                pattern=pattern,
            )
        )
    return rules


def _external_candidate_model_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    excluded = set(_known_placeholder_model_files(roots))
    for root in roots:
        if not root.exists():
            continue
        candidates = (
            root.rglob("*")
            if root.is_dir()
            else [root]
        )
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in STEP_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            if resolved in excluded:
                continue
            seen.add(resolved)
            files.append(resolved)
    return sorted(files, key=lambda item: str(item).lower())


def _known_placeholder_model_files(roots: list[Path]) -> list[Path]:
    """Find STEP/STP files explicitly marked as generated placeholder geometry."""
    placeholders: set[Path] = set()
    sidecars: set[Path] = set()
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            search_root = root.parent
            candidates = search_root.rglob("*")
        elif root.is_dir():
            candidates = root.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.name not in {"placeholder_3d_report.json", "3d_model_map.json"}:
                continue
            sidecars.add(path.resolve())

    for sidecar in sorted(sidecars, key=lambda item: str(item).lower()):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if sidecar.name == "placeholder_3d_report.json":
            rows = data.get("generated", [])
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("kind", "")) != PLACEHOLDER_MODEL_KIND:
                    continue
                candidate = _sidecar_model_path(sidecar, row.get("file"))
                if candidate is not None:
                    placeholders.add(candidate)
        else:
            rows = data.get("models", [])
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or not _is_placeholder_model_row(row):
                    continue
                for value in [row.get("file"), row.get("source_file")]:
                    candidate = _sidecar_model_path(sidecar, value)
                    if candidate is not None:
                        placeholders.add(candidate)
    return sorted(placeholders, key=lambda item: str(item).lower())


def _sidecar_model_path(sidecar: Path, value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = sidecar.parent / candidate
    if candidate.suffix.lower() not in STEP_SUFFIXES:
        return None
    return candidate.resolve()


def _is_placeholder_model_row(row: dict[str, Any]) -> bool:
    if str(row.get("transform_source", "")) == PLACEHOLDER_MODEL_KIND:
        return True
    properties = row.get("properties")
    return isinstance(properties, dict) and str(properties.get("kind", "")) == PLACEHOLDER_MODEL_KIND


def _rule_model_files(
    rule: ModelRule,
    roots: list[Path],
    model_files: list[Path],
    excluded_placeholder_files: list[Path],
    rule_base: Path | None,
) -> list[Path]:
    """Resolve a rule's model reference without allowing placeholder assets."""
    reference = Path(rule.model).expanduser()
    excluded = {path.resolve() for path in excluded_placeholder_files}
    candidates: set[Path] = set()

    if reference.is_absolute():
        candidates.add(reference.resolve())
    elif rule_base is not None:
        candidates.add((rule_base / reference).resolve())

    reference_name = reference.name.casefold()
    reference_text = reference.as_posix().replace("\\", "/").casefold()
    for path in model_files:
        path = path.resolve()
        if path.name.casefold() == reference_name:
            candidates.add(path)
            continue
        for root in roots:
            root = root.expanduser().resolve()
            if not root.is_dir():
                continue
            try:
                relative = path.relative_to(root).as_posix().casefold()
            except ValueError:
                continue
            if relative == reference_text:
                candidates.add(path)

    eligible = []
    for path in sorted(candidates, key=lambda item: str(item).lower()):
        if not path.is_file() or path.suffix.lower() not in STEP_SUFFIXES:
            continue
        if path.resolve() in excluded:
            continue
        eligible.append(path.resolve())
    return list(dict.fromkeys(eligible))


STANDARD_RULE_TOKENS = {
    "0201",
    "0402",
    "0603",
    "0805",
    "1206",
    "1210",
    "1812",
    "2010",
    "2512",
}


def _standard_package_model_files(model_files: list[Path], excluded: set[Path]) -> dict[str, list[Path]]:
    packages: dict[str, list[Path]] = {}
    for path in model_files:
        resolved = path.resolve()
        if resolved in excluded:
            continue
        token = _standard_package_token(normalized_key(path.stem))
        if token:
            packages.setdefault(token, []).append(resolved)
    return packages


def _standard_package_token(model_key: str) -> str:
    for token in sorted(STANDARD_RULE_TOKENS):
        if model_key == token:
            return token
    return ""


def _standard_rule_row_values(row_group: dict[str, Any]) -> set[str]:
    values = {
        normalized_key(str(value or ""))
        for value in row_group.get("candidate_keys", set())
        if str(value or "").strip()
    }
    values.update(
        normalized_key(str(value or ""))
        for value in row_group.get("footprints", [])
        if str(value or "").strip()
    )
    alias_key = str(row_group.get("alias_key", "") or "").strip()
    if alias_key:
        values.add(normalized_key(alias_key))
    return {value for value in values if value}


def _value_matches_standard_token(value: str, token: str) -> bool:
    return bool(re.search(rf"^(c|r|l|f|fb|res|cap|ind)?{re.escape(token)}.*$", value))


def _standard_model_rule_regex(token: str) -> str:
    return f"^(c|r|l|f|fb|res|cap|ind)?{token}.*$"


def _model_rule_reference(model_path: Path, roots: list[Path]) -> str:
    candidates: list[str] = [model_path.name]
    for root in roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            continue
        try:
            candidates.append(model_path.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return min(candidates, key=lambda value: (len(value), value.casefold()))


def _rule_matches_row(rule: ModelRule, row_group: dict[str, Any]) -> bool:
    values = set(str(value or "").strip() for value in row_group.get("candidate_keys", set()))
    values.update(
        normalized_key(str(value or ""))
        for value in row_group.get("footprints", [])
        if str(value or "").strip()
    )
    alias_key = str(row_group.get("alias_key", "") or "").strip()
    if alias_key:
        values.add(normalized_key(alias_key))
    return any(value and rule.pattern.search(value) for value in values)


def _matching_model_rule(
    row_group: dict[str, Any],
    rules: list[ModelRule],
    rule_model_files: dict[str, list[Path]],
) -> tuple[ModelRule | None, list[dict[str, Any]], list[str]]:
    for rule in rules:
        values = sorted(
            {
                str(value)
                for value in row_group.get("candidate_keys", set())
                if str(value or "").strip() and rule.pattern.search(str(value))
            }
        )
        if not values:
            values = sorted(
                {
                    normalized_key(str(value))
                    for value in row_group.get("footprints", [])
                    if str(value or "").strip() and rule.pattern.search(str(value))
                }
            )
        if not values:
            alias_key = str(row_group.get("alias_key", "") or "").strip()
            normalized_alias = normalized_key(alias_key)
            if normalized_alias and rule.pattern.search(normalized_alias):
                values = [normalized_alias]
        if not values:
            continue
        matches = [
            {
                "path": path,
                "matched_keys": set(values),
            }
            for path in rule_model_files.get(rule.rule_id, [])
        ]
        if matches:
            return rule, matches, values
    return None, [], []


def _set_alias_rule_metadata(
    mapping: dict[str, Any],
    alias_key: str,
    target: str,
    metadata: dict[str, Any],
) -> None:
    current = mapping.get(alias_key)
    if not isinstance(current, dict):
        current = {"target": target}
        mapping[alias_key] = current
    else:
        current["target"] = target
    for key in [
        "rule_id",
        "match_regex",
        "rule_model",
        "source_type",
        "source_model",
        "matched_values",
        "shared_model",
        "shared_with",
    ]:
        if key in metadata:
            current[key] = metadata[key]


def _annotate_shared_rule_models(rule_imports: list[dict[str, Any]]) -> None:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in rule_imports:
        source_model = str(item.get("source_model", "") or "")
        if source_model:
            by_source.setdefault(source_model, []).append(item)
    for items in by_source.values():
        shared_with = sorted(
            {
                str(footprint)
                for item in items
                for footprint in item.get("footprints", [])
                if str(footprint or "").strip()
            },
            key=str.casefold,
        )
        shared = len(items) > 1 or len(shared_with) > 1
        for item in items:
            item["shared_model"] = shared
            item["shared_with"] = shared_with


def _sync_candidate_model_files(models_dir: Path) -> list[Path]:
    if not models_dir.exists():
        return []
    return sorted(
        path.resolve()
        for path in models_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in STEP_SUFFIXES
    )


def _index_model_files(model_files: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in model_files:
        key = normalized_key(path.stem)
        if not key:
            continue
        index.setdefault(key, []).append(path)
    return index


def _missing_rows_by_alias(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in report.get("missing_footprints", []) or []:
        alias_key = str(row.get("alias_key", "") or "").strip()
        if not alias_key:
            continue
        group = rows.setdefault(
            alias_key,
            {
                "alias_key": alias_key,
                "footprints": [],
                "candidate_keys": set(),
            },
        )
        footprint = str(row.get("footprint", "") or "").strip()
        if footprint and footprint not in group["footprints"]:
            group["footprints"].append(footprint)
        group["candidate_keys"].update(_sync_row_candidate_keys(row))
    return rows


def _sync_row_candidate_keys(row: dict[str, Any]) -> set[str]:
    values: list[str] = [
        str(row.get("footprint", "") or ""),
        str(row.get("alias_key", "") or ""),
    ]
    values.extend(_safe_model_id_values(row))
    values.extend(str(value or "") for value in row.get("model_titles", []) or [])
    values.extend(str(value or "") for value in row.get("candidate_keys", []) or [])
    values.extend(str(value or "") for value in row.get("candidate_filenames", []) or [])
    footprint = str(row.get("footprint", "") or "").strip()
    if footprint:
        values.append(cadence_safe_name(footprint))

    keys: set[str] = set()
    for value in values:
        text = value.strip()
        if not text:
            continue
        candidates = model_match_id_candidates(text)
        for candidate in candidates:
            candidate_path = Path(candidate)
            stem = candidate_path.stem if candidate_path.suffix.lower() in MODEL_SUFFIXES else candidate
            key = normalized_key(stem)
            if key:
                keys.add(key)
    return keys


def _safe_model_id_values(row: dict[str, Any]) -> list[str]:
    """Return composite model-ID aliases without shared workspace-only parts."""
    raw_values = [
        str(row.get("easyeda_model_id", "") or ""),
        *(str(value or "") for value in row.get("candidate_model_ids", []) or []),
    ]
    composite_parts = {
        candidate
        for value in raw_values
        if "|" in value
        for candidate in model_match_id_candidates(value)
    }
    values: list[str] = []
    for value in raw_values:
        text = value.strip()
        if not text:
            continue
        if "|" not in text and composite_parts and text not in composite_parts:
            continue
        values.extend(model_match_id_candidates(text))
    return values


def _alias_target_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ["target", "model", "model_key", "file", "step", "path", "to"]:
            if value.get(key):
                return str(value[key])
    return ""


def _set_alias_target(mapping: dict[str, Any], alias_key: str, target: str) -> None:
    current = mapping.get(alias_key)
    if isinstance(current, dict):
        for key in ["target", "model", "model_key", "file", "step", "path", "to"]:
            if key in current:
                current[key] = target
                return
        current["target"] = target
        return
    mapping[alias_key] = target


def _matching_model_files(candidate_keys: set[str], model_index: dict[str, list[Path]]) -> list[dict[str, Any]]:
    by_path: dict[Path, set[str]] = {}
    for key in candidate_keys:
        for path in model_index.get(key, []):
            by_path.setdefault(path, set()).add(key)
    return [
        {"path": path, "matched_keys": matched_keys}
        for path, matched_keys in sorted(by_path.items(), key=lambda item: str(item[0]).lower())
    ]


def _prepare_imported_model(
    source: Path,
    kit: ModelRepairKit,
    used_names: set[str],
    copied_by_source: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source = source.resolve()
    source_key = str(source)
    existing = copied_by_source.get(source_key)
    if existing is not None:
        return existing
    if _is_relative_to(source, kit.models_dir):
        target = source
        copy = False
    else:
        target = _unique_model_target(kit.models_dir, source.name, used_names)
        copy = True
    info = {
        "source_key": source_key,
        "source_model": str(source),
        "target_path": str(target),
        "relative_target": _relative_alias_target(target, kit.directory),
        "copy": copy,
    }
    copied_by_source[source_key] = info
    return info


def _relative_alias_target(path: Path, kit_dir: Path) -> str:
    try:
        return path.resolve().relative_to(kit_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _repair_alias_mapping(report: dict[str, Any], copied_models: dict[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in report.get("linked_footprints", []):
        target = _linked_alias_target(row, copied_models)
        if not target:
            continue
        for alias_key in _linked_alias_keys(row):
            mapping.setdefault(alias_key, target)
    for row in report.get("missing_footprints", []):
        alias_key = str(row.get("alias_key", "") or "").strip()
        footprint = str(row.get("footprint", "") or "").strip()
        if not alias_key or not footprint:
            continue
        mapping.setdefault(
            alias_key,
            f"models/REPLACE_WITH_{cadence_safe_name(footprint)}.step",
        )
    return mapping


def _linked_model_source_path(row: dict[str, Any]) -> Path | None:
    value = str(row.get("model_path", "") or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.exists() or path.suffix.lower() not in MODEL_SUFFIXES:
        return None
    return path.resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _unique_model_target(models_dir: Path, source_name: str, used_names: set[str]) -> Path:
    source = Path(source_name)
    suffix = source.suffix.lower()
    stem = cadence_safe_name(source.stem)
    if not stem:
        stem = "model"
    candidate = f"{stem}{suffix}"
    index = 2
    while candidate.lower() in used_names or (models_dir / candidate).exists():
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used_names.add(candidate.lower())
    return models_dir / candidate


def _linked_alias_target(row: dict[str, Any], copied_models: dict[str, str]) -> str:
    source = _linked_model_source_path(row)
    if source is None:
        return ""
    return copied_models.get(str(source.resolve()), "")


def _linked_alias_keys(row: dict[str, Any]) -> list[str]:
    values: list[str] = [
        str(row.get("alias_key", "") or ""),
        str(row.get("footprint", "") or ""),
    ]
    values.extend(_safe_model_id_values(row))
    values.extend(str(value or "") for value in row.get("model_titles", []) or [])
    result = []
    seen = set()
    for value in values:
        text = value.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _input_from_report(report: dict[str, Any], base_dir: Path) -> Path | None:
    value = str(report.get("input", "") or "").strip()
    if not value or value.startswith("<"):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _resolve_alias_target(value: str, base_dir: Path) -> Path | None:
    if not value or Path(value).suffix.lower() not in MODEL_SUFFIXES:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _write_todo_csv(
    report: dict[str, Any],
    path: Path,
    alias_mapping: dict[str, str],
) -> None:
    fields = [
        "footprint",
        "alias_key",
        "easyeda_model_id",
        "model_titles",
        "candidate_keys",
        "candidate_filenames",
        "target_filename",
        "component_names",
        "component_part_candidates",
        "candidate_model_ids",
        "source_file",
        "description",
        "suggested_step_filename",
        "sourcing_queries",
        "sourcing_urls",
        "action",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in report.get("missing_footprints", []):
            alias_key = str(row.get("alias_key", "") or "")
            target = alias_mapping.get(alias_key, "")
            writer.writerow(
                {
                    "footprint": row.get("footprint", ""),
                    "alias_key": alias_key,
                    "easyeda_model_id": row.get("easyeda_model_id", ""),
                    "model_titles": "; ".join(row.get("model_titles", []) or []),
                    "candidate_keys": "; ".join(row.get("candidate_keys", []) or []),
                    "candidate_filenames": "; ".join(row.get("candidate_filenames", []) or []),
                    "target_filename": target,
                    "component_names": "; ".join(row.get("component_names", []) or []),
                    "component_part_candidates": "; ".join(row.get("component_part_candidates", []) or []),
                    "candidate_model_ids": "; ".join(row.get("candidate_model_ids", []) or []),
                    "source_file": _source_file_label(row),
                    "description": row.get("description", ""),
                    "suggested_step_filename": Path(target).name if target else "",
                    "sourcing_queries": "; ".join(row.get("sourcing_queries", []) or []),
                    "sourcing_urls": "; ".join(row.get("sourcing_urls", []) or []),
                    "action": "place real STEP/STP in models folder and sync aliases",
                }
            )


def _write_todo_markdown(
    report: dict[str, Any],
    path: Path,
    alias_mapping: dict[str, str],
) -> None:
    counts = report.get("counts", {})
    lines = [
        "# 3D model sourcing checklist",
        "",
        f"- Missing models: {counts.get('missing_models', 0)}",
        f"- Linked models: {counts.get('linked_models', 0)}",
        "",
        "Put real vendor/JLC STEP or STP files in `models/`, then run `jlc2cadence sync-repair-kit`.",
        "OBJ files are useful as references, but complete Cadence package mapping requires STEP/STP.",
        "",
        "| Footprint | Components | Part candidates | Model IDs/titles | Target alias | Suggested filename | Candidate keys | Search links |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rows = report.get("missing_footprints", []) or []
    if not rows:
        lines.append("| - | - | - | - | - | - | - | - |")
    for row in rows:
        alias_key = str(row.get("alias_key", "") or "")
        target = alias_mapping.get(alias_key, "")
        model_context = _join_markdown_values(
            [
                *(row.get("candidate_model_ids", []) or []),
                *(row.get("model_titles", []) or []),
            ]
        )
        lines.append(
            "| {footprint} | {components} | {parts} | {model_context} | {target} | {suggested} | {keys} | {links} |".format(
                footprint=_repair_md(row.get("footprint", "")),
                components=_repair_md(_join_markdown_values(row.get("component_names", []) or [])),
                parts=_repair_md(_join_markdown_values(row.get("component_part_candidates", []) or [])),
                model_context=_repair_md(model_context),
                target=_repair_md(target or "-"),
                suggested=_repair_md(Path(target).name if target else "-"),
                keys=_repair_md(_join_markdown_values(row.get("candidate_keys", []) or [])),
                links=_sourcing_links_markdown(row),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _join_markdown_values(values: list[Any]) -> str:
    items = [str(value or "").strip() for value in values if str(value or "").strip()]
    return "; ".join(items) if items else "-"


def _source_file_label(row: dict[str, Any]) -> str:
    value = str(row.get("source_file", "") or "").strip()
    return Path(value).name if value else ""


def _sourcing_links_markdown(row: dict[str, Any]) -> str:
    queries = [str(value or "").strip() for value in row.get("sourcing_queries", []) or []]
    urls = [str(value or "").strip() for value in row.get("sourcing_urls", []) or []]
    links = []
    for query, url in zip(queries, urls):
        if not query or not url:
            continue
        links.append(f"[{_repair_md(query)}]({url})")
    return "<br>".join(links) if links else "-"


def _repair_md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_repair_kit_readme(
    report: dict[str, Any],
    alias_path: Path,
    models_dir: Path,
) -> str:
    input_path = report.get("input") or "<path-to-jlc-export>"
    kit_dir = alias_path.parent
    counts = report.get("counts", {})
    return "\n".join(
        [
            "# jlc2cadence 3D repair kit",
            "",
            f"- Converter version: {report.get('version', f'v{__version__}')}",
            f"- Input export: `{input_path}`",
            f"- Footprints: {counts.get('footprints', 0)}",
            f"- Missing 3D models: {counts.get('missing_models', 0)}",
            "",
            "This folder is a fill-in workspace for STEP/STP models that were not matched automatically.",
            "The converter does not create fake placeholder geometry.",
            "",
            "## Fill the kit",
            "",
            "1. Export or download the missing STEP/STP files from JLC EDA Pro or the component vendor.",
            f"2. Put the files in `{models_dir}`.",
            f"3. Or run `jlc2cadence import-repair-kit-models --repair-kit \"{kit_dir}\" --model-root \"<model-library>\"` to copy clear matches from another folder.",
            f"4. Run `jlc2cadence sync-repair-kit --repair-kit \"{kit_dir}\"` to auto-fill clear filename matches.",
            "5. Edit `model_aliases.json` for any unresolved or ambiguous rows.",
            "6. Use `models_todo.csv` or `models_todo.md` to track the remaining files, source components, part numbers, and model IDs.",
            "",
            "The default alias targets are Cadence-friendly `.step` filenames. OBJ files may be kept for",
            "reference, but complete Cadence STEP mapping still requires STEP/STP geometry.",
            "",
            "## Convert after filling",
            "",
            "```powershell",
            f'jlc2cadence repair-kit-build --repair-kit "{kit_dir}" --model-root "<model-library>" --out ".\\out_jlc2cadence" --require-3d --strict',
            f'jlc2cadence convert --input "{input_path}" --models "{models_dir}" --model-aliases "{alias_path}" --out ".\\out_jlc2cadence" --run-cadence --strict',
            'jlc2cadence verify-output --out ".\\out_jlc2cadence" --require-cadence --require-3d --strict',
            "```",
            "",
            f"The alias targets are intentionally relative to `{kit_dir}` so the kit can be moved as one folder.",
            "",
        ]
    )
