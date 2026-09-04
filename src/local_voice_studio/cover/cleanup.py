"""Optional, provenance-preserving cleanup for separated vocal stems."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..audio import sha256_file
from ..paths import AppPaths, ensure_within, validate_id
from ..runtime import EngineRuntimeResolver
from .project import CoverAsset, CoverProject


@dataclass(frozen=True)
class VocalCleanupSettings:
    """A deliberately small cleanup surface for the v1 vocal input chain.

    ``dereverb`` follows the v1 product spec: Off / Light / Strong.  All
    levels are optional and default to Off; any cleaned stem keeps
    ``content_origin = separated`` so it can never be mistaken for an
    AI-generated final product.
    """

    denoise: bool = False
    dereverb: str = "off"
    highpass_hz: int = 80
    version: str = "vocal-cleanup-v2"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "VocalCleanupSettings":
        raw = payload.get("cleanup_settings", payload)
        values = dict(raw) if isinstance(raw, dict) else {}
        mode = str(values.get("mode", "denoise" if values.get("denoise") else "off")).strip().lower()
        if mode not in {"off", "denoise"}:
            raise ValueError("当前仅支持关闭或降噪人声清理")
        dereverb = str(values.get("dereverb", "off")).strip().lower()
        if dereverb not in {"off", "light", "strong"}:
            raise ValueError("去混响仅支持关闭、轻度或强力")
        highpass = int(values.get("highpass_hz", cls.highpass_hz))
        if not 20 <= highpass <= 250:
            raise ValueError("人声高通频率必须在 20 到 250 Hz 之间")
        return cls(denoise=mode == "denoise", dereverb=dereverb, highpass_hz=highpass)

    def canonical(self) -> dict[str, object]:
        return {"denoise": self.denoise, "dereverb": self.dereverb,
                "highpass_hz": self.highpass_hz, "version": self.version}


class VocalCleanupBackend(Protocol):
    id: str
    version: str
    model_sha256: str

    def cleanup(self, source: Path, output: Path, settings: VocalCleanupSettings, cancel: Any = None) -> None:
        ...


class FFmpegVocalCleanupBackend:
    """Run only application-owned FFmpeg; no PATH or implicit model download."""

    id = "ffmpeg-afftdn"
    version = "ffmpeg-afftdn-v1"
    model_sha256 = ""

    def __init__(self, paths: AppPaths):
        executable = EngineRuntimeResolver(paths).resolve_private_tool("ffmpeg")
        if executable is None:
            raise RuntimeError("人声清理需要已安装的私有 FFmpeg")
        self.executable = executable
        self.process: subprocess.Popen | None = None

    def cancel(self) -> None:
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
            else:
                self.process.terminate()

    def cleanup(self, source: Path, output: Path, settings: VocalCleanupSettings, cancel: Any = None) -> None:
        if not settings.denoise and settings.dereverb == "off":
            raise ValueError("未启用任何人声清理")
        output.parent.mkdir(parents=True, exist_ok=True)
        filters = _cleanup_filters(settings)
        self.process = subprocess.Popen(
            [str(self.executable), "-hide_banner", "-nostdin", "-y", "-i", str(source), "-af", filters,
             "-c:a", "pcm_s16le", str(output)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            while self.process.poll() is None:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    self.cancel()
                    self.process.wait()
                    raise InterruptedError("人声清理已取消")
                if cancel is not None and hasattr(cancel, "wait"):
                    cancel.wait(0.1)
                else:
                    import time
                    time.sleep(0.1)
            if self.process.wait() != 0:
                error = (self.process.stderr.read() if self.process.stderr else b"").decode("utf-8", errors="replace")
                raise RuntimeError("人声清理失败: " + error[-300:])
        finally:
            self.process = None


class VocalCleanupService:
    def __init__(self, project_path: Path, *, paths: AppPaths, backend: VocalCleanupBackend | None = None):
        self.project_path = ensure_within(paths.projects_root, Path(project_path))
        self.paths = paths
        self.backend = backend or FFmpegVocalCleanupBackend(paths)

    def cancel(self) -> None:
        cancel = getattr(self.backend, "cancel", None)
        if callable(cancel):
            cancel()

    def cleanup(self, cover_id: str, settings: VocalCleanupSettings, *, output_id: str = "", cancel: Any = None) -> dict[str, object]:
        if not settings.denoise and settings.dereverb == "off":
            raise ValueError("未启用任何人声清理")
        cover = CoverProject.load(self.project_path, cover_id)
        source = next((asset for asset in reversed(cover.assets) if asset.role == "vocal" and asset.content_origin == "separated"
                       and asset.producer != self.backend.id), cover.get_asset(role="vocal"))
        if source is None or source.content_origin != "separated":
            raise ValueError("人声清理输入必须是已分离的人声轨")
        source_path = ensure_within(cover.root, cover.root / source.relative_path)
        if not source_path.is_file() or sha256_file(source_path) != source.sha256:
            raise ValueError("已分离人声资产缺失或 Hash 不匹配")
        material = json.dumps({"source": source.sha256, "engine": self.backend.id, "engine_version": self.backend.version,
                               "model_sha256": self.backend.model_sha256, "settings": settings.canonical()}, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(material.encode()).hexdigest()
        cached = next((asset for asset in reversed(cover.assets) if asset.role == "vocal" and asset.content_origin == "separated"
                       and asset.producer == self.backend.id and asset.producer_version == cache_key and asset.source_asset_ids == [source.id]), None)
        if cached:
            cached_path = ensure_within(cover.root, cover.root / cached.relative_path)
            if cached_path.is_file() and sha256_file(cached_path) == cached.sha256 and _valid_wav(cached_path):
                return {"asset_id": cached.id, "output_path": str(cached_path), "output_sha256": cached.sha256, "cache_hit": True,
                        "content_origin": "separated"}
        asset_id = validate_id(output_id or "vocal-cleanup-" + cache_key[:16], legacy=True, field="output_id")
        output = ensure_within(cover.root, cover.root / "stems" / "cleanup" / f"{asset_id}.wav")
        staging = ensure_within(cover.root, cover.root / "stems" / "cleanup" / f"{asset_id}.staging.wav")
        if output.exists():
            raise ValueError("清理输出资产已存在")
        try:
            self.backend.cleanup(source_path, staging, settings, cancel)
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise InterruptedError("人声清理已取消")
            if not _valid_wav(staging):
                raise RuntimeError("人声清理未生成有效 WAV")
            staging.replace(output)
            asset = CoverAsset(asset_id, "vocal", output.relative_to(cover.root).as_posix(), sha256_file(output), "separated",
                               self.backend.id, cache_key, model_sha256=self.backend.model_sha256, source_asset_ids=[source.id],
                               metadata={"cleanup": settings.canonical()})
            cover.add_asset(asset)
            return {"asset_id": asset.id, "output_path": str(output), "output_sha256": asset.sha256, "cache_hit": False,
                    "content_origin": "separated"}
        except Exception:
            staging.unlink(missing_ok=True)
            raise


def _cleanup_filters(settings: VocalCleanupSettings) -> str:
    """Build the ffmpeg filter chain for the requested cleanup surface.

    ``dereverb`` is implemented as an optional non-local-means denoiser tuned
    for the vocal band: Light keeps a conservative search window, Strong widens
    it and adds a gentle compressor to tame the reverb tail.  Nothing here
    fabricates AI processing -- the stem stays ``content_origin = separated``.
    """
    chain: list[str] = []
    if settings.dereverb == "light":
        chain.append("anlmdn=s=1:p=1:sc=0.05")
    elif settings.dereverb == "strong":
        chain.append("anlmdn=s=3:p=2:sc=0.08")
        chain.append("acompressor=threshold=0.5:ratio=2:attack=10:release=120")
    if settings.denoise:
        chain.append(f"afftdn=nf=-25")
    if chain:
        chain.append(f"highpass=f={settings.highpass_hz}")
    if not chain:
        raise ValueError("未启用任何人声清理")
    return ",".join(chain)


def _valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getframerate() > 0 and stream.getnchannels() > 0 and stream.getnframes() > 0
    except (OSError, EOFError, wave.Error):
        return False
