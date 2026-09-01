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
    training_source_asset_ids: list[str] = field(default_factory=list)
    origin: str = "trained-local"
    trust_status: str = "verified"
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SingingModelVersion":
        value = value if isinstance(value, dict) else {}
        allowed = cls.__dataclass_fields__
        source_ids = value.get("training_source_asset_ids", [])
        if not isinstance(source_ids, list):
            source_ids = list(source_ids) if source_ids else []
        return cls(
            **{key: item for key, item in value.items() if key in allowed and key != "training_source_asset_ids"},
            training_source_asset_ids=[str(item) for item in source_ids],
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
        return not self.index_relative_path or (index is not None and index.is_file())

    def hashes_match(self, project_root: Path | None = None) -> bool:
        """Verify declared file digests (empty digests mean legacy/unpinned)."""
        return self._matches(project_root, self.checkpoint_relative_path, self.checkpoint_sha256) and self._matches(
            project_root, self.index_relative_path, self.index_sha256
        )
