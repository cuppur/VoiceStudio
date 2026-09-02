"""FFmpeg-only export encoding seam."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..process import ManagedProcess

class FFmpegExportBackend:
    def __init__(self, ffmpeg: Path): self.ffmpeg = Path(ffmpeg)
    def encode(self, source: Path, target: Path, *, format: str, cancel: Any = None) -> Path:
        codec = "pcm_s16le" if format == "wav" else "libmp3lame"
        args = [str(self.ffmpeg), "-y", "-i", str(source), "-ar", "48000", "-ac", "2", "-c:a", codec, str(target)]
        process = ManagedProcess(args, cancel=cancel)
        try:
            return_code = process.run()
        except InterruptedError as exc:
            raise RuntimeError("导出已取消") from exc
        if return_code:
            detail = f"：{process.stderr_tail}" if process.stderr_tail else ""
            raise RuntimeError("FFmpeg 导出失败" + detail)
        return target
