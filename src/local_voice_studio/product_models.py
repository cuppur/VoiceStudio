"""Product-level project, take, asset, cache and export records.

These records deliberately sit beside the legacy CoverProject schema.  They
provide a stable product vocabulary without forcing an unsafe migration of
existing projects.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar
from datetime import datetime, timezone
from uuid import uuid4

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid4().hex


@dataclass
class AssetRecord:
    kind: str
    relative_path: str
    sha256: str = ""
    id: str = field(default_factory=_id)
    source_asset_ids: list[str] = field(default_factory=list)
    producer_job_id: str = ""
    model_version_id: str = ""
    parameters_hash: str = ""
    status: str = "ready"
    created_at: str = field(default_factory=utc_now)


@dataclass
class CacheArtifact:
    operation: str
    cache_key: str
    asset_id: str
    source_sha256: str
    engine_version: str
    model_version: str = ""
    model_sha256: str = ""
    parameters_hash: str = ""
    status: str = "ready"
    created_at: str = field(default_factory=utc_now)


@dataclass
class Take:
    project_id: str
    voice_profile_id: str
    name: str = "新 Take"
    id: str = field(default_factory=_id)
    parent_take_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    asset_ids: list[str] = field(default_factory=list)
    status: str = "draft"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass
class ExportRecord:
    project_id: str
    take_id: str
    format: str
    relative_path: str
    sha256: str = ""
    id: str = field(default_factory=_id)
    status: str = "ready"
    manifest_path: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass
class SongProject:
    name: str
    source_asset_id: str
    id: str = field(default_factory=_id)
    source_sha256: str = ""
    duration_ms: int = 0
    rights_status: str = "unknown"
    active_take_id: str = ""
    active_voice_profile_id: str = ""
    asset_records: list[AssetRecord] = field(default_factory=list)
    takes: list[Take] = field(default_factory=list)
    cache_artifacts: list[CacheArtifact] = field(default_factory=list)
    exports: list[ExportRecord] = field(default_factory=list)
    schema_version: ClassVar[int] = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def add_take(self, take: Take) -> None:
        if take.project_id != self.id:
            raise ValueError("Take 不属于当前 SongProject")
        self.takes = [item for item in self.takes if item.id != take.id]
        self.takes.append(take)
        self.active_take_id = take.id
        self.active_voice_profile_id = take.voice_profile_id
        self.updated_at = utc_now()

    def get_take(self, take_id: str) -> Take:
        for take in self.takes:
            if take.id == take_id:
                return take
        raise KeyError(take_id)

    def create_take_variant(self, parent_take_id: str, *, name: str | None = None, parameters: dict[str, Any] | None = None) -> Take:
        parent = self.get_take(parent_take_id)
        variant = Take(self.id, parent.voice_profile_id, name or f"{parent.name} B", parent_take_id=parent.id, parameters={**parent.parameters, **(parameters or {})})
        self.add_take(variant)
        return variant

    def compare_takes(self, take_a_id: str, take_b_id: str) -> dict[str, Any]:
        a, b = self.get_take(take_a_id), self.get_take(take_b_id)
        return {"a": a.id, "b": b.id, "same_voice": a.voice_profile_id == b.voice_profile_id, "parameter_changes": {key: (a.parameters.get(key), b.parameters.get(key)) for key in sorted(set(a.parameters) | set(b.parameters)) if a.parameters.get(key) != b.parameters.get(key)}, "asset_changes": {"a_only": sorted(set(a.asset_ids) - set(b.asset_ids)), "b_only": sorted(set(b.asset_ids) - set(a.asset_ids))}}

    def register_cache(self, artifact: CacheArtifact) -> CacheArtifact:
        existing = next((item for item in self.cache_artifacts if item.cache_key == artifact.cache_key and item.status == "ready"), None)
        if existing is not None:
            return existing
        self.cache_artifacts = [item for item in self.cache_artifacts if item.cache_key != artifact.cache_key]
        self.cache_artifacts.append(artifact)
        self.updated_at = utc_now()
        return artifact

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = self.schema_version
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SongProject":
        payload = dict(value)
        payload.pop("schema_version", None)
        payload["asset_records"] = [AssetRecord(**item) for item in payload.get("asset_records", [])]
        payload["takes"] = [Take(**item) for item in payload.get("takes", [])]
        payload["cache_artifacts"] = [CacheArtifact(**item) for item in payload.get("cache_artifacts", [])]
        payload["exports"] = [ExportRecord(**item) for item in payload.get("exports", [])]
        allowed = set(cls.__dataclass_fields__) - {"schema_version"}
        return cls(**{key: item for key, item in payload.items() if key in allowed})

@dataclass
class VoiceCapabilityStatus:
    """User-facing capability/readiness view for one VoiceProfile."""
    tts: str = "unavailable"
    singing_conversion: str = "unavailable"
    training: str = "available"
    reasons: dict[str, str] = field(default_factory=dict)

    def can(self, capability: str) -> bool:
        return getattr(self, capability, "unavailable") == "ready"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def voice_capabilities(profile: Any) -> VoiceCapabilityStatus:
    """Derive capabilities from the existing legacy VoiceProfile safely.

    This is intentionally read-only: missing runtime/model files remain
    unavailable instead of being represented as a fake ready state.
    """
    tts_ready = bool(getattr(profile, "active_gpt_checkpoint", "") and getattr(profile, "active_sovits_checkpoint", ""))
    singing_models = list(getattr(profile, "singing_models", []) or [])
    singing_ready = any(str(getattr(model, "trust_status", "")) in {"verified", "active", "ready"} for model in singing_models)
    reasons: dict[str, str] = {}
    if not tts_ready:
        reasons["tts"] = "尚未验证 GPT-SoVITS 模型"
    if not singing_ready:
        reasons["singing_conversion"] = "尚未验证 Singing/RVC 模型"
    return VoiceCapabilityStatus(
        tts="ready" if tts_ready else "unavailable",
        singing_conversion="ready" if singing_ready else "unavailable",
        training="available",
        reasons=reasons,
    )

@dataclass
class ModelVersionRecord:
    """Unified product view of TTS or singing model versions."""
    profile_id: str
    kind: str
    id: str = field(default_factory=_id)
    engine: str = ""
    engine_version: str = ""
    checkpoint_path: str = ""
    checkpoint_sha256: str = ""
    index_path: str = ""
    index_sha256: str = ""
    trust_status: str = "unverified"
    origin: str = "trained-local"
    created_at: str = field(default_factory=utc_now)


def model_version_records(profile: Any) -> list[ModelVersionRecord]:
    """Expose legacy TTS and singing versions through one product view."""
    result: list[ModelVersionRecord] = []
    for item in list(getattr(profile, "model_versions", []) or []):
        result.append(ModelVersionRecord(
            profile_id=str(getattr(profile, "id", "")), kind="tts", id=str(getattr(item, "id", _id())),
            engine="gpt-sovits", engine_version=str(getattr(profile, "engine_version", "")),
            checkpoint_path=str(getattr(item, "sovits_checkpoint", "")), checkpoint_sha256=str(getattr(item, "sovits_sha256", "")),
            trust_status=str(getattr(item, "trust_status", "unverified")), origin=str(getattr(item, "origin", "trained-local")),
            created_at=str(getattr(item, "created_at", utc_now())),
        ))
    for item in list(getattr(profile, "singing_models", []) or []):
        result.append(ModelVersionRecord(
            profile_id=str(getattr(profile, "id", "")), kind="singing", id=str(getattr(item, "id", _id())),
            engine=str(getattr(item, "engine", "rvc")), engine_version=str(getattr(item, "engine_version", "")),
            checkpoint_path=str(getattr(item, "checkpoint_relative_path", "")), checkpoint_sha256=str(getattr(item, "checkpoint_sha256", "")),
            index_path=str(getattr(item, "index_relative_path", "")), index_sha256=str(getattr(item, "index_sha256", "")),
            trust_status=str(getattr(item, "trust_status", "unverified")), origin=str(getattr(item, "origin", "trained-local")),
            created_at=str(getattr(item, "created_at", utc_now())),
        ))
    return result


def asset_records_from_cover(cover: Any) -> list[AssetRecord]:
    """Map legacy CoverAsset entries without mutating the CoverProject."""
    return [AssetRecord(
        id=str(asset.id), kind=str(asset.role), relative_path=str(asset.relative_path), sha256=str(asset.sha256),
        source_asset_ids=list(asset.source_asset_ids), model_version_id=str(asset.model_id), status="ready",
        created_at=str(asset.created_at),
    ) for asset in list(getattr(cover, "assets", []) or [])]

