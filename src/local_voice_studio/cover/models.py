"""Shared semantic domain enums for cover assets."""
from enum import Enum

class CoverAssetRole(str, Enum):
    ORIGINAL = "original"
    VOCAL = "vocal"
    INSTRUMENTAL = "instrumental"
    AI_VOCAL = "ai_vocal"
    FINAL_MIX = "final_mix"

class ContentOrigin(str, Enum):
    ORIGINAL = "original"
    SEPARATED = "separated"
    AI_GENERATED = "ai_generated"
