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
