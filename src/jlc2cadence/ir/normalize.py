from __future__ import annotations

from dataclasses import replace

from .geometry import normalized_key
from .schema import Component, Footprint, LibraryIR, Model3D, Pin
from .units import parse_easyeda_3d_transform


def merge_libraries(*libraries: LibraryIR) -> LibraryIR:
    merged = LibraryIR()
    for library in libraries:
        for component in library.components:
            merged.upsert_component(component)
        for footprint in library.footprints:
            merged.upsert_footprint(footprint)
        merged.source_files.extend(library.source_files)
        merged.warnings.extend(library.warnings)
        merged.properties.update(library.properties)
    return merged


def ensure_components_for_footprints(library: LibraryIR) -> None:
    existing = {component.footprint for component in library.components if component.footprint}
    for footprint in library.footprints:
        if footprint.name in existing:
            continue
        pins = [Pin(number=pad.number, name=pad.number, type="Passive") for pad in footprint.pads if pad.is_electrical()]
        component = Component(
            name=footprint.name,
            ref_prefix=_guess_ref_prefix(footprint.name),
            value=footprint.name,
            footprint=footprint.name,
            pins=pins,
            description=footprint.description,
            source_file=footprint.source_file,
        )
        library.upsert_component(component)


def attach_models(library: LibraryIR, models: list[Model3D], model_aliases: dict[str, str] | None = None) -> None:
    if not models:
        return
    by_key: dict[str, Model3D] = {}
    for model in models:
        for key in _model_asset_keys(model):
            by_key[key] = model
    for footprint in library.footprints:
        for candidate in model_match_candidates(footprint, model_aliases=model_aliases):
            key = str(candidate["key"])
            if key and key in by_key:
                footprint.model3d = _apply_footprint_model_transform(
                    by_key[key],
                    footprint.properties.get("EasyEDA_3D_Model_Transform"),
                )
                break


def model_match_candidates(
    footprint: Footprint,
    model_aliases: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    raw_candidates = [
        ("footprint", footprint.name),
        ("Model", footprint.properties.get("Model", "")),
        ("Model3D", footprint.properties.get("Model3D", "")),
        ("EasyEDA_3D_Model_Id", footprint.properties.get("EasyEDA_3D_Model_Id", "")),
        ("EasyEDA_3D_Model_Title", footprint.properties.get("EasyEDA_3D_Model_Title", "")),
    ]
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    base_candidates: list[dict[str, str]] = []
    for source, value in raw_candidates:
        values = model_match_id_candidates(value) if source == "EasyEDA_3D_Model_Id" else [str(value or "").strip()]
        for index, text in enumerate(values):
            key = normalized_key(text)
            if not key or key in seen:
                continue
            seen.add(key)
            candidate_source = source if index == 0 else f"{source}:part"
            base_candidates.append({"source": candidate_source, "value": text, "key": key})
    if model_aliases:
        for candidate in base_candidates:
            alias_key = model_aliases.get(candidate["key"])
            if not alias_key:
                continue
            alias_key = _alias_target_key(str(alias_key))
            if not alias_key or alias_key in seen:
                continue
            seen.add(alias_key)
            candidates.append(
                {
                    "source": f"alias:{candidate['source']}",
                    "value": f"{candidate['value']} -> {alias_key}",
                    "key": alias_key,
                }
            )
    candidates.extend(base_candidates)
    return candidates


def _model_asset_keys(model: Model3D) -> list[str]:
    raw_keys = [
        model.key,
        _stem(model.file),
        _stem(model.source_file),
    ]
    keys = []
    seen = set()
    for raw_key in raw_keys:
        key = normalized_key(str(raw_key or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _stem(value: str) -> str:
    if not value:
        return ""
    from pathlib import Path

    path = Path(value)
    return path.stem if path.suffix else value


def _alias_target_key(value: str) -> str:
    if not value:
        return ""
    for candidate in model_id_candidates(_stem(value)):
        key = normalized_key(candidate)
        if key:
            return key
    return ""


def model_id_candidates(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    candidates = [text]
    if "|" in text:
        for part in text.split("|"):
            part = part.strip()
            if part and part not in candidates:
                candidates.append(part)
    return candidates


def model_match_id_candidates(value: object) -> list[str]:
    """Return safe library-match keys for a possibly composite EasyEDA ID.

    A composite ID is ``model_id|workspace_id``. The workspace is shared by
    many models and must not become an independent binding key.
    """
    text = str(value or "").strip()
    candidates = model_id_candidates(text)
    return candidates[:2] if "|" in text else candidates


def _apply_footprint_model_transform(model: Model3D, raw_transform: str | None) -> Model3D:
    parsed = parse_easyeda_3d_transform(raw_transform)
    if parsed is None:
        return model
    target_size, rotation, offset = parsed
    return replace(
        model,
        target_size=target_size,
        rotation=rotation,
        offset=offset,
        transform_source="easyeda_pro_v3",
        units="mm",
    )


def _guess_ref_prefix(name: str) -> str:
    upper = name.upper()
    if upper.startswith(("R_", "RES", "0402", "0603", "0805")):
        return "R"
    if upper.startswith(("C_", "CAP")):
        return "C"
    if "CONN" in upper or "USB" in upper or "FPC" in upper:
        return "J"
    return "U"
