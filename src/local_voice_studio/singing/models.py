"""Product-layer data models for singing voice conversion.

The product model deliberately does not expose RVC implementation details beyond
the engine identifier.  Paths are stored as project-relative values by callers;
the model remains useful for both persisted manifests and in-memory validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..paths import ensure_within


@dataclass(frozen=True)
class RVCInferenceSettings:
    """Validated, cacheable product settings for one RVC conversion."""

    transpose: int = 0
    index_rate: float = 0.75
    protect: float = 0.33
    filter_radius: int = 3
    f0_method: str = "rmvpe"
    pitch_backend_version: str = "rvc-rmvpe-v1"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RVCInferenceSettings":
        raw = payload.get("inference_settings", {})
        values = dict(raw) if isinstance(raw, dict) else {}
        transpose = int(values.get("transpose", payload.get("pitch_shift", payload.get("transpose", 0))))
        index_rate = float(values.get("index_rate", payload.get("index_rate", cls.index_rate)))
        protect = float(values.get("protect", payload.get("protect", cls.protect)))
        filter_radius = int(values.get("filter_radius", payload.get("filter_radius", cls.filter_radius)))
        f0_method = str(values.get("f0_method", payload.get("f0_method", cls.f0_method))).strip().lower()
        if not -12 <= transpose <= 12: raise ValueError("变调必须在 -12 到 +12 半音之间")
        if not 0.0 <= index_rate <= 1.0: raise ValueError("音色相似度必须在 0 到 1 之间")
        if not 0.0 <= protect <= 1.0: raise ValueError("辅音保护必须在 0 到 1 之间")
        if not 0 <= filter_radius <= 7: raise ValueError("滤波半径必须在 0 到 7 之间")
        if f0_method not in {"auto", "rmvpe"}: raise ValueError("不支持的 F0 方法")
        return cls(transpose, index_rate, protect, filter_radius, f0_method)

    def canonical(self) -> dict[str, Any]:
        return {"transpose": self.transpose, "index_rate": self.index_rate, "protect": self.protect,
                "filter_radius": self.filter_radius, "f0_method": self.f0_method,
                "pitch_backend_version": self.pitch_backend_version}

    def to_payload(self) -> dict[str, Any]:
        return {"transpose": self.transpose, "pitch_shift": self.transpose, "index_rate": self.index_rate,
                "protect": self.protect, "filter_radius": self.filter_radius, "f0_method": self.f0_method,
                "inference_settings": self.canonical()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SingingModelVersion:
    """One immutable singing-model version belonging to a VoiceProfile."""

    id: str = field(default_factory=lambda: uuid4().hex)
    profile_id: str = ""
    engine: str = ""
    engine_version: str = ""
    checkpoint_relative_path: str = ""
    checkpoint_sha256: str = ""
    index_relative_path: str = ""
    index_sha256: str = ""
    training_dataset_sha256: str = ""
    training_dataset_id: str = ""
    training_source_asset_ids: list[str] = field(default_factory=list)
    training_lineage: list[dict[str, Any]] = field(default_factory=list)
    origin: str = "trained-local"
    trust_status: str = "unverified"
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SingingModelVersion":
        value = value if isinstance(value, dict) else {}
        allowed = cls.__dataclass_fields__
        source_ids = value.get("training_source_asset_ids", [])
        lineage = value.get("training_lineage", [])
        if not isinstance(source_ids, list):
            source_ids = list(source_ids) if source_ids else []
        if not isinstance(lineage, list): lineage = []
        return cls(
            **{key: item for key, item in value.items() if key in allowed and key not in {"training_source_asset_ids", "training_lineage"}},
            training_source_asset_ids=[str(item) for item in source_ids],
            training_lineage=[dict(item) for item in lineage if isinstance(item, dict)],
        )

    @staticmethod
    def _safe_path(project_root: Path | None, path_value: str) -> Path | None:
        """Resolve a persisted relative path inside the owning project."""
        if project_root is None or not path_value:
            return None
        candidate = Path(path_value)
        if candidate.is_absolute():
            return None
        try:
            return ensure_within(Path(project_root), Path(project_root) / candidate)
        except (ValueError, OSError):
            return None

    @classmethod
    def _matches(cls, project_root: Path | None, path_value: str, expected: str) -> bool:
        if not expected:
            return True
        path = cls._safe_path(project_root, path_value)
        if path is None or not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower() == expected.lower()

    def files_available(self, project_root: Path | None = None) -> bool:
        """Return whether all declared model files are present."""
        checkpoint = self._safe_path(project_root, self.checkpoint_relative_path)
        if checkpoint is None or not checkpoint.is_file():
            return False
        index = self._safe_path(project_root, self.index_relative_path)
        return index is not None and index.is_file()

    def hashes_match(self, project_root: Path | None = None) -> bool:
        """Verify declared file digests (empty digests mean legacy/unpinned)."""
        if len(self.checkpoint_sha256) != 64 or len(self.index_sha256) != 64:
            return False
        return self._matches(project_root, self.checkpoint_relative_path, self.checkpoint_sha256) and self._matches(
            project_root, self.index_relative_path, self.index_sha256
        )
