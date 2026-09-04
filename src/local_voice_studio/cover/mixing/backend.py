"""FFmpeg infrastructure for final cover mixing.

The backend receives validated inputs only. Project policy, asset ownership,
and authorization stay in :mod:`validation` and the application service.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from ..cancellation import as_cancellation_token
from ..errors import RenderCancelledError, classify_backend_error
from ..process import FFMPEG_QUIET_ARGS, ManagedProcess
from .models import CoverMixSettings, MixInput


@dataclass(frozen=True)
class AudioRenderResult:
    path: Path
    duration_seconds: float = 0.0


class MixBackend(Protocol):
    def render(
        self,
        inputs: Sequence[MixInput],
        settings: CoverMixSettings,
        staging_path: Path,
        *,
        duration_seconds: float | None = None,
        cancel: Any = None,
    ) -> AudioRenderResult: ...

    def cancel(self) -> None: ...


class FFmpegMixBackend:
    """The only FFmpeg command/filter implementation used by the mixer."""

    def __init__(self, ffmpeg: Path):
        self.ffmpeg = Path(ffmpeg)
        self.process: ManagedProcess | None = None

    def cancel(self) -> None:
        if self.process is not None:
            self.process.stop()

    @staticmethod
    def build_filter(
        inputs: Sequence[MixInput],
        settings: CoverMixSettings,
        *,
        duration_seconds: float | None = None,
    ) -> str:
        if not inputs:
            raise ValueError("混音至少需要一个输入")
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("混音输入时长无效")
        fade_in_seconds = settings.fade_in_ms / 1000.0
        fade_out_seconds = settings.fade_out_ms / 1000.0
        if duration_seconds is not None and fade_in_seconds > duration_seconds:
            raise ValueError("fade-in 不得超过输入时长")
        if fade_out_seconds > 0 and duration_seconds is None:
            raise ValueError("fade-out 需要已验证的输入时长")
        if duration_seconds is not None and fade_out_seconds > duration_seconds:
            raise ValueError("fade-out 不得超过输入时长")

        filters: list[str] = []
        for index, item in enumerate(inputs):
            filters.append(
                f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={item.gain_db:g}dB[a{index}]"
            )
        labels = "".join(f"[a{index}]" for index in range(len(inputs)))
        chain = labels + (
            f"amix=inputs={len(inputs)}:duration=longest:"
            f"normalize={'1' if settings.normalize else '0'},"
            f"volume={settings.master_gain_db:g}dB"
        )
        if settings.limiter:
            chain += ",alimiter=limit=0.95"
        if fade_in_seconds:
            chain += f",afade=t=in:st=0:d={fade_in_seconds:g}"
        if fade_out_seconds:
            start = float(duration_seconds) - fade_out_seconds
            chain += f",afade=t=out:st={start:g}:d={fade_out_seconds:g}"
        filters.append(chain + ",aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[out]")
        return ";".join(filters)

    def render(
        self,
        inputs: Sequence[MixInput],
        settings: CoverMixSettings,
        staging_path: Path,
        *,
        duration_seconds: float | None = None,
        cancel: Any = None,
    ) -> AudioRenderResult:
        """Render validated inputs into a caller-owned staging WAV."""
        token = as_cancellation_token(cancel)
        args = [*FFMPEG_QUIET_ARGS, "-y"]
        for item in inputs:
            args += ["-i", str(item.path)]
        args += [
            "-filter_complex",
            self.build_filter(inputs, settings, duration_seconds=duration_seconds),
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(staging_path),
        ]
        process = ManagedProcess([str(self.ffmpeg), *args], cancel=token)
        self.process = process
        try:
            if token.is_cancelled():
                raise RenderCancelledError("混音已取消")
            return_code = process.run()
        except InterruptedError as exc:
            raise RenderCancelledError("混音已取消") from exc
        finally:
            self.process = None
        if return_code:
            detail = f"：{process.stderr_tail}" if process.stderr_tail else ""
            raise RuntimeError(classify_backend_error("FFmpeg 混音失败" + detail, process.stderr_tail))
        return AudioRenderResult(Path(staging_path), float(duration_seconds or 0.0))
