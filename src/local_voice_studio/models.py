from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobKind(str, Enum):
    SYNTHESIZE = "synthesize"
    PREPARE_DATASET = "prepare_dataset"
    TRAIN = "train"
    DOWNLOAD = "download"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ReferenceAsset:
    path: str
    sha256: str
    transcript: str = ""
    language: str = "zh"
    duration_seconds: float = 0.0
    approved: bool = False
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class SourceAsset:
    profile_id: str
    original_path: str
    project_path: str
    sha256: str
    id: str = field(default_factory=lambda: uuid4().hex)
    source_kind: str = "import"
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    codec: str = "unknown"
    duplicate_of: str = ""
    quality_flags: list[str] = field(default_factory=list)
    enabled: bool = True
    processing_status: str = "未处理"
    segment_count: int = 0
    confirmed_seconds: float = 0.0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceAsset":
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass
class VoiceProfile:
    name: str
    consent_confirmed: bool
    id: str = field(default_factory=lambda: uuid4().hex)
    engine_version: str = "v2ProPlus"
    reference_assets: list[ReferenceAsset] = field(default_factory=list)
    source_asset_ids: list[str] = field(default_factory=list)
    dataset_snapshot_id: str = ""
    active_gpt_checkpoint: str = ""
    active_sovits_checkpoint: str = ""
    default_model_mode: str = "zero_shot"
    training_state: str = ""
    consent_record: str = ""
    consent_confirmed_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VoiceProfile":
        value = dict(value)
        refs = [ReferenceAsset(**item) for item in value.pop("reference_assets", [])]
        allowed = cls.__dataclass_fields__
        return cls(reference_assets=refs, **{key: item for key, item in value.items() if key in allowed})

    def status(self, assets: list["SourceAsset"] | None = None) -> str:
        if not self.consent_confirmed: return "待确认授权"
        if self.training_state == "training": return "训练中"
        if self.training_state == "preparing": return "数据准备中"
        if self.active_gpt_checkpoint and self.active_sovits_checkpoint:
            if PathLikeMissing(self.active_gpt_checkpoint) or PathLikeMissing(self.active_sovits_checkpoint):
                return "模型不可用"
            return "已有微调模型"
        if assets and any(item.processing_status == "处理中" for item in assets):
            return "数据准备中"
        if assets and sum(item.confirmed_seconds for item in assets) >= 60:
            return "可训练"
        if any(item.approved and item.transcript.strip() for item in self.reference_assets):
            return "可零样本克隆"
        return "未准备参考片段"


def PathLikeMissing(value: str) -> bool:
    from pathlib import Path
    return bool(value) and not Path(value).is_file()


@dataclass
class DatasetSegment:
    source_sha256: str
    audio_path: str
    start_seconds: float
    end_seconds: float
    language: str = "zh"
    text: str = ""
    asr_text: str = ""
    asr_confidence: float | None = None
    quality_flags: list[str] = field(default_factory=list)
    approved: bool = False
    included: bool = True
    human_confirmed: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass
class DatasetManifest:
    name: str
    voice_profile_id: str
    segments: list[DatasetSegment] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    frozen: bool = False
    snapshot_sha256: str = ""
    list_path: str = ""
    wav_dir: str = ""
    list_sha256: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DatasetManifest":
        value = dict(value)
        segment_fields = DatasetSegment.__dataclass_fields__
        segments = [DatasetSegment(**{key: item for key, item in segment.items() if key in segment_fields}) for segment in value.pop("segments", [])]
        allowed = cls.__dataclass_fields__
        return cls(segments=segments, **{key: item for key, item in value.items() if key in allowed})

    @property
    def approved_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.segments if s.approved and s.included and s.human_confirmed and not s.quality_flags and s.text.strip())

    def can_train(self) -> tuple[bool, str]:
        if not self.frozen:
            return False, "请先冻结数据集快照"
        if self.approved_seconds < 60:
            return False, "已校对并通过的音频不足 60 秒"
        if any(s.approved and s.included and not s.text.strip() for s in self.segments):
            return False, "存在已通过但没有文本的片段"
        return True, "可以训练"


def dataset_snapshot_sha256(dataset: DatasetManifest | dict[str, Any]) -> str:
    """Hash immutable snapshot metadata, including every copied audio digest."""
    value = dataset.to_dict() if isinstance(dataset, DatasetManifest) else DatasetManifest.from_dict(dataset).to_dict()
    value.pop("snapshot_sha256", None)
    # Storage locations can move with a project. Their content hashes and all
    # semantic segment metadata remain part of the immutable identity.
    value.pop("list_path", None)
    value.pop("wav_dir", None)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class Job:
    kind: JobKind
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid4().hex)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""
    error: str = ""
    outputs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["status"] = self.status.value
        return result
