"""Validation of encoded export files before they enter the publish transaction."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ...audio import AudioProbe, probe_audio
from ..cancellation import as_cancellation_token
from ..errors import AssetValidationError
from .models import ExportFormat


@dataclass(frozen=True)
class ValidatedExportAudio:
    path: Path
    format: str
    duration_seconds: float
    sample_rate: int
    channels: int
    bit_rate: int | None = None
    codec_name: str = ""


class ExportOutputValidator:
    """Probe and enforce the audio contract for a staged WAV or MP3."""

    def __init__(self, probe: Callable[..., AudioProbe] | None = None):
        self.probe = probe or probe_audio

    def validate(
        self,
        path: Path,
        *,
        expected_format: ExportFormat | str,
        source_duration_seconds: float,
        probe: Callable[..., AudioProbe] | None = None,
        cancel: Any = None,
    ) -> ValidatedExportAudio:
        token = as_cancellation_token(cancel)
        if token.is_cancelled():
            raise InterruptedError("导出验证已取消")
        fmt = expected_format.value if isinstance(expected_format, ExportFormat) else str(expected_format)
        if fmt not in {"wav", "mp3"}:
            raise AssetValidationError(f"不支持验证的导出格式: {fmt}")
        path = Path(path)
        minimum = 44 if fmt == "wav" else 128
        if not path.is_file() or path.stat().st_size < minimum:
            raise AssetValidationError(f"导出的 {fmt.upper()} 文件无效或为空")
        try:
            result = (probe or self.probe)(path, cancel=token)
        except InterruptedError:
            raise
        except Exception as exc:
            raise AssetValidationError(f"无法读取导出的 {fmt.upper()} 音频") from exc
        def field(name: str, default: Any = 0) -> Any:
            if isinstance(result, Mapping):
                return result.get(name, default)
            return getattr(result, name, default)

        duration = float(field("duration_seconds") or 0)
        sample_rate = int(field("sample_rate") or 0)
        channels = int(field("channels") or 0)
        codec = str(field("codec", field("codec_name", "")) or "").lower()
        bit_rate_value = int(field("bit_rate") or 0)
        if duration <= 0 or sample_rate != 48000 or channels != 2:
            raise AssetValidationError(f"{fmt.upper()} 音频参数不符合导出契约")
        if fmt == "wav" and not codec.startswith("pcm"):
            raise AssetValidationError("WAV 编码必须为 PCM")
        if fmt == "mp3":
            if codec != "mp3":
                raise AssetValidationError("MP3 编码必须为 mp3")
            if bit_rate_value and not 300000 <= bit_rate_value <= 340000:
                raise AssetValidationError("MP3 比特率不符合 320 kbps 契约")
        tolerance = 0.1 if fmt == "wav" else 0.25
        if source_duration_seconds > 0 and abs(duration - source_duration_seconds) > tolerance:
            raise AssetValidationError(f"{fmt.upper()} 时长与 Final Mix 不一致")
        if token.is_cancelled():
            raise InterruptedError("导出验证已取消")
        return ValidatedExportAudio(path, fmt, duration, sample_rate, channels,
                                    bit_rate_value or None, codec)


__all__ = ["ExportOutputValidator", "ValidatedExportAudio"]
