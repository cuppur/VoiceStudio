"""QWebChannel bridge exposed to the HTML shell as ``bridge``.

The page never touches the filesystem or the database directly: it calls
``invoke(action, payload)`` and receives a JSON reply.  Actions that are not
wired to a real local operation answer with an explicit "not connected yet"
message instead of pretending to work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QInputDialog, QWidget

from ...cover.project import CoverProject
from ...models import utc_now
from ...paths import AppPaths, ensure_within
from ...storage import StudioStore
from .data import StudioSnapshot
from .services.cover import CoverService
from .services.base import WebService

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")
NOT_CONNECTED = "该功能尚未接入新界面（可用 --ui-qt 打开经典界面）"


class _EngineVerifyTask(QRunnable):
    """Hash-verify the installed engine off the UI thread (it can take seconds)."""

    def __init__(self, snapshot: StudioSnapshot, done):
        super().__init__()
        self._snapshot = snapshot
        self._done = done

    def run(self) -> None:  # pragma: no cover - executed on a pool thread
        try:
            data = self._snapshot.engine(deep=True)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the pool
            data = {"ready": False, "message": f"校验失败：{exc}", "manifest_checked": True, "manifest_valid": False}
        self._done(data)


class StudioBridge(QObject):
    """Small, explicit surface between the HTML shell and the local core."""

    event = Signal(str, str)

    def __init__(self, paths: AppPaths, store: StudioStore, project: Path, client=None, parent: QObject | None = None):
        super().__init__(parent)
        self.paths = paths
        self.store = store
        self.project = Path(project)
        self.client = client
        self.snapshot = StudioSnapshot(paths, store, self.project)
        self._window: QWidget | None = parent if isinstance(parent, QWidget) else None
        self._closing = False
        self.cover = CoverService(paths, store, self.project, client, self)
        self.cover.event.connect(self.event)
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "app.refresh": self._refresh,
            "engine.verify": self._engine_verify,
            "project.activate": self._project_activate,
            "project.create": self._project_create,
            "project.open": self._project_open,
            "project.reveal": self._project_reveal,
            "song.import": self._song_import,
            "song.select": self._song_select,
            "file.reveal": self._file_reveal,
            "file.open": self._file_open,
            "settings.set": self._settings_set,
            "settings.save": self._settings_save,
            "settings.pick_directory": self._settings_pick_directory,
            "cover.state": self._cover_state,
            "cover.attest": self._cover_attest,
            "cover.separate": self._cover_separate,
            "cover.cleanup": self._cover_cleanup,
            "cover.transpose": self._cover_transpose,
            "cover.convert": self._cover_convert,
            "cover.render": self._cover_render,
            "cover.export": self._cover_export,
            "cover.lyrics": self._cover_lyrics,
            "cover.cancel": self._cover_cancel,
        }

    # ------------------------------------------------------------------
    def set_project(self, project: Path) -> None:
        self.project = Path(project)
        self.snapshot = StudioSnapshot(self.paths, self.store, self.project)
        if self.cover is not None:
            self.cover.set_project(self.project)

    def notify(self, name: str, payload: dict[str, Any] | None = None) -> None:
        self.event.emit(str(name), json.dumps(payload or {}, ensure_ascii=False))

    # ------------------------------------------------------------------
    @Slot(result=str)
    def bootstrap(self) -> str:
        try:
            return json.dumps({"ok": True, "data": self.snapshot.state()}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - the page must never see a crash
            return json.dumps({"ok": False, "message": f"无法读取本地状态：{exc}"}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def invoke(self, action: str, payload: str) -> str:
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        handler = self._handlers.get(str(action))
        if handler is None:
            return self._reply(False, f"{NOT_CONNECTED}：{action}")
        try:
            return self._reply(**handler(data))
        except PermissionError as exc:
            return self._reply(False, f"没有访问权限：{exc}")
        except (OSError, ValueError, KeyError) as exc:
            return self._reply(False, WebService.translate_error(exc))
        except Exception as exc:  # noqa: BLE001 - surface real errors to the UI
            return self._reply(False, f"{type(exc).__name__}: {exc}")

    @Slot(str)
    def log(self, message: str) -> None:
        if str(message).strip():
            print(f"[web] {message}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    @staticmethod
    def _reply(ok: bool, message: str = "", **extra: Any) -> str:
        payload = {"ok": bool(ok), "message": str(message)}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------------
    def _refresh(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": self.snapshot.state()}

    def _engine_verify(self, _data: dict[str, Any]) -> dict[str, Any]:
        """Full manifest verification (hashes every installed file) off-thread."""
        if self._closing:
            return {"ok": False, "message": "窗口正在关闭"}
        QThreadPool.globalInstance().start(_EngineVerifyTask(self.snapshot, self._engine_verified))
        return {"ok": True, "message": "正在校验引擎完整性，请稍候……"}

    def _engine_verified(self, data: dict[str, Any]) -> None:
        if self._closing:
            return
        self.notify("engine.verified", {"engine": data})

    def _owned_roots(self) -> tuple[Path, ...]:
        """Directories the page may open or reveal; never the whole data root."""
        return (
            self.paths.projects_root,
            self.paths.models_root,
            self.paths.cache_root,
            self.paths.runtime_root,
            self.paths.engine_root,
        )

    def _resolve_owned(self, raw: str, *, field: str = "路径") -> Path:
        """Resolve a page-supplied path and reject anything outside app roots.

        The page is local content, but a compromised page must not be able to
        make the desktop app open or write arbitrary files on disk.
        """
        if not raw:
            raise ValueError(f"缺少{field}")
        candidate = Path(raw).resolve()
        for root in self._owned_roots():
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            if candidate == resolved_root or resolved_root in candidate.parents:
                return candidate
        raise ValueError(f"{field}不在 VoiceStudio 管理的目录内：{candidate}")

    def _project_path(self, raw: str) -> Path:
        """Only project directories under the configured projects root qualify."""
        if not raw:
            raise ValueError("缺少工程路径")
        path = ensure_within(self.paths.projects_root, Path(raw))
        if not (path / "project.json").is_file():
            raise ValueError(f"不是 VoiceStudio 工程：{path}")
        return path

    def _project_activate(self, data: dict[str, Any]) -> dict[str, Any]:
        path = self._project_path(str(data.get("path", "")))
        self.set_project(path)
        self.notify("project.changed", {"path": str(path)})
        return {"ok": True, "data": self.snapshot.state()}

    def _project_create(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        if not name and self._window is not None:
            name, accepted = QInputDialog.getText(self._window, "新建项目", "项目名称", text="我的有声项目")
            if not accepted:
                return {"ok": False, "message": "已取消新建项目"}
        if not name:
            raise ValueError("项目名称不能为空")
        project = self.store.create_project(name)
        self.set_project(project)
        self.notify("project.changed", {"path": str(project)})
        return {"ok": True, "message": f"已创建工程：{name}", "data": self.snapshot.state()}

    def _project_open(self, _data: dict[str, Any]) -> dict[str, Any]:
        if self._window is None:
            raise ValueError("没有可用的窗口")
        chosen = QFileDialog.getExistingDirectory(self._window, "打开 VoiceStudio 项目", str(self.paths.projects_root))
        if not chosen:
            return {"ok": False, "message": "已取消打开项目"}
        path = self._project_path(chosen)
        self.set_project(path)
        self.notify("project.changed", {"path": str(path)})
        return {"ok": True, "data": self.snapshot.state()}

    def _project_reveal(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = str(data.get("path", "")) or str(self.project)
        path = ensure_within(self.paths.projects_root, Path(raw))
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return {"ok": True, "message": "已在文件管理器中打开工程目录"}

    # ------------------------------------------------------------------
    def _song_import(self, data: dict[str, Any]) -> dict[str, Any]:
        """Import an audio file.

        A page-supplied path must stay inside an app-managed root; only the
        native file dialog (an explicit user gesture) may reach anywhere else.
        """
        source: Path | None = None
        raw = str(data.get("path", "")).strip()
        if raw:
            source = self._resolve_owned(raw, field="音频路径")
        elif self._window is not None:
            chosen, _filter = QFileDialog.getOpenFileName(
                self._window, "导入歌曲到工程", "", "音频文件 (*.wav *.mp3 *.flac *.m4a *.aac *.ogg);;所有文件 (*)"
            )
            if chosen:
                source = Path(chosen)
        if source is None:
            return {"ok": False, "message": "已取消导入"}
        result = self._cover_service().import_song(source)
        return {"ok": True, "message": f"已导入：{source.name}", "data": self.snapshot.state(), **result}

    def _song_select(self, data: dict[str, Any]) -> dict[str, Any]:
        cover_id = str(data.get("id", ""))
        if not cover_id:
            raise ValueError("缺少歌曲 ID")
        for item in self.snapshot.songs():
            if item["id"] == cover_id:
                return {"ok": True, "data": item}
        raise ValueError("找不到该歌曲工程")

    # ------------------------------------------------------------------
    def _file_reveal(self, data: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_owned(str(data.get("path", "")), field="文件路径")
        if not path.exists():
            raise ValueError(f"文件不存在：{path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))
        return {"ok": True, "message": "已打开所在目录"}

    def _file_open(self, data: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_owned(str(data.get("path", "")), field="文件路径")
        if not path.is_file():
            raise ValueError(f"文件不存在：{path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return {"ok": True, "message": "已用默认程序打开"}

    # ------------------------------------------------------------------
    _SETTING_PREFIXES = ("ui.", "generation.")
    _SETTING_EXACT = frozenset({"smart_optimization", "default_output_dir"})

    def _setting_key_allowed(self, key: str) -> bool:
        """Only preferences may be written from the page.

        ``paths.overrides`` is deliberately excluded: it relocates the project,
        model and cache roots, so it must stay behind the native directory
        picker (``settings.pick_directory``) instead of page-supplied JSON.
        """
        if not key or not key.replace(".", "").replace("_", "").isalnum():
            return False
        # `paths.` anywhere is rejected, so `ui.paths.overrides` cannot be used
        # as a lazy alias for the root-redirect key.
        if "paths." in key:
            return False
        return key.startswith(self._SETTING_PREFIXES) or key in self._SETTING_EXACT

    def _settings_set(self, data: dict[str, Any]) -> dict[str, Any]:
        key = str(data.get("key", "")).strip()
        if not self._setting_key_allowed(key):
            raise ValueError(f"不允许从界面写入该设置：{key or '(空)'}")
        self.store.set_setting(key, data.get("value"))
        return {"ok": True, "message": "已保存到本机", "data": self.snapshot.settings()}

    def _settings_save(self, data: dict[str, Any]) -> dict[str, Any]:
        values = data.get("values")
        skipped: list[str] = []
        if isinstance(values, dict):
            for key, value in values.items():
                name = str(key)
                if self._setting_key_allowed(name):
                    self.store.set_setting(name, value)
                else:
                    skipped.append(name)
        self.store.set_setting("ui.last_saved_at", utc_now())
        message = "设置已保存到本机" if not skipped else f"设置已保存；忽略不可写键：{', '.join(skipped[:4])}"
        return {"ok": True, "message": message, "data": self.snapshot.settings()}

    def _settings_pick_directory(self, data: dict[str, Any]) -> dict[str, Any]:
        key = str(data.get("key", "")).strip()
        if key not in {"projects", "models", "cache"}:
            raise ValueError("只支持工程、模型与缓存目录")
        if self._window is None:
            raise ValueError("没有可用的窗口")
        current = str((self.store.get_setting("paths.overrides", {}) or {}).get(key, ""))
        chosen = QFileDialog.getExistingDirectory(self._window, "选择目录", current)
        if not chosen:
            return {"ok": False, "message": "已取消选择目录"}
        target = Path(chosen).resolve()
        if not target.is_dir():
            raise ValueError(f"目录不存在：{target}")
        values = dict(self.store.get_setting("paths.overrides", {}) or {})
        values[key] = str(target)
        self.store.set_setting("paths.overrides", values)
        return {"ok": True, "message": "目录已保存；重启后完全生效", "data": self.snapshot.settings()}

    # ------------------------------------------------------------------ cover
    def _cover_service(self) -> CoverService:
        if self.cover is None:
            raise RuntimeError("本地工作进程未连接，无法执行翻唱任务")
        return self.cover

    def _cover_state(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": self._cover_service().state(str(data.get("cover_id", "")))}

    def _cover_attest(self, data: dict[str, Any]) -> dict[str, Any]:
        state = self._cover_service().attest_rights(str(data.get("cover_id", "")), bool(data.get("confirmed", True)))
        return {"ok": True, "message": "已确认歌曲处理与使用权利", "data": state}

    def _cover_separate(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().separate(str(data.get("cover_id", "")), str(data.get("mode", "uvr5")))
        return {"ok": True, "message": "已开始分离人声与伴奏", **task}

    def _cover_cleanup(self, data: dict[str, Any]) -> dict[str, Any]:
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        task = self._cover_service().cleanup_vocal(str(data.get("cover_id", "")), settings)
        return {"ok": True, "message": "已开始人声清理", **task}

    def _cover_transpose(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().suggest_transpose(str(data.get("cover_id", "")), str(data.get("profile_id", "")))
        return {"ok": True, "message": "正在用 RMVPE 分析音高", **task}

    def _cover_convert(self, data: dict[str, Any]) -> dict[str, Any]:
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        cleanup = data.get("cleanup") if isinstance(data.get("cleanup"), dict) else None
        task = self._cover_service().convert_vocal(
            str(data.get("cover_id", "")), str(data.get("profile_id", "")),
            int(data.get("pitch_shift", 0) or 0), settings, cleanup,
        )
        return {"ok": True, "message": "已开始生成 AI 人声", **task}

    def _cover_render(self, data: dict[str, Any]) -> dict[str, Any]:
        mix = data.get("mix") if isinstance(data.get("mix"), dict) else {}
        task = self._cover_service().render(str(data.get("cover_id", "")), str(data.get("profile_id", "")), mix)
        return {"ok": True, "message": "已开始生成最终混音", **task}

    def _cover_export(self, data: dict[str, Any]) -> dict[str, Any]:
        cover_id = str(data.get("cover_id", ""))
        destination = str(data.get("destination", "")).strip()
        if destination:
            target = self._resolve_owned(destination, field="导出目录")
        else:
            target = ensure_within(self.paths.projects_root, self.project / "exports")
        file_name = str(data.get("file_name", "")).strip() or f"{self._cover(cover_id).title}_final"
        task = self._cover_service().export(
            cover_id, format=str(data.get("format", "wav")), file_name=file_name,
            destination=target, existing_policy=str(data.get("existing_policy", "reject")),
        )
        return {"ok": True, "message": "已开始导出", **task}

    def _cover(self, cover_id: str):
        return CoverProject.load(self.project, str(cover_id))

    def _cover_lyrics(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().transcribe_lyrics(str(data.get("cover_id", "")), str(data.get("language", "zh")))
        return {"ok": True, "message": "已开始本地歌词识别", **task}

    def _cover_cancel(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().cancel(str(data.get("request_id", "")))
        return {"ok": True, "message": "已请求取消任务", **task}

    def handle_worker_event(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        """Route worker events to the services that own the request."""
        for service in (self.cover,):
            if service is not None and service.handle_worker_event(request_id, event, payload):
                return

    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        self._closing = True
        if self.cover is not None:
            self.cover.close()
        self._handlers.clear()
