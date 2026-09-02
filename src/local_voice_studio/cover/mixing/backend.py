"""FFmpeg-only mixer backend seam.

This adapter intentionally accepts already-validated paths and knows nothing
about projects, rights, profiles, or assets.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Any
from ..process import ManagedProcess
from .models import CoverMixSettings, MixInput

@dataclass(frozen=True)
class AudioRenderResult:
    path: Path
    duration_seconds: float = 0.0

class FFmpegMixBackend:
    def __init__(self, ffmpeg: Path):
        self.ffmpeg = Path(ffmpeg)
        self.process: ManagedProcess | None = None

    def cancel(self) -> None:
        if self.process is not None:
            self.process.stop()
    def render(self, inputs: Sequence[MixInput], settings: CoverMixSettings,
               staging_path: Path, cancel: Any = None) -> AudioRenderResult:
        """Render already-resolved inputs; no project/profile policy here."""
        args = ["-y"]
        for item in inputs:
            args += ["-i", str(item.path)]
        filters = []
        for index, item in enumerate(inputs):
            filters.append(
                f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={item.gain_db}dB[a{index}]"
            )
        labels = "".join(f"[a{index}]" for index in range(len(inputs)))
        chain = labels + (
            f"amix=inputs={len(inputs)}:duration=longest:"
            f"normalize={'1' if settings.normalize else '0'},"
            f"volume={settings.master_gain_db}dB"
        )
        if settings.limiter:
            chain += ",alimiter=limit=0.95"
        if settings.fade_in_ms:
            chain += f",afade=t=in:st=0:d={settings.fade_in_ms / 1000:g}"
        filters.append(chain + ",aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[out]")
        args += ["-filter_complex", ";".join(filters), "-map", "[out]",
                 "-c:a", "pcm_s16le", "-f", "wav"]
        process = ManagedProcess([str(self.ffmpeg), *args, str(staging_path)], cancel=cancel)
        self.process = process
        try:
            return_code = process.run()
        except InterruptedError as exc:
            raise RuntimeError("混音已取消") from exc
        finally:
            self.process = None
        if return_code:
            detail = f"：{process.stderr_tail}" if process.stderr_tail else ""
            raise RuntimeError("FFmpeg 混音失败" + detail)
        return AudioRenderResult(Path(staging_path))
