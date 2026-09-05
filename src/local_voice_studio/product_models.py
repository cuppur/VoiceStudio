"""Product-level project, take, asset, cache and export records.

These records deliberately sit beside the legacy CoverProject schema.  They
provide a stable product vocabulary without forcing an unsafe migration of
existing projects.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

from .models import utc_now


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
