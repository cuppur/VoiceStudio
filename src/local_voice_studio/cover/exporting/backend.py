"""The single audio-encoding backend used by Cover export."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .. import process as process_module
from ..cancellation import as_cancellation_token
from ..errors import classify_backend_error
from ..process import FFMPEG_QUIET_ARGS


class ExportBackend(Protocol):
    """Small seam for real FFmpeg and deterministic transaction fakes."""

    def encode(self, source: Path, target: Path, *, format: str, cancel: Any = None) -> Path:
        ...

    def cancel(self) -> None:
        ...


class FFmpegExportBackend:
    """Encode WAV/MP3 while delegating lifecycle and cancellation to ManagedProcess."""

    def __init__(self, ffmpeg: Path):
        self.ffmpeg = Path(ffmpeg)
        self.process: process_module.ManagedProcess | None = None

    def cancel(self) -> None:
        process = self.process
        if process is not None:
            process.stop()

    def encode(self, source: Path, target: Path, *, format: str, cancel: Any = None) -> Path:
        if format not in {"wav", "mp3"}:
            raise ValueError(f"不支持的导出格式: {format}")
        token = as_cancellation_token(cancel)
        if token.is_cancelled():
            raise InterruptedError("导出已取消")
        codec = "pcm_s16le" if format == "wav" else "libmp3lame"
        # Keep diagnostic flags centralized in cover.process; encoding options
        # belong exclusively to this backend, never to the transaction service.
        args = [str(self.ffmpeg), "-y", *FFMPEG_QUIET_ARGS, "-i", str(source), "-ar", "48000", "-ac", "2",
                "-c:a", codec]
        if format == "mp3":
            args += ["-b:a", "320k"]
        args += ["-f", format, str(target)]
        process = process_module.ManagedProcess(args, cancel=token)
        self.process = process
        try:
            return_code = process.run()
        except InterruptedError as exc:
            raise InterruptedError("导出已取消") from exc
        finally:
            self.process = None
        if return_code:
            detail = f"：{process.stderr_tail}" if process.stderr_tail else ""
            raise RuntimeError(classify_backend_error("FFmpeg 导出失败" + detail, process.stderr_tail))
        return target

    render = encode


__all__ = ["ExportBackend", "FFmpegExportBackend"]
