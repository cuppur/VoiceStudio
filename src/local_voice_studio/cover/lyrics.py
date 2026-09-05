"""Optional SenseVoice-based lyric transcription for cover projects.

This module owns the domain rules for auto-recognized lyrics: it requires a
confirmed-rights cover with a separated vocal asset, runs the pinned engine
ASR inside the private runtime, and persists the result as a project-owned LRC
file with ``lyrics_origin = "auto"``.  Auto lyrics are a transcription aid, not
official lyrics, and the UI must keep showing that distinction.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..audio import sha256_file
from ..paths import AppPaths, ensure_within
from ..infrastructure.cache import build_cache_key
from ..product_models import CacheArtifact, SongProject
from .project import CoverProject

LYRICS_ORIGIN_AUTO = "auto"
LYRICS_ORIGIN_MANUAL = "manual"


@dataclass(frozen=True)
class LyricSegment:
    """One timestamped line produced by the transcription engine."""

    start_seconds: float
    end_seconds: float
    text: str


def serialize_lrc_lines(lines: list[LyricSegment]) -> str:
    """Render timestamped segments as a standard, sorted LRC document."""
    ordered = sorted(lines, key=lambda line: line.start_seconds)
    if not ordered:
        return ""
    parts = []
    for line in ordered:
        seconds = max(0.0, line.start_seconds)
        minutes = int(seconds // 60)
        remainder = seconds - minutes * 60
        centiseconds = int(round(remainder * 100))
        if centiseconds >= 6000:
            minutes += 1
            centiseconds -= 6000
        parts.append(f"[{minutes:02d}:{centiseconds // 100:02d}.{centiseconds % 100:02d}]{line.text}")
    return "\n".join(parts) + "\n"


class CoverLyricsService:
    """Transcribe the separated vocal into a project-owned auto LRC file."""

    def __init__(self, project_path: Path, *, paths: AppPaths | None = None,
                 python: Path | None = None):
        self.paths = paths or AppPaths.default()
        self.project_path = ensure_within(self.paths.projects_root, Path(project_path))
        self.python = python
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        """Kill the running transcription child process (cancel contract)."""
        process = self._process
        if process is not None and process.poll() is None:
            self._kill(process)

    def _engine_python(self) -> Path:
        if self.python is not None:
            return self.python
        python = self.paths.private_python
        if not python.is_file():
            raise RuntimeError("本地引擎尚未安装完整，无法自动识别歌词")
        return python

    def transcribe(self, cover_id: str, *, language: str = "zh",
                   cancel: Any = None,
                   progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
        cover = CoverProject.load(self.project_path, cover_id)
        if not cover.rights_confirmed or not cover.rights_confirmed_at:
            raise ValueError("自动识别歌词前必须确认歌曲处理权利")
        vocal = cover.get_asset(role="vocal")
        if vocal is None or vocal.content_origin != "separated":
            raise ValueError("自动识别歌词需要已分离的人声轨")
        vocal_path = ensure_within(cover.root, cover.root / vocal.relative_path)
        if not vocal_path.is_file() or sha256_file(vocal_path) != vocal.sha256:
            raise ValueError("已分离人声资产缺失或 Hash 不匹配")
        lyrics_key = build_cache_key(source_sha256=vocal.sha256, operation="lyrics", engine_version="sensevoice-v1", parameters={"language": language}, output_format="lrc")
        cached = _product_cache(self.project_path, lyrics_key)
        if cached and cover.lyrics_path:
            candidate = ensure_within(cover.root, cover.root / cover.lyrics_path)
            if candidate.is_file():
                return {"lyrics_path": str(candidate), "line_count": len(candidate.read_text(encoding="utf-8", errors="replace").splitlines()), "origin": cover.lyrics_origin, "language": language, "cache_hit": True}
        if progress:
            progress(0.1, "正在准备语音识别模型")
        python = self._engine_python()
        output_root = ensure_within(cover.root, cover.root / "lyrics")
        output_root.mkdir(parents=True, exist_ok=True)
        segments_json = output_root / f".{cover.id}.segments.jsonl"
        command = [str(python), "-X", "utf8", "-u", "-m", "local_voice_studio.lyrics_cli",
                   "--input", str(vocal_path), "--output", str(segments_json),
                   "--language", str(language), "--model-dir", str(self.paths.sensevoice_model_root),
                   "--vad-dir", str(self.paths.fsmn_vad_model_root)]
        env = self._engine_env()
        process = subprocess.Popen(command, cwd=str(self.paths.engine_root), env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        self._process = process
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.rstrip()
                if progress and stripped:
                    progress(0.4, stripped[-120:])
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    self.cancel()
                    process.wait()
                    raise InterruptedError("自动识别歌词已取消")
            if process.wait() != 0:
                raise RuntimeError("语音识别进程失败，请确认引擎已安装并可用")
            segments = _load_segments(segments_json)
            if not segments:
                raise RuntimeError("语音识别没有识别到歌词文本")
            if progress:
                progress(0.9, "正在保存歌词")
            lrc_path = output_root / "lyrics.lrc"
            _write_lrc_atomic(lrc_path, segments)
            cover.set_lyrics(lrc_path, origin=LYRICS_ORIGIN_AUTO)
            _register_product_cache(self.project_path, lyrics_key, vocal.id, vocal.sha256)
            if progress:
                progress(1.0, "歌词识别完成")
            return {"lyrics_path": str(lrc_path), "line_count": len(segments),
                    "origin": LYRICS_ORIGIN_AUTO, "language": language}
        except InterruptedError:
            segments_json.unlink(missing_ok=True)
            raise
        except Exception:
            segments_json.unlink(missing_ok=True)
            raise
        finally:
            try:
                if process.poll() is None:
                    self._kill(process)
                    process.wait()
            finally:
                if self._process is process:
                    self._process = None

    def _engine_env(self) -> dict[str, str]:
        env = {**os.environ}
        private_env = self.paths.runtime_root / "env"
        path_entries = [
            self.paths.data_root / "tools",
            private_env,
            private_env / "Scripts",
            private_env / "Library" / "bin",
        ]
        env["PATH"] = os.pathsep.join([str(item) for item in path_entries if item.exists()])
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("HF_HOME", str(self.paths.models_root / "huggingface"))
        env.setdefault("MODELSCOPE_CACHE", str(self.paths.models_root / "modelscope"))
        env.setdefault("TORCH_HOME", str(self.paths.models_root / "torch"))
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([
            str(self.paths.engine_root),
            str(self.paths.engine_root / "GPT_SoVITS"),
            *([existing] if existing else []),
        ])
        return env

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            process.terminate()


def _load_segments(path: Path) -> list[LyricSegment]:
    if not path.is_file():
        return []
    result: list[LyricSegment] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            start = float(item["start"])
            end = float(item.get("end", start))
            text = str(item.get("text", "")).strip()
        except (ValueError, TypeError, KeyError):
            continue
        if start < 0 or end < start or not text:
            continue
        result.append(LyricSegment(start, end, text))
    return result


def _write_lrc_atomic(path: Path, segments: list[LyricSegment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".lrc.tmp")
    temporary.write_text(serialize_lrc_lines(segments), encoding="utf-8")
    temporary.replace(path)


__all__ = ["CoverLyricsService", "LyricSegment", "serialize_lrc_lines", "LYRICS_ORIGIN_AUTO"]

def _product_cache(project_path: Path, cache_key: str) -> CacheArtifact | None:
    manifest_path = Path(project_path) / "project.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        product = SongProject.from_dict(payload.get("product_project", {}))
        return next((item for item in product.cache_artifacts if item.cache_key == cache_key and item.status == "ready"), None)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _register_product_cache(project_path: Path, cache_key: str, asset_id: str, source_sha256: str) -> None:
    manifest_path = Path(project_path) / "project.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        product = SongProject.from_dict(payload.get("product_project", {}))
        product.register_cache(CacheArtifact(operation="lyrics", cache_key=cache_key, asset_id=asset_id, source_sha256=source_sha256, engine_version="sensevoice-v1", model_version="sensevoice", parameters_hash=cache_key))
        payload["product_project"] = product.to_dict()
        temporary = manifest_path.with_name(manifest_path.name + ".staging")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, manifest_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
