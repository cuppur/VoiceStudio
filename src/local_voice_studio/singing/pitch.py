"""Small product-level helpers for transparent RMVPE pitch recommendations."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Protocol


@dataclass(frozen=True)
class PitchAnalysis:
    backend: str
    version: str
    median_hz: float
    voiced_frames: int
    minimum_hz: float = 0.0
    maximum_hz: float = 0.0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PitchAnalysis":
        result = cls(str(value.get("backend", "")), str(value.get("version", "")), float(value.get("median_hz", 0.0)),
                     int(value.get("voiced_frames", 0)), float(value.get("minimum_hz", 0.0)), float(value.get("maximum_hz", 0.0)))
        if result.backend not in {"auto", "rmvpe"} or not result.version or result.median_hz <= 0 or result.voiced_frames <= 0:
            raise ValueError("RMVPE 未能提取足够的有效音高")
        return result


class PitchBackend(Protocol):
    def analyze_pitch(self, path: Path, cancel: Any = None) -> dict[str, Any]:
        ...


def recommend_transpose(source: PitchAnalysis, targets: list[PitchAnalysis]) -> int:
    """Return a visible, bounded semitone suggestion; callers choose whether to apply it."""
    target_values = [item.median_hz for item in targets if item.median_hz > 0]
    if source.median_hz <= 0 or not target_values:
        raise ValueError("缺少可用的源音高或目标声音参考")
    semitones = round(12.0 * math.log2(float(median(target_values)) / source.median_hz))
    return max(-12, min(12, semitones))
