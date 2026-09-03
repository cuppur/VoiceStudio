"""Small, strict ffprobe boundary for exported audio."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..process import ManagedProcess


@dataclass(frozen=True)
class ExportAudioInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    codec_name: str
    bit_rate: int | None = None


class ExportAudioProbe:
    """Probe exactly one audio stream and reject ambiguous ffprobe output."""

    def __init__(self, ffprobe: Path | str):
        self.ffprobe = Path(ffprobe)
        self.process: ManagedProcess | None = None

    def cancel(self) -> None:
        if self.process is not None:
            self.process.stop()

    def __call__(self, path: Path, *, cancel: Any = None) -> ExportAudioInfo:
        return self.probe(path, cancel=cancel)

    def probe(self, path: Path, *, cancel: Any = None) -> ExportAudioInfo:
        command = [str(self.ffprobe), "-v", "error", "-select_streams", "a:0",
                   "-show_entries", "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
                   "-of", "json", str(path)]
        process = ManagedProcess(command, cancel=cancel, capture_stdout=True)
        self.process = process
        try:
            code = process.run()
            if code:
                raise RuntimeError(f"ffprobe 失败{(': ' + process.stderr_tail) if process.stderr_tail else ''}")
            try:
                payload = json.loads(process.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("ffprobe 输出不是合法 JSON") from exc
            return self._parse(payload)
        except InterruptedError:
            raise
        finally:
            self.process = None

    @staticmethod
    def _parse(payload: Mapping[str, Any]) -> ExportAudioInfo:
        streams = payload.get("streams")
        if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
            raise ValueError("ffprobe 未返回唯一音频流")
        stream = streams[0]
        fmt = payload.get("format")
        if not isinstance(fmt, Mapping):
            raise ValueError("ffprobe 缺少 format")
        try:
            duration = float(fmt["duration"])
            rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
            codec = str(stream["codec_name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("ffprobe 缺少音频字段") from exc
        raw_bitrate = fmt.get("bit_rate")
        try:
            bitrate = int(raw_bitrate) if raw_bitrate not in (None, "", "N/A") else None
        except (TypeError, ValueError) as exc:
            raise ValueError("ffprobe 比特率无效") from exc
        if not math.isfinite(duration) or duration <= 0 or rate <= 0 or channels <= 0 or not codec:
            raise ValueError("ffprobe 音频字段无效")
        return ExportAudioInfo(duration, rate, channels, codec, bitrate)


__all__ = ["ExportAudioInfo", "ExportAudioProbe"]
