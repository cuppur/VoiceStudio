"""QWebChannel bridge exposed to the HTML shell as ``bridge``.

The page never touches the filesystem or the database directly: it calls
``invoke(action, payload)`` and receives a JSON reply.  Actions that are not
wired to a real local operation answer with an explicit "not connected yet"
message instead of pretending to work.
"""
from __future__ import annotations

import json
import sys
import time
from uuid import uuid4
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
from .services.base import WebService
from .services.cover import ENCRYPTED_SUFFIXES, CoverService
from .services.engine import EngineService
from .services.exports import ExportsService
from .services.training import TrainingService
from .services.tts import TtsService
from .services.voices import VoicesService
from .services.media import MediaService

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")
# Listed in the file picker so an encrypted music-app download is selectable and
# then explained, instead of being silently absent from the dialog.
PICKER_SUFFIXES = AUDIO_SUFFIXES + ENCRYPTED_SUFFIXES
NOT_CONNECTED = "该功能尚未接入新界面"


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
        self._native_selections = {}
        self.cover = CoverService(paths, store, self.project, client, self)
        self.cover.event.connect(self.event)
        self.tts = TtsService(paths, store, self.project, client, self)
        self.tts.event.connect(self.event)
        self.voices = VoicesService(paths, store, self.project, client, self)
        self.voices.event.connect(self.event)
        self.training = TrainingService(paths, store, self.project, client, self)
        self.training.event.connect(self.event)
        self.exports = ExportsService(paths, store, self.project, client, self)
        self.exports.event.connect(self.event)
        self.engine = EngineService(paths, store, self.project, client, self)
        self.engine.event.connect(self.event)
        self.media = MediaService(paths, store, self.project, client, self)
        self.media.event.connect(self.event)
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "app.refresh": self._refresh,
            "engine.verify": self._engine_verify,
            "project.activate": self._project_activate,
            "project.create": self._project_create,
            "project.open": self._project_open,
            "project.reveal": self._project_reveal,
            "song.import": self._song_import,
            "song.rename": self._song_rename,
            "song.delete": self._song_delete,
            "cover.import_lrc": self._import_lrc,
            "cover.media": lambda data: self.media.analyze(str(data.get('cover_id', ''))),
            "preview.configure": self.media.configure,
            "preview.control": self.media.control,
            "task.cancel": self._task_cancel,
            "task.resume": self._task_resume,
            "training.edit_draft": self._training_edit_draft,
            "song.select": self._song_select,
            "file.reveal": self._file_reveal,
            "file.open": self._file_open,
            "settings.set": self._settings_set,
            "settings.save": self._settings_save,
            "settings.pick_directory": self._settings_pick_directory,
            "cover.state": self._cover_state,
            "cover.one_click": self._cover_one_click,
            "cover.resume": self._cover_resume,
            "cover.select_take": self._cover_select_take,
            "cover.attest": self._cover_attest,
            "cover.separate": self._cover_separate,
            "cover.cleanup": self._cover_cleanup,
            "cover.transpose": self._cover_transpose,
            "cover.convert": self._cover_convert,
            "cover.render": self._cover_render,
            "cover.export": self._cover_export,
            "cover.lyrics": self._cover_lyrics,
            "cover.cancel": self._cover_cancel,
            "separator.import": self._separator_import,
            "tts.generate": self._tts_generate,
            "tts.cancel": self._tts_cancel,
            "tts.history": self._tts_history,
            "voices.detail": self._voices_detail,
            "voices.create": self._voices_create,
            "voices.rename": self._voices_rename,
            "voices.archive": self._voices_archive,
            "voices.activate": self._voices_activate,
            "voices.import_model": self._voices_import,
            "training.state": self._training_state,
            "training.import": self._training_import,
            "training.remove_asset": self._training_remove,
            "training.remove_assets": self._training_remove_many,
            "training.start": self._training_start,
            "training.confirm": self._training_confirm,
            "training.resume": self._training_resume,
            "training.cancel": self._training_cancel,
            "training.singing": self._training_singing,
            "exports.clean_cache": self._exports_clean,
            "exports.open_folder": self._exports_open,
            "engine.install": self._engine_install,
            "engine.cancel_install": self._engine_cancel,
        }

    # ------------------------------------------------------------------
    def set_project(self, project: Path) -> None:
        if Path(project) != self.project and (self.training._scanning or self.cover._tasks or self.tts._pending
                or self.training._tasks or (self.training.controller and (self.training.controller.requests or self.training.controller.background))):
            raise ValueError('请等待当前任务结束或取消后再切换工程')
        self.project = Path(project)
        self.snapshot = StudioSnapshot(self.paths, self.store, self.project)
        for service in (self.cover, self.tts, self.voices, self.training, self.exports, self.engine, self.media):
            service.set_project(self.project)
        self.store.set_setting('ui.last_project', str(self.project))
        if self._window is not None and hasattr(self._window, 'session'):
            if self._window.session.current != self.project:
                self._window.session.activate(self.project)

    def offer_files(self, paths):
        """Only called by the native view's drop event, never exposed as a Slot."""
        token = uuid4().hex
        self._native_selections = {token: (time.monotonic(), list(paths))}
        self.notify('files.dropped', {'selection': token})

    def _song_rename(self, data):
        result = self.cover.rename_song(str(data.get('cover_id', '')), str(data.get('title', '')))
        return {'ok': True, 'message': '已重命名歌曲', **result, 'data': self.snapshot.state()}

    def _song_delete(self, data):
        cover_id = str(data.get('cover_id', ''))
        if self.cover.active_request(cover_id):
            raise ValueError('歌曲正在处理，请先取消或等待任务完成')
        if self.media.cover_id == cover_id:
            self.media.set_project(self.project)
            for player in self.media.players.values():
                player.setSource(QUrl())
        result = self.cover.delete_song(cover_id)
        return {'ok': True, **result, 'data': self.snapshot.state()}

    def _selection(self, data):
        token = data.get('selection')
        if not token: return None
        selected = self._native_selections.pop(str(token), None)
        if not selected or time.monotonic() - selected[0] > 300:
            raise ValueError('文件选择已过期，请重新拖入文件')
        return selected[1]

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
        selected = self._selection(data)
        if selected is not None:
            if len(selected) != 1: raise ValueError('请每次导入一首歌曲；训练素材支持多选')
            source = Path(selected[0])
        elif raw:
            source = self._resolve_owned(raw, field="音频路径")
        elif self._window is not None:
            picker = " ".join(f"*{suffix}" for suffix in PICKER_SUFFIXES)
            chosen, _filter = QFileDialog.getOpenFileName(
                self._window, "导入歌曲到工程", "", f"音频文件 ({picker});;所有文件 (*)"
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

    def _import_lrc(self, data):
        cover_id = str(data.get('cover_id', ''))
        self._cover(cover_id)
        raw = str(data.get('path', ''))
        if raw:
            source = self._resolve_owned(raw)
        else:
            if self._window is None: raise ValueError('没有可用的窗口')
            chosen, _ = QFileDialog.getOpenFileName(self._window, '导入 LRC 歌词', '', 'LRC 歌词 (*.lrc)')
            if not chosen: return {'ok': False, 'message': '已取消导入歌词'}
            source = Path(chosen)
        result = self.cover.import_lrc(cover_id, source)
        return {'ok': True, 'message': f"已导入 {result['line_count']} 行歌词", 'data': self.snapshot.state()}

    def _task_cancel(self, data):
        identifier = str(data.get('id', ''))
        if identifier:
            for request, parent_id in self.cover._cover_runs.items():
                if parent_id == identifier:
                    return {'ok': True, **self.cover.cancel_task(request)}
        for service in (self.cover, self.training):
            for request, info in service._tasks.items():
                if info['job_id'] == identifier or request == str(data.get('request_id', '')):
                    return {'ok': True, **service.cancel_task(request)}
        for request, info in self.tts._pending.items():
            if info['job'].id == identifier:
                return {'ok': True, **self.tts.cancel(request)}
        if self.training.controller:
            for request, (workflow_id, operation, job) in self.training.controller.requests.items():
                if job.id == identifier:
                    return {'ok': True, **self.training.cancel(workflow_id)}
        raise ValueError('任务已结束或不属于当前运行会话，请刷新任务列表')

    def _task_resume(self, data):
        job_id = str(data.get('id',''))
        if not job_id:
            raise ValueError('请选择需要继续的任务')
        try:
            product = self.store.load_product_job(job_id)
        except KeyError:
            product = None
        if product and product.kind == 'ai_cover':
            return self._cover_resume({'job_id':job_id})
        job = next((item for item in self.store.list_jobs(200) if item.id == job_id),None)
        if job is None or not job.payload.get('workflow_id') or Path(str(job.payload.get('project_path',''))).resolve() != self.project.resolve():
            raise ValueError('该任务不能从当前工程恢复')
        result = self.training.resume(str(job.payload['workflow_id']))
        return {'ok':True,'data':result,'message':'训练已继续'}

    def _training_edit_draft(self, data):
        result = self.training.edit_draft(str(data.get('draft_id', '')), data.get('segments') or [])
        return {'ok': True, 'data': result, 'message': '校对已保存'}

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

    def _cover_one_click(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().start_cover(
            str(data.get('cover_id','')), str(data.get('profile_id','')),
            mode=str(data.get('mode','uvr5')), pitch_shift=int(data.get('pitch_shift',0)),
            settings=data.get('settings') or {}, mix=data.get('mix') or {})
        return {'ok':True,'message':'一键翻唱已开始，将自动完成分离、转换和混音',**task}

    def _cover_resume(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self._cover_service().resume_cover(str(data.get('job_id','')))
        return {'ok':True,'message':'已继续一键翻唱任务',**task}

    def _cover_select_take(self, data: dict[str, Any]) -> dict[str, Any]:
        state = self._cover_service().select_take(str(data.get('cover_id','')), str(data.get('take_id','')))
        return {'ok':True,'data':state,'message':'已恢复成品版本'}

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

    def _separator_import(self, data: dict[str, Any]) -> dict[str, Any]:
        """Import an audio file for the standalone separation page."""
        raw = str(data.get("path", "")).strip()
        source: Path | None = None
        selected = self._selection(data)
        if selected is not None:
            if len(selected) != 1: raise ValueError('请每次选择一首待分离歌曲')
            source = Path(selected[0])
        elif raw:
            source = self._resolve_owned(raw, field="音频路径")
        elif self._window is not None:
            chosen, _filter = QFileDialog.getOpenFileName(
                self._window, "选择要分离的歌曲", "", "音频文件 (*.wav *.mp3 *.flac *.m4a *.aac *.ogg);;所有文件 (*)"
            )
            if chosen:
                source = Path(chosen)
        if source is None:
            return {"ok": False, "message": "已取消选择"}
        result = self._cover_service().import_song(source)
        return {"ok": True, "message": f"已导入：{source.name}", "data": self.snapshot.state(), **result}

    def handle_worker_event(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        """Route worker events to the services that own the request."""
        for service in (self.cover, self.tts, self.training):
            if service is not None and service.handle_worker_event(request_id, event, payload):
                return

    # --------------------------------------------------------------- training
    def _training_state(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": self.training.state(str(data.get("profile_id", "")))}

    def _training_import(self, data: dict[str, Any]) -> dict[str, Any]:
        paths = data.get("paths")
        native = self._selection(data)
        chosen = native if native is not None else ([str(self._resolve_owned(str(item))) for item in paths] if isinstance(paths, list) and paths else [])
        if not chosen:
            if self._window is None:
                raise ValueError("没有选择音频文件或文件夹")
            files, _filter = QFileDialog.getOpenFileNames(
                self._window, "选择声音素材", "", "音频文件 (*.wav *.mp3 *.flac *.m4a *.aac *.ogg);;所有文件 (*)"
            )
            chosen = [str(item) for item in files]
        if not chosen:
            return {"ok": False, "message": "已取消导入"}
        result = self.training.import_assets(
            str(data.get("profile_id", "")), chosen,
            name=str(data.get("name", "")), consent=bool(data.get("consent", False)),
        )
        return {"ok": True, **result}

    def _training_remove(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.training.remove_assets(str(data.get("profile_id", "")), [str(data.get("asset_id", ""))])
        return {"ok": True, "message": "已移除素材", "removed": result["removed"], "data": result["state"]}

    def _training_remove_many(self, data: dict[str, Any]) -> dict[str, Any]:
        ids = data.get("asset_ids")
        result = self.training.remove_assets(
            str(data.get("profile_id", "")),
            [str(item) for item in ids] if isinstance(ids, list) else [],
        )
        return {"ok": True, "message": f"已移除 {result['removed']} 个素材", "removed": result["removed"], "data": result["state"]}

    def _training_start(self, data: dict[str, Any]) -> dict[str, Any]:
        asset_ids = data.get("asset_ids")
        result = self.training.start(
            str(data.get("profile_id", "")), smart=bool(data.get("smart", True)),
            manual_review=bool(data.get('manual_review', False)), quality=str(data.get('quality', 'standard')),
            train_tts=bool(data.get('train_tts', True)), train_singing=bool(data.get('train_singing', False)),
            asset_ids=[str(item) for item in asset_ids] if isinstance(asset_ids, list) else None,
        )
        return {"ok": True, "message": "已开始自动处理素材", **result}

    def _training_confirm(self, data: dict[str, Any]) -> dict[str, Any]:
        include = data.get("include") if isinstance(data.get("include"), list) else None
        exclude = data.get("exclude") if isinstance(data.get("exclude"), list) else None
        result = self.training.confirm(
            str(data.get("draft_id", "")),
            include=[str(item) for item in include] if include else None,
            exclude=[str(item) for item in exclude] if exclude else None,
            options=data,
        )
        return {"ok": True, "message": "已确认并开始训练", **result}

    def _training_resume(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.training.resume(str(data.get("workflow_id", "")), options=data)
        return {"ok": True, "message": "已继续上次任务", **result}

    def _training_cancel(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.training.cancel(str(data.get("workflow_id", "")))
        return {"ok": True, **result}

    def _training_singing(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self.training.train_singing(str(data.get("profile_id", "")))
        return {"ok": True, "message": "已开始歌唱模型训练", **task}

    # ---------------------------------------------------------------- exports
    def _exports_clean(self, _data: dict[str, Any]) -> dict[str, Any]:
        result = self.exports.clean_cache()
        return {"ok": True, "message": result["message"], "data": result["data"]}

    def _exports_open(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.exports.open_folder(str(data.get("path", "")))
        return {"ok": True, "message": result["message"]}

    # ----------------------------------------------------------------- engine
    def _engine_install(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.engine.install(tools=bool(data.get("tools", True)))
        return {"ok": True, **result}

    def _engine_cancel(self, _data: dict[str, Any]) -> dict[str, Any]:
        result = self.engine.cancel_install()
        return {"ok": True, **result}

    # -------------------------------------------------------------------- tts
    def _tts_generate(self, data: dict[str, Any]) -> dict[str, Any]:
        task = self.tts.generate(
            str(data.get("profile_id", "")), str(data.get("text", "")),
            speed=float(data.get("speed", 1.0) or 1.0),
            pause=float(data.get("pause", 0.3) or 0.3),
            seed=int(data.get("seed", -1) or -1),
            language=str(data.get("language", "zh")),
            output_dir=str(data.get("output_dir", "")),
        )
        return {"ok": True, "message": "已开始生成语音", **task}

    def _tts_cancel(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "message": "已请求取消生成", **self.tts.cancel(str(data.get("request_id", "")))}

    def _tts_history(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": {"history": self.tts.history(), "profiles": self.tts.usable_profiles(),
                                     "output_dir": self.tts.default_output_dir()}}

    # ----------------------------------------------------------------- voices
    def _voices_detail(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "data": self.voices.detail(str(data.get("profile_id", "")))}

    def _voices_create(self, data: dict[str, Any]) -> dict[str, Any]:
        detail = self.voices.create(str(data.get("name", "")), bool(data.get("consent", False)))
        return {"ok": True, "message": f"已创建声音：{detail['name']}", "data": detail}

    def _voices_rename(self, data: dict[str, Any]) -> dict[str, Any]:
        detail = self.voices.rename(str(data.get("profile_id", "")), str(data.get("name", "")))
        return {"ok": True, "message": "已重命名", "data": detail}

    def _voices_archive(self, data: dict[str, Any]) -> dict[str, Any]:
        self.voices.archive(str(data.get("profile_id", "")))
        return {"ok": True, "message": "已移除声音配置（素材与模型保留）", "data": self.snapshot.state()}

    def _voices_activate(self, data: dict[str, Any]) -> dict[str, Any]:
        detail = self.voices.activate_version(
            str(data.get("profile_id", "")), str(data.get("version_id", "")), str(data.get("kind", "tts")),
        )
        return {"ok": True, "message": "已切换模型版本", "data": detail}

    def _voices_import(self, data: dict[str, Any]) -> dict[str, Any]:
        paths = data.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("没有选择模型文件")
        result = self.voices.import_model([str(item) for item in paths])
        return {"ok": True, "message": result["message"], "data": result}

    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        self._closing = True
        for service in (self.cover, self.tts, self.voices, self.training, self.exports, self.engine, self.media):
            service.close()
        self._handlers.clear()
