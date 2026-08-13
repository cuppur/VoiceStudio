from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import wave
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable


SUPPORTED_AUDIO = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}
MAX_AUDIO_FILES = 2_000
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_DURATION_SECONDS = 6 * 60 * 60
FFPROBE_TIMEOUT_SECONDS = 30
QUALITY_TIMEOUT_SECONDS = 180


@dataclass
class AudioProbe:
    path: str
    sha256: str
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    codec: str = "unknown"
    bit_rate: int = 0
    quality_flags: list[str] = field(default_factory=list)
    duplicate_of: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _cancelled(cancel) -> bool:
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024, cancel=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            if _cancelled(cancel):
                raise RuntimeError("扫描已取消")
            digest.update(chunk)
    return digest.hexdigest()


def scan_audio_files(
    paths: Iterable[Path], ffprobe: Path | None = None, *, progress: Callable[[float, str], None] | None = None,
    cancel=None, max_files: int = MAX_AUDIO_FILES, max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES, max_duration_seconds: float = MAX_DURATION_SECONDS,
) -> list[AudioProbe]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO)
        elif path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO:
            files.append(path)

    files = sorted(set(files), key=lambda item: str(item).lower())
    if len(files) > max_files:
        raise ValueError(f"音频文件超过上限 {max_files}")
    total_bytes = 0
    for path in files:
        size = path.stat().st_size
        if size > max_file_bytes:
            raise ValueError(f"单个音频超过 2 GiB 上限: {path.name}")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ValueError("导入音频总大小超过 20 GiB 上限")
    seen: dict[str, str] = {}
    result: list[AudioProbe] = []
    for index, path in enumerate(files, 1):
        if _cancelled(cancel): raise RuntimeError("扫描已取消")
        probe = probe_audio(path, ffprobe, cancel=cancel)
        if probe.duration_seconds > max_duration_seconds:
            raise ValueError(f"单个音频超过 6 小时上限: {path.name}")
        if probe.sha256 in seen:
            probe.duplicate_of = seen[probe.sha256]
            probe.quality_flags.append("duplicate")
        else:
            seen[probe.sha256] = str(path)
        result.append(probe)
        if progress: progress(index / max(1, len(files)), f"检查素材 {index}/{len(files)}")
    return result


def probe_audio(path: Path, ffprobe: Path | None = None, *, cancel=None) -> AudioProbe:
    digest = sha256_file(path, cancel=cancel)
    resolved_ffprobe = ffprobe or _find_tool("ffprobe")
    if resolved_ffprobe:
        try:
            probe = _probe_with_ffprobe(path, resolved_ffprobe, digest)
            ffmpeg = resolved_ffprobe.with_name("ffmpeg.exe" if resolved_ffprobe.suffix.lower() == ".exe" else "ffmpeg")
            if ffmpeg.is_file(): probe.quality_flags.extend(flag for flag in _quality_with_ffmpeg(path, ffmpeg) if flag not in probe.quality_flags)
            return probe
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    if path.suffix.lower() == ".wav":
        return _probe_wav(path, digest)
    if path.suffix.lower() == ".mp3":
        return _probe_mp3(path, digest)
    return AudioProbe(str(path), digest, codec=path.suffix.lower().lstrip("."), quality_flags=["metadata_unavailable"])


def _find_tool(name: str) -> Path | None:
    app = Path(os.environ.get("LOCAL_VOICE_STUDIO_HOME", Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LocalVoiceStudio"))
    executable = name + (".exe" if os.name == "nt" and not name.endswith(".exe") else "")
    candidates = [app / "tools" / executable, app / "runtime" / "env" / "Library" / "bin" / executable, app / "engines" / "GPT-SoVITS" / executable]
    return next((item for item in candidates if item.is_file()), None)


def _quality_with_ffmpeg(path: Path, ffmpeg: Path) -> list[str]:
    completed = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path), "-af", "volumedetect,silencedetect=noise=-40dB:d=2", "-f", "null", os.devnull], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=QUALITY_TIMEOUT_SECONDS)
    report = completed.stderr; flags: list[str] = []
    maximum = re.search(r"max_volume:\s*(-?[\d.]+) dB", report); mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", report)
    if maximum and float(maximum.group(1)) >= -0.1: flags.append("clipping_risk")
    if mean and float(mean.group(1)) < -35: flags.append("low_loudness")
    if "silence_duration:" in report: flags.append("long_silence")
    return flags


def _probe_with_ffprobe(path: Path, ffprobe: Path, digest: str) -> AudioProbe:
    command = [
        str(ffprobe), "-v", "error", "-show_entries",
        "format=duration,bit_rate:stream=codec_name,sample_rate,channels",
        "-select_streams", "a:0", "-of", "json", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=FFPROBE_TIMEOUT_SECONDS)
    value = json.loads(completed.stdout)
    stream = (value.get("streams") or [{}])[0]
    fmt = value.get("format") or {}
    flags: list[str] = []
    duration = float(fmt.get("duration") or 0)
    channels = int(stream.get("channels") or 0)
    if duration <= 0:
        flags.append("invalid_duration")
    if channels > 1:
        flags.append("stereo_review_required")
    return AudioProbe(
        path=str(path), sha256=digest, duration_seconds=duration,
        sample_rate=int(stream.get("sample_rate") or 0), channels=channels,
        codec=str(stream.get("codec_name") or "unknown"), bit_rate=int(fmt.get("bit_rate") or 0),
        quality_flags=flags,
    )


def _probe_wav(path: Path, digest: str) -> AudioProbe:
    flags: list[str] = []
    with wave.open(str(path), "rb") as value:
        frames = value.getnframes()
        rate = value.getframerate()
        channels = value.getnchannels()
        duration = frames / rate if rate else 0
        if channels > 1:
            flags.append("stereo_review_required")
        return AudioProbe(str(path), digest, duration, rate, channels, "pcm", rate * channels * value.getsampwidth() * 8, flags)


_BITRATES = {
    3: {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]},
    2: {1: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]},
}
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _probe_mp3(path: Path, digest: str) -> AudioProbe:
    """Parse MPEG Layer III frames sufficiently for duration and basic validation."""
    file_size = path.stat().st_size
    frames = 0
    samples = 0
    total_bitrate = 0
    sample_rate = 0
    channel_mode = 0
    broken = False
    with path.open("rb") as stream:
        prefix = stream.read(10)
        offset = _skip_id3v2(prefix, file_size)
        stream.seek(offset)
        while offset + 4 <= file_size:
            header_bytes = stream.read(4)
            if len(header_bytes) < 4: break
            header = int.from_bytes(header_bytes, "big")
            if header & 0xFFE00000 != 0xFFE00000:
                offset += 1; stream.seek(offset)
                if frames: broken = True
                continue
            version_bits = (header >> 19) & 0x3
            layer_bits = (header >> 17) & 0x3
            bitrate_index = (header >> 12) & 0xF
            rate_index = (header >> 10) & 0x3
            padding = (header >> 9) & 0x1
            if version_bits == 1 or layer_bits != 1 or bitrate_index in (0, 15) or rate_index == 3:
                offset += 1; stream.seek(offset); continue
            version = 3 if version_bits == 3 else (2 if version_bits == 2 else 0)
            table_version = 3 if version == 3 else 2
            bitrate = _BITRATES[table_version][1][bitrate_index] * 1000
            rate = _SAMPLE_RATES[version][rate_index]
            frame_length = int((144 if version == 3 else 72) * bitrate / rate + padding)
            if frame_length <= 4 or offset + frame_length > file_size: break
            frames += 1; samples += 1152 if version == 3 else 576; total_bitrate += bitrate
            sample_rate = rate; channel_mode = (header >> 6) & 0x3
            offset += frame_length; stream.seek(offset)
    flags: list[str] = ["lossy_source"]
    if broken:
        flags.append("frame_gaps_detected")
    if not frames or not sample_rate:
        flags.append("invalid_mp3")
    channels = 1 if channel_mode == 3 else 2
    if channels > 1:
        flags.append("stereo_review_required")
    return AudioProbe(
        str(path), digest, samples / sample_rate if sample_rate else 0,
        sample_rate, channels, "mp3", int(total_bitrate / frames) if frames else 0, flags,
    )


def _skip_id3v2(data: bytes, file_size: int | None = None) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return min(file_size if file_size is not None else len(data), 10 + size)


def copy_original(source: Path, raw_dir: Path, digest: str | None = None) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = digest or sha256_file(source)
    destination = raw_dir / f"{digest[:12]}_{source.name}"
    if not destination.exists():
        shutil.copy2(source, destination)
    return destination
