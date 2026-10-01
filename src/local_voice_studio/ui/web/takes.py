"""Read-only, provenance-based labels for a song's completed cover versions."""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...cover.project import CoverAsset, CoverProject
from ...models import VoiceProfile


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ancestors(asset: CoverAsset, assets: Mapping[str, CoverAsset]) -> list[CoverAsset]:
    """Follow recorded input IDs, never substitute the newest unrelated vocal."""
    pending = deque([asset])
    visited: set[str] = set()
    result: list[CoverAsset] = []
    while pending:
        current = pending.popleft()
        if current.id in visited:
            continue
        visited.add(current.id)
        result.append(current)
        metadata = _mapping(current.metadata)
        source_ids = list(current.source_asset_ids)
        input_ids = metadata.get("input_asset_ids")
        if isinstance(input_ids, list):
            source_ids.extend(input_ids)
        for identifier in source_ids:
            parent = assets.get(str(identifier))
            if parent is not None and parent.id not in visited:
                pending.append(parent)
    return result


def _pitch_shift(asset: CoverAsset) -> int | None:
    metadata = _mapping(asset.metadata)
    for settings in (_mapping(metadata.get("inference_settings")), metadata):
        for key in ("transpose", "pitch_shift"):
            raw = settings.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                number = float(raw)
                pitch = int(number)
            except (TypeError, ValueError, OverflowError):
                continue
            if number == pitch and -12 <= pitch <= 12:
                return pitch
    return None


def take_rows(cover: CoverProject, profiles: Iterable[VoiceProfile]) -> list[dict[str, Any]]:
    """Enrich existing final mixes without changing their order or active take.

    Profiles are supplied by the caller once per snapshot; no project/model
    manifests or audio are opened here. Missing provenance stays explicitly
    unknown instead of borrowing today's voice, model or transpose settings.
    """
    profiles_by_id = {str(profile.id): profile for profile in profiles}
    models_by_id: dict[str, list[tuple[VoiceProfile, Any]]] = defaultdict(list)
    for profile in profiles_by_id.values():
        for model in profile.singing_models:
            models_by_id[str(model.id)].append((profile, model))
    assets_by_id = {str(asset.id): asset for asset in cover.assets}
    versions: dict[str, int] = defaultdict(int)
    result: list[dict[str, Any]] = []
    for asset in cover.assets:
        if asset.role != "final_mix":
            continue
        metadata = _mapping(asset.metadata)
        lineage = _ancestors(asset, assets_by_id)
        vocals = [item for item in lineage[1:] if item.role == "ai_vocal"]
        voice_id = str(metadata.get("profile_id") or "")
        if not voice_id:
            voice_id = next((str(_mapping(item.metadata).get("profile_id"))
                             for item in vocals if _mapping(item.metadata).get("profile_id")), "")
        linked_model_id = str(asset.model_id or metadata.get("model_id") or
                              next((item.model_id for item in vocals if item.model_id), ""))
        if not voice_id and len(models_by_id.get(linked_model_id, [])) == 1:
            voice_id = str(models_by_id[linked_model_id][0][0].id)
        profile = profiles_by_id.get(voice_id)
        model = next((item for item in profile.singing_models if str(item.id) == linked_model_id), None) if profile else None
        model_name = ""
        if model is not None:
            model_name = str(getattr(model, "name", "") or
                             Path(str(model.checkpoint_relative_path)).stem or
                             model.engine_version or model.id)
        matching_vocals = [item for item in vocals
                           if not linked_model_id or not item.model_id or item.model_id == linked_model_id]
        inference_settings = next((dict(settings) for item in matching_vocals
                                   if (settings := _mapping(_mapping(item.metadata).get("inference_settings")))), {})
        pitch = next((value for item in matching_vocals if (value := _pitch_shift(item)) is not None), None)
        if pitch is None:
            pitch = _pitch_shift(asset)
        versions[voice_id] += 1
        path = cover.root / asset.relative_path
        result.append({
            "id": asset.id,
            "path": str(path),
            "created_at": asset.created_at,
            "sha256": asset.sha256,
            "settings": dict(_mapping(metadata.get("settings"))),
            "inference_settings": inference_settings,
            "model_id": asset.model_id,
            "source_asset_ids": list(asset.source_asset_ids),
            "voice_id": voice_id,
            "voice_name": str(profile.name) if profile else "未登记声音",
            "model_name": model_name or linked_model_id or "未登记模型",
            "pitch_shift": pitch,
            "version_label": f"V{versions[voice_id]:02d}",
            "exists": path.is_file(),
        })
    return result
