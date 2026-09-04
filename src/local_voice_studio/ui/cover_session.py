"""Small, dependency-light data layer for the cover/song screen.

The cover UI should not need to know about ffmpeg's command line or cache
layout.  This module deliberately keeps all audio work local and deterministic.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from array import array
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..paths import AppPaths


@dataclass(frozen=True)
class LyricLine:
    timestamp_seconds: float
    text: str


@dataclass
class AudioMetadata:
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    codec: str = "unknown"
    bit_rate: int = 0


@dataclass
class SongSession:
    audio_path: str
    lyrics: list[LyricLine] = field(default_factory=list)
    metadata: AudioMetadata = field(default_factory=AudioMetadata)
    sha256: str = ""
    peaks: list[tuple[int, int]] = field(default_factory=list)
    peak_count: int = 6000

    @classmethod
    def load(
        cls, audio_path: Path, *, lrc_path: Path | None = None,
        paths: AppPaths | None = None, cache_dir: Path | None = None,
        peak_count: int = 6000,
        cancel: Callable[[], bool] | None = None,
    ) -> "SongSession":
        audio_path = Path(audio_path).resolve()
        digest = sha256_file(audio_path)
        lyrics = parse_lrc(lrc_path.read_text(encoding="utf-8-sig") if lrc_path and lrc_path.is_file() else "")
        cache_file = Path(cache_dir) / f"{digest}.json" if cache_dir is not None else None
        if cache_file and cache_file.is_file():
            try:
                cached = load_session_cache(cache_file, expected_sha256=digest)
                if cached.peak_count != max(1, int(peak_count)):
                    raise ValueError("波形缓存规格已变化")
                cached.audio_path = str(audio_path)
                cached.lyrics = lyrics
                return cached
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
        metadata = probe_audio_metadata(audio_path, paths=paths, cancel=cancel)
        session = cls(str(audio_path), lyrics, metadata, digest, peak_count=max(1, int(peak_count)))
        session.peaks = decode_pcm_peaks(audio_path, metadata, peak_count=peak_count, paths=paths, cancel=cancel)
        if cache_dir is not None:
            save_session_cache(session, Path(cache_dir))
        return session

    def to_dict(self) -> dict:
        return {"version": 1, "audio_path": self.audio_path, "sha256": self.sha256, "peak_count": self.peak_count,
                "metadata": asdict(self.metadata),
                "lyrics": [asdict(item) for item in self.lyrics],
                "peaks": [list(item) for item in self.peaks]}


_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(content: str) -> list[LyricLine]:
    """Parse LRC lines, expanding all timestamps on multi-tag lines.

    Metadata tags and malformed timestamps are ignored. Empty text is valid
    input but does not create a lyric line.
    """
    if not content:
        return []
    result: list[LyricLine] = []
    for raw in content.splitlines():
        tags = list(_TIME_TAG.finditer(raw))
        if not tags:
            continue
        text = raw[tags[-1].end():].strip()
        if not text:
            continue
        for tag in tags:
            minutes, seconds, fraction = int(tag.group(1)), int(tag.group(2)), tag.group(3)
            if seconds >= 60:
                continue
            frac = float(f"0.{fraction}") if fraction else 0.0
            result.append(LyricLine(minutes * 60 + seconds + frac, text))
    return sorted(result, key=lambda line: line.timestamp_seconds)


def _format_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    centis = int(round(remainder * 100))
    if centis >= 6000:
        minutes += 1
        centis -= 6000
    return f"[{minutes:02d}:{centis // 100:02d}.{centis % 100:02d}]"


def serialize_lrc(lines: list[LyricLine]) -> str:
    """Render lyric lines back to a plain, sorted LRC document."""
    ordered = sorted(lines, key=lambda line: line.timestamp_seconds)
    if not ordered:
        return ""
    return "\n".join(f"{_format_ts(line.timestamp_seconds)}{line.text}" for line in ordered) + "\n"


def serialize_lrc(lines: Iterable[LyricLine]) -> str:
    """Render lyric lines back to a standard, timestamp-sorted LRC document."""
    ordered = sorted(lines, key=lambda line: line.timestamp_seconds)
    if not ordered:
        return ""
    parts = []
    for line in ordered:
        seconds = max(0.0, line.timestamp_seconds)
        minutes = int(seconds // 60)
        remainder = seconds - minutes * 60
        centiseconds = int(round(remainder * 100))
        if centiseconds >= 6000:
            minutes += 1
            centiseconds -= 6000
        parts.append(f"[{minutes:02d}:{centiseconds // 100:02d}.{centiseconds % 100:02d}]{line.text}")
    return "\n".join(parts) + "\n"


def write_lrc(path: Path, lines: Iterable[LyricLine]) -> None:
    """Atomically persist edited lyrics so a crash never leaves a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".lrc.tmp")
    temporary.write_text(serialize_lrc(lines), encoding="utf-8")
    temporary.replace(path)


def serialize_lrc(lines: Iterable[LyricLine]) -> str:
    """Render lyric lines back to a standard LRC document, timestamps sorted."""
    ordered = sorted(lines, key=lambda line: line.timestamp_seconds)
    if not ordered:
        return ""
    parts = []
    for line in ordered:
        seconds = max(0.0, line.timestamp_seconds)
        minutes = int(seconds // 60)
        remainder = seconds - minutes * 60
        centiseconds = int(round(remainder * 100))
        if centiseconds >= 6000:
            minutes += 1
            centiseconds -= 6000
        parts.append(f"[{minutes:02d}:{centiseconds // 100:02d}.{centiseconds % 100:02d}]{line.text}")
    return "\n".join(parts) + "\n"


def write_lrc(path: Path, lines: Iterable[LyricLine]) -> None:
    """Persist edited lyrics atomically so a crash never leaves a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".lrc.tmp")
    temporary.write_text(serialize_lrc(lines), encoding="utf-8")
    temporary.replace(path)


def serialize_lrc(lines: list[LyricLine]) -> str:
    """Render lyric lines back to a standard, timestamp-sorted LRC document."""
    ordered = sorted(lines, key=lambda line: line.timestamp_seconds)
    if not ordered:
        return ""
    parts = []
    for line in ordered:
        seconds = max(0.0, line.timestamp_seconds)
        minutes = int(seconds // 60)
        remainder = seconds - minutes * 60
        centiseconds = int(round(remainder * 100))
        if centiseconds >= 6000:
            minutes += 1
            centiseconds -= 6000
        parts.append(f"[{minutes:02d}:{centiseconds // 100:02d}.{centiseconds % 100:02d}]{line.text}")
    return "\n".join(parts) + "\n"


def write_lrc(path: Path, lines: list[LyricLine]) -> None:
    """Persist lyrics atomically so a crash never leaves a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".lrc.tmp")
    temporary.write_text(serialize_lrc(lines), encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool(name: str, paths: AppPaths | None = None) -> Path:
    root = paths or AppPaths.default()
    candidates = (root.data_root / "tools" / f"{name}.exe",
                  root.runtime_root / "env" / "Library" / "bin" / f"{name}.exe",
                  root.engine_root / f"{name}.exe",
                  root.engine_root / "tools" / f"{name}.exe")
    found = next((item for item in candidates if item.is_file()), None)
    if found is None:
        raise FileNotFoundError(f"找不到私有 {name}")
    return found


def _run_cancellable(command: list[str], *, timeout: float, cancel: Callable[[], bool] | None = None, text: bool = False):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, encoding="utf-8" if text else None, errors="replace" if text else None)
    deadline = time.monotonic() + timeout
    while True:
        if cancel and cancel():
            process.terminate(); process.communicate()
            raise InterruptedError("音频读取已取消")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill(); process.communicate()
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(.1, remaining))
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            return stdout
        except subprocess.TimeoutExpired:
            continue


def probe_audio_metadata(audio_path: Path, *, paths: AppPaths | None = None, cancel: Callable[[], bool] | None = None) -> AudioMetadata:
    ffprobe = _tool("ffprobe", paths)
    command = [str(ffprobe), "-v", "error", "-show_entries",
               "format=duration,bit_rate:stream=codec_name,sample_rate,channels",
               "-select_streams", "a:0", "-of", "json", str(audio_path)]
    value = json.loads(_run_cancellable(command, timeout=30, cancel=cancel, text=True))
    stream = (value.get("streams") or [{}])[0]
    fmt = value.get("format") or {}
    return AudioMetadata(float(fmt.get("duration") or 0), int(stream.get("sample_rate") or 0),
                         int(stream.get("channels") or 0), str(stream.get("codec_name") or "unknown"),
                         int(fmt.get("bit_rate") or 0))


def decode_pcm_peaks(audio_path: Path, metadata: AudioMetadata | None = None, *,
                     start_seconds: float = 0.0, end_seconds: float | None = None,
                     peak_count: int = 6000, paths: AppPaths | None = None,
                     cancel: Callable[[], bool] | None = None) -> list[tuple[int, int]]:
    """Decode real mono s16 PCM and return (min,max) values per bucket."""
    metadata = metadata or probe_audio_metadata(audio_path, paths=paths)
    count = max(1, int(peak_count))
    duration = max(0.0, float(metadata.duration_seconds))
    start = min(duration, max(0.0, float(start_seconds)))
    end = duration if end_seconds is None else min(duration, max(start, float(end_seconds)))
    if duration <= 0 or end <= start or metadata.sample_rate <= 0:
        return []
    ffmpeg = _tool("ffmpeg", paths)
    command = [str(ffmpeg), "-v", "error", "-ss", f"{start:.6f}", "-i", str(audio_path)]
    command += ["-t", f"{end - start:.6f}", "-f", "s16le", "-ac", "1", "-ar", str(metadata.sample_rate), "pipe:1"]
    expected_samples = max(1, round((end - start) * metadata.sample_rate))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    try:
        result = peaks_from_pcm_chunks(
            iter(lambda: process.stdout.read(64 * 1024), b""),
            total_samples=expected_samples,
            peak_count=count,
            cancel=cancel,
        )
        _, stderr = process.communicate(timeout=10)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr)
        return result
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        raise


def peaks_from_pcm_chunks(chunks: Iterable[bytes], *, total_samples: int, peak_count: int = 6000,
                          cancel: Callable[[], bool] | None = None) -> list[tuple[int, int]]:
    """Aggregate little-endian signed 16-bit PCM without retaining the decoded song."""
    total = max(1, int(total_samples)); count = max(1, min(int(peak_count), total))
    lows = [32767] * count; highs = [-32768] * count
    sample_index = 0; carry = b""
    for chunk in chunks:
        if cancel and cancel(): raise InterruptedError("音频读取已取消")
        data = carry + bytes(chunk); usable = len(data) - (len(data) % 2); carry = data[usable:]
        values = array("h"); values.frombytes(data[:usable])
        if sys.byteorder != "little": values.byteswap()
        for value in values:
            bucket = min(count - 1, sample_index * count // total)
            if value < lows[bucket]: lows[bucket] = value
            if value > highs[bucket]: highs[bucket] = value
            sample_index += 1
    if sample_index == 0: return []
    return [(low, high) for low, high in zip(lows, highs) if low <= high]


def save_session_cache(session: SongSession, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{session.sha256}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(session.to_dict(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return path


def load_session_cache(cache_file: Path, *, expected_sha256: str | None = None) -> SongSession:
    value = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    if value.get("version") != 1:
        raise ValueError("不支持的波形缓存版本")
    if expected_sha256 and value.get("sha256") != expected_sha256:
        raise ValueError("音频缓存哈希不匹配")
    metadata = AudioMetadata(**value.get("metadata", {}))
    lyrics = [LyricLine(float(item["timestamp_seconds"]), str(item["text"])) for item in value.get("lyrics", [])]
    peaks = [tuple(int(v) for v in item[:2]) for item in value.get("peaks", [])]
    return SongSession(str(value["audio_path"]), lyrics, metadata, str(value["sha256"]), peaks, int(value.get("peak_count", 6000)))
