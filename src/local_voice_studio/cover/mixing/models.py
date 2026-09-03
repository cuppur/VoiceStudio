"""Pure mixing domain models; no filesystem or FFmpeg dependencies."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any

from ..models import CoverAssetRole


class GainScale:
    """Single conversion boundary between UI sliders and domain dB values."""

    MIN_DB = -24.0
    UNITY_DB = 0.0
    MAX_DB = 6.0
    UNITY_SLIDER = 80

    @classmethod
    def slider_to_db(cls, value: int | float) -> float | str:
        slider = max(0.0, min(100.0, float(value)))
        if slider <= 0:
            return "-inf"
        if slider <= cls.UNITY_SLIDER:
            return round(cls.MIN_DB + (slider - 1.0) * (0.0 - cls.MIN_DB) / (cls.UNITY_SLIDER - 1.0), 2)
        return round((slider - cls.UNITY_SLIDER) * cls.MAX_DB / (100.0 - cls.UNITY_SLIDER), 2)

    @classmethod
    def db_to_slider(cls, value: float | str) -> int:
        if value == "-inf" or float(value) == float("-inf"):
            return 0
        db = float(value)
        if db <= cls.UNITY_DB:
            return int(round(1.0 + (db - cls.MIN_DB) * (cls.UNITY_SLIDER - 1.0) / (0.0 - cls.MIN_DB)))
        return int(round(cls.UNITY_SLIDER + db * (100.0 - cls.UNITY_SLIDER) / cls.MAX_DB))

    @staticmethod
    def db_to_linear(value: float | str) -> float:
        import math
        if value == "-inf" or float(value) == float("-inf"):
            return 0.0
        return 10.0 ** (float(value) / 20.0)

@dataclass(frozen=True)
class CoverMixSettings:
    ai_gain_db: float = 0.0
    instrumental_gain_db: float = 0.0
    original_vocal_gain_db: float = float("-inf")
    master_gain_db: float = 0.0
    normalize: bool = True
    limiter: bool = True
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    alignment_tolerance_ms: int = 250
    version: str = "cover-mix-v1"
    # Compatibility spellings used by the architecture brief.  They are
    # accepted at the API boundary but are normalized into the canonical
    # ``ai_gain_db``/``normalize`` fields below.
    ai_vocal_gain_db: float | None = None
    normalize_mode: str | None = None

    def __post_init__(self) -> None:
        if self.ai_vocal_gain_db is not None:
            object.__setattr__(self, "ai_gain_db", self.ai_vocal_gain_db)
        if self.normalize_mode is not None:
            mode = str(self.normalize_mode).strip().lower()
            if mode not in {"on", "off", "true", "false", "1", "0", "none"}:
                raise ValueError("normalize_mode 无效")
            object.__setattr__(self, "normalize", mode in {"on", "true", "1"})
        for name in ("ai_gain_db", "instrumental_gain_db", "original_vocal_gain_db", "master_gain_db"):
            value = getattr(self, name)
            if value == "-inf": object.__setattr__(self, name, float("-inf")); value = float("-inf")
            if value != float("-inf") and not -60 <= float(value) <= 24:
                raise ValueError("增益必须在 -60 到 24 dB 之间")
        if self.alignment_tolerance_ms < 0 or self.alignment_tolerance_ms > 1000:
            raise ValueError("对齐容差无效")
        if self.fade_in_ms < 0 or self.fade_out_ms < 0:
            raise ValueError("淡入淡出时长不能为负数")

    def canonical(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("ai_vocal_gain_db", None)
        value.pop("normalize_mode", None)
        for key, item in value.items():
            if isinstance(item, float) and item == float("-inf"): value[key] = "-inf"
        return value

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def mix_normalization(self) -> bool:
        """Product-facing name; ``normalize`` remains the schema-compatible field."""
        return self.normalize

@dataclass(frozen=True)
class MixInput:
    role: CoverAssetRole | str
    asset_id: str
    path: Path
    sha256: str
    gain_db: float

    def __post_init__(self) -> None:
        role = self.role.value if isinstance(self.role, Enum) else self.role
        object.__setattr__(self, "role", CoverAssetRole(str(role).strip().lower().replace("-", "_")))
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "gain_db", float(self.gain_db))

    def as_cache_tuple(self) -> tuple[str, str, str, float]:
        return self.role.value, self.asset_id, self.sha256, self.gain_db
