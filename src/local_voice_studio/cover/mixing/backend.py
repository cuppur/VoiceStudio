"""FFmpeg-only mixer backend seam.

This adapter intentionally accepts already-validated paths and knows nothing
about projects, rights, profiles, or assets.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Any
from ..process import ManagedProcess

@dataclass(frozen=True)
class AudioRenderResult:
    path: Path
    duration_seconds: float = 0.0

class FFmpegMixBackend:
    def __init__(self, ffmpeg: Path): self.ffmpeg = Path(ffmpeg)
    def render(self, args: Sequence[str], staging_path: Path, cancel: Any = None) -> AudioRenderResult:
        process = ManagedProcess([str(self.ffmpeg), *args, str(staging_path)], cancel=cancel)
        try:
            return_code = process.run()
        except InterruptedError as exc:
            raise RuntimeError("混音已取消") from exc
        if return_code:
            detail = f"：{process.stderr_tail}" if process.stderr_tail else ""
            raise RuntimeError("FFmpeg 混音失败" + detail)
        return AudioRenderResult(Path(staging_path))
