"""Shared semantic domain enums for cover assets."""
from enum import Enum

class CoverAssetRole(str, Enum):
    ORIGINAL = "original"
    VOCAL = "vocal"
    INSTRUMENTAL = "instrumental"
    AI_VOCAL = "ai_vocal"
    FINAL_MIX = "final_mix"

    @classmethod
    def parse(cls, value: "CoverAssetRole | str") -> "CoverAssetRole":
        """Parse persisted and legacy role spellings at the boundary."""
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().lower().replace("-", "_"))

class ContentOrigin(str, Enum):
    ORIGINAL = "original"
    SEPARATED = "separated"
    AI_GENERATED = "ai_generated"


class CoverStageStatus(str, Enum):
    """Six-state lifecycle for every cover workflow stage.

    ``interrupted`` is the crash-recovery state: it is assigned when a
    project is loaded and a stage is still marked ``running`` although the
    owning worker is gone, mirroring the training-side recovery contract.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @classmethod
    def normalize(cls, value: "CoverStageStatus | str | None") -> "CoverStageStatus":
        """Coerce persisted values (including legacy separation strings)."""
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        legacy = {
            "not_started": "pending",
            "processing": "running",
            "error": "failed",
            "done": "completed",
        }
        text = legacy.get(text, text)
        try:
            return cls(text)
        except ValueError:
            return cls.PENDING
