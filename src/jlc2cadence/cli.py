from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from time import perf_counter

from . import __version__
from .extractors.altium_ascii import parse_altium_design_files
from .extractors.archive import find_design_files, prepare_input_files
from .extractors.easyeda_3d import (
    fetch_easyeda_3d_models,
    load_model_sources,
    write_easyeda_3d_fetch_report,
)
from .extractors.easyeda_cache import (
    default_easyeda_cache_roots,
    import_easyeda_cached_3d_models,
    write_easyeda_cache_import_report,
)
from .extractors.easyeda_parts import (
    resolve_lcsc_3d_models,
    write_lcsc_3d_resolution_report,
)
from .extractors.easyeda_pro import parse_easyeda_pro_design_files
from .extractors.easyeda_legacy import parse_easyeda_legacy_design_files
from .extractors.model_aliases import collect_model_alias_assets, load_model_aliases
from .extractors.models import collect_model_assets
from .extractors.pads_ascii import parse_pads_design_files
from .generators.allegro_skill import write_allegro_outputs
from .generators.capture import write_capture_outputs
from .generators.model3d import write_model_map
from .generators.placeholder3d import synthesize_missing_step_models, write_placeholder_3d_report
from .ir.normalize import attach_models, ensure_components_for_footprints, merge_libraries
from .reports.audit import build_output_audit, write_output_audit
from .reports.cadence_assets import (
    cadence_asset_reuse_statuses,
    import_cadence_assets,
    write_cadence_asset_import_report,
)
from .reports.html import write_html_report
from .reports.missing_models import (
    build_missing_3d_report,
    render_missing_3d_markdown,
    write_missing_3d_markdown,
)
from .reports.model_request import (
    build_3d_model_request_report,
    render_3d_model_request_csv,
    render_3d_model_request_markdown,
    receive_3d_model_request_models,
    write_3d_model_request_report,
)
from .reports.package_output import build_handoff_bundle
from .reports.padstacks import (
    build_padstack_coverage_report,
    render_padstack_coverage_markdown,
    write_padstack_coverage_report_json,
    write_padstack_coverage_report_markdown,
)
from .reports.repair_kit import (
    check_model_repair_kit,
    import_model_repair_kit_models,
    load_model_repair_kit,
    suggest_model_repair_kit_rules,
    sync_model_repair_kit,
    write_model_repair_kit,
)
from .reports.three_d_review import (
    CANVAS_SIGNED_OFF,
    CANVAS_SIGNED_OFF_WITH_EXCEPTIONS,
    CANVAS_SIGNOFF_FAILED,
    NONCOMPONENT_3D_EXEMPT,
    READY_FOR_CANVAS_REVIEW,
    READY_FOR_CANVAS_REVIEW_WITH_EXCEPTIONS,
    build_3d_review_report,
    write_3d_signoff_template,
    write_3d_review_report,
)
from .reports.svg_preview import write_svg_previews
from .validators.cadence_runner import (
    CadenceTools,
    compile_existing_package_symbols,
    run_capture_build,
    run_step_mapping,
    run_symbol_build_plan,
    run_sym_3d_update,
    write_cadence_run_notes,
)
from .validators.doctor import build_doctor_report
from .validators.library_checks import validate_library
from .validators.output_package import validate_output_package, write_output_validation_report
from .validators.step_mapping import validate_step_mapping_output, write_step_mapping_validation


def _add_padstack_requirement_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--require-pad-kind",
        action="append",
        default=[],
        help="Require a pad kind in the padstack report, such as smd or thru_hole.",
    )
    parser.add_argument(
        "--require-pad-role",
        action="append",
        default=[],
        help="Require a pad role in the padstack report, such as electrical, shield, npth, mounting, or mechanical.",
    )
    parser.add_argument(
        "--require-pad-shape",
        action="append",
        default=[],
        help="Require a pad shape in the padstack report, such as rect, circle, oval, roundrect, or octagon.",
    )
    parser.add_argument("--require-pad-slot", action="store_true", help="Require at least one slotted pad.")
    parser.add_argument("--require-pad-drill", action="store_true", help="Require at least one drilled or slotted pad.")
    parser.add_argument(
        "--require-pad-nonplated",
        action="store_true",
        help="Require at least one nonplated drill or slot.",
    )


_OPTIONAL_3D_WARNING_MARKERS = (
    "has no linked 3D model",
    "STEP mapping validation warning:",
    "generated placeholder STEP envelope",
)


def _strict_output_warnings(report) -> list[str]:
    warnings = list(getattr(report, "warnings", []) or [])
    if getattr(report, "require_3d", False):
        return warnings
    return [warning for warning in warnings if not _is_optional_3d_warning(warning)]


def _is_optional_3d_warning(warning: str) -> bool:
    return any(marker in warning for marker in _OPTIONAL_3D_WARNING_MARKERS)


def _three_d_review_strict_ok(status: str, allow_noncomponent_3d_exceptions: bool) -> bool:
    ok = {READY_FOR_CANVAS_REVIEW, CANVAS_SIGNED_OFF}
    if allow_noncomponent_3d_exceptions:
        ok.update(
            {
                READY_FOR_CANVAS_REVIEW_WITH_EXCEPTIONS,
                CANVAS_SIGNED_OFF_WITH_EXCEPTIONS,
                NONCOMPONENT_3D_EXEMPT,
            }
        )
    return status in ok


def _three_d_review_signoff_ok(status: str, allow_noncomponent_3d_exceptions: bool) -> bool:
    ok = {CANVAS_SIGNED_OFF}
    if allow_noncomponent_3d_exceptions:
        ok.add(CANVAS_SIGNED_OFF_WITH_EXCEPTIONS)
    return status in ok


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jlc2cadence",
        description="Convert JLC EDA Pro exports into Cadence 17.4 generation assets.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="Print the tool version.")
    p_version.set_defaults(func=cmd_version)

    p_inspect = sub.add_parser("inspect", help="Parse input and print a compact summary.")
    p_inspect.add_argument("--input", required=True, type=Path, help="Input file, zip, or directory.")
    p_inspect.add_argument("--models", type=Path, help="Optional model file, zip, or directory.")
    p_inspect.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_inspect.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_inspect.set_defaults(func=cmd_inspect)

    p_convert = sub.add_parser("convert", help="Convert input into output assets.")
    p_convert.add_argument("--input", type=Path, help="Input file, zip, or directory.")
    p_convert.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory.")
    p_convert.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_convert.add_argument("--repair-kit", type=Path, help="Optional missing-3d repair-kit directory to supply input, models, and aliases.")
    p_convert.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional existing Cadence asset directory or ZIP to reuse by exact footprint filename match.",
    )
    p_convert.add_argument("--out", required=True, type=Path, help="Output directory.")
    p_convert.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_convert.add_argument("--strict", action="store_true", help="Return nonzero when validation errors exist.")
    p_convert.add_argument("--run-cadence", action="store_true", help="Try to run Cadence helper commands.")
    p_convert.add_argument(
        "--require-3d",
        action="store_true",
        help="Require linked STEP/STP 3D models in output validation.",
    )
    p_convert.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_convert.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Generate marked STEP envelope placeholders for footprints that still have no linked 3D model.",
    )
    p_convert.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_convert.add_argument(
        "--fetch-3d",
        action="store_true",
        help="Download EasyEDA STEP/OBJ models referenced by EasyEDA 3D Model IDs.",
    )
    p_convert.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="Format to download when --fetch-3d is enabled.",
    )
    p_convert.add_argument(
        "--model-cache",
        type=Path,
        help="Optional persistent cache directory for downloaded EasyEDA 3D models.",
    )
    p_convert.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Read-only EasyEDA/LCEDA cache directory to scan for cached STEP/OBJ models before HTTP fetching.",
    )
    p_convert.add_argument(
        "--model-sources",
        type=Path,
        help="Optional JSON model-source file with custom STEP/OBJ URL templates or per-model overrides.",
    )
    p_convert.add_argument(
        "--fetch-3d-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each EasyEDA 3D model request.",
    )
    p_convert.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties before fetching models.",
    )
    p_convert.add_argument(
        "--lcsc-cache",
        type=Path,
        help="Optional persistent cache directory for LCSC component metadata.",
    )
    p_convert.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each LCSC component metadata request.",
    )
    p_convert.set_defaults(func=cmd_convert)

    p_one_shot = sub.add_parser(
        "one-shot",
        aliases=["run"],
        help="Run preflight, conversion, validation, audit, 3D review, optional signoff template generation, and optional 3D update.",
    )
    p_one_shot.add_argument("--input", type=Path, help="Input file, zip, or directory.")
    p_one_shot.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory.")
    p_one_shot.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_one_shot.add_argument("--repair-kit", type=Path, help="Optional missing-3d repair-kit directory to supply input, models, and aliases.")
    p_one_shot.add_argument(
        "--model-root",
        "--model-roots",
        action="append",
        type=Path,
        help="External STEP/STP file or directory to import into --repair-kit before the one-shot conversion. Repeat for multiple roots.",
    )
    p_one_shot.add_argument(
        "--model-rules",
        type=Path,
        help="Optional JSON rules for mapping footprint keys to shared standard STEP/STP models during repair-kit import.",
    )
    p_one_shot.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional existing Cadence asset directory or ZIP to reuse by exact footprint filename match.",
    )
    p_one_shot.add_argument("--out", required=True, type=Path, help="Output directory.")
    p_one_shot.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_one_shot.add_argument("--strict", action="store_true", help="Return nonzero when final conversion, validation, audit, or 3D review warnings/errors remain.")
    p_one_shot.add_argument("--run-cadence", action="store_true", help="Run Cadence helper commands during conversion.")
    p_one_shot.add_argument("--require-cadence", action="store_true", help="Require real Cadence .olb/.dra/.psm outputs in final validation.")
    p_one_shot.add_argument("--require-3d", action="store_true", help="Require complete linked STEP/STP models in final validation.")
    p_one_shot.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_one_shot.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Generate marked STEP envelope placeholders for footprints that still have no linked 3D model.",
    )
    p_one_shot.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_one_shot.add_argument("--skip-doctor", action="store_true", help="Skip the preflight doctor report.")
    p_one_shot.add_argument("--skip-update-3d", action="store_true", help="Skip automatic sym_3d_update after a Cadence 3D mapping run.")
    p_one_shot.add_argument(
        "--signoff-template",
        type=Path,
        help="Write an editable 3D Canvas signoff template after the final review report is generated.",
    )
    p_one_shot.add_argument(
        "--fetch-3d",
        action="store_true",
        help="Download EasyEDA STEP/OBJ models referenced by EasyEDA 3D Model IDs.",
    )
    p_one_shot.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="Format to download when --fetch-3d is enabled.",
    )
    p_one_shot.add_argument("--model-cache", type=Path, help="Optional persistent cache directory for downloaded EasyEDA 3D models.")
    p_one_shot.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Read-only EasyEDA/LCEDA cache directory to scan for cached STEP/OBJ models before HTTP fetching.",
    )
    p_one_shot.add_argument(
        "--model-sources",
        type=Path,
        help="Optional JSON model-source file with custom STEP/OBJ URL templates or per-model overrides.",
    )
    p_one_shot.add_argument(
        "--fetch-3d-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each EasyEDA 3D model request.",
    )
    p_one_shot.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties before fetching models.",
    )
    p_one_shot.add_argument("--lcsc-cache", type=Path, help="Optional persistent cache directory for LCSC component metadata.")
    p_one_shot.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each LCSC component metadata request.",
    )
    p_one_shot.set_defaults(func=cmd_one_shot)

    p_deliver = sub.add_parser(
        "deliver",
        aliases=["handoff", "one-click"],
        help="Run one-shot conversion and package the result into a Cadence handoff ZIP.",
    )
    p_deliver.add_argument("--input", type=Path, help="Input file, zip, or directory.")
    p_deliver.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory.")
    p_deliver.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_deliver.add_argument("--repair-kit", type=Path, help="Optional missing-3d repair-kit directory to supply input, models, and aliases.")
    p_deliver.add_argument(
        "--model-root",
        "--model-roots",
        action="append",
        type=Path,
        help="External STEP/STP file or directory to import into --repair-kit before conversion. Repeat for multiple roots.",
    )
    p_deliver.add_argument(
        "--model-rules",
        type=Path,
        help="Optional JSON rules for mapping footprint keys to shared standard STEP/STP models during repair-kit import.",
    )
    p_deliver.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional existing Cadence asset directory or ZIP to reuse by exact footprint filename match.",
    )
    p_deliver.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_deliver.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_deliver.add_argument(
        "--strict",
        action="store_true",
        help="Return nonzero when one-shot or handoff validation reports errors or strict-mode warnings.",
    )
    p_deliver.add_argument("--run-cadence", action="store_true", help="Run Cadence helper commands during conversion.")
    p_deliver.add_argument("--require-cadence", action="store_true", help="Require real Cadence .olb/.dra/.psm outputs in the handoff.")
    p_deliver.add_argument("--require-3d", action="store_true", help="Require complete linked STEP/STP models in the handoff.")
    p_deliver.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_deliver.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Generate marked STEP envelope placeholders for footprints that still have no linked 3D model.",
    )
    p_deliver.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_deliver.add_argument("--skip-doctor", action="store_true", help="Skip the preflight doctor report.")
    p_deliver.add_argument("--skip-update-3d", action="store_true", help="Skip automatic sym_3d_update after a Cadence 3D mapping run.")
    p_deliver.add_argument(
        "--signoff-template",
        type=Path,
        help="Write an editable 3D Canvas signoff template after the final review report is generated.",
    )
    p_deliver.add_argument("--fetch-3d", action="store_true", help="Download EasyEDA STEP/OBJ models referenced by EasyEDA 3D Model IDs.")
    p_deliver.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="Format to download when --fetch-3d is enabled.",
    )
    p_deliver.add_argument("--model-cache", type=Path, help="Optional persistent cache directory for downloaded EasyEDA 3D models.")
    p_deliver.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Read-only EasyEDA/LCEDA cache directory to scan for cached STEP/OBJ models before HTTP fetching.",
    )
    p_deliver.add_argument(
        "--model-sources",
        type=Path,
        help="Optional JSON model-source file with custom STEP/OBJ URL templates or per-model overrides.",
    )
    p_deliver.add_argument("--fetch-3d-timeout", type=float, default=30.0, help="HTTP timeout in seconds for each EasyEDA 3D model request.")
    p_deliver.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties before fetching models.",
    )
    p_deliver.add_argument("--lcsc-cache", type=Path, help="Optional persistent cache directory for LCSC component metadata.")
    p_deliver.add_argument("--lcsc-timeout", type=float, default=30.0, help="HTTP timeout in seconds for each LCSC component metadata request.")
    p_deliver.add_argument(
        "--package-out",
        type=Path,
        help="ZIP output path. Defaults to <out-name>.cadence-package.zip beside the conversion output.",
    )
    p_deliver.add_argument("--manifest-out", type=Path, help="Optional JSON manifest path written outside the ZIP.")
    p_deliver.add_argument("--include-extract", action="store_true", help="Include the temporary _extract input directory in the handoff package.")
    p_deliver.add_argument(
        "--include-cadence-pcbenv",
        action="store_true",
        help="Include the generated _cadence_pcbenv directory in the handoff package.",
    )
    p_deliver.set_defaults(func=cmd_deliver)

    p_fetch_3d = sub.add_parser(
        "fetch-3d",
        help="Download EasyEDA STEP/OBJ models referenced by an export.",
    )
    p_fetch_3d.add_argument("--input", required=True, type=Path, help="Input file, zip, or directory.")
    p_fetch_3d.add_argument("--out", required=True, type=Path, help="Directory for downloaded model files.")
    p_fetch_3d.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_fetch_3d.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="Model format to request from EasyEDA.",
    )
    p_fetch_3d.add_argument(
        "--model-cache",
        type=Path,
        help="Optional persistent cache directory for downloaded EasyEDA 3D models.",
    )
    p_fetch_3d.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Read-only EasyEDA/LCEDA cache directory to scan for cached STEP/OBJ models before HTTP fetching.",
    )
    p_fetch_3d.add_argument(
        "--model-sources",
        type=Path,
        help="Optional JSON model-source file with custom STEP/OBJ URL templates or per-model overrides.",
    )
    p_fetch_3d.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each model request.",
    )
    p_fetch_3d.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties before fetching models.",
    )
    p_fetch_3d.add_argument(
        "--lcsc-cache",
        type=Path,
        help="Optional persistent cache directory for LCSC component metadata.",
    )
    p_fetch_3d.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each LCSC component metadata request.",
    )
    p_fetch_3d.add_argument("--strict", action="store_true", help="Return nonzero when any model is missing.")
    p_fetch_3d.set_defaults(func=cmd_fetch_3d)

    p_scan_cache_3d = sub.add_parser(
        "scan-cache-3d",
        help="Scan local EasyEDA/LCEDA caches and extract valid cached STEP/OBJ model payloads.",
    )
    p_scan_cache_3d.add_argument("--input", type=Path, help="Optional input file, zip, or directory used to match model IDs.")
    p_scan_cache_3d.add_argument("--out", required=True, type=Path, help="Directory for extracted cached model files and report.")
    p_scan_cache_3d.add_argument(
        "--cache",
        dest="local_model_cache",
        type=Path,
        action="append",
        help="EasyEDA/LCEDA cache root to scan. Can be supplied more than once; defaults to common local app cache roots.",
    )
    p_scan_cache_3d.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_scan_cache_3d.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="both",
        help="Model format to extract from cache.",
    )
    p_scan_cache_3d.add_argument("--strict", action="store_true", help="Return nonzero when requested models are not fully recovered.")
    p_scan_cache_3d.set_defaults(func=cmd_scan_cache_3d)

    p_validate_3d = sub.add_parser("validate-3d", help="Validate generated Cadence STEP mapping assets.")
    p_validate_3d.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_validate_3d.add_argument(
        "--require-mapped",
        action="store_true",
        help="Require Cadence STEP mapping data to be present in each built .dra symbol.",
    )
    p_validate_3d.add_argument(
        "--require-facet",
        action="store_true",
        help="Require stepFacetFiles4Map data for 3D Canvas; implies --require-mapped.",
    )
    p_validate_3d.add_argument("--write", action="store_true", help="Write reports/3d_validation.json.")
    p_validate_3d.add_argument("--strict", action="store_true", help="Return nonzero when warnings exist.")
    p_validate_3d.set_defaults(func=cmd_validate_3d)

    p_update_3d = sub.add_parser("update-3d", help="Run Cadence sym_3d_update after STEP mapping import/review.")
    p_update_3d.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_update_3d.add_argument("--symbols", type=Path, help="Optional source directory containing .dra symbols.")
    p_update_3d.add_argument("--destination", type=Path, help="Optional destination directory for updated symbols.")
    p_update_3d.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_update_3d.add_argument("--strict", action="store_true", help="Return nonzero unless at least one symbol was updated.")
    p_update_3d.set_defaults(func=cmd_update_3d)

    p_check_repair_kit = sub.add_parser(
        "check-repair-kit",
        aliases=["repair-kit-status"],
        help="Validate a missing-3d repair kit before using it for conversion.",
    )
    p_check_repair_kit.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_check_repair_kit.add_argument("--strict", action="store_true", help="Return nonzero unless the kit is ready.")
    p_check_repair_kit.set_defaults(func=cmd_check_repair_kit)

    p_sync_repair_kit = sub.add_parser(
        "sync-repair-kit",
        aliases=["repair-kit-sync"],
        help="Update repair-kit placeholder aliases from clearly named STEP/STP files.",
    )
    p_sync_repair_kit.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_sync_repair_kit.add_argument("--dry-run", action="store_true", help="Report matches without writing model_aliases.json.")
    p_sync_repair_kit.add_argument("--strict", action="store_true", help="Return nonzero unless the kit is ready after syncing.")
    p_sync_repair_kit.set_defaults(func=cmd_sync_repair_kit)

    p_import_repair_kit_models = sub.add_parser(
        "import-repair-kit-models",
        aliases=["repair-kit-import-models"],
        help="Copy clearly matching STEP/STP files from external model roots into a repair kit.",
    )
    p_import_repair_kit_models.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_import_repair_kit_models.add_argument(
        "--model-root",
        "--models",
        required=True,
        action="append",
        type=Path,
        help="External STEP/STP file or directory to scan. Repeat for multiple roots.",
    )
    p_import_repair_kit_models.add_argument(
        "--model-rules",
        type=Path,
        help="Optional JSON rules for mapping footprint keys to shared standard STEP/STP models.",
    )
    p_import_repair_kit_models.add_argument("--dry-run", action="store_true", help="Report imports without copying files or writing aliases.")
    p_import_repair_kit_models.add_argument("--strict", action="store_true", help="Return nonzero unless the kit is ready after importing.")
    p_import_repair_kit_models.set_defaults(func=cmd_import_repair_kit_models)

    p_suggest_model_rules = sub.add_parser(
        "suggest-model-rules",
        aliases=["model-rule-suggest"],
        help="Suggest reviewable --model-rules JSON entries from a repair kit and external STEP/STP roots.",
    )
    p_suggest_model_rules.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_suggest_model_rules.add_argument(
        "--model-root",
        "--models",
        required=True,
        action="append",
        type=Path,
        help="External STEP/STP file or directory to scan. Repeat for multiple roots.",
    )
    p_suggest_model_rules.add_argument("--out", type=Path, help="Optional output path for the generated model-rules JSON.")
    p_suggest_model_rules.add_argument("--report-out", type=Path, help="Optional output path for the full suggestion report JSON.")
    p_suggest_model_rules.add_argument("--source", default="suggested_standard_model_library", help="Source label to write into each suggested rule.")
    p_suggest_model_rules.add_argument("--strict", action="store_true", help="Return nonzero if no rule suggestions are found.")
    p_suggest_model_rules.set_defaults(func=cmd_suggest_model_rules)

    p_repair_kit_build = sub.add_parser(
        "repair-kit-build",
        aliases=["build-repair-kit"],
        help="Sync a repair kit, convert it, and write a one-command build report.",
    )
    p_repair_kit_build.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_repair_kit_build.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_repair_kit_build.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_repair_kit_build.add_argument(
        "--model-root",
        "--models",
        action="append",
        type=Path,
        help="External STEP/STP file or directory to import into the repair kit before syncing.",
    )
    p_repair_kit_build.add_argument(
        "--model-rules",
        type=Path,
        help="Optional JSON rules for mapping footprint keys to shared standard STEP/STP models.",
    )
    p_repair_kit_build.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional existing Cadence asset directory or ZIP to reuse by exact footprint filename match.",
    )
    p_repair_kit_build.add_argument("--no-sync", action="store_true", help="Skip the automatic repair-kit alias sync step.")
    p_repair_kit_build.add_argument("--run-cadence", action="store_true", help="Run Cadence helper commands during conversion.")
    p_repair_kit_build.add_argument("--require-cadence", action="store_true", help="Require real Cadence .olb/.dra/.psm outputs in the final check.")
    p_repair_kit_build.add_argument("--require-3d", action="store_true", help="Require complete linked STEP/STP models in the final check.")
    p_repair_kit_build.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_repair_kit_build.add_argument(
        "--signoff",
        type=Path,
        help="Optional JSON manual Cadence 3D Canvas signoff file to merge into the repair-kit review report.",
    )
    p_repair_kit_build.add_argument(
        "--signoff-template",
        type=Path,
        help="Write an editable signoff JSON template after the repair-kit 3D review is generated.",
    )
    p_repair_kit_build.add_argument(
        "--require-signoff",
        action="store_true",
        help="Return nonzero unless the repair-kit 3D review is canvas_signed_off when --signoff is used.",
    )
    p_repair_kit_build.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Generate marked STEP envelope placeholders for missing models during conversion.",
    )
    p_repair_kit_build.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties before fetching models.",
    )
    p_repair_kit_build.add_argument(
        "--lcsc-cache",
        type=Path,
        help="Optional persistent cache directory for LCSC component metadata.",
    )
    p_repair_kit_build.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each LCSC component metadata request.",
    )
    p_repair_kit_build.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_repair_kit_build.add_argument("--strict", action="store_true", help="Return nonzero when sync, conversion, or final checks are not clean.")
    p_repair_kit_build.set_defaults(func=cmd_repair_kit_build)

    p_verify_output = sub.add_parser("verify-output", help="Validate a generated conversion output package.")
    p_verify_output.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_verify_output.add_argument(
        "--require-cadence",
        action="store_true",
        help="Require real Cadence outputs such as .olb, .dra, and .psm.",
    )
    p_verify_output.add_argument(
        "--require-3d",
        action="store_true",
        help="Require complete linked STEP/STP 3D model mapping entries.",
    )
    p_verify_output.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_verify_output.add_argument(
        "--strict",
        action="store_true",
        help="Return nonzero when non-optional warnings exist. Missing 3D remains optional unless --require-3d is set.",
    )
    p_verify_output.add_argument("--write", action="store_true", help="Refresh reports/output_validation.json.")
    p_verify_output.set_defaults(func=cmd_verify_output)

    p_audit = sub.add_parser("audit", help="Summarize one generated output package for handoff/review.")
    p_audit.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_audit.add_argument(
        "--require-cadence",
        action="store_true",
        help="Audit as a Cadence-built package that should contain .olb, .dra, and .psm.",
    )
    p_audit.add_argument(
        "--require-3d",
        action="store_true",
        help="Require complete linked STEP/STP 3D model mapping entries during audit.",
    )
    p_audit.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete component-3D coverage.",
    )
    p_audit.add_argument("--write", action="store_true", help="Write reports/audit.json and reports/audit.md.")
    p_audit.add_argument("--strict", action="store_true", help="Return nonzero when warnings exist.")
    p_audit.set_defaults(func=cmd_audit)

    p_package_output = sub.add_parser(
        "package-output",
        aliases=["bundle-output", "bundle"],
        help="Package a conversion output into a Cadence handoff ZIP with manifests and SHA256 checksums.",
    )
    p_package_output.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_package_output.add_argument(
        "--package-out",
        type=Path,
        help="ZIP output path. Defaults to <out-name>.cadence-package.zip beside the conversion output.",
    )
    p_package_output.add_argument(
        "--manifest-out",
        type=Path,
        help="Optional JSON manifest path written outside the ZIP.",
    )
    p_package_output.add_argument(
        "--include-extract",
        action="store_true",
        help="Include the temporary _extract input directory in the handoff package.",
    )
    p_package_output.add_argument(
        "--include-cadence-pcbenv",
        action="store_true",
        help="Include the generated _cadence_pcbenv directory in the handoff package.",
    )
    p_package_output.add_argument(
        "--require-cadence",
        action="store_true",
        help="Validate that Capture .olb and Allegro .dra/.psm assets are present before handoff.",
    )
    p_package_output.add_argument(
        "--require-3d",
        action="store_true",
        help="Validate complete real STEP/STP coverage before handoff.",
    )
    p_package_output.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from required component 3D coverage.",
    )
    p_package_output.add_argument(
        "--strict",
        action="store_true",
        help="Return nonzero when non-optional validation warnings remain.",
    )
    p_package_output.set_defaults(func=cmd_package_output)

    p_padstack_report = sub.add_parser(
        "padstack-report",
        aliases=["pad-report"],
        help="Summarize footprint padstack coverage by kind, role, shape, drill, and slot.",
    )
    p_padstack_source = p_padstack_report.add_mutually_exclusive_group(required=True)
    p_padstack_source.add_argument("--out", type=Path, help="Existing conversion output directory with reports/library.ir.json.")
    p_padstack_source.add_argument("--input", type=Path, help="Input file, zip, or directory to parse directly.")
    p_padstack_report.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_padstack_report.add_argument("--format", choices=["json", "markdown"], default="json", help="Format printed to stdout.")
    p_padstack_report.add_argument("--json-out", type=Path, help="Optional JSON report output path.")
    p_padstack_report.add_argument("--markdown-out", type=Path, help="Optional Markdown report output path.")
    p_padstack_report.add_argument("--write", action="store_true", help="Write reports/padstack_report.json and .md when --out is used.")
    p_padstack_report.add_argument("--require-kind", action="append", default=[], help="Require a pad kind such as smd or thru_hole.")
    p_padstack_report.add_argument("--require-role", action="append", default=[], help="Require a pad role such as electrical, shield, npth, mounting, or mechanical.")
    p_padstack_report.add_argument("--require-shape", action="append", default=[], help="Require a pad shape such as rect, circle, oval, roundrect, or octagon.")
    p_padstack_report.add_argument("--require-slot", action="store_true", help="Require at least one slotted pad.")
    p_padstack_report.add_argument("--require-drill", action="store_true", help="Require at least one drilled or slotted pad.")
    p_padstack_report.add_argument("--require-nonplated", action="store_true", help="Require at least one nonplated drill or slot.")
    p_padstack_report.add_argument("--strict", action="store_true", help="Return nonzero when coverage errors or warnings exist.")
    p_padstack_report.set_defaults(func=cmd_padstack_report)

    p_3d_review = sub.add_parser(
        "3d-review",
        aliases=["review-3d"],
        help="Write a per-footprint Cadence 3D Canvas review checklist.",
    )
    p_3d_review.add_argument("--out", required=True, type=Path, help="Conversion output directory.")
    p_3d_review.add_argument("--write", action="store_true", help="Write reports/3d_review.json and reports/3d_review.md.")
    p_3d_review.add_argument(
        "--signoff",
        type=Path,
        help="Optional JSON manual Cadence 3D Canvas signoff file to merge into the review report.",
    )
    p_3d_review.add_argument(
        "--signoff-template",
        type=Path,
        help="Write an editable signoff JSON template for all current real Canvas-ready footprints.",
    )
    p_3d_review.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Classify recognized mounting-hole and test-point PCB features as explicit 3D coverage exceptions.",
    )
    p_3d_review.add_argument(
        "--require-signoff",
        action="store_true",
        help="Return nonzero unless every footprint has all manual Canvas checks signed off.",
    )
    p_3d_review.add_argument(
        "--strict",
        action="store_true",
        help="Return nonzero unless every footprint is ready for real STEP Canvas review, or signed off when --signoff is used.",
    )
    p_3d_review.set_defaults(func=cmd_3d_review)

    p_missing_3d = sub.add_parser(
        "missing-3d",
        aliases=["model-report"],
        help="Report missing 3D models and generate a model_aliases.json template.",
    )
    p_missing_3d.add_argument("--input", required=True, type=Path, help="Input file, zip, or directory.")
    p_missing_3d.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory.")
    p_missing_3d.add_argument(
        "--model-aliases",
        type=Path,
        help="Optional existing JSON mapping to apply before reporting missing models.",
    )
    p_missing_3d.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_missing_3d.add_argument("--format", choices=["json", "markdown"], default="json", help="Format printed to stdout.")
    p_missing_3d.add_argument("--json-out", type=Path, help="Optional JSON report output path.")
    p_missing_3d.add_argument("--markdown-out", type=Path, help="Optional Markdown report output path.")
    p_missing_3d.add_argument("--alias-template", type=Path, help="Optional ready-to-edit model_aliases.json output path.")
    p_missing_3d.add_argument(
        "--repair-kit",
        type=Path,
        help="Optional directory for a fill-in 3D repair kit with models_todo.csv, models_todo.md, aliases, and README.",
    )
    p_missing_3d.add_argument("--strict", action="store_true", help="Return nonzero when models are missing or parsing fails.")
    p_missing_3d.set_defaults(func=cmd_missing_3d)

    p_request_3d_models = sub.add_parser(
        "request-3d-models",
        aliases=["3d-model-request", "model-request"],
        help="Generate a supplier-facing request package for unresolved repair-kit STEP/STP models.",
    )
    p_request_3d_models.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_request_3d_models.add_argument(
        "--build-out",
        type=Path,
        help="Optional existing conversion output with reports/3d_review.json for filtering 3D exceptions.",
    )
    p_request_3d_models.add_argument("--project-name", default="", help="Project label to print in the request package.")
    p_request_3d_models.add_argument(
        "--model-drop-dir",
        type=Path,
        help="Folder where supplied STEP/STP files should be placed. Defaults to the repair kit's models folder.",
    )
    p_request_3d_models.add_argument(
        "--output-name",
        default="",
        help="Future conversion output path to use in rerun commands after models are supplied.",
    )
    p_request_3d_models.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude rows already classified as mounting-hole or test-point 3D exceptions in --build-out.",
    )
    p_request_3d_models.add_argument(
        "--format",
        choices=["json", "markdown", "csv"],
        default="json",
        help="Format printed to stdout.",
    )
    p_request_3d_models.add_argument("--json-out", type=Path, help="Optional JSON request report output path.")
    p_request_3d_models.add_argument("--markdown-out", type=Path, help="Optional Markdown request report output path.")
    p_request_3d_models.add_argument("--csv-out", type=Path, help="Optional CSV request table output path.")
    p_request_3d_models.add_argument("--xlsx-out", type=Path, help="Optional XLSX request workbook output path.")
    p_request_3d_models.add_argument(
        "--write",
        action="store_true",
        help="Write 3d_model_request.json/.md/.csv/.xlsx into the repair kit unless explicit output paths are supplied.",
    )
    p_request_3d_models.set_defaults(func=cmd_request_3d_models)

    p_receive_3d_models = sub.add_parser(
        "receive-3d-models",
        aliases=["accept-3d-models"],
        help="Copy supplier-returned STEP/STP files into a repair kit from a request package.",
    )
    p_receive_3d_models.add_argument("--repair-kit", "--kit", required=True, type=Path, help="Repair-kit directory.")
    p_receive_3d_models.add_argument(
        "--request",
        type=Path,
        help="Request package path (.csv/.json/.xlsx). Defaults to the request package in the repair kit.",
    )
    p_receive_3d_models.add_argument(
        "--model-root",
        "--models",
        action="append",
        type=Path,
        help="Returned STEP/STP file or directory to scan. Repeat for multiple roots.",
    )
    p_receive_3d_models.add_argument("--dry-run", action="store_true", help="Report matches without copying files or writing aliases.")
    p_receive_3d_models.add_argument("--strict", action="store_true", help="Return nonzero unless the kit is ready after receiving models.")
    p_receive_3d_models.set_defaults(func=cmd_receive_3d_models)

    p_doctor = sub.add_parser("doctor", help="Preflight Cadence tools, input exports, models, and parser readiness.")
    p_doctor.add_argument("--input", type=Path, help="Optional input file, zip, or directory to inspect.")
    p_doctor.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory to inspect.")
    p_doctor.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_doctor.add_argument("--repair-kit", type=Path, help="Optional missing-3d repair-kit directory to supply input, models, and aliases.")
    p_doctor.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_doctor.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_doctor.add_argument("--out", type=Path, help="Optional JSON report path.")
    p_doctor.add_argument("--strict", action="store_true", help="Return nonzero when warnings exist.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_manifest = sub.add_parser("manifest", help="Create or append a batch manifest entry from one sample.")
    p_manifest.add_argument("--out", required=True, type=Path, help="Manifest JSON file to create or append.")
    p_manifest.add_argument("--name", required=True, help="Sample name to write into the manifest.")
    p_manifest.add_argument("--input", type=Path, help="Input file, zip, or directory.")
    p_manifest.add_argument("--models", type=Path, help="Optional STEP/STP/OBJ file, zip, or directory.")
    p_manifest.add_argument("--model-aliases", type=Path, help="Optional JSON mapping from footprint/model IDs to model files or keys.")
    p_manifest.add_argument("--repair-kit", type=Path, help="Optional missing-3d repair-kit directory to supply input, models, and aliases.")
    p_manifest.add_argument(
        "--model-root",
        "--model-roots",
        action="append",
        type=Path,
        help="External STEP/STP file or directory to import into --repair-kit before creating the sample. Repeat for multiple roots.",
    )
    p_manifest.add_argument(
        "--model-rules",
        type=Path,
        help="Optional JSON rules for mapping footprint keys to shared standard STEP/STP models during repair-kit import.",
    )
    p_manifest.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional existing Cadence asset directory or ZIP to record for batch conversion.",
    )
    p_manifest.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_manifest.add_argument(
        "--fetch-3d",
        action="store_true",
        help="Record EasyEDA STEP/OBJ fetching for this sample during batch runs.",
    )
    p_manifest.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="EasyEDA model format to record when --fetch-3d is enabled.",
    )
    p_manifest.add_argument("--model-cache", type=Path, help="Optional persistent EasyEDA 3D cache directory.")
    p_manifest.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Optional read-only EasyEDA/LCEDA cache directory to record for batch 3D recovery.",
    )
    p_manifest.add_argument(
        "--model-sources",
        type=Path,
        help="Optional JSON model-source file to record for batch 3D fetching.",
    )
    p_manifest.add_argument(
        "--fetch-3d-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds to record for batch 3D fetching.",
    )
    p_manifest.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Record LCSC metadata resolution for this sample during batch conversion.",
    )
    p_manifest.add_argument(
        "--lcsc-cache",
        type=Path,
        help="Optional persistent LCSC component metadata cache to record for batch runs.",
    )
    p_manifest.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds to record for LCSC metadata resolution.",
    )
    p_manifest.add_argument("--append", action="store_true", help="Append to an existing manifest instead of creating a new one.")
    p_manifest.add_argument("--run-cadence", action="store_true", help="Mark this sample for Cadence execution during batch runs.")
    p_manifest.add_argument("--require-3d", action="store_true", help="Mark this sample as requiring complete STEP/STP 3D models.")
    p_manifest.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Mark this sample to exclude recognized mounting-hole and test-point PCB features from complete 3D coverage.",
    )
    _add_padstack_requirement_args(p_manifest)
    p_manifest.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Mark this sample to generate placeholder STEP envelopes for missing 3D models during batch conversion.",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    p_batch = sub.add_parser("batch", help="Run a pilot manifest with multiple conversion samples.")
    p_batch.add_argument("--manifest", required=True, type=Path, help="JSON manifest describing conversion samples.")
    p_batch.add_argument("--out", required=True, type=Path, help="Batch output directory.")
    p_batch.add_argument("--model-aliases", type=Path, help="Optional default model-alias JSON for samples without their own alias file.")
    p_batch.add_argument(
        "--model-root",
        "--model-roots",
        action="append",
        type=Path,
        help="External STEP/STP file or directory to import into repair-kit samples before conversion. Repeat for multiple roots.",
    )
    p_batch.add_argument(
        "--model-rules",
        type=Path,
        help="Optional default JSON rules for mapping repair-kit footprint keys to shared standard STEP/STP models.",
    )
    p_batch.add_argument(
        "--cadence-assets",
        type=Path,
        action="append",
        help="Optional default Cadence asset directory or ZIP for samples without their own asset source.",
    )
    p_batch.add_argument("--default-unit", choices=["mm", "mil", "inch"], default="mm")
    p_batch.add_argument(
        "--fetch-3d",
        action="store_true",
        help="Download EasyEDA STEP/OBJ models for every sample unless sample configuration omits the request.",
    )
    p_batch.add_argument(
        "--model-format",
        choices=["step", "obj", "both"],
        default="step",
        help="EasyEDA model format to use with batch --fetch-3d.",
    )
    p_batch.add_argument("--model-cache", type=Path, help="Optional persistent EasyEDA 3D cache directory.")
    p_batch.add_argument(
        "--local-model-cache",
        type=Path,
        action="append",
        help="Optional default read-only EasyEDA/LCEDA cache directory for batch 3D recovery.",
    )
    p_batch.add_argument(
        "--model-sources",
        type=Path,
        help="Optional default JSON model-source file for batch 3D fetching.",
    )
    p_batch.add_argument(
        "--fetch-3d-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each batch 3D model request.",
    )
    p_batch.add_argument(
        "--resolve-lcsc-3d",
        action="store_true",
        help="Resolve missing EasyEDA 3D IDs from LCSC/Supplier Part properties for every sample.",
    )
    p_batch.add_argument(
        "--lcsc-cache",
        type=Path,
        help="Optional default persistent LCSC component metadata cache for batch runs.",
    )
    p_batch.add_argument(
        "--lcsc-timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for each batch LCSC metadata request.",
    )
    p_batch.add_argument("--strict", action="store_true", help="Return nonzero when any sample fails or mismatches expectations.")
    p_batch.add_argument("--run-cadence", action="store_true", help="Run Cadence helper commands for every sample unless disabled per sample.")
    p_batch.add_argument("--require-3d", action="store_true", help="Require complete STEP/STP 3D models for every sample unless disabled per sample.")
    p_batch.add_argument(
        "--allow-noncomponent-3d-exceptions",
        action="store_true",
        help="Exclude recognized mounting-hole and test-point PCB features from complete 3D coverage for every sample unless disabled per sample.",
    )
    p_batch.add_argument(
        "--require-signoff",
        action="store_true",
        help="Require batch samples to reach canvas_signed_off using manifest signoff files.",
    )
    p_batch.add_argument(
        "--signoff-template",
        type=Path,
        help="Write editable signoff template files for each batch sample after its 3D review is generated.",
    )
    _add_padstack_requirement_args(p_batch)
    p_batch.add_argument(
        "--synthesize-missing-3d",
        action="store_true",
        help="Generate marked STEP envelope placeholders for missing 3D models in every sample unless overridden.",
    )
    p_batch.add_argument("--cadence-root", type=Path, help="Cadence SPB root or bin directory.")
    p_batch.set_defaults(func=cmd_batch)

    return parser


def cmd_version(args: argparse.Namespace) -> int:
    print(f"jlc2cadence v{__version__}")
    return 0


def _load_library(args: argparse.Namespace, work_dir: Path | None = None):
    input_paths = prepare_input_files(args.input, work_dir=work_dir)
    design_files = find_design_files(input_paths)
    library = merge_libraries(
        parse_altium_design_files(design_files, default_unit=args.default_unit),
        parse_pads_design_files(design_files, default_unit=args.default_unit),
        parse_easyeda_pro_design_files(design_files, default_unit=args.default_unit),
        parse_easyeda_legacy_design_files(input_paths, default_unit=args.default_unit),
    )
    if not library.source_files:
        library.source_files.extend(str(p) for p in design_files)
    return library, input_paths, design_files


def cmd_inspect(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="jlc2cadence_inspect_") as tmp:
        library, input_paths, design_files = _load_library(args, work_dir=Path(tmp) / "input")
        model_aliases = load_model_aliases(args.model_aliases)
        model_assets = collect_model_assets(args.models, None) if args.models else []
        model_assets.extend(collect_model_alias_assets(model_aliases, None))
        model_assets = _dedupe_models(model_assets)
        attach_models(library, model_assets, model_aliases.mapping)
        ensure_components_for_footprints(library)
        report = validate_library(library)

        summary = {
            "version": f"v{__version__}",
            "input_files": len(input_paths),
            "design_files": [str(path) for path in design_files],
            "components": len(library.components),
            "footprints": len(library.footprints),
            "pins": _library_pin_count(library),
            "pads": _library_pad_count(library),
            "models": len(model_assets),
            "model_aliases": model_aliases.to_dict() if model_aliases.rows else None,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if report.errors else 0


def cmd_convert(args: argparse.Namespace) -> int:
    if not _apply_repair_kit_defaults(args, require_input=True):
        return 2
    out_dir = args.out.resolve()
    work_dir = out_dir / "_extract"
    reports_dir = out_dir / "reports"
    scripts_dir = out_dir / "scripts"
    capture_dir = out_dir / "capture"
    allegro_dir = out_dir / "allegro"
    models_dir = out_dir / "3d"

    for path in [out_dir, work_dir, reports_dir, scripts_dir, capture_dir, allegro_dir, models_dir]:
        path.mkdir(parents=True, exist_ok=True)

    library, input_paths, design_files = _load_library(args, work_dir=work_dir / "input")
    model_aliases = load_model_aliases(args.model_aliases)
    model_assets = []
    if args.models:
        model_assets.extend(collect_model_assets(args.models, models_dir))
    model_assets.extend(collect_model_assets(work_dir / "input", models_dir))
    lcsc_resolution_report = None
    if getattr(args, "resolve_lcsc_3d", False):
        lcsc_resolution_report = resolve_lcsc_3d_models(
            library,
            cache_dir=getattr(args, "lcsc_cache", None),
            timeout=float(getattr(args, "lcsc_timeout", 30.0)),
        )
        write_lcsc_3d_resolution_report(
            lcsc_resolution_report,
            models_dir / "easyeda_lcsc_3d_resolution.json",
        )
    cache_import_report = None
    local_cache_roots = _local_model_cache_roots(args)
    if local_cache_roots:
        cache_import_report = import_easyeda_cached_3d_models(
            library,
            local_cache_roots,
            models_dir,
            formats=[getattr(args, "model_format", "step")],
        )
        write_easyeda_cache_import_report(
            cache_import_report,
            models_dir / "easyeda_3d_cache_import.json",
        )
        model_assets.extend(cache_import_report.models)
    fetch_report = None
    if getattr(args, "fetch_3d", False):
        model_sources = load_model_sources(getattr(args, "model_sources", None))
        fetch_report = fetch_easyeda_3d_models(
            library,
            models_dir,
            formats=[getattr(args, "model_format", "step")],
            cache_dir=getattr(args, "model_cache", None),
            timeout=float(getattr(args, "fetch_3d_timeout", 30.0)),
            model_sources=model_sources,
        )
        write_easyeda_3d_fetch_report(fetch_report, models_dir / "easyeda_3d_download.json")
        model_assets.extend(fetch_report.models)
    model_assets.extend(collect_model_alias_assets(model_aliases, models_dir))
    model_assets = _dedupe_models(model_assets)

    attach_models(library, model_assets, model_aliases.mapping)
    placeholder_report = None
    if getattr(args, "synthesize_missing_3d", False):
        placeholder_report = synthesize_missing_step_models(library, models_dir)
        write_placeholder_3d_report(placeholder_report, models_dir / "placeholder_3d_report.json")
        model_assets.extend(placeholder_report.models)
        model_assets = _dedupe_models(model_assets)
        library.warnings.extend(placeholder_report.warnings)
    ensure_components_for_footprints(library)
    library.properties["tool_version"] = f"v{__version__}"
    library.properties["input_count"] = str(len(input_paths))
    library.properties["design_file_count"] = str(len(design_files))
    if model_aliases.rows:
        library.properties["model_alias_count"] = str(len(model_aliases.rows))
    if lcsc_resolution_report is not None:
        library.properties["easyeda_lcsc_3d_resolution_status"] = lcsc_resolution_report.status
        library.properties["easyeda_lcsc_3d_resolution_count"] = str(
            lcsc_resolution_report.resolved_count
        )
    if fetch_report is not None:
        library.properties["easyeda_3d_download_status"] = fetch_report.status
        library.properties["easyeda_3d_download_count"] = str(len(fetch_report.models))
    if cache_import_report is not None:
        library.properties["easyeda_3d_cache_status"] = cache_import_report.status
        library.properties["easyeda_3d_cache_model_count"] = str(len(cache_import_report.models))
    if placeholder_report is not None:
        library.properties["placeholder_3d_status"] = placeholder_report.status
        library.properties["placeholder_3d_model_count"] = str(placeholder_report.generated_count)

    validation = validate_library(library)
    if placeholder_report is not None:
        validation.errors.extend(placeholder_report.errors)
    summary = validation.to_dict()
    summary["pin_count"] = _library_pin_count(library)
    summary["pad_count"] = _library_pad_count(library)
    ir_path = reports_dir / "library.ir.json"
    ir_path.write_text(json.dumps(library.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    summary_path = reports_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_model_map(library, model_assets, models_dir, scripts_dir)
    write_capture_outputs(library, capture_dir, scripts_dir)
    write_allegro_outputs(library, allegro_dir, scripts_dir)
    cadence_asset_report = None
    cadence_asset_reuse = {}
    cadence_asset_roots = getattr(args, "cadence_assets", None)
    if cadence_asset_roots:
        cadence_asset_report = import_cadence_assets(library, cadence_asset_roots, out_dir)
        write_cadence_asset_import_report(
            cadence_asset_report,
            reports_dir / "cadence_asset_import.json",
        )
        cadence_asset_reuse = cadence_asset_reuse_statuses(cadence_asset_report)
    write_svg_previews(library, reports_dir)
    write_html_report(library, validation, reports_dir / "summary.html")

    cadence_tools = CadenceTools.discover(args.cadence_root)
    run_results = None
    capture_result = None
    step_mapping_result = None
    if args.run_cadence:
        if cadence_asset_reuse:
            run_results = run_symbol_build_plan(
                cadence_tools,
                allegro_dir / "symbols",
                reuse_statuses=cadence_asset_reuse,
            )
        else:
            run_results = run_symbol_build_plan(cadence_tools, allegro_dir / "symbols")
        step_mapping_result = run_step_mapping(cadence_tools, out_dir)
        if step_mapping_result.get("status") in {"mapped", "partial"}:
            if cadence_asset_reuse:
                run_results = compile_existing_package_symbols(
                    cadence_tools,
                    allegro_dir / "symbols",
                    reuse_statuses=cadence_asset_reuse,
                )
            else:
                run_results = compile_existing_package_symbols(cadence_tools, allegro_dir / "symbols")
        capture_result = run_capture_build(cadence_tools, out_dir)
    write_cadence_run_notes(
        cadence_tools=cadence_tools,
        scripts_dir=scripts_dir,
        out_dir=out_dir,
        run_requested=args.run_cadence,
        run_results=run_results,
        capture_result=capture_result,
        step_mapping_result=step_mapping_result,
    )
    step_validation_path = write_step_mapping_validation(out_dir, reports_dir / "3d_validation.json")
    output_validation_path = reports_dir / "output_validation.json"
    output_validation = write_output_validation_report(
        out_dir,
        output_validation_path,
        require_cadence=args.run_cadence,
        require_3d=getattr(args, "require_3d", False),
        allow_noncomponent_3d_exceptions=bool(getattr(args, "allow_noncomponent_3d_exceptions", False)),
    )

    print(f"Wrote conversion assets to {out_dir}")
    print(f"IR: {ir_path}")
    print(f"Report: {reports_dir / 'summary.html'}")
    step_validation = validate_step_mapping_output(out_dir)
    cadence_errors = _cadence_error_count(
        run_results,
        capture_result,
        step_mapping_result,
        args.run_cadence,
        library,
    )
    error_count = len(validation.errors) + len(step_validation.errors) + cadence_errors
    if (
        args.strict
        and lcsc_resolution_report is not None
        and lcsc_resolution_report.status not in {"complete", "no_lcsc_parts"}
    ):
        error_count = max(error_count, len(lcsc_resolution_report.errors) + 1)
    if args.strict and cadence_asset_report is not None:
        asset_errors = len(cadence_asset_report.errors) + len(cadence_asset_report.conflicts)
        if asset_errors:
            error_count = max(error_count, asset_errors)
    if output_validation.status == "failed" and error_count == 0:
        error_count = len(output_validation.errors) or 1
    if step_validation.errors:
        print(f"3D validation errors: {len(step_validation.errors)}", file=sys.stderr)
    if cadence_errors:
        print(f"Cadence execution errors: {cadence_errors}", file=sys.stderr)
    if output_validation.status == "failed":
        print(f"Output package errors: {len(output_validation.errors)}", file=sys.stderr)
    print(f"3D validation: {step_validation_path}")
    print(f"Output validation: {output_validation_path}")
    if error_count:
        print(f"Validation errors: {error_count}", file=sys.stderr)
        return 2 if args.strict else 0
    return 0


def cmd_fetch_3d(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="jlc2cadence_fetch_3d_") as tmp:
        library, _input_paths, _design_files = _load_library(args, work_dir=Path(tmp) / "input")
    out_dir = args.out.expanduser().resolve()
    lcsc_resolution_report = None
    if getattr(args, "resolve_lcsc_3d", False):
        lcsc_resolution_report = resolve_lcsc_3d_models(
            library,
            cache_dir=getattr(args, "lcsc_cache", None),
            timeout=float(getattr(args, "lcsc_timeout", 30.0)),
        )
        write_lcsc_3d_resolution_report(
            lcsc_resolution_report,
            out_dir / "easyeda_lcsc_3d_resolution.json",
        )
    cache_import_report = None
    local_cache_roots = _local_model_cache_roots(args)
    if local_cache_roots:
        cache_import_report = import_easyeda_cached_3d_models(
            library,
            local_cache_roots,
            out_dir,
            formats=[args.model_format],
        )
        write_easyeda_cache_import_report(
            cache_import_report,
            out_dir / "easyeda_3d_cache_import.json",
        )
    model_sources = load_model_sources(getattr(args, "model_sources", None))
    report = fetch_easyeda_3d_models(
        library,
        out_dir,
        formats=[args.model_format],
        cache_dir=args.model_cache,
        timeout=args.timeout,
        model_sources=model_sources,
    )
    report_path = write_easyeda_3d_fetch_report(
        report,
        out_dir / "easyeda_3d_download.json",
    )
    payload = report.to_dict()
    payload["report"] = str(report_path)
    if cache_import_report is not None:
        payload["cache_import"] = str(out_dir / "easyeda_3d_cache_import.json")
        payload["cache_import_status"] = cache_import_report.status
        payload["cache_import_model_count"] = len(cache_import_report.models)
    if lcsc_resolution_report is not None:
        payload["lcsc_resolution"] = str(out_dir / "easyeda_lcsc_3d_resolution.json")
        payload["lcsc_resolution_status"] = lcsc_resolution_report.status
        payload["lcsc_resolution_count"] = lcsc_resolution_report.resolved_count
        payload["lcsc_resolution_error_count"] = lcsc_resolution_report.error_count
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if args.strict and (
        report.status != "complete"
        or (
            lcsc_resolution_report is not None
            and lcsc_resolution_report.status not in {"complete", "no_lcsc_parts"}
        )
    ):
        return 2
    return 0


def cmd_scan_cache_3d(args: argparse.Namespace) -> int:
    library = None
    if args.input:
        with tempfile.TemporaryDirectory(prefix="jlc2cadence_scan_cache_3d_") as tmp:
            library, _input_paths, _design_files = _load_library(args, work_dir=Path(tmp) / "input")
    out_dir = args.out.expanduser().resolve()
    cache_roots = _local_model_cache_roots(args) or default_easyeda_cache_roots()
    report = import_easyeda_cached_3d_models(
        library,
        cache_roots,
        out_dir,
        formats=[args.model_format],
    )
    report_path = write_easyeda_cache_import_report(
        report,
        out_dir / "easyeda_3d_cache_import.json",
    )
    payload = _cache_import_stdout_payload(report)
    payload["report"] = str(report_path)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if args.strict and report.status not in {"complete", "found_payloads"}:
        return 2
    return 0


def _cadence_error_count(run_results, capture_result, step_mapping_result, run_requested: bool, library) -> int:
    if not run_requested:
        return 0
    errors = 0
    if library.footprints and not run_results:
        errors += 1
    for result in run_results or []:
        if result.get("status") not in {"created_psm", "reused_psm"}:
            errors += 1
    if step_mapping_result and int(step_mapping_result.get("entry_count", 0) or 0) > 0:
        if step_mapping_result.get("status") != "mapped":
            errors += 1
    if library.components and (not capture_result or capture_result.get("status") != "created_olb"):
        errors += 1
    return errors


def cmd_validate_3d(args: argparse.Namespace) -> int:
    report = validate_step_mapping_output(
        args.out,
        require_mapped=args.require_mapped or args.require_facet,
        require_facet=args.require_facet,
    )
    if args.write:
        report_path = args.out.expanduser().resolve() / "reports" / "3d_validation.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    if report.errors or (args.strict and report.warnings):
        return 2
    return 0


def cmd_update_3d(args: argparse.Namespace) -> int:
    out_dir = args.out.expanduser().resolve()
    tools = CadenceTools.discover(args.cadence_root)
    result = run_sym_3d_update(
        tools,
        out_dir,
        symbols_dir=args.symbols,
        destination_dir=args.destination,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["status"] in {
        "failed",
        "timeout",
        "missing_sym_3d_update",
        "missing_symbol_list",
        "missing_symbol_dir",
        "missing_model_dir",
    }:
        return 2
    if args.strict and result["status"] != "updated":
        if result["status"] == "no_updates":
            mapped_report = validate_step_mapping_output(out_dir, require_mapped=True)
            if not mapped_report.errors:
                return 0
        return 2
    return 0


def cmd_check_repair_kit(args: argparse.Namespace) -> int:
    report = check_model_repair_kit(args.repair_kit)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["errors"] or (args.strict and report["status"] != "ready"):
        return 2
    return 0


def cmd_sync_repair_kit(args: argparse.Namespace) -> int:
    report = sync_model_repair_kit(args.repair_kit, write=not args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["errors"] or (args.strict and report["after"]["status"] != "ready"):
        return 2
    return 0


def cmd_import_repair_kit_models(args: argparse.Namespace) -> int:
    report = import_model_repair_kit_models(
        args.repair_kit,
        args.model_root,
        write=not args.dry_run,
        model_rules=getattr(args, "model_rules", None),
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["errors"] or (args.strict and report["after"]["status"] != "ready"):
        return 2
    return 0


def cmd_suggest_model_rules(args: argparse.Namespace) -> int:
    report = suggest_model_repair_kit_rules(
        args.repair_kit,
        args.model_root,
        source=args.source,
    )
    if args.out:
        rules_path = args.out.expanduser().resolve()
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            json.dumps({"rules": report.get("rules", [])}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        report["rules_out"] = str(rules_path)
    if args.report_out:
        report_path = args.report_out.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report["report_out"] = str(report_path)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["errors"] or (args.strict and not report.get("rules")):
        return 2
    return 0


def cmd_repair_kit_build(args: argparse.Namespace) -> int:
    out_dir = args.out.expanduser().resolve()
    signoff_path = getattr(args, "signoff", None)
    signoff_template_path = getattr(args, "signoff_template", None)
    require_signoff = bool(getattr(args, "require_signoff", False))
    allow_noncomponent = bool(getattr(args, "allow_noncomponent_3d_exceptions", False))
    import_report = None
    if args.model_root:
        import_report = import_model_repair_kit_models(
            args.repair_kit,
            args.model_root,
            write=True,
            model_rules=getattr(args, "model_rules", None),
        )
    sync_report = None
    if not args.no_sync:
        sync_report = sync_model_repair_kit(args.repair_kit, write=True)
    repair_report = check_model_repair_kit(args.repair_kit)
    convert_args = argparse.Namespace(
        input=None,
        models=None,
        model_aliases=None,
        repair_kit=args.repair_kit,
        signoff=signoff_path,
        cadence_assets=getattr(args, "cadence_assets", None),
        out=out_dir,
        default_unit=args.default_unit,
        strict=args.strict,
        run_cadence=args.run_cadence,
        require_3d=args.require_3d,
        cadence_root=args.cadence_root,
        fetch_3d=False,
        model_format="step",
        model_cache=None,
        local_model_cache=[],
        model_sources=None,
        fetch_3d_timeout=30.0,
        resolve_lcsc_3d=bool(getattr(args, "resolve_lcsc_3d", False)),
        lcsc_cache=getattr(args, "lcsc_cache", None),
        lcsc_timeout=float(getattr(args, "lcsc_timeout", 30.0)),
        require_signoff=require_signoff,
        allow_noncomponent_3d_exceptions=allow_noncomponent,
        synthesize_missing_3d=args.synthesize_missing_3d,
    )
    convert_stdout = io.StringIO()
    with contextlib.redirect_stdout(convert_stdout):
        convert_code = cmd_convert(convert_args)
    output_validation = write_output_validation_report(
        out_dir,
        out_dir / "reports" / "output_validation.json",
        require_cadence=bool(args.require_cadence or args.run_cadence),
        require_3d=bool(args.require_3d),
        allow_noncomponent_3d_exceptions=allow_noncomponent,
    )
    review_report = write_3d_review_report(
        out_dir,
        signoff_path=signoff_path,
        allow_noncomponent_3d_exceptions=allow_noncomponent,
    )
    template_written = ""
    if signoff_template_path is not None:
        template_written = str(
            write_3d_signoff_template(
                out_dir,
                signoff_template_path,
                allow_noncomponent_3d_exceptions=allow_noncomponent,
            )
        )
    payload = {
        "version": f"v{__version__}",
        "status": output_validation.status,
        "repair_kit": str(args.repair_kit.expanduser().resolve()),
        "out": str(out_dir),
        "allow_noncomponent_3d_exceptions": allow_noncomponent,
        "signoff_file": str(signoff_path.expanduser().resolve()) if signoff_path else "",
        "signoff_template": template_written,
        "require_signoff": require_signoff,
        "import": _repair_kit_build_import_summary(import_report),
        "sync": _repair_kit_build_sync_summary(sync_report),
        "3d_review": {
            **_three_d_review_summary(review_report, out_dir / "reports"),
            "signoff_template": template_written,
        },
        "repair_kit_status": repair_report.get("status", ""),
        "repair_kit_counts": repair_report.get("counts", {}),
        "convert_exit_code": convert_code,
        "convert_stdout": [line for line in convert_stdout.getvalue().splitlines() if line.strip()],
        "output_validation": output_validation.to_dict(),
        "report": str(out_dir / "reports" / "repair_kit_build.json"),
        "errors": [],
        "warnings": [],
    }
    if sync_report and sync_report.get("errors"):
        payload["errors"].extend(f"sync: {error}" for error in sync_report["errors"])
    if import_report and import_report.get("errors"):
        payload["errors"].extend(f"import: {error}" for error in import_report["errors"])
    if repair_report.get("errors"):
        payload["errors"].extend(f"repair-kit: {error}" for error in repair_report["errors"])
    if review_report.errors:
        payload["errors"].extend(f"3d-review: {error}" for error in review_report.errors)
    if signoff_path is not None and review_report.status == CANVAS_SIGNOFF_FAILED:
        payload["errors"].append(
            f"3d-review signoff failed: {review_report.signoff_file or signoff_path}"
        )
    if require_signoff and not _three_d_review_signoff_ok(review_report.status, allow_noncomponent):
        if signoff_path is None:
            payload["errors"].append("3d-review signoff required but no signoff file was provided")
        else:
            payload["errors"].append(f"3d-review signoff required but status is {review_report.status}")
    if convert_code:
        payload["errors"].append(f"convert returned exit code {convert_code}")
    payload["errors"].extend(output_validation.errors)
    payload["warnings"].extend(output_validation.warnings)
    if args.strict and payload["warnings"]:
        payload["errors"].append("strict mode treats output validation warnings as failures")
    if payload["errors"]:
        payload["status"] = "failed"
    report_path = out_dir / "reports" / "repair_kit_build.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 2 if payload["errors"] else 0


def cmd_one_shot(args: argparse.Namespace) -> int:
    if not _apply_repair_kit_defaults(args, require_input=True):
        return 2

    out_dir = args.out.expanduser().resolve()
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    require_cadence = bool(args.require_cadence or args.run_cadence)
    allow_noncomponent = bool(getattr(args, "allow_noncomponent_3d_exceptions", False))
    model_roots = _flow_model_roots(args)
    model_rules = getattr(args, "model_rules", None)

    payload = {
        "version": f"v{__version__}",
        "command": "one-shot",
        "status": "running",
        "input": str(args.input.expanduser().resolve()) if getattr(args, "input", None) else "",
        "models": str(args.models.expanduser().resolve()) if getattr(args, "models", None) else "",
        "model_aliases": str(args.model_aliases.expanduser().resolve()) if getattr(args, "model_aliases", None) else "",
        "repair_kit": str(args.repair_kit.expanduser().resolve()) if getattr(args, "repair_kit", None) else "",
        "model_roots": [str(path) for path in model_roots],
        "model_rules": str(model_rules.expanduser().resolve()) if model_rules else "",
        "cadence_assets": [str(path.expanduser().resolve()) for path in (getattr(args, "cadence_assets", None) or [])],
        "out": str(out_dir),
        "strict": bool(args.strict),
        "run_cadence": bool(args.run_cadence),
        "require_cadence": require_cadence,
        "require_3d": bool(args.require_3d),
        "allow_noncomponent_3d_exceptions": allow_noncomponent,
        "fetch_3d": bool(args.fetch_3d),
        "resolve_lcsc_3d": bool(getattr(args, "resolve_lcsc_3d", False)),
        "synthesize_missing_3d": bool(getattr(args, "synthesize_missing_3d", False)),
        "signoff_template": str(args.signoff_template.expanduser().resolve()) if getattr(args, "signoff_template", None) else "",
        "steps": {},
        "reports": {
            "one_shot": str(reports_dir / "one_shot.json"),
            "doctor": str(reports_dir / "one_shot_doctor.json"),
            "output_validation": str(reports_dir / "output_validation.json"),
            "audit_json": str(reports_dir / "audit.json"),
            "audit_markdown": str(reports_dir / "audit.md"),
            "3d_review_json": str(reports_dir / "3d_review.json"),
            "3d_review_markdown": str(reports_dir / "3d_review.md"),
            "signoff_template": str(args.signoff_template.expanduser().resolve()) if getattr(args, "signoff_template", None) else "",
            "repair_kit_import": str(reports_dir / "one_shot_repair_kit_import.json"),
            "repair_kit_sync": str(reports_dir / "one_shot_repair_kit_sync.json"),
        },
        "errors": [],
        "warnings": [],
    }

    if model_roots or model_rules:
        if not model_roots:
            payload["steps"]["repair_kit_import"] = {
                "status": "skipped_no_model_root",
                "exit_code": 0,
                "model_rules": str(model_rules.expanduser().resolve()) if model_rules else "",
            }
            payload["warnings"].append("--model-rules was supplied without --model-root; no rule import was performed")
        else:
            try:
                import_report, sync_report = _prepare_repair_kit_models(
                    args.repair_kit,
                    model_roots,
                    model_rules,
                )
                import_path = reports_dir / "one_shot_repair_kit_import.json"
                sync_path = reports_dir / "one_shot_repair_kit_sync.json"
                import_path.write_text(
                    json.dumps(import_report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                sync_path.write_text(
                    json.dumps(sync_report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                import_summary = _repair_kit_build_import_summary(import_report)
                payload["steps"]["repair_kit_import"] = {
                    **import_summary,
                    "status": "complete" if not import_report.get("errors") else "failed",
                    "exit_code": 2 if import_report.get("errors") else 0,
                    "report": str(import_path),
                    "sync": _repair_kit_build_sync_summary(sync_report),
                    "sync_report": str(sync_path),
                }
                payload["warnings"].extend(import_report.get("warnings", []))
                payload["warnings"].extend(sync_report.get("warnings", []))
                payload["errors"].extend(
                    f"repair-kit import: {error}" for error in import_report.get("errors", [])
                )
                payload["errors"].extend(
                    f"repair-kit sync: {error}" for error in sync_report.get("errors", [])
                )
            except Exception as exc:  # pragma: no cover - defensive for user-supplied paths
                payload["steps"]["repair_kit_import"] = {
                    "status": "failed",
                    "exit_code": 2,
                    "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                }
                payload["errors"].append("repair-kit model import raised an exception")

    if args.skip_doctor:
        payload["steps"]["doctor"] = {"status": "skipped", "exit_code": 0}
    else:
        doctor_path = reports_dir / "one_shot_doctor.json"
        try:
            doctor_report = build_doctor_report(
                input_path=args.input,
                models_path=args.models,
                model_aliases_path=args.model_aliases,
                default_unit=args.default_unit,
                cadence_root=args.cadence_root,
            )
            doctor_path.write_text(
                json.dumps(doctor_report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            doctor_status = str(doctor_report.get("status", ""))
            doctor_exit_code = 2 if doctor_status == "failed" else 0
            payload["steps"]["doctor"] = {
                "status": doctor_status,
                "exit_code": doctor_exit_code,
                "report": str(doctor_path),
            }
            if doctor_status == "failed":
                payload["errors"].append("doctor preflight failed")
        except Exception as exc:  # pragma: no cover - defensive for user-supplied inputs
            payload["steps"]["doctor"] = {
                "status": "failed",
                "exit_code": 2,
                "report": str(doctor_path),
                "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
            }
            payload["errors"].append("doctor preflight raised an exception")

    convert_args = _one_shot_convert_args(args, out_dir)
    convert_stdout = io.StringIO()
    convert_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(convert_stdout):
            with contextlib.redirect_stderr(convert_stderr):
                convert_code = cmd_convert(convert_args)
        convert_exception = ""
    except Exception as exc:  # pragma: no cover - defensive for user-supplied inputs
        convert_code = 2
        convert_exception = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    payload["steps"]["convert"] = {
        "status": "complete" if convert_code == 0 else "failed",
        "exit_code": convert_code,
        "stdout": [line for line in convert_stdout.getvalue().splitlines() if line.strip()],
        "stderr": [line for line in convert_stderr.getvalue().splitlines() if line.strip()],
    }
    if convert_exception:
        payload["steps"]["convert"]["exception"] = convert_exception
    if convert_code:
        payload["errors"].append(f"convert returned exit code {convert_code}")

    update_step = _one_shot_update_3d(args, out_dir)
    payload["steps"]["update_3d"] = update_step
    if int(update_step.get("exit_code", 0) or 0):
        payload["errors"].append(f"update-3d returned exit code {update_step.get('exit_code')}")

    try:
        output_validation = write_output_validation_report(
            out_dir,
            reports_dir / "output_validation.json",
            require_cadence=require_cadence,
            require_3d=bool(args.require_3d),
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
        payload["steps"]["verify_output"] = {
            "status": output_validation.status,
            "exit_code": 2 if output_validation.errors or (args.strict and output_validation.warnings) else 0,
            "report": str(reports_dir / "output_validation.json"),
            "errors": len(output_validation.errors),
            "warnings": len(output_validation.warnings),
            "counts": dict(output_validation.counts),
        }
        payload["output_validation"] = output_validation.to_dict()
        payload["errors"].extend(output_validation.errors)
        payload["warnings"].extend(output_validation.warnings)
    except Exception as exc:  # pragma: no cover - defensive for partial outputs
        payload["steps"]["verify_output"] = {
            "status": "failed",
            "exit_code": 2,
            "report": str(reports_dir / "output_validation.json"),
            "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        }
        payload["errors"].append("output validation raised an exception")

    try:
        review_report = write_3d_review_report(
            out_dir,
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
        review_summary = _three_d_review_summary(review_report, reports_dir)
        if getattr(args, "signoff_template", None):
            template_path = write_3d_signoff_template(
                out_dir,
                args.signoff_template,
                allow_noncomponent_3d_exceptions=allow_noncomponent,
            )
            review_summary["signoff_template"] = str(template_path)
            payload["reports"]["signoff_template"] = str(template_path)
        payload["steps"]["3d_review"] = {
            **review_summary,
            "exit_code": 2
            if review_report.errors or (args.strict and not _three_d_review_strict_ok(review_report.status, allow_noncomponent))
            else 0,
        }
        payload["3d_review"] = review_summary
        payload["errors"].extend(f"3d-review: {error}" for error in review_report.errors)
        payload["warnings"].extend(f"3d-review: {warning}" for warning in review_report.warnings)
    except Exception as exc:  # pragma: no cover - defensive for partial outputs
        payload["steps"]["3d_review"] = {
            "status": "failed",
            "exit_code": 2,
            "report": str(reports_dir / "3d_review.json"),
            "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        }
        payload["errors"].append("3d-review raised an exception")

    try:
        audit_report = write_output_audit(
            out_dir,
            require_cadence=require_cadence,
            require_3d=bool(args.require_3d),
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
        audit_errors = audit_report.issues.get("errors", [])
        audit_warnings = audit_report.issues.get("warnings", [])
        payload["steps"]["audit"] = {
            "status": audit_report.status,
            "exit_code": 2 if audit_errors or (args.strict and audit_warnings) else 0,
            "audit_json": str(reports_dir / "audit.json"),
            "audit_markdown": str(reports_dir / "audit.md"),
            "errors": len(audit_errors),
            "warnings": len(audit_warnings),
        }
        payload["audit"] = {
            "status": audit_report.status,
            "counts": dict(audit_report.counts),
            "errors": len(audit_errors),
            "warnings": len(audit_warnings),
        }
        payload["errors"].extend(f"audit: {error}" for error in audit_errors)
        payload["warnings"].extend(f"audit: {warning}" for warning in audit_warnings)
    except Exception as exc:  # pragma: no cover - defensive for partial outputs
        payload["steps"]["audit"] = {
            "status": "failed",
            "exit_code": 2,
            "audit_json": str(reports_dir / "audit.json"),
            "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        }
        payload["errors"].append("audit raised an exception")

    payload["errors"] = list(dict.fromkeys(str(error) for error in payload["errors"] if str(error).strip()))
    payload["warnings"] = list(dict.fromkeys(str(warning) for warning in payload["warnings"] if str(warning).strip()))
    if args.strict and payload["warnings"]:
        payload["errors"].append("strict mode treats warnings as failures")
        payload["errors"] = list(dict.fromkeys(payload["errors"]))

    payload["duration_seconds"] = round(perf_counter() - started, 3)
    if payload["errors"]:
        payload["status"] = "failed"
    elif payload["warnings"]:
        payload["status"] = "complete_with_warnings"
    else:
        payload["status"] = "complete"
    one_shot_path = reports_dir / "one_shot.json"
    one_shot_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    compact = {
        "version": payload["version"],
        "status": payload["status"],
        "out": payload["out"],
        "report": str(one_shot_path),
        "doctor_status": payload["steps"].get("doctor", {}).get("status", ""),
        "convert_exit_code": payload["steps"].get("convert", {}).get("exit_code"),
        "update_3d_status": payload["steps"].get("update_3d", {}).get("status", ""),
        "output_status": payload.get("output_validation", {}).get("status", ""),
        "audit_status": payload.get("audit", {}).get("status", ""),
        "3d_review_status": payload.get("3d_review", {}).get("status", ""),
        "signoff_template": payload.get("signoff_template", ""),
        "errors": len(payload["errors"]),
        "warnings": len(payload["warnings"]),
    }
    print(json.dumps(compact, indent=2, ensure_ascii=False))
    return 2 if payload["errors"] else 0


def cmd_deliver(args: argparse.Namespace) -> int:
    """Run the one-shot flow and package its output as one handoff artifact."""
    out_dir = args.out.expanduser().resolve()
    one_shot_stdout = io.StringIO()
    one_shot_stderr = io.StringIO()
    with contextlib.redirect_stdout(one_shot_stdout), contextlib.redirect_stderr(one_shot_stderr):
        one_shot_code = cmd_one_shot(args)

    one_shot_text = one_shot_stdout.getvalue().strip()
    try:
        one_shot_result = json.loads(one_shot_text) if one_shot_text else {}
    except json.JSONDecodeError:
        one_shot_result = {"raw_stdout": [line for line in one_shot_text.splitlines() if line.strip()]}

    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "deliver.json"
    prepackage_payload = {
        "version": f"v{__version__}",
        "command": "deliver",
        "status": "packaging",
        "one_shot_exit_code": one_shot_code,
        "one_shot": one_shot_result,
        "one_shot_stderr": [line for line in one_shot_stderr.getvalue().splitlines() if line.strip()],
        "report": str(report_path),
    }
    report_path.write_text(json.dumps(prepackage_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    package_result: dict[str, object] = {}
    package_exception = ""
    try:
        package_result = build_handoff_bundle(
            out_dir,
            package_out=args.package_out,
            manifest_out=args.manifest_out,
            include_extract=args.include_extract,
            include_cadence_pcbenv=args.include_cadence_pcbenv,
            require_cadence=bool(args.require_cadence or args.run_cadence),
            require_3d=bool(args.require_3d),
            allow_noncomponent_3d_exceptions=bool(args.allow_noncomponent_3d_exceptions),
        )
    except Exception as exc:  # pragma: no cover - defensive for user-supplied paths
        package_exception = "".join(traceback.format_exception_only(type(exc), exc)).strip()

    validation = package_result.get("validation", {}) if package_result else {}
    validation = validation if isinstance(validation, dict) else {}
    package_warnings = list(validation.get("warnings", []) or [])
    if args.require_3d:
        strict_package_warnings = package_warnings
    else:
        strict_package_warnings = [warning for warning in package_warnings if not _is_optional_3d_warning(warning)]
    package_errors = list(validation.get("errors", []) or [])
    package_code = 2 if package_exception or package_errors or (args.strict and strict_package_warnings) else 0

    errors: list[str] = []
    if one_shot_code:
        errors.append(f"one-shot returned exit code {one_shot_code}")
    if package_exception:
        errors.append(f"handoff packaging raised an exception: {package_exception}")
    if package_errors:
        errors.extend(f"package: {error}" for error in package_errors)
    warnings = list(package_warnings)
    if one_shot_result.get("warnings"):
        warnings.append(f"one-shot reported {one_shot_result['warnings']} warning(s)")
    if args.strict and strict_package_warnings:
        errors.append("strict mode treats handoff validation warnings as failures")
    errors = list(dict.fromkeys(str(error) for error in errors if str(error).strip()))
    warnings = list(dict.fromkeys(str(warning) for warning in warnings if str(warning).strip()))

    if errors:
        status = "failed"
    elif warnings:
        status = "complete_with_warnings"
    else:
        status = "complete"

    payload = {
        "version": f"v{__version__}",
        "command": "deliver",
        "status": status,
        "one_shot_exit_code": one_shot_code,
        "one_shot": one_shot_result,
        "one_shot_stderr": [line for line in one_shot_stderr.getvalue().splitlines() if line.strip()],
        "package_exit_code": package_code,
        "package": package_result,
        "package_exception": package_exception,
        "package_path": str(package_result.get("package", "")) if package_result else "",
        "manifest_path": str(package_result.get("manifest", "")) if package_result else "",
        "report": str(report_path),
        "errors": errors,
        "warnings": warnings,
    }
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 2 if one_shot_code or package_code or errors else 0


def _one_shot_convert_args(args: argparse.Namespace, out_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        input=args.input,
        models=args.models,
        model_aliases=args.model_aliases,
        repair_kit=args.repair_kit,
        cadence_assets=getattr(args, "cadence_assets", None),
        out=out_dir,
        default_unit=args.default_unit,
        strict=args.strict,
        run_cadence=args.run_cadence,
        require_3d=args.require_3d,
        cadence_root=args.cadence_root,
        fetch_3d=args.fetch_3d,
        model_format=args.model_format,
        model_cache=args.model_cache,
        local_model_cache=getattr(args, "local_model_cache", None) or [],
        model_sources=args.model_sources,
        fetch_3d_timeout=args.fetch_3d_timeout,
        resolve_lcsc_3d=bool(getattr(args, "resolve_lcsc_3d", False)),
        lcsc_cache=getattr(args, "lcsc_cache", None),
        lcsc_timeout=float(getattr(args, "lcsc_timeout", 30.0)),
        synthesize_missing_3d=bool(getattr(args, "synthesize_missing_3d", False)),
    )


def _one_shot_update_3d(args: argparse.Namespace, out_dir: Path) -> dict:
    if getattr(args, "skip_update_3d", False):
        return {"status": "skipped_user_requested", "exit_code": 0}
    if not getattr(args, "run_cadence", False):
        return {"status": "skipped_no_cadence_run", "exit_code": 0}
    step_report = validate_step_mapping_output(out_dir)
    entry_count = len(step_report.entries)
    if entry_count == 0:
        return {"status": "skipped_no_3d_mapping", "exit_code": 0, "entry_count": 0}
    update_args = argparse.Namespace(
        out=out_dir,
        symbols=None,
        destination=None,
        cadence_root=args.cadence_root,
        strict=args.strict,
    )
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exit_code = cmd_update_3d(update_args)
        raw_stdout = stdout.getvalue().strip()
        try:
            result = json.loads(raw_stdout) if raw_stdout else {}
        except json.JSONDecodeError:
            result = {"raw_stdout": raw_stdout}
        return {
            "status": str(result.get("status", "complete" if exit_code == 0 else "failed")),
            "exit_code": exit_code,
            "entry_count": entry_count,
            "result": result,
            "stdout": [line for line in raw_stdout.splitlines() if line.strip()],
        }
    except Exception as exc:  # pragma: no cover - defensive for local Cadence failures
        return {
            "status": "failed",
            "exit_code": 2,
            "entry_count": entry_count,
            "exception": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
            "stdout": [line for line in stdout.getvalue().splitlines() if line.strip()],
        }


def cmd_verify_output(args: argparse.Namespace) -> int:
    report = validate_output_package(
        args.out,
        require_cadence=args.require_cadence,
        require_3d=args.require_3d,
        allow_noncomponent_3d_exceptions=args.allow_noncomponent_3d_exceptions,
    )
    if args.write:
        out_path = args.out.expanduser().resolve() / "reports" / "output_validation.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    strict_warnings = _strict_output_warnings(report)
    if report.errors or (args.strict and strict_warnings):
        return 2
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    if args.write:
        report = write_output_audit(
            args.out,
            require_cadence=args.require_cadence,
            require_3d=args.require_3d,
            allow_noncomponent_3d_exceptions=args.allow_noncomponent_3d_exceptions,
        )
        reports_dir = args.out.expanduser().resolve() / "reports"
        payload = {
            "version": report.version,
            "status": report.status,
            "audit_json": str(reports_dir / "audit.json"),
            "audit_markdown": str(reports_dir / "audit.md"),
            "errors": len(report.issues.get("errors", [])),
            "warnings": len(report.issues.get("warnings", [])),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        report = build_output_audit(
            args.out,
            require_cadence=args.require_cadence,
            require_3d=args.require_3d,
            allow_noncomponent_3d_exceptions=args.allow_noncomponent_3d_exceptions,
        )
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    if report.issues.get("errors") or (args.strict and report.issues.get("warnings")):
        return 2
    return 0


def cmd_package_output(args: argparse.Namespace) -> int:
    result = build_handoff_bundle(
        args.out,
        package_out=args.package_out,
        manifest_out=args.manifest_out,
        include_extract=args.include_extract,
        include_cadence_pcbenv=args.include_cadence_pcbenv,
        require_cadence=args.require_cadence,
        require_3d=args.require_3d,
        allow_noncomponent_3d_exceptions=args.allow_noncomponent_3d_exceptions,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    validation = result.get("validation", {}) or {}
    errors = list(validation.get("errors", []) or [])
    warnings = list(validation.get("warnings", []) or [])
    strict_warnings = warnings if args.require_3d else [warning for warning in warnings if not _is_optional_3d_warning(warning)]
    if errors or (args.strict and strict_warnings):
        return 2
    return 0


def cmd_padstack_report(args: argparse.Namespace) -> int:
    if args.out:
        out_dir = args.out.expanduser().resolve()
        ir_path = out_dir / "reports" / "library.ir.json"
        if not ir_path.exists():
            print(
                json.dumps(
                    {
                        "version": f"v{__version__}",
                        "status": "failed",
                        "errors": [f"missing IR report: {ir_path}"],
                        "warnings": [],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 2
        library_data = json.loads(ir_path.read_text(encoding="utf-8-sig"))
        source = {"type": "output", "path": str(out_dir), "ir": str(ir_path)}
    else:
        with tempfile.TemporaryDirectory(prefix="jlc2cadence_padstack_report_") as tmp:
            library, input_paths, design_files = _load_library(args, work_dir=Path(tmp) / "input")
            source = {
                "type": "input",
                "path": str(args.input.expanduser().resolve()),
                "input_files": len(input_paths),
                "design_files": [str(path) for path in design_files],
            }
            library_data = library.to_dict()

    report = build_padstack_coverage_report(
        library_data,
        source=source,
        require_kinds=args.require_kind,
        require_roles=args.require_role,
        require_shapes=args.require_shape,
        require_slot=args.require_slot,
        require_drill=args.require_drill,
        require_nonplated=args.require_nonplated,
    )
    if args.write and not args.out:
        report["warnings"].append("--write is ignored unless --out is supplied; use --json-out or --markdown-out for parsed inputs.")
        if report["status"] == "complete":
            report["status"] = "warning"

    json_path = args.json_out.expanduser().resolve() if args.json_out else None
    markdown_path = args.markdown_out.expanduser().resolve() if args.markdown_out else None
    if args.write and args.out:
        reports_dir = args.out.expanduser().resolve() / "reports"
        json_path = json_path or reports_dir / "padstack_report.json"
        markdown_path = markdown_path or reports_dir / "padstack_report.md"
    outputs: dict[str, str] = {}
    if json_path:
        outputs["json"] = str(json_path)
    if markdown_path:
        outputs["markdown"] = str(markdown_path)
    if outputs:
        report["outputs"] = outputs

    if json_path:
        write_padstack_coverage_report_json(report, json_path)
    if markdown_path:
        write_padstack_coverage_report_markdown(report, markdown_path)

    if args.format == "markdown":
        print(render_padstack_coverage_markdown(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("errors") or (args.strict and report.get("warnings")):
        return 2
    return 0


def cmd_3d_review(args: argparse.Namespace) -> int:
    template_path = None
    allow_noncomponent = bool(getattr(args, "allow_noncomponent_3d_exceptions", False))
    if args.signoff_template:
        template_path = write_3d_signoff_template(
            args.out,
            args.signoff_template,
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
    if args.write:
        report = write_3d_review_report(
            args.out,
            signoff_path=args.signoff,
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
        payload = _three_d_review_summary(report, args.out.expanduser().resolve() / "reports")
    else:
        report = build_3d_review_report(
            args.out,
            signoff_path=args.signoff,
            allow_noncomponent_3d_exceptions=allow_noncomponent,
        )
        payload = report.to_dict()
    if template_path is not None:
        payload["signoff_template"] = str(template_path)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if report.errors:
        return 2
    if args.require_signoff and not _three_d_review_signoff_ok(report.status, allow_noncomponent):
        return 2
    if args.strict and not _three_d_review_strict_ok(report.status, allow_noncomponent):
        return 2
    return 0


def cmd_missing_3d(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="jlc2cadence_missing_3d_") as tmp:
        library, input_paths, _design_files = _load_library(args, work_dir=Path(tmp) / "input")
        model_aliases = load_model_aliases(args.model_aliases)
        model_assets = []
        if args.models:
            model_assets.extend(collect_model_assets(args.models, None))
        for path in input_paths:
            model_assets.extend(collect_model_assets(path, None))
        model_assets.extend(collect_model_alias_assets(model_aliases, None))
        model_assets = _dedupe_models(model_assets)
        attach_models(library, model_assets, model_aliases.mapping)
        ensure_components_for_footprints(library)
        validation = validate_library(library)
        report = build_missing_3d_report(
            library,
            model_assets,
            model_aliases=model_aliases.mapping,
            input_path=args.input,
            models_path=args.models,
            alias_path=args.model_aliases,
        )
        report["library_errors"] = list(validation.errors)
        report["library_warnings"] = list(validation.warnings)

    json_path = args.json_out.expanduser().resolve() if args.json_out else None
    markdown_path = args.markdown_out.expanduser().resolve() if args.markdown_out else None
    alias_path = args.alias_template.expanduser().resolve() if args.alias_template else None
    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if markdown_path:
        write_missing_3d_markdown(report, markdown_path)
    if alias_path:
        alias_path.parent.mkdir(parents=True, exist_ok=True)
        alias_path.write_text(
            json.dumps(report["model_aliases_template"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.repair_kit:
        repair_kit = write_model_repair_kit(report, args.repair_kit)
        report["repair_kit"] = repair_kit["directory"]
        report["repair_kit_files"] = repair_kit

    if args.format == "markdown":
        print(_missing_3d_markdown_text(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["library_errors"]:
        return 2
    if args.strict and report["counts"]["missing_models"] > 0:
        return 2
    return 0


def cmd_request_3d_models(args: argparse.Namespace) -> int:
    repair_kit = args.repair_kit.expanduser().resolve()
    report = build_3d_model_request_report(
        repair_kit,
        project_name=args.project_name,
        build_out=args.build_out,
        model_drop_dir=args.model_drop_dir,
        output_name=args.output_name,
        allow_noncomponent_3d_exceptions=bool(args.allow_noncomponent_3d_exceptions),
    )

    json_path = args.json_out.expanduser().resolve() if args.json_out else None
    markdown_path = args.markdown_out.expanduser().resolve() if args.markdown_out else None
    csv_path = args.csv_out.expanduser().resolve() if args.csv_out else None
    xlsx_path = args.xlsx_out.expanduser().resolve() if args.xlsx_out else None
    if args.write:
        json_path = json_path or repair_kit / "3d_model_request.json"
        markdown_path = markdown_path or repair_kit / "3d_model_request.md"
        csv_path = csv_path or repair_kit / "3d_model_request.csv"
        xlsx_path = xlsx_path or repair_kit / "3d_model_request.xlsx"
    planned_outputs = {}
    if json_path:
        planned_outputs["json"] = str(json_path.expanduser().resolve())
    if markdown_path:
        planned_outputs["markdown"] = str(markdown_path.expanduser().resolve())
    if csv_path:
        planned_outputs["csv"] = str(csv_path.expanduser().resolve())
    if xlsx_path:
        planned_outputs["xlsx"] = str(xlsx_path.expanduser().resolve())
    if planned_outputs:
        report["outputs"] = planned_outputs
    outputs = write_3d_model_request_report(
        report,
        json_path=json_path,
        markdown_path=markdown_path,
        csv_path=csv_path,
        xlsx_path=xlsx_path,
    )
    if outputs:
        report["outputs"] = outputs

    if args.format == "markdown":
        print(render_3d_model_request_markdown(report))
    elif args.format == "csv":
        print(render_3d_model_request_csv(report), end="")
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 2 if report.get("errors") else 0


def cmd_receive_3d_models(args: argparse.Namespace) -> int:
    report = receive_3d_model_request_models(
        args.repair_kit,
        request_path=args.request,
        model_roots=args.model_root or [],
        write=not args.dry_run,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["errors"] or (args.strict and report["after"]["status"] != "ready"):
        return 2
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    if not _apply_repair_kit_defaults(args, require_input=False):
        return 2
    report = build_doctor_report(
        input_path=args.input,
        models_path=args.models,
        model_aliases_path=args.model_aliases,
        default_unit=args.default_unit,
        cadence_root=args.cadence_root,
    )
    if args.out:
        out_path = args.out.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["status"] == "failed" or (args.strict and report["status"] == "warning"):
        return 2
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    if not _apply_repair_kit_defaults(args, require_input=True):
        return 2
    out_path = args.out.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not args.append:
        raise FileExistsError(f"Manifest already exists: {out_path}. Use --append to add a sample.")

    import_report = None
    sync_report = None
    model_roots = _flow_model_roots(args)
    if model_roots:
        try:
            import_report, sync_report = _prepare_repair_kit_models(
                args.repair_kit,
                model_roots,
                getattr(args, "model_rules", None),
            )
        except Exception as exc:
            payload = {
                "version": f"v{__version__}",
                "manifest": str(out_path),
                "sample": args.name,
                "validation_errors": 1,
                "validation_warnings": 0,
                "errors": [str(exc)],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 2

    sample, validation, padstack_report = _build_manifest_sample(args, out_path.parent)
    if args.append and out_path.exists():
        manifest = _read_batch_manifest(out_path)
        samples = manifest.setdefault("samples", [])
        if not isinstance(samples, list):
            raise ValueError("Existing manifest 'samples' field must be a list.")
        samples.append(sample)
    else:
        manifest = {"version": f"v{__version__}", "samples": [sample]}

    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload = {
        "version": f"v{__version__}",
        "manifest": str(out_path),
        "sample": sample["name"],
        "expected": sample["expected"],
        "validation_errors": len(validation.errors),
        "validation_warnings": len(validation.warnings),
        "padstack": _padstack_manifest_summary(padstack_report),
        "repair_kit_import": _repair_kit_build_import_summary(import_report),
        "repair_kit_sync": _repair_kit_build_sync_summary(sync_report),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if validation.errors or padstack_report.get("errors") else 0


def cmd_batch(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    manifest = _read_batch_manifest(manifest_path)
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Batch manifest must contain a non-empty 'samples' list.")

    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    sample_count = len(samples)
    for index, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            raise ValueError(f"Batch sample #{index} must be an object.")
        rows.append(_run_batch_sample(args, manifest_path.parent, out_root, sample, index, sample_count))

    passed = sum(1 for row in rows if row["status"] == "passed")
    failed = len(rows) - passed
    payload = {
        "version": f"v{__version__}",
        "manifest": str(manifest_path),
        "out": str(out_root),
        "sample_count": len(rows),
        "passed": passed,
        "failed": failed,
        "run_cadence": bool(args.run_cadence),
        "fetch_3d": bool(args.fetch_3d),
        "model_roots": [str(path.expanduser().resolve()) for path in (getattr(args, "model_root", None) or [])],
        "model_rules": str(args.model_rules.expanduser().resolve()) if getattr(args, "model_rules", None) else "",
        "signoff_template": str(args.signoff_template.expanduser().resolve()) if getattr(args, "signoff_template", None) else "",
        "padstack_requirements": _padstack_requirements_from_args(args),
        "resolve_lcsc_3d": bool(getattr(args, "resolve_lcsc_3d", False))
        or any(bool(row.get("resolve_lcsc_3d_requested")) for row in rows),
        "require_3d": bool(args.require_3d),
        "allow_noncomponent_3d_exceptions": bool(getattr(args, "allow_noncomponent_3d_exceptions", False))
        or any(bool(row.get("allow_noncomponent_3d_exceptions")) for row in rows),
        "require_signoff": bool(getattr(args, "require_signoff", False))
        or any(bool(row.get("require_signoff")) for row in rows),
        "samples": rows,
    }
    summary_json = out_root / "batch_summary.json"
    summary_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_batch_csv(rows, out_root / "batch_summary.csv")
    _write_batch_markdown(payload, out_root / "batch_summary.md")
    print(json.dumps({"version": payload["version"], "sample_count": len(rows), "passed": passed, "failed": failed, "summary": str(summary_json)}, indent=2, ensure_ascii=False))
    if failed and args.strict:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def _read_batch_manifest(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Batch manifest does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Batch manifest root must be an object.")
    return data


def _build_manifest_sample(args: argparse.Namespace, manifest_dir: Path) -> tuple[dict, object, dict]:
    with tempfile.TemporaryDirectory(prefix="jlc2cadence_manifest_") as tmp:
        work_dir = Path(tmp)
        library, input_paths, _design_files = _load_library(args, work_dir=work_dir / "input")
        model_aliases = load_model_aliases(args.model_aliases)
        model_assets = []
        if args.models:
            model_assets.extend(collect_model_assets(args.models, None))
        for path in input_paths:
            model_assets.extend(collect_model_assets(path, None))
        local_cache_roots = _local_model_cache_roots(args)
        if local_cache_roots:
            cache_report = import_easyeda_cached_3d_models(
                library,
                local_cache_roots,
                work_dir / "cache_models",
                formats=[getattr(args, "model_format", "step")],
            )
            model_assets.extend(cache_report.models)
        model_assets.extend(collect_model_alias_assets(model_aliases, None))
        model_assets = _dedupe_models(model_assets)
        attach_models(library, model_assets, model_aliases.mapping)
        ensure_components_for_footprints(library)
        validation = validate_library(library)

    padstack_requirements = _padstack_requirements_from_args(args)
    padstack_report = build_padstack_coverage_report(
        library,
        source={"type": "manifest", "path": str(args.input) if args.input else ""},
        **_padstack_report_kwargs(padstack_requirements),
    )
    linked_models = sum(1 for footprint in library.footprints if footprint.model3d)
    placeholder_models = max(len(library.footprints) - linked_models, 0) if args.synthesize_missing_3d else 0
    warning_count = len(validation.warnings)
    if placeholder_models:
        warning_count = sum(1 for warning in validation.warnings if "has no linked 3D model" not in warning) + 1
    expected = {
        "components": len(library.components),
        "footprints": len(library.footprints),
        "pins": _library_pin_count(library),
        "pads": _library_pad_count(library),
        "models": linked_models + placeholder_models,
        "errors": len(validation.errors),
        "warnings": warning_count,
    }
    expected.update(_padstack_expected_counts(padstack_report))
    if args.synthesize_missing_3d:
        expected["placeholder_3d_models"] = placeholder_models
    sample_name = str(args.name).strip()
    if not sample_name:
        raise ValueError("Manifest sample name cannot be empty.")
    sample = {
        "name": sample_name,
        "input": _manifest_output_path(args.input, manifest_dir),
        "expected": expected,
    }
    if getattr(args, "repair_kit", None):
        sample["repair_kit"] = _manifest_output_path(args.repair_kit, manifest_dir)
    model_roots = _flow_model_roots(args)
    if model_roots:
        sample["model_roots"] = [
            _manifest_output_path(path, manifest_dir)
            for path in model_roots
        ]
    if getattr(args, "model_rules", None):
        sample["model_rules"] = _manifest_output_path(args.model_rules, manifest_dir)
    if args.models:
        sample["models"] = _manifest_output_path(args.models, manifest_dir)
    if args.model_aliases:
        sample["model_aliases"] = _manifest_output_path(args.model_aliases, manifest_dir)
    if getattr(args, "cadence_assets", None):
        sample["cadence_assets"] = [
            _manifest_output_path(path, manifest_dir)
            for path in args.cadence_assets
        ]
    if args.default_unit != "mm":
        sample["default_unit"] = args.default_unit
    if args.fetch_3d:
        sample["fetch_3d"] = True
    if args.fetch_3d or args.model_format != "step":
        sample["model_format"] = args.model_format
    if args.model_cache:
        sample["model_cache"] = _manifest_output_path(args.model_cache, manifest_dir)
    local_model_cache = _local_model_cache_roots(args)
    if local_model_cache:
        sample["local_model_cache"] = [
            _manifest_output_path(path, manifest_dir) for path in local_model_cache
        ]
    if args.model_sources:
        sample["model_sources"] = _manifest_output_path(args.model_sources, manifest_dir)
    if args.fetch_3d or args.fetch_3d_timeout != 30.0:
        sample["fetch_3d_timeout"] = args.fetch_3d_timeout
    if getattr(args, "resolve_lcsc_3d", False):
        sample["resolve_lcsc_3d"] = True
    if getattr(args, "lcsc_cache", None):
        sample["lcsc_cache"] = _manifest_output_path(args.lcsc_cache, manifest_dir)
    if getattr(args, "resolve_lcsc_3d", False) or getattr(args, "lcsc_timeout", 30.0) != 30.0:
        sample["lcsc_timeout"] = args.lcsc_timeout
    if args.run_cadence:
        sample["run_cadence"] = True
    if args.require_3d:
        sample["require_3d"] = True
    if getattr(args, "allow_noncomponent_3d_exceptions", False):
        sample["allow_noncomponent_3d_exceptions"] = True
    if _has_padstack_requirements(padstack_requirements):
        sample["padstack_requirements"] = padstack_requirements
    if args.synthesize_missing_3d:
        sample["synthesize_missing_3d"] = True
    return sample, validation, padstack_report


def _run_batch_sample(
    args: argparse.Namespace,
    manifest_dir: Path,
    out_root: Path,
    sample: dict,
    index: int,
    sample_count: int,
) -> dict:
    name = str(sample.get("name") or f"sample_{index}").strip() or f"sample_{index}"
    safe_name = _batch_safe_name(name, index)
    sample_out = out_root / safe_name
    repair_kit_path = _manifest_optional_path(manifest_dir, sample.get("repair_kit"))
    if "model_roots" in sample:
        model_roots = _manifest_optional_paths(manifest_dir, sample.get("model_roots"))
    elif "model_root" in sample:
        model_roots = _manifest_optional_paths(manifest_dir, sample.get("model_root"))
    else:
        model_roots = list(getattr(args, "model_root", []) or [])
    if "model_rules" in sample and sample.get("model_rules") not in [None, ""]:
        model_rules = _manifest_optional_path(manifest_dir, sample.get("model_rules"), "model_rules")
    else:
        model_rules = getattr(args, "model_rules", None)
        if model_rules is not None:
            model_rules = model_rules.expanduser().resolve()
    repair_kit_import = None
    repair_kit_sync = None
    repair_kit_import_error = ""
    if model_roots:
        try:
            repair_kit_import, repair_kit_sync = _prepare_repair_kit_models(
                repair_kit_path,
                model_roots,
                model_rules,
            )
        except Exception as exc:
            repair_kit_import_error = str(exc)
    elif model_rules is not None:
        repair_kit_import_error = "--model-rules requires --model-root for batch repair-kit import."

    repair_kit = None
    repair_kit_report = None
    if repair_kit_path is not None:
        repair_kit = load_model_repair_kit(repair_kit_path)
        repair_kit_report = check_model_repair_kit(repair_kit_path)
    if sample.get("input") not in [None, ""]:
        input_path = _manifest_path(manifest_dir, sample.get("input"), f"sample {name} input")
    elif repair_kit is not None and repair_kit.input_path is not None:
        input_path = repair_kit.input_path
    else:
        input_path = _manifest_path(manifest_dir, sample.get("input"), f"sample {name} input")
    model_path = _manifest_optional_path(manifest_dir, sample.get("models"))
    if model_path is None and repair_kit is not None:
        model_path = repair_kit.models_dir
    alias_path = _manifest_optional_path(manifest_dir, sample.get("model_aliases"))
    if alias_path is None:
        alias_path = args.model_aliases or (repair_kit.aliases_path if repair_kit is not None else None)
    if "cadence_assets" in sample:
        cadence_assets = _manifest_optional_paths(manifest_dir, sample.get("cadence_assets"))
    else:
        cadence_assets = list(getattr(args, "cadence_assets", []) or [])
    fetch_3d_requested = bool(sample["fetch_3d"]) if "fetch_3d" in sample else bool(args.fetch_3d)
    model_format = str(sample.get("model_format") or args.model_format)
    signoff_path = _manifest_optional_path(manifest_dir, sample.get("signoff"), "signoff")
    require_signoff = (
        bool(sample["require_signoff"])
        if "require_signoff" in sample
        else bool(getattr(args, "require_signoff", False))
    )
    signoff_template = _manifest_optional_path(manifest_dir, sample.get("signoff_template"), "signoff_template")
    if signoff_template is None and getattr(args, "signoff_template", None):
        signoff_template = _batch_signoff_template_path(args.signoff_template, name, index, sample_count)
    if "model_cache" in sample and sample.get("model_cache") not in [None, ""]:
        model_cache = _manifest_optional_path(manifest_dir, sample.get("model_cache"), "model_cache")
    else:
        model_cache = args.model_cache
    if "model_sources" in sample and sample.get("model_sources") not in [None, ""]:
        model_sources = _manifest_optional_path(manifest_dir, sample.get("model_sources"), "model_sources")
    else:
        model_sources = getattr(args, "model_sources", None)
    if "local_model_cache" in sample:
        local_model_cache = _manifest_optional_paths(manifest_dir, sample.get("local_model_cache"))
    else:
        local_model_cache = list(getattr(args, "local_model_cache", []) or [])
    fetch_3d_timeout = float(sample.get("fetch_3d_timeout") or args.fetch_3d_timeout)
    resolve_lcsc_3d = (
        bool(sample["resolve_lcsc_3d"])
        if "resolve_lcsc_3d" in sample
        else bool(getattr(args, "resolve_lcsc_3d", False))
    )
    if "lcsc_cache" in sample and sample.get("lcsc_cache") not in [None, ""]:
        lcsc_cache = _manifest_optional_path(manifest_dir, sample.get("lcsc_cache"), "lcsc_cache")
    else:
        lcsc_cache = getattr(args, "lcsc_cache", None)
    lcsc_timeout = float(sample.get("lcsc_timeout") or getattr(args, "lcsc_timeout", 30.0))
    require_3d = bool(sample["require_3d"]) if "require_3d" in sample else bool(args.require_3d)
    allow_noncomponent = (
        bool(sample["allow_noncomponent_3d_exceptions"])
        if "allow_noncomponent_3d_exceptions" in sample
        else bool(getattr(args, "allow_noncomponent_3d_exceptions", False))
    )
    padstack_requirements = _batch_padstack_requirements(args, sample)
    synthesize_missing_3d = (
        bool(sample["synthesize_missing_3d"])
        if "synthesize_missing_3d" in sample
        else bool(getattr(args, "synthesize_missing_3d", False))
    )
    sample_args = argparse.Namespace(
        input=input_path,
        models=model_path,
        model_aliases=alias_path,
        out=sample_out,
        cadence_assets=cadence_assets,
        default_unit=str(sample.get("default_unit") or args.default_unit),
        strict=False,
        run_cadence=bool(args.run_cadence or sample.get("run_cadence", False)),
        require_3d=require_3d,
        allow_noncomponent_3d_exceptions=allow_noncomponent,
        cadence_root=args.cadence_root,
        fetch_3d=fetch_3d_requested,
        model_format=model_format,
        model_cache=model_cache,
        local_model_cache=local_model_cache,
        model_sources=model_sources,
        fetch_3d_timeout=fetch_3d_timeout,
        resolve_lcsc_3d=resolve_lcsc_3d,
        lcsc_cache=lcsc_cache,
        lcsc_timeout=lcsc_timeout,
        repair_kit=repair_kit_path,
        synthesize_missing_3d=synthesize_missing_3d,
    )
    started = perf_counter()
    repair_counts = repair_kit_report.get("counts", {}) if isinstance(repair_kit_report, dict) else {}
    row = {
        "name": name,
        "safe_name": safe_name,
        "input": str(input_path),
        "models": str(model_path) if model_path else "",
        "cadence_assets": [str(path) for path in cadence_assets],
        "model_roots": [str(path) for path in model_roots],
        "model_rules": str(model_rules) if model_rules else "",
        "repair_kit_import": _repair_kit_build_import_summary(repair_kit_import),
        "repair_kit_sync": _repair_kit_build_sync_summary(repair_kit_sync),
        "repair_kit_import_status": str(repair_kit_import.get("status", "")) if repair_kit_import else ("failed" if repair_kit_import_error else "skipped"),
        "repair_kit_import_rule_matches": int((repair_kit_import or {}).get("counts", {}).get("rule_matched_aliases", 0) or 0),
        "repair_kit_import_copied_models": int((repair_kit_import or {}).get("counts", {}).get("copied_models", 0) or 0),
        "repair_kit_import_errors": len((repair_kit_import or {}).get("errors", [])) + (1 if repair_kit_import_error else 0),
        "repair_kit_import_warnings": len((repair_kit_import or {}).get("warnings", [])),
        "repair_kit": str(repair_kit_path) if repair_kit_path else "",
        "repair_kit_status": str(repair_kit_report.get("status", "")) if isinstance(repair_kit_report, dict) else "",
        "repair_kit_errors": len(repair_kit_report.get("errors", [])) if isinstance(repair_kit_report, dict) else 0,
        "repair_kit_warnings": len(repair_kit_report.get("warnings", [])) if isinstance(repair_kit_report, dict) else 0,
        "repair_kit_missing_footprints": int(repair_counts.get("missing_footprints", 0) or 0),
        "repair_kit_alias_rows": int(repair_counts.get("alias_rows", 0) or 0),
        "repair_kit_placeholder_aliases": int(repair_counts.get("placeholder_aliases", 0) or 0),
        "repair_kit_missing_alias_targets": int(repair_counts.get("missing_alias_targets", 0) or 0),
        "repair_kit_non_file_alias_targets": int(repair_counts.get("non_file_alias_targets", 0) or 0),
        "repair_kit_non_step_alias_targets": int(repair_counts.get("non_step_alias_targets", 0) or 0),
        "repair_kit_model_files": int(repair_counts.get("model_files", 0) or 0),
        "signoff_file": str(signoff_path) if signoff_path else "",
        "signoff_template": "",
        "require_signoff": require_signoff,
        "out": str(sample_out),
        "exit_code": None,
        "status": "failed",
        "duration_seconds": 0.0,
        "components": 0,
        "footprints": 0,
        "pins": 0,
        "pads": 0,
        "models_count": 0,
        "errors": 0,
        "warnings": 0,
        "step_errors": 0,
        "step_warnings": 0,
        "expected": sample.get("expected", {}),
        "expectation_failures": [],
        "cadence_failures": [],
        "cadence_statuses": [],
        "capture_status": "",
        "capture_result": {},
        "step_statuses": [],
        "fetch_3d_requested": fetch_3d_requested,
        "resolve_lcsc_3d_requested": resolve_lcsc_3d,
        "require_3d": require_3d,
        "allow_noncomponent_3d_exceptions": allow_noncomponent,
        "padstack_requirements": padstack_requirements,
        "padstack_status": "",
        "padstack_count": 0,
        "padstack_errors": 0,
        "padstack_warnings": 0,
        "padstack_json": "",
        "padstack_markdown": "",
        "padstack_exception": "",
        "padstack_kind_counts": {},
        "padstack_role_counts": {},
        "padstack_shape_counts": {},
        "padstack_slot_pads": 0,
        "padstack_drilled_pads": 0,
        "padstack_nonplated_drill_pads": 0,
        "padstack_electrical_pads": 0,
        "padstack_shield_pads": 0,
        "padstack_mechanical_pads": 0,
        "padstack_mounting_pads": 0,
        "padstack_npth_pads": 0,
        "synthesize_missing_3d": synthesize_missing_3d,
        "placeholder_3d_count": 0,
        "placeholder_3d_status": "",
        "placeholder_3d_json": "",
        "review_3d_status": "",
        "review_3d_ready_for_canvas_review": 0,
        "review_3d_placeholder_not_for_signoff": 0,
        "review_3d_errors": 0,
        "review_3d_warnings": 0,
        "review_3d_signoff_file": "",
        "review_3d_canvas_signed_off": 0,
        "review_3d_canvas_signoff_incomplete": 0,
        "review_3d_canvas_signoff_failed": 0,
        "review_3d_noncomponent_3d_exemptions": 0,
        "review_3d_allowed_noncomponent_3d_exemptions": 0,
        "review_3d_missing_required_3d_models": 0,
        "review_3d_json": "",
        "review_3d_markdown": "",
        "review_3d_exception": "",
        "fetch_3d_status": "",
        "fetch_3d_download_count": 0,
        "fetch_3d_model_count": 0,
        "fetch_3d_error_count": 0,
        "fetch_3d_json": "",
        "lcsc_resolution_status": "",
        "lcsc_resolution_count": 0,
        "lcsc_resolution_error_count": 0,
        "lcsc_resolution_json": "",
        "cache_3d_status": "",
        "cache_3d_model_count": 0,
        "cache_3d_payload_count": 0,
        "cache_3d_missing_count": 0,
        "cache_3d_json": "",
        "cadence_asset_status": "",
        "cadence_asset_matches": 0,
        "cadence_asset_unmatched": 0,
        "cadence_asset_conflicts": 0,
        "cadence_asset_library_assets": 0,
        "cadence_asset_errors": 0,
        "cadence_asset_json": "",
        "output_status": "",
        "output_errors": 0,
        "output_warnings": 0,
        "output_validation": "",
        "audit_status": "",
        "audit_errors": 0,
        "audit_warnings": 0,
        "audit_json": "",
        "audit_markdown": "",
        "audit_exception": "",
        "exception": "",
    }
    if repair_kit_import is not None:
        import_path = sample_out / "reports" / "repair_kit_import.json"
        import_path.parent.mkdir(parents=True, exist_ok=True)
        import_path.write_text(
            json.dumps(repair_kit_import, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        row["repair_kit_import_json"] = str(import_path)
    if repair_kit_sync is not None:
        sync_path = sample_out / "reports" / "repair_kit_sync.json"
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(
            json.dumps(repair_kit_sync, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        row["repair_kit_sync_json"] = str(sync_path)
    if repair_kit_import_error:
        row["expectation_failures"].append(f"repair-kit model import: {repair_kit_import_error}")
    try:
        row["exit_code"] = cmd_convert(sample_args)
        _populate_batch_row_from_outputs(
            row,
            sample_out,
            require_cadence=sample_args.run_cadence,
            require_3d=sample_args.require_3d,
            allow_noncomponent_3d_exceptions=allow_noncomponent,
            signoff_path=signoff_path,
            padstack_requirements=padstack_requirements,
        )
        if signoff_template is not None:
            row["signoff_template"] = str(
                write_3d_signoff_template(
                    sample_out,
                    signoff_template,
                    allow_noncomponent_3d_exceptions=allow_noncomponent,
                )
            )
        row["expectation_failures"] = _batch_expectation_failures(row, row["expected"])
        if signoff_path is not None and row["review_3d_status"] == CANVAS_SIGNOFF_FAILED:
            row["expectation_failures"].append(
                f"3d-review signoff failed for {name}: {row['review_3d_signoff_file'] or signoff_path}"
            )
        if require_signoff and not _three_d_review_signoff_ok(row["review_3d_status"], allow_noncomponent):
            if signoff_path is None:
                row["expectation_failures"].append("3d-review signoff required but no signoff file was provided")
            else:
                row["expectation_failures"].append(
                    f"3d-review signoff required but status is {row['review_3d_status']}"
                )
        row["cadence_failures"] = _batch_cadence_failures(row, sample_args.run_cadence)
        if (
            row["exit_code"] == 0
            and row["errors"] == 0
            and row["step_errors"] == 0
            and row["fetch_3d_error_count"] == 0
            and row["lcsc_resolution_error_count"] == 0
            and row["padstack_errors"] == 0
            and row["review_3d_errors"] == 0
            and row["cadence_asset_conflicts"] == 0
            and row["cadence_asset_errors"] == 0
            and row["output_errors"] == 0
            and row["audit_errors"] == 0
            and not row["expectation_failures"]
            and not row["cadence_failures"]
        ):
            row["status"] = "passed"
    except Exception as exc:  # pragma: no cover - exercised by user-provided manifests
        row["exception"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    finally:
        row["duration_seconds"] = round(perf_counter() - started, 3)
    return row


def _populate_batch_row_from_outputs(
    row: dict,
    sample_out: Path,
    require_cadence: bool = False,
    require_3d: bool = False,
    allow_noncomponent_3d_exceptions: bool = False,
    signoff_path: Path | None = None,
    padstack_requirements: dict | None = None,
) -> None:
    summary_path = sample_out / "reports" / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        row["components"] = int(summary.get("component_count", 0))
        row["footprints"] = int(summary.get("footprint_count", 0))
        row["pins"] = int(summary.get("pin_count", 0) or 0)
        row["pads"] = int(summary.get("pad_count", 0) or 0)
        row["errors"] = len(summary.get("errors", []))
        row["warnings"] = len(summary.get("warnings", []))
    ir_path = sample_out / "reports" / "library.ir.json"
    if ir_path.exists():
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        row["pins"] = _ir_pin_count(ir)
        row["pads"] = _ir_pad_count(ir)
        row["models_count"] = sum(1 for footprint in ir.get("footprints", []) if footprint.get("model3d"))
        _populate_padstack_batch_row(row, sample_out, ir, padstack_requirements or {})
    cadence_results_path = sample_out / "allegro" / "symbols" / "create_symbols.results.json"
    if cadence_results_path.exists():
        cadence_results = json.loads(cadence_results_path.read_text(encoding="utf-8"))
        row["cadence_statuses"] = [
            {
                "footprint": item.get("footprint", ""),
                "status": item.get("status", ""),
                "buildExitCode": item.get("buildExitCode"),
                "compileExitCode": item.get("compileExitCode"),
            }
            for item in _as_list(cadence_results)
        ]
    capture_results_path = sample_out / "capture" / "capture_build.results.json"
    if capture_results_path.exists():
        capture_result = json.loads(capture_results_path.read_text(encoding="utf-8"))
        row["capture_result"] = capture_result
        row["capture_status"] = str(capture_result.get("status", ""))
    step_path = sample_out / "reports" / "3d_validation.json"
    if step_path.exists():
        step_report = json.loads(step_path.read_text(encoding="utf-8"))
        row["step_errors"] = len(step_report.get("errors", []))
        row["step_warnings"] = len(step_report.get("warnings", []))
        row["step_statuses"] = [
            {"package": item.get("package", ""), "status": item.get("status", "")}
            for item in step_report.get("entries", [])
        ]
    fetch_path = sample_out / "3d" / "easyeda_3d_download.json"
    if fetch_path.exists():
        fetch_report = json.loads(fetch_path.read_text(encoding="utf-8"))
        row["fetch_3d_json"] = str(fetch_path)
        row["fetch_3d_status"] = str(fetch_report.get("status", ""))
        row["fetch_3d_download_count"] = int(fetch_report.get("download_count", 0))
        row["fetch_3d_model_count"] = int(fetch_report.get("model_count", 0))
        row["fetch_3d_error_count"] = int(fetch_report.get("error_count", 0))
    lcsc_path = sample_out / "3d" / "easyeda_lcsc_3d_resolution.json"
    if lcsc_path.exists():
        lcsc_report = json.loads(lcsc_path.read_text(encoding="utf-8"))
        row["lcsc_resolution_json"] = str(lcsc_path)
        row["lcsc_resolution_status"] = str(lcsc_report.get("status", ""))
        row["lcsc_resolution_count"] = int(lcsc_report.get("resolved_count", 0))
        row["lcsc_resolution_error_count"] = int(lcsc_report.get("error_count", 0))
    cache_path = sample_out / "3d" / "easyeda_3d_cache_import.json"
    if cache_path.exists():
        cache_report = json.loads(cache_path.read_text(encoding="utf-8"))
        row["cache_3d_json"] = str(cache_path)
        row["cache_3d_status"] = str(cache_report.get("status", ""))
        row["cache_3d_model_count"] = int(cache_report.get("model_count", 0))
        row["cache_3d_payload_count"] = int(cache_report.get("payload_count", 0))
        row["cache_3d_missing_count"] = len(cache_report.get("missing_model_ids", []))
    cadence_asset_path = sample_out / "reports" / "cadence_asset_import.json"
    if cadence_asset_path.exists():
        cadence_asset_report = json.loads(cadence_asset_path.read_text(encoding="utf-8"))
        counts = cadence_asset_report.get("counts", {})
        row["cadence_asset_json"] = str(cadence_asset_path)
        row["cadence_asset_status"] = str(cadence_asset_report.get("status", ""))
        row["cadence_asset_matches"] = int(counts.get("matched_assets", 0) or 0)
        row["cadence_asset_unmatched"] = int(counts.get("unmatched_assets", 0) or 0)
        row["cadence_asset_conflicts"] = int(counts.get("conflicts", 0) or 0)
        row["cadence_asset_library_assets"] = int(counts.get("library_assets", 0) or 0)
        row["cadence_asset_errors"] = len(cadence_asset_report.get("errors", []))
    placeholder_path = sample_out / "3d" / "placeholder_3d_report.json"
    if placeholder_path.exists():
        placeholder_report = json.loads(placeholder_path.read_text(encoding="utf-8"))
        row["placeholder_3d_json"] = str(placeholder_path)
        row["placeholder_3d_status"] = str(placeholder_report.get("status", ""))
        row["placeholder_3d_count"] = int(placeholder_report.get("generated_count", 0))
    output_path = sample_out / "reports" / "output_validation.json"
    if output_path.exists():
        output_report = json.loads(output_path.read_text(encoding="utf-8"))
        row["output_validation"] = str(output_path)
        row["output_status"] = str(output_report.get("status", ""))
        row["output_errors"] = len(output_report.get("errors", []))
        row["output_warnings"] = len(output_report.get("warnings", []))
    try:
        review = write_3d_review_report(
            sample_out,
            signoff_path=signoff_path,
            allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
        )
        reports_dir = sample_out / "reports"
        row["review_3d_status"] = review.status
        row["review_3d_ready_for_canvas_review"] = int(review.counts.get("ready_for_canvas_review", 0))
        row["review_3d_canvas_signed_off"] = int(review.counts.get("canvas_signed_off", 0))
        row["review_3d_canvas_signoff_incomplete"] = int(review.counts.get("canvas_signoff_incomplete", 0))
        row["review_3d_canvas_signoff_failed"] = int(review.counts.get("canvas_signoff_failed", 0))
        row["review_3d_placeholder_not_for_signoff"] = int(review.counts.get("placeholder_not_for_signoff", 0))
        row["review_3d_noncomponent_3d_exemptions"] = int(review.counts.get("noncomponent_3d_exemptions", 0))
        row["review_3d_allowed_noncomponent_3d_exemptions"] = int(review.counts.get("allowed_noncomponent_3d_exemptions", 0))
        row["review_3d_missing_required_3d_models"] = int(review.counts.get("missing_required_3d_models", 0))
        row["review_3d_errors"] = len(review.errors)
        row["review_3d_warnings"] = len(review.warnings)
        row["review_3d_signoff_file"] = review.signoff_file
        row["review_3d_json"] = str(reports_dir / "3d_review.json")
        row["review_3d_markdown"] = str(reports_dir / "3d_review.md")
    except Exception as exc:  # pragma: no cover - defensive for user-supplied partial outputs
        row["review_3d_status"] = "failed"
        row["review_3d_errors"] = 1
        row["review_3d_signoff_file"] = str(signoff_path) if signoff_path else ""
        row["review_3d_exception"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    try:
        audit = write_output_audit(
            sample_out,
            require_cadence=require_cadence,
            require_3d=require_3d,
            allow_noncomponent_3d_exceptions=allow_noncomponent_3d_exceptions,
        )
        reports_dir = sample_out / "reports"
        row["audit_status"] = audit.status
        row["audit_errors"] = len(audit.issues.get("errors", []))
        row["audit_warnings"] = len(audit.issues.get("warnings", []))
        row["audit_json"] = str(reports_dir / "audit.json")
        row["audit_markdown"] = str(reports_dir / "audit.md")
    except Exception as exc:  # pragma: no cover - defensive for user-supplied partial outputs
        row["audit_status"] = "failed"
        row["audit_errors"] = 1
        row["audit_exception"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()


def _populate_padstack_batch_row(
    row: dict,
    sample_out: Path,
    ir: dict,
    padstack_requirements: dict,
) -> None:
    reports_dir = sample_out / "reports"
    json_path = reports_dir / "padstack_report.json"
    markdown_path = reports_dir / "padstack_report.md"
    try:
        report = build_padstack_coverage_report(
            ir,
            source={"type": "conversion_output", "path": str(sample_out)},
            **_padstack_report_kwargs(padstack_requirements),
        )
        write_padstack_coverage_report_json(report, json_path)
        write_padstack_coverage_report_markdown(report, markdown_path)
        counts = report.get("counts", {})
        coverage = report.get("coverage", {})
        row["padstack_status"] = str(report.get("status", ""))
        row["padstack_count"] = int(counts.get("padstacks", 0) or 0)
        row["padstack_errors"] = len(report.get("errors", []))
        row["padstack_warnings"] = len(report.get("warnings", []))
        row["padstack_json"] = str(json_path)
        row["padstack_markdown"] = str(markdown_path)
        row["padstack_kind_counts"] = coverage.get("kind_counts", {}) if isinstance(coverage, dict) else {}
        row["padstack_role_counts"] = coverage.get("role_counts", {}) if isinstance(coverage, dict) else {}
        row["padstack_shape_counts"] = coverage.get("shape_counts", {}) if isinstance(coverage, dict) else {}
        row["padstack_slot_pads"] = int(counts.get("slot_pads", 0) or 0)
        row["padstack_drilled_pads"] = int(counts.get("drilled_pads", 0) or 0)
        row["padstack_nonplated_drill_pads"] = int(counts.get("nonplated_drill_pads", 0) or 0)
        row["padstack_electrical_pads"] = int(counts.get("electrical_pads", 0) or 0)
        row["padstack_shield_pads"] = int(counts.get("shield_pads", 0) or 0)
        row["padstack_mechanical_pads"] = int(counts.get("mechanical_pads", 0) or 0)
        row["padstack_mounting_pads"] = int(counts.get("mounting_pads", 0) or 0)
        row["padstack_npth_pads"] = int(counts.get("npth_pads", 0) or 0)
    except Exception as exc:  # pragma: no cover - defensive for user-supplied partial outputs
        row["padstack_status"] = "failed"
        row["padstack_errors"] = 1
        row["padstack_exception"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()


def _padstack_manifest_summary(report: dict) -> dict:
    counts = report.get("counts", {}) if isinstance(report, dict) else {}
    return {
        "status": str(report.get("status", "")) if isinstance(report, dict) else "",
        "requirements": report.get("requirements", {}) if isinstance(report, dict) else {},
        "padstacks": int(counts.get("padstacks", 0) or 0),
        "slot_pads": int(counts.get("slot_pads", 0) or 0),
        "drilled_pads": int(counts.get("drilled_pads", 0) or 0),
        "nonplated_drill_pads": int(counts.get("nonplated_drill_pads", 0) or 0),
        "shield_pads": int(counts.get("shield_pads", 0) or 0),
        "npth_pads": int(counts.get("npth_pads", 0) or 0),
        "errors": len(report.get("errors", [])) if isinstance(report, dict) else 0,
        "warnings": len(report.get("warnings", [])) if isinstance(report, dict) else 0,
    }


def _padstack_expected_counts(report: dict) -> dict[str, int]:
    counts = report.get("counts", {}) if isinstance(report, dict) else {}
    return {
        "padstacks": int(counts.get("padstacks", 0) or 0),
        "slot_pads": int(counts.get("slot_pads", 0) or 0),
        "drilled_pads": int(counts.get("drilled_pads", 0) or 0),
        "nonplated_drill_pads": int(counts.get("nonplated_drill_pads", 0) or 0),
        "electrical_pads": int(counts.get("electrical_pads", 0) or 0),
        "shield_pads": int(counts.get("shield_pads", 0) or 0),
        "mechanical_pads": int(counts.get("mechanical_pads", 0) or 0),
        "mounting_pads": int(counts.get("mounting_pads", 0) or 0),
        "npth_pads": int(counts.get("npth_pads", 0) or 0),
        "padstack_errors": len(report.get("errors", [])) if isinstance(report, dict) else 0,
        "padstack_warnings": len(report.get("warnings", [])) if isinstance(report, dict) else 0,
    }


def _padstack_requirements_from_args(args: argparse.Namespace) -> dict:
    return _clean_padstack_requirements(
        {
            "kinds": getattr(args, "require_pad_kind", []) or [],
            "roles": getattr(args, "require_pad_role", []) or [],
            "shapes": getattr(args, "require_pad_shape", []) or [],
            "slot": bool(getattr(args, "require_pad_slot", False)),
            "drill": bool(getattr(args, "require_pad_drill", False)),
            "nonplated": bool(getattr(args, "require_pad_nonplated", False)),
        }
    )


def _batch_padstack_requirements(args: argparse.Namespace, sample: dict) -> dict:
    return _merge_padstack_requirements(
        _padstack_requirements_from_args(args),
        _clean_padstack_requirements(sample.get("padstack_requirements", {})),
    )


def _padstack_report_kwargs(requirements: dict) -> dict:
    clean = _clean_padstack_requirements(requirements)
    return {
        "require_kinds": clean.get("kinds", []),
        "require_roles": clean.get("roles", []),
        "require_shapes": clean.get("shapes", []),
        "require_slot": bool(clean.get("slot", False)),
        "require_drill": bool(clean.get("drill", False)),
        "require_nonplated": bool(clean.get("nonplated", False)),
    }


def _merge_padstack_requirements(*requirements: dict) -> dict:
    merged = {"kinds": [], "roles": [], "shapes": [], "slot": False, "drill": False, "nonplated": False}
    for requirement in requirements:
        clean = _clean_padstack_requirements(requirement)
        for key in ["kinds", "roles", "shapes"]:
            values = list(merged[key])
            for value in clean.get(key, []):
                if value not in values:
                    values.append(value)
            merged[key] = values
        for key in ["slot", "drill", "nonplated"]:
            merged[key] = bool(merged[key] or clean.get(key, False))
    return merged


def _clean_padstack_requirements(value: object) -> dict:
    if not isinstance(value, dict):
        value = {}
    return {
        "kinds": _normal_padstack_values(value.get("kinds", value.get("kind", []))),
        "roles": _normal_padstack_values(value.get("roles", value.get("role", []))),
        "shapes": _normal_padstack_values(value.get("shapes", value.get("shape", []))),
        "slot": bool(value.get("slot", value.get("require_slot", False))),
        "drill": bool(value.get("drill", value.get("require_drill", False))),
        "nonplated": bool(value.get("nonplated", value.get("require_nonplated", False))),
    }


def _has_padstack_requirements(requirements: dict) -> bool:
    clean = _clean_padstack_requirements(requirements)
    return any(
        [
            clean.get("kinds"),
            clean.get("roles"),
            clean.get("shapes"),
            clean.get("slot"),
            clean.get("drill"),
            clean.get("nonplated"),
        ]
    )


def _padstack_requirements_text(requirements: object) -> str:
    clean = _clean_padstack_requirements(requirements)
    parts: list[str] = []
    for key, label in [("kinds", "kind"), ("roles", "role"), ("shapes", "shape")]:
        values = clean.get(key, [])
        if values:
            parts.append(f"{label}={','.join(values)}")
    for key in ["slot", "drill", "nonplated"]:
        if clean.get(key):
            parts.append(key)
    return "; ".join(parts) or "none"


def _normal_padstack_values(value: object) -> list[str]:
    if value in [None, ""]:
        return []
    values = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in values:
        text = _normal_padstack_value(item)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normal_padstack_value(value: object) -> str:
    return str(value if value not in [None, ""] else "").strip().lower().replace("-", "_")


def _batch_expectation_failures(row: dict, expected: object) -> list[str]:
    if not isinstance(expected, dict):
        return ["expected must be an object when provided"]
    key_map = {
        "components": "components",
        "footprints": "footprints",
        "pins": "pins",
        "pads": "pads",
        "models": "models_count",
        "errors": "errors",
        "warnings": "warnings",
        "step_errors": "step_errors",
        "step_warnings": "step_warnings",
        "fetch_3d_models": "fetch_3d_model_count",
        "fetch_3d_errors": "fetch_3d_error_count",
        "lcsc_resolution_models": "lcsc_resolution_count",
        "lcsc_resolution_errors": "lcsc_resolution_error_count",
        "cache_3d_models": "cache_3d_model_count",
        "cache_3d_payloads": "cache_3d_payload_count",
        "cache_3d_missing": "cache_3d_missing_count",
        "placeholder_3d_models": "placeholder_3d_count",
        "review_3d_ready_for_canvas_review": "review_3d_ready_for_canvas_review",
        "review_3d_placeholder_not_for_signoff": "review_3d_placeholder_not_for_signoff",
        "review_3d_errors": "review_3d_errors",
        "review_3d_warnings": "review_3d_warnings",
        "padstacks": "padstack_count",
        "padstack_errors": "padstack_errors",
        "padstack_warnings": "padstack_warnings",
        "slot_pads": "padstack_slot_pads",
        "drilled_pads": "padstack_drilled_pads",
        "nonplated_drill_pads": "padstack_nonplated_drill_pads",
        "electrical_pads": "padstack_electrical_pads",
        "shield_pads": "padstack_shield_pads",
        "mechanical_pads": "padstack_mechanical_pads",
        "mounting_pads": "padstack_mounting_pads",
        "npth_pads": "padstack_npth_pads",
        "repair_kit_errors": "repair_kit_errors",
        "repair_kit_warnings": "repair_kit_warnings",
        "repair_kit_missing_footprints": "repair_kit_missing_footprints",
        "repair_kit_alias_rows": "repair_kit_alias_rows",
        "repair_kit_placeholder_aliases": "repair_kit_placeholder_aliases",
        "repair_kit_missing_alias_targets": "repair_kit_missing_alias_targets",
        "repair_kit_model_files": "repair_kit_model_files",
    }
    failures = []
    for manifest_key, row_key in key_map.items():
        if manifest_key not in expected:
            continue
        if int(expected[manifest_key]) != int(row[row_key]):
            failures.append(f"{manifest_key}: expected {expected[manifest_key]}, got {row[row_key]}")
    _append_expected_padstack_categories(
        failures,
        expected,
        "pad_kinds",
        row.get("padstack_kind_counts", {}),
        "pad kind",
    )
    _append_expected_padstack_categories(
        failures,
        expected,
        "pad_roles",
        row.get("padstack_role_counts", {}),
        "pad role",
    )
    _append_expected_padstack_categories(
        failures,
        expected,
        "pad_shapes",
        row.get("padstack_shape_counts", {}),
        "pad shape",
    )
    if "padstack_status" in expected:
        actual_status = str(row.get("padstack_status", ""))
        expected_status = str(expected.get("padstack_status", ""))
        if expected_status != actual_status:
            failures.append(f"padstack_status: expected {expected_status}, got {actual_status}")
    if "review_3d_status" in expected:
        actual_status = str(row.get("review_3d_status", ""))
        expected_status = str(expected.get("review_3d_status", ""))
        if expected_status != actual_status:
            failures.append(f"review_3d_status: expected {expected_status}, got {actual_status}")
    if "lcsc_resolution_status" in expected:
        actual_status = str(row.get("lcsc_resolution_status", ""))
        expected_status = str(expected.get("lcsc_resolution_status", ""))
        if expected_status != actual_status:
            failures.append(
                f"lcsc_resolution_status: expected {expected_status}, got {actual_status}"
            )
    if "repair_kit_status" in expected:
        actual_status = str(row.get("repair_kit_status", ""))
        expected_status = str(expected.get("repair_kit_status", ""))
        if expected_status != actual_status:
            failures.append(f"repair_kit_status: expected {expected_status}, got {actual_status}")
    return failures


def _append_expected_padstack_categories(
    failures: list[str],
    expected: dict,
    manifest_key: str,
    counts: object,
    label: str,
) -> None:
    if manifest_key not in expected:
        return
    if not isinstance(counts, dict):
        counts = {}
    values = _normal_padstack_values(expected.get(manifest_key))
    if not values:
        failures.append(f"{manifest_key}: expected a non-empty string or list")
        return
    for value in values:
        if int(counts.get(value, 0) or 0) <= 0:
            failures.append(f"{label}: expected coverage for {value}")


def _batch_cadence_failures(row: dict, run_cadence: bool) -> list[str]:
    if not run_cadence:
        return []
    statuses = row.get("cadence_statuses") or []
    if not statuses:
        return ["Cadence run requested but no create_symbols.results.json was produced"]
    failures = []
    for item in statuses:
        if item.get("status") not in {"created_psm", "reused_psm"}:
            failures.append(f"{item.get('footprint', '<unknown>')}: {item.get('status', '<missing status>')}")
    capture_status = row.get("capture_status")
    if not capture_status:
        failures.append("Capture run requested but no capture_build.results.json was produced")
    elif capture_status != "created_olb":
        failures.append(f"Capture OLB: {capture_status}")
    return failures


def _write_batch_csv(rows: list[dict], path: Path) -> None:
    fields = [
        "name",
        "status",
        "exit_code",
        "components",
        "footprints",
        "pins",
        "pads",
        "models_count",
        "errors",
        "warnings",
        "step_errors",
        "step_warnings",
        "signoff_file",
        "signoff_template",
        "require_signoff",
        "fetch_3d_status",
        "cache_3d_status",
        "cadence_asset_status",
        "cadence_asset_matches",
        "cadence_asset_unmatched",
        "cadence_asset_conflicts",
        "cadence_asset_library_assets",
        "cadence_asset_errors",
        "cadence_asset_json",
        "placeholder_3d_status",
        "placeholder_3d_count",
        "repair_kit_status",
        "repair_kit_placeholder_aliases",
        "repair_kit_missing_alias_targets",
        "repair_kit_model_files",
        "model_roots",
        "model_rules",
        "repair_kit_import_status",
        "repair_kit_import_rule_matches",
        "repair_kit_import_copied_models",
        "repair_kit_import_errors",
        "repair_kit_import_warnings",
        "repair_kit_import_json",
        "repair_kit_sync_json",
        "require_3d",
        "resolve_lcsc_3d_requested",
        "fetch_3d_download_count",
        "fetch_3d_model_count",
        "fetch_3d_error_count",
        "lcsc_resolution_status",
        "lcsc_resolution_count",
        "lcsc_resolution_error_count",
        "lcsc_resolution_json",
        "cache_3d_model_count",
        "cache_3d_payload_count",
        "cache_3d_missing_count",
        "padstack_status",
        "padstack_count",
        "padstack_errors",
        "padstack_warnings",
        "padstack_slot_pads",
        "padstack_drilled_pads",
        "padstack_nonplated_drill_pads",
        "padstack_shield_pads",
        "padstack_npth_pads",
        "padstack_json",
        "padstack_markdown",
        "review_3d_status",
        "review_3d_ready_for_canvas_review",
        "review_3d_canvas_signed_off",
        "review_3d_canvas_signoff_incomplete",
        "review_3d_canvas_signoff_failed",
        "review_3d_placeholder_not_for_signoff",
        "review_3d_errors",
        "review_3d_warnings",
        "review_3d_signoff_file",
        "duration_seconds",
        "out",
        "repair_kit",
        "expectation_failures",
        "cadence_failures",
        "capture_status",
        "output_status",
        "output_errors",
        "output_warnings",
        "audit_status",
        "audit_errors",
        "audit_warnings",
        "audit_markdown",
        "audit_exception",
        "exception",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            csv_row = {field: row.get(field, "") for field in fields}
            csv_row["expectation_failures"] = "; ".join(row.get("expectation_failures", []))
            csv_row["cadence_failures"] = "; ".join(row.get("cadence_failures", []))
            writer.writerow(csv_row)


def _write_batch_markdown(payload: dict, path: Path) -> None:
    lines = [
        f"# jlc2cadence batch summary",
        "",
        f"- Version: {payload['version']}",
        f"- Samples: {payload['sample_count']}",
        f"- Passed: {payload['passed']}",
        f"- Failed: {payload['failed']}",
        f"- Require 3D: {str(payload.get('require_3d', False)).lower()}",
        f"- Require signoff: {str(payload.get('require_signoff', False)).lower()}",
        f"- Model roots: {', '.join(payload.get('model_roots', [])) or 'none'}",
        f"- Model rules: {payload.get('model_rules') or 'none'}",
        f"- Padstack requirements: {_padstack_requirements_text(payload.get('padstack_requirements', {}))}",
        "",
        "| Sample | Status | Output | Audit | Capture | 3D Review | Signoff | Signoff Template | Require 3D | Require Signoff | HTTP 3D | Cache 3D | 3D errors | Repair Kit | Rule 3D | Placeholder 3D | Padstack | Padstack Errors | Padstacks | Slot Pads | NPTH Pads | Shield Pads | Components | Footprints | Pins | Pads | Models | Errors | Warnings | LCSC 3D |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["samples"]:
        lines.append(
            "| {name} | {status} | {output_status} | {audit_status} | {capture_status} | {review_3d_status} | {review_3d_signoff_file} | {signoff_template} | {require_3d} | {require_signoff} | {fetch_3d_status} | {cache_3d_status} | {review_3d_errors} | {repair_kit_status} | {repair_kit_import_rule_matches} | {placeholder_3d_count} | {padstack_status} | {padstack_errors} | {padstack_count} | {padstack_slot_pads} | {padstack_npth_pads} | {padstack_shield_pads} | {components} | {footprints} | {pins} | {pads} | {models_count} | {errors} | {warnings} | {lcsc_resolution_status}:{lcsc_resolution_count}/{lcsc_resolution_error_count} |".format(
                **row
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _manifest_path(manifest_dir: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {label}.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_dir / path
    return path.resolve()


def _manifest_optional_path(manifest_dir: Path, value: object, label: str = "models") -> Path | None:
    if value in [None, ""]:
        return None
    return _manifest_path(manifest_dir, value, label)


def _manifest_optional_paths(manifest_dir: Path, value: object) -> list[Path]:
    if value in [None, ""]:
        return []
    if isinstance(value, (str, Path)):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("Optional path list must be a string or list.")
    paths: list[Path] = []
    for item in values:
        if item in [None, ""]:
            continue
        paths.append(_manifest_path(manifest_dir, str(item), "local_model_cache"))
    return paths


def _local_model_cache_roots(args: argparse.Namespace) -> list[Path]:
    roots = getattr(args, "local_model_cache", None) or []
    if isinstance(roots, (str, Path)):
        roots = [roots]
    return [Path(path).expanduser().resolve() for path in roots if str(path).strip()]


def _repair_kit_build_sync_summary(report: dict | None) -> dict:
    if report is None:
        return {"status": "skipped", "counts": {}}
    return {
        "status": report.get("status", ""),
        "before": report.get("before", {}),
        "after": report.get("after", {}),
        "counts": report.get("counts", {}),
        "updated_aliases": report.get("updated_aliases", []),
        "ambiguous_aliases": report.get("ambiguous_aliases", []),
        "unresolved_aliases": report.get("unresolved_aliases", []),
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


def _repair_kit_build_import_summary(report: dict | None) -> dict:
    if report is None:
        return {"status": "skipped", "counts": {}}
    return {
        "status": report.get("status", ""),
        "model_roots": report.get("model_roots", []),
        "model_rules": report.get("model_rules", ""),
        "model_rules_loaded": report.get("model_rules_loaded", []),
        "before": report.get("before", {}),
        "after": report.get("after", {}),
        "counts": report.get("counts", {}),
        "imported_aliases": report.get("imported_aliases", []),
        "rule_imports": report.get("rule_imports", []),
        "copied_models": report.get("copied_models", []),
        "ambiguous_aliases": report.get("ambiguous_aliases", []),
        "unresolved_aliases": report.get("unresolved_aliases", []),
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


def _three_d_review_summary(report: object | None, reports_dir: Path | None = None) -> dict:
    if report is None:
        payload = {
            "version": f"v{__version__}",
            "status": "skipped",
            "counts": {},
            "errors": [],
            "warnings": [],
            "ready_for_canvas_review": 0,
            "canvas_signed_off": 0,
            "canvas_signoff_incomplete": 0,
            "canvas_signoff_failed": 0,
            "placeholder_not_for_signoff": 0,
            "canvas_ready": 0,
            "real_step_candidates": 0,
            "noncomponent_3d_exemptions": 0,
            "allowed_noncomponent_3d_exemptions": 0,
            "required_3d_footprints": 0,
            "linked_required_3d_models": 0,
            "missing_required_3d_models": 0,
            "allow_noncomponent_3d_exceptions": False,
            "signoff_file": "",
        }
    else:
        if isinstance(report, dict):
            counts = dict(report.get("counts", {}))
            errors = list(report.get("errors", []))
            warnings = list(report.get("warnings", []))
            status = str(report.get("status", ""))
            version = str(report.get("version", f"v{__version__}"))
            out_dir = str(report.get("out_dir", ""))
            signoff_file = str(report.get("signoff_file", ""))
            allow_noncomponent = bool(report.get("allow_noncomponent_3d_exceptions", False))
        else:
            counts = dict(getattr(report, "counts", {}) or {})
            errors = list(getattr(report, "errors", []) or [])
            warnings = list(getattr(report, "warnings", []) or [])
            status = str(getattr(report, "status", ""))
            version = str(getattr(report, "version", f"v{__version__}"))
            out_dir = str(getattr(report, "out_dir", ""))
            signoff_file = str(getattr(report, "signoff_file", ""))
            allow_noncomponent = bool(getattr(report, "allow_noncomponent_3d_exceptions", False))
        payload = {
            "version": version,
            "out_dir": out_dir,
            "allow_noncomponent_3d_exceptions": allow_noncomponent,
            "status": status,
            "counts": counts,
            "errors": errors,
            "warnings": warnings,
            "ready_for_canvas_review": int(counts.get("ready_for_canvas_review", 0) or 0),
            "canvas_signed_off": int(counts.get("canvas_signed_off", 0) or 0),
            "canvas_signoff_incomplete": int(counts.get("canvas_signoff_incomplete", 0) or 0),
            "canvas_signoff_failed": int(counts.get("canvas_signoff_failed", 0) or 0),
            "placeholder_not_for_signoff": int(counts.get("placeholder_not_for_signoff", 0) or 0),
            "canvas_ready": int(counts.get("canvas_ready", 0) or 0),
            "real_step_candidates": int(counts.get("real_step_candidates", 0) or 0),
            "noncomponent_3d_exemptions": int(counts.get("noncomponent_3d_exemptions", 0) or 0),
            "allowed_noncomponent_3d_exemptions": int(counts.get("allowed_noncomponent_3d_exemptions", 0) or 0),
            "required_3d_footprints": int(counts.get("required_3d_footprints", 0) or 0),
            "linked_required_3d_models": int(counts.get("linked_required_3d_models", 0) or 0),
            "missing_required_3d_models": int(counts.get("missing_required_3d_models", 0) or 0),
            "signoff_file": signoff_file,
        }
    if reports_dir is not None:
        payload["3d_review_json"] = str(reports_dir / "3d_review.json")
        payload["3d_review_markdown"] = str(reports_dir / "3d_review.md")
    return payload


def _manifest_output_path(path: Path, manifest_dir: Path) -> str:
    resolved = path.expanduser().resolve()
    manifest_dir = manifest_dir.expanduser().resolve()
    try:
        return str(resolved.relative_to(manifest_dir))
    except ValueError:
        try:
            return os.path.relpath(resolved, manifest_dir)
        except ValueError:
            return str(resolved)


def _batch_safe_name(name: str, index: int) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_$" else "_" for ch in name).strip("_")
    return safe or f"sample_{index}"


def _batch_signoff_template_path(template_target: Path, sample_name: str, sample_index: int, sample_count: int) -> Path:
    target = template_target.expanduser().resolve()
    safe_name = _batch_safe_name(sample_name, sample_index)
    if target.exists() and target.is_dir():
        return target / f"{safe_name}_canvas_signoff.json"
    if not target.suffix:
        return target / f"{safe_name}_canvas_signoff.json"
    if sample_count == 1:
        return target
    return target.with_name(f"{target.stem}_{safe_name}{target.suffix}")


def _as_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _library_pin_count(library) -> int:
    return sum(len(component.pins) for component in library.components)


def _library_pad_count(library) -> int:
    return sum(len(footprint.pads) for footprint in library.footprints)


def _ir_pin_count(ir: dict) -> int:
    return sum(
        len(_as_list(component.get("pins")))
        for component in _as_list(ir.get("components"))
        if isinstance(component, dict)
    )


def _ir_pad_count(ir: dict) -> int:
    return sum(
        len(_as_list(footprint.get("pads")))
        for footprint in _as_list(ir.get("footprints"))
        if isinstance(footprint, dict)
    )


def _dedupe_models(models):
    deduped = []
    seen = set()
    for model in models:
        key = (model.file, model.source_file, model.key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(model)
    return deduped


def _apply_repair_kit_defaults(args: argparse.Namespace, require_input: bool) -> bool:
    repair_kit_path = getattr(args, "repair_kit", None)
    if repair_kit_path:
        try:
            repair_kit = load_model_repair_kit(repair_kit_path)
        except Exception as exc:
            print(f"Could not read repair kit: {exc}", file=sys.stderr)
            return False
        if getattr(args, "input", None) is None and repair_kit.input_path is not None:
            args.input = repair_kit.input_path
        if getattr(args, "models", None) is None:
            args.models = repair_kit.models_dir
        if getattr(args, "model_aliases", None) is None:
            args.model_aliases = repair_kit.aliases_path
    if require_input and getattr(args, "input", None) is None:
        print("--input is required unless --repair-kit contains an input export path.", file=sys.stderr)
        return False
    return True


def _flow_model_roots(args: argparse.Namespace) -> list[Path]:
    roots = getattr(args, "model_root", None) or []
    if isinstance(roots, (str, Path)):
        roots = [roots]
    return [Path(root).expanduser().resolve() for root in roots if str(root).strip()]


def _prepare_repair_kit_models(
    repair_kit_path: Path | None,
    model_roots: list[Path],
    model_rules: Path | None,
) -> tuple[dict | None, dict | None]:
    if not model_roots:
        return None, None
    if repair_kit_path is None:
        raise ValueError("--model-root requires --repair-kit.")
    import_report = import_model_repair_kit_models(
        repair_kit_path,
        model_roots,
        write=True,
        model_rules=model_rules,
    )
    sync_report = sync_model_repair_kit(repair_kit_path, write=True)
    return import_report, sync_report


def _cache_import_stdout_payload(report) -> dict[str, object]:
    return {
        "status": report.status,
        "output_dir": report.output_dir,
        "formats": list(report.formats),
        "roots": [root.to_dict() for root in report.roots],
        "requested_model_ids": list(report.requested_model_ids),
        "matched_model_ids": report.matched_model_ids,
        "missing_model_ids": report.missing_model_ids,
        "payload_count": len(report.payloads),
        "model_count": len(report.models),
        "reference_count": len(report.references),
        "payloads": [payload.to_dict() for payload in report.payloads],
        "reference_sample": [
            reference.to_dict() for reference in report.references[:20]
        ],
    }


def _missing_3d_markdown_text(report: dict) -> str:
    return render_missing_3d_markdown(report)


if __name__ == "__main__":
    raise SystemExit(main())
