"""Read-only snapshots of real local state for the HTML shell.

Every field exposed here is derived from the project database, the project
directory, or the installed runtime.  Nothing is invented: when a value is
unknown the snapshot says so explicitly instead of showing demo numbers.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...cover.project import CoverProject
from ...models import JobStatus
from ...paths import AppPaths
from ...product_models import voice_capabilities
from ...runtime import EngineRuntimeError, EngineRuntimeResolver
from ...storage import StudioStore

APP_TITLE = "VoiceStudio"
APP_SUBTITLE = "LOCAL AI AUDIO"

_LRC_RE = re.compile(r"^\[(\d{1,3}):(\d{1,2}(?:\.\d+)?)\](.*)$")

_JOB_STATUS_TEXT = {
    JobStatus.QUEUED: ("排队中", ""),
    JobStatus.RUNNING: ("运行中", "orange"),
    JobStatus.INTERRUPTED: ("已中断", "orange"),
    JobStatus.COMPLETED: ("完成", "ready"),
    JobStatus.FAILED: ("失败", "red"),
    JobStatus.CANCELLED: ("已取消", ""),
}

_PRODUCT_STATUS_TEXT = {
    "queued": ("排队中", ""),
    "running": ("运行中", "orange"),
    "succeeded": ("完成", "ready"),
    "preparing": ("准备中", "orange"),
    "cancelling": ("取消中", "orange"),
    "waiting_dependency": ("等待前置步骤", "orange"),
    "completed": ("完成", "ready"),
    "published": ("完成", "ready"),
    "failed": ("失败", "red"),
    "cancelled": ("已取消", ""),
    "interrupted": ("已中断", "orange"),
    "recoverable": ("可恢复", "orange"),
}

_KIND_TEXT = {
    "synthesize": "文字生成",
    "prepare_dataset": "数据准备",
    "train": "模型训练",
    "download": "组件下载",
}

_PRODUCT_KIND_TEXT = {
    "ai_cover": "一键翻唱",
    "separate": "音频分离",
    "render_cover": "AI 翻唱",
    "export_cover": "翻唱导出",
    "train_singing": "歌唱模型训练",
    "convert_singing": "歌唱转换",
}

_STEM_LABELS = (
    ("vocal", "主唱人声"),
    ("instrumental", "伴奏"),
    ("ai_vocal", "AI 人声"),
    ("final_mix", "最终混音"),
)


def duration_text(milliseconds: Any) -> str:
    total = max(0, int(float(milliseconds or 0) / 1000))
    return f"{total // 60:02d}:{total % 60:02d}"


def size_text(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def clock_text(value: str) -> str:
    """Render an ISO timestamp the way the prototype does (today/yesterday)."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    now = datetime.now().astimezone()
    stamp = local.strftime("%H:%M")
    if local.date() == now.date():
        return stamp
    if (now.date() - local.date()).days == 1:
        return f"昨天 {stamp}"
    return local.strftime("%Y-%m-%d")


def date_text(value: str) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d")


class StudioSnapshot:
    """Collects the real state the HTML shell renders."""

    def __init__(self, paths: AppPaths, store: StudioStore, project: Path):
        self.paths = paths
        self.store = store
        self.project = Path(project)

    # ------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "app": self.app(),
            "project": self.project_state(),
            "projects": self.projects(),
            "songs": self.songs(),
            "voices": self.voices(),
            "tasks": self.tasks(),
            "exports": self.exports(),
            "generations": self.generations(),
            "engine": self.engine(),
            "storage": self.storage(),
            "settings": self.settings(),
        }

    def app(self) -> dict[str, Any]:
        return {"title": APP_TITLE, "subtitle": APP_SUBTITLE, "bridge": True}

    def project_state(self) -> dict[str, Any]:
        return {"name": self.project.name, "path": str(self.project)}

    def projects(self) -> list[dict[str, Any]]:
        rows = []
        for item in self.store.list_projects():
            path = Path(str(item.get("path", "")))
            rows.append({
                "id": str(item.get("id", "")),
                "name": str(item.get("name") or path.name),
                "path": str(path),
                "updated_at": str(item.get("updated_at", "")),
                "updated_text": clock_text(str(item.get("updated_at", ""))),
                "active": path.resolve() == self.project.resolve(),
            })
        return rows

    # ------------------------------------------------------------------
    @staticmethod
    def _lyrics(cover: CoverProject, limit: int = 200) -> list[dict[str, Any]]:
        """Parse the project's real LRC file, never inventing lyric lines."""
        if not cover.lyrics_path:
            return []
        path = (cover.root / cover.lyrics_path).resolve()
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines: list[dict[str, Any]] = []
        for raw in text.splitlines():
            match = _LRC_RE.match(raw.strip())
            if not match:
                continue
            minutes, seconds, content = match.group(1), match.group(2), match.group(3).strip()
            if not content:
                continue
            total = int(minutes) * 60 + float(seconds)
            lines.append({"seconds": round(total, 2), "time": duration_text(total * 1000), "text": content})
            if len(lines) >= limit:
                break
        return lines

    # ------------------------------------------------------------------
    def songs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            covers = CoverProject.list(self.project)
        except (OSError, ValueError):
            covers = []
        for cover in covers:
            assets = {}
            for asset in cover.assets:
                assets[str(asset.role)] = asset
            active_mix = cover.get_asset(role='final_mix')
            if active_mix:
                assets['final_mix'] = active_mix
            stems = []
            for role, label in _STEM_LABELS:
                asset = assets.get(role)
                if asset is None:
                    continue
                path = (cover.root / asset.relative_path).resolve()
                stems.append({
                    "role": role,
                    "label": label,
                    "path": str(path),
                    "name": path.name,
                    "exists": path.is_file(),
                    "size_text": size_text(path.stat().st_size) if path.is_file() else "文件缺失",
                    "sha256": str(asset.sha256),
                })
            if str(cover.mix_status) == "completed" or "final_mix" in assets:
                status, status_text = "done", "已生成"
            elif str(cover.separation_status) == "completed" or {"vocal", "instrumental"} <= set(assets):
                status, status_text = "ready", "已分离"
            else:
                status, status_text = "todo", "未分离"
            source = (cover.root / cover.source_relative_path) if cover.source_relative_path else None
            result.append({
                "id": cover.id,
                "title": cover.title or "未命名翻唱",
                "duration_ms": int(cover.duration_ms or 0),
                "duration_text": duration_text(cover.duration_ms) if cover.duration_ms else "—",
                "format": (source.suffix.lstrip(".").upper() if source and source.suffix else "WAV"),
                "status": status,
                "status_text": status_text,
                "rights_confirmed": bool(cover.rights_confirmed),
                "separation_status": str(cover.separation_status),
                "ai_vocal_status": str(cover.ai_vocal_status),
                "mix_status": str(cover.mix_status),
                "stems": stems,
                "lyrics": self._lyrics(cover),
                "has_lyrics": bool(cover.lyrics_path),
                "created_at": str(cover.created_at),
                "updated_at": str(cover.updated_at),
                "updated_text": clock_text(str(cover.updated_at)),
                "source_name": cover.original_source_name or (source.name if source else ""),
                "source_path": str(source) if source else "",
                "source_exists": bool(source and source.is_file()),
                "root": str(cover.root),
            })
        result.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return result

    # ------------------------------------------------------------------
    def voices(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            profiles = self.store.list_profiles(self.project)
        except (OSError, ValueError):
            profiles = []
        for profile in profiles:
            capabilities = voice_capabilities(profile)
            try:
                assets = self.store.list_source_assets(self.project, profile.id)
            except (OSError, ValueError):
                assets = []
            seconds = sum(float(getattr(asset, "duration_seconds", 0) or 0) for asset in assets)
            asset_rows = [self._asset_row(asset, profile.name) for asset in assets]
            models = []
            for version in list(getattr(profile, "model_versions", []) or []):
                models.append({
                    "kind": "tts",
                    "name": str(getattr(version, "name", "训练版本")),
                    "meta": f"GPT-SoVITS · {date_text(str(getattr(version, 'created_at', '')))}",
                    "status": str(getattr(version, "status", "available")),
                    "active": str(getattr(version, "id", "")) == str(getattr(profile, "active_model_version_id", "")),
                })
            for version in list(getattr(profile, "singing_models", []) or []):
                models.append({
                    "kind": "singing",
                    "name": str(getattr(version, "name", "歌唱模型")),
                    "meta": f"RVC · {date_text(str(getattr(version, 'created_at', '')))}",
                    "status": str(getattr(version, "trust_status", "unverified")),
                    "active": str(getattr(version, "id", "")) == str(getattr(profile, "active_singing_model_id", "")),
                })
            result.append({
                "id": profile.id,
                "name": profile.name,
                "subtitle": f"{'已授权' if profile.consent_confirmed else '未确认授权'} · {len(assets)} 段素材",
                "consent": bool(profile.consent_confirmed),
                "archived": bool(getattr(profile, "archived", False)),
                "asset_count": len(assets),
                "seconds": round(seconds, 1),
                "duration_text": duration_text(seconds * 1000),
                "tts_ready": capabilities.tts == "ready",
                "cover_ready": capabilities.singing_conversion == "ready",
                "tts_reason": capabilities.reasons.get("tts", ""),
                "cover_reason": capabilities.reasons.get("singing_conversion", ""),
                "training_state": str(getattr(profile, "training_state", "")),
                "models": models,
                "assets": asset_rows,
                "created_text": date_text(str(getattr(profile, "created_at", ""))),
            })
        return result

    @staticmethod
    def _asset_row(asset: Any, profile_name: str) -> dict[str, Any]:
        duration = float(getattr(asset, "duration_seconds", 0) or 0)
        confirmed = float(getattr(asset, "confirmed_seconds", 0) or 0)
        ratio = 0.0
        if duration > 0:
            ratio = max(0.0, min(1.0, confirmed / duration if confirmed else 0.0))
        path = Path(str(getattr(asset, "project_path", "") or getattr(asset, "original_path", "")))
        flags = [str(item) for item in list(getattr(asset, "quality_flags", []) or [])]
        if not confirmed:
            state, style = "待确认", "orange"
        elif flags:
            state, style = "需处理", "red"
        else:
            state, style = "已确认", "ready"
        return {
            "id": str(getattr(asset, "id", "")),
            "name": path.name or "未命名素材",
            "profile": profile_name,
            "path": str(path),
            "exists": path.is_file(),
            "duration_text": duration_text(duration * 1000) if duration else "—",
            "seconds": round(duration, 1),
            "segments": int(getattr(asset, "segment_count", 0) or 0),
            "quality": round(ratio * 100),
            "state_text": state,
            "state_class": style,
            "flags": flags,
        }

    # ------------------------------------------------------------------
    def tasks(self, limit: int = 60) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for job in self.store.list_jobs(limit):
                text, style = _JOB_STATUS_TEXT.get(job.status, (str(job.status), ""))
                rows.append({
                    "id": job.id,
                    "title": self._job_title(job),
                    "kind": job.kind.value,
                    "kind_text": _KIND_TEXT.get(job.kind.value, job.kind.value),
                    "status": job.status.value,
                    "status_text": text,
                    "status_class": style,
                    "progress": max(0.0, min(100.0, float(job.progress or 0))),
                    "message": str(job.message or ""),
                    "error": str(job.error or ""),
                    "created_at": str(job.created_at),
                    "updated_at": str(job.updated_at),
                    "updated_text": clock_text(str(job.updated_at)),
                    "outputs": [str(item) for item in list(job.outputs or [])],
                    "cancellable": job.status in {JobStatus.QUEUED, JobStatus.RUNNING},
                    "removable": job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED},
                })
        except (OSError, ValueError):
            pass
        try:
            for job in self.store.list_product_jobs(limit):
                raw_status = str(job.status.value if hasattr(job.status, "value") else job.status)
                text, style = _PRODUCT_STATUS_TEXT.get(raw_status, (raw_status, ""))
                try:
                    elapsed = max(0, int((datetime.now(timezone.utc)-datetime.fromisoformat(job.created_at)).total_seconds()))
                except ValueError:
                    elapsed = 0
                workflow_id = str(job.payload.get('workflow_id', ''))
                workflow_resumable = (
                    bool(workflow_id)
                    and raw_status in {'interrupted', 'failed', 'cancelled', 'recoverable'}
                    and Path(str(job.payload.get('project_path', ''))).resolve() == self.project.resolve()
                )
                cover_resumable = (
                    job.kind == 'ai_cover'
                    and raw_status in {'recoverable', 'failed', 'cancelled'}
                )
                rows.append({
                    "id": job.id,
                    "title": self._product_title(job),
                    "kind": f"product:{job.kind}",
                    "kind_text": _PRODUCT_KIND_TEXT.get(str(job.kind), str(job.kind)),
                    "status": raw_status,
                    "status_text": text,
                    "status_class": style,
                    "progress": max(0.0, min(100.0, float(job.progress or 0))),
                    "message": str(job.current_stage or ""),
                    "error": str(job.error or ""),
                    "created_at": str(job.created_at),
                    "updated_at": str(getattr(job, "updated_at", job.created_at)),
                    "updated_text": clock_text(str(getattr(job, "updated_at", job.created_at))),
                    "outputs": [str(item) for item in list(job.outputs or [])],
                    "workflow_id": workflow_id,
                    "elapsed_seconds": elapsed,
                    "stages": [{"name":s.name,"status":s.status.value,"progress":round(s.progress*100),"error":s.error} for s in job.stages],
                    "log": list(job.log),
                    "resumable": workflow_resumable or cover_resumable,
                    "waiting_reason": str(job.error or ''),
                    "cancellable": raw_status in {"queued", "running"},
                    "removable": raw_status in {"succeeded", "published", "failed", "cancelled", "interrupted"},
                })
        except (OSError, ValueError):
            pass
        rows.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return rows[:limit]

    @staticmethod
    def _job_title(job: Any) -> str:
        payload = dict(getattr(job, "payload", {}) or {})
        for key in ("text", "title", "name", "path"):
            value = str(payload.get(key, "")).strip()
            if value:
                return value if len(value) <= 42 else value[:41] + "…"
        return _KIND_TEXT.get(job.kind.value, job.kind.value)

    @staticmethod
    def _product_title(job: Any) -> str:
        payload = dict(getattr(job, "payload", {}) or {})
        for key in ("title", "name", "path"):
            value = str(payload.get(key, "")).strip()
            if value:
                return Path(value).name if key == "path" else value
        return _PRODUCT_KIND_TEXT.get(str(job.kind), str(job.kind))

    # ------------------------------------------------------------------
    def exports(self, limit: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            covers = CoverProject.list(self.project)
        except (OSError, ValueError):
            covers = []
        for cover in covers:
            folder = cover.root / "exports"
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.name.endswith(".voicestudio.json"):
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                stat = path.stat()
                rows.append({
                    "name": path.name,
                    "path": str(path),
                    "kind": "cover",
                    "kind_text": "AI 翻唱",
                    "format": path.suffix.lstrip(".").upper() or "FILE",
                    "size_text": size_text(stat.st_size),
                    "time_text": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
                    "exists": True,
                })
        try:
            records = self.store.list_generation_records(self.project, limit)
        except (OSError, ValueError):
            records = []
        for record in records:
            path = Path(str(record.wav_path)) if record.wav_path else None
            if path is None or not path.is_file():
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            stat = path.stat()
            rows.append({
                "name": path.name,
                "path": str(path),
                "kind": "tts",
                "kind_text": "文字生成",
                "format": path.suffix.lstrip(".").upper() or "FILE",
                "size_text": size_text(stat.st_size),
                "time_text": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
                "exists": True,
            })
        rows.sort(key=lambda item: str(item.get("time_text", "")), reverse=True)
        return rows[:limit]

    def generations(self, limit: int = 40) -> list[dict[str, Any]]:
        try:
            records = self.store.list_generation_records(self.project, limit)
        except (OSError, ValueError):
            return []
        result = []
        for record in records:
            path = Path(str(record.wav_path)) if record.wav_path else None
            result.append({
                "id": record.id,
                "text": str(record.text or "")[:60],
                "voice_id": str(record.voice_profile_id or ""),
                "status": str(record.status or ""),
                "duration_text": duration_text(float(record.duration_seconds or 0) * 1000) if record.duration_seconds else "—",
                "path": str(path) if path else "",
                "exists": bool(path and path.is_file()),
                "created_text": clock_text(str(record.created_at)),
            })
        return result

    # ------------------------------------------------------------------
    def engine(self, deep: bool = False) -> dict[str, Any]:
        """Engine status.  ``deep`` verifies every manifest hash (slow: GBs).

        The shell must paint immediately, so ``bootstrap`` uses the cheap check
        and the page can ask for the full verification explicitly.
        """
        resolver = EngineRuntimeResolver(self.paths)
        try:
            python = resolver.resolve_private_python()
            python_text = str(python)
            ready = True
            message = "本地引擎已就绪"
        except EngineRuntimeError as exc:
            python_text, ready, message = "", False, str(exc)
        manifest_path = self.paths.runtime_root / "install-manifest.json"
        if deep:
            integrity = resolver.verify_install_manifest()
            manifest_valid, manifest_errors = bool(integrity.valid), list(integrity.errors)[:6]
            manifest_checked = True
        else:
            manifest_valid, manifest_errors, manifest_checked = manifest_path.is_file(), [], False
        ffmpeg = resolver.resolve_private_tool("ffmpeg")
        return {
            "ready": ready,
            "message": message,
            "python": python_text,
            "manifest_present": manifest_path.is_file(),
            "manifest_valid": manifest_valid,
            "manifest_errors": manifest_errors,
            "manifest_checked": manifest_checked,
            "ffmpeg": str(ffmpeg) if ffmpeg else "",
            "label": "本地引擎就绪" if ready else "本地引擎未安装",
        }

    def storage(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.paths.data_root if self.paths.data_root.exists() else Path.home())
        used = usage.total - usage.free
        return {
            "data_root": str(self.paths.data_root),
            "projects_root": str(self.paths.projects_root),
            "models_root": str(self.paths.models_root),
            "cache_root": str(self.paths.cache_root),
            "runtime_root": str(self.paths.runtime_root),
            "disk_total_gb": round(usage.total / 1024 ** 3, 1),
            "disk_free_gb": round(usage.free / 1024 ** 3, 1),
            "disk_used_gb": round(used / 1024 ** 3, 1),
            "disk_percent": int(round(used / usage.total * 100)) if usage.total else 0,
        }

    def settings(self) -> dict[str, Any]:
        overrides = self.store.get_setting("paths.overrides", {}) or {}
        return {
            "theme": str(self.store.get_setting("ui.theme", "light")),
            "density": str(self.store.get_setting("ui.density", "standard")),
            "language": str(self.store.get_setting("ui.language", "zh-CN")),
            "autoplay": bool(self.store.get_setting("generation.autoplay", True)),
            "smart_optimization": bool(self.store.get_setting("smart_optimization", True)),
            "restore_workspace": bool(self.store.get_setting("ui.restore_workspace", True)),
            "task_notifications": bool(self.store.get_setting("ui.task_notifications", True)),
            "output_dir": str(self.store.get_setting("default_output_dir", str(self.project / "exports"))),
            "paths": {
                "projects": str(overrides.get("projects") or self.paths.projects_root),
                "models": str(overrides.get("models") or self.paths.models_root),
                "cache": str(overrides.get("cache") or self.paths.cache_root),
            },
            "last_saved_at": str(self.store.get_setting("ui.last_saved_at", "")),
        }
