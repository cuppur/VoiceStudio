"""Voice training service: material import, dataset preparation, review, training.

The heavy orchestration already lives in :class:`TrainingWorkflowController`
(no Qt widgets); this service exposes it to the HTML shell, runs the blocking
audio scan on a pool thread and forwards workflow/draft events to the page.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, QThreadPool, Signal, Slot

from ....audio import copy_original, scan_audio_files
from ....models import SourceAsset, VoiceProfile, WorkflowStage, WorkflowStatus, utc_now
from ....runtime import EngineRuntimeResolver
from ....workflow import TrainingWorkflowController
from .base import WebService

SINGING_MIN_SECONDS = 180.0


class _ScanTask(QRunnable):
    """Run the blocking material scan off the UI thread."""

    def __init__(self, paths: list[Path], ffprobe: Path | None, done, failed, register=None):
        super().__init__()
        self._paths, self._ffprobe, self._done, self._failed = paths, ffprobe, done, failed
        self._register = register

    def run(self) -> None:  # pragma: no cover - pool thread
        try:
            probes = scan_audio_files(self._paths, self._ffprobe)
            if self._register:
                probes = self._register(probes)
        except Exception as exc:  # noqa: BLE001
            self._failed(str(exc))
            return
        self._done(probes)


class TrainingService(WebService):
    """Real training workflow for the HTML shell."""

    scan_finished = Signal(object)
    scan_failed = Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = TrainingWorkflowController(self.store, self.project, self.client, self) if self.client is not None else None
        self._workflow_id = ""
        self._draft_id = ""
        self._scanning = False
        self.scan_finished.connect(self._finish_scan)
        self.scan_failed.connect(self._fail_scan)
        if self.controller is not None:
            self.controller.workflow_changed.connect(self._on_workflow)
            self.controller.draft_ready.connect(self._on_draft)
            self.controller.profile_changed.connect(lambda profile_id: self.notify("voices.changed", {"profile_id": profile_id}))

    # ------------------------------------------------------------------ state
    def state(self, profile_id: str = "") -> dict[str, Any]:
        profiles = [item for item in self.store.list_profiles(self.project) if not item.archived]
        profile = next((item for item in profiles if item.id == str(profile_id)), None)
        profile = profile or (profiles[0] if profiles else None)
        assets: list[dict[str, Any]] = []
        workflow: dict[str, Any] = {}
        draft: dict[str, Any] = {}
        if profile is not None:
            rows = [item for item in self.store.list_source_assets(self.project, profile.id) if not item.duplicate_of]
            assets = [{
                "id": item.id, "name": Path(item.original_path or item.project_path).name,
                "seconds": round(float(item.duration_seconds or 0), 1),
                "sample_rate": int(item.sample_rate or 0), "channels": int(item.channels or 0),
                "codec": str(item.codec or ""), "enabled": bool(item.enabled),
                "confirmed_seconds": round(float(item.confirmed_seconds or 0), 1),
                "segments": int(item.segment_count or 0), "status": str(item.processing_status or ""),
                "flags": [str(flag) for flag in item.quality_flags],
                "exists": bool(self._asset_path(item)) and Path(self._asset_path(item)).is_file(),
                "path": str(self._asset_path(item)),
            } for item in rows]
            workflows = self.store.list_workflows(self.project, profile.id)
            current = next((item for item in workflows if item.id == self._workflow_id), workflows[0] if workflows else None)
            if current is not None:
                workflow = self._workflow_dict(current)
                if current.draft_id and current.stage == WorkflowStage.REVIEW_REQUIRED:
                    try:
                        draft = self._draft_dict(self.store.load_draft(self.project, current.draft_id))
                    except (KeyError, ValueError, OSError):
                        draft = {}
        total = sum(item["seconds"] for item in assets if item["enabled"])
        return {
            "profile": {"id": profile.id, "name": profile.name, "consent": bool(profile.consent_confirmed)} if profile else None,
            "profiles": [{"id": item.id, "name": item.name} for item in profiles],
            "assets": assets,
            "total_seconds": round(total, 1),
            "total_text": f"{int(total // 60)}:{int(total % 60):02d}",
            "singing_ready_seconds": round(total, 1),
            "singing_min_seconds": SINGING_MIN_SECONDS,
            "workflow": workflow,
            "draft": draft,
            "scanning": self._scanning,
            "singing_request": self.active_request(kind='singing'),
        }

    def _asset_path(self, asset):
        from ....paths import ensure_within
        try:
            return ensure_within(self.project, Path(asset.project_path or asset.original_path))
        except ValueError:
            return ''

    @staticmethod
    def _workflow_dict(workflow) -> dict[str, Any]:
        return {
            "id": workflow.id, "stage": workflow.stage.value, "status": workflow.status.value,
            "progress": round(float(workflow.progress or 0) * 100, 1),
            "message": str(workflow.message or ""), "error": str(workflow.error or ""),
            "waiting_reason": str(workflow.waiting_reason or ""),
            "draft_id": str(workflow.draft_id or ""),
            "dataset_snapshot_id": str(workflow.dataset_snapshot_id or ""),
            "can_resume": workflow.status in {WorkflowStatus.INTERRUPTED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED},
            "running": workflow.status == WorkflowStatus.RUNNING,
        }

    @staticmethod
    def _draft_dict(draft) -> dict[str, Any]:
        rows = []
        for item in draft.segments:
            rows.append({
                "id": item.id, "seconds": round(float(item.duration_seconds or 0), 2),
                "text": str(item.text or ""),
                "flags": [str(flag) for flag in item.quality_flags],
                "included": bool(item.included), "confirmed": bool(item.human_confirmed),
                "eligible": bool(item.eligible),
                "override_reason": item.override_reason,
                "hard_blocked": item.hard_blocked,
            })
        return {
            "id": draft.id, "workflow_id": draft.workflow_id,
            "confirmed_seconds": round(float(draft.confirmed_seconds or 0), 1),
            "segments": rows,
            "abnormal": [row for row in rows if row["flags"] or not row["eligible"]],
        }

    # ---------------------------------------------------------------- actions
    def import_assets(self, profile_id: str, paths: list[str], *, name: str = "", consent: bool = False) -> dict[str, Any]:
        if self._scanning:
            raise ValueError("正在检查上一批素材，请稍候")
        files = [Path(str(item)) for item in paths or []]
        existing_paths = [item for item in files if item.exists()]
        if not existing_paths:
            raise ValueError("没有找到可导入的音频文件或文件夹")
        profile = self._profile(profile_id, name=name, consent=consent, create=not profile_id)
        self._scanning = True
        self.notify("training.scanning", {"profile_id": profile.id})

        ffprobe = EngineRuntimeResolver(self.paths).resolve_private_tool("ffprobe")
        QThreadPool.globalInstance().start(_ScanTask(existing_paths, ffprobe, self.scan_finished.emit, self.scan_failed.emit,
                                                   lambda probes: self._register(profile, probes)))
        return {"profile_id": profile.id, "message": "正在检查素材文件……"}

    @Slot(object)
    def _finish_scan(self, payload):
        self._scanning = False
        if self._closing: return
        self.notify('training.scanned', payload)

    @Slot(str)
    def _fail_scan(self, message):
        self._scanning = False
        self.notify('training.error', {'message': message})

    def _profile(self, profile_id: str, *, name: str, consent: bool, create: bool) -> VoiceProfile:
        if profile_id:
            profile = next((item for item in self.store.list_profiles(self.project) if item.id == str(profile_id) and not item.archived), None)
            if profile is None:
                raise ValueError("声音配置不存在或已归档")
            return profile
        label = str(name or "").strip() or "我的声音"
        if not consent:
            raise ValueError("请先确认这是本人声音，或已经取得明确授权")
        profile = next((item for item in self.store.list_profiles(self.project) if not item.archived and item.name == label), None)
        if profile is None:
            profile = VoiceProfile(label, True, consent_record="训练页确认：本人声音或已取得明确授权", consent_confirmed_at=utc_now())
        else:
            profile.consent_confirmed = True
            profile.consent_record = "训练页再次确认：本人声音或已取得明确授权"
            profile.consent_confirmed_at = utc_now()
        self.store.save_profile(self.project, profile)
        return profile

    def _register(self, profile: VoiceProfile, probes) -> dict[str, Any]:
        existing = {item.sha256 for item in self.store.list_source_assets(self.project)}
        assets: list[SourceAsset] = []
        duplicates = 0
        for probe in probes:
            if probe.duplicate_of or probe.sha256 in existing:
                duplicates += 1
                continue
            copied = copy_original(Path(probe.path), self.project / "raw" / profile.id, probe.sha256)
            asset = SourceAsset(
                profile.id, probe.path, str(copied), probe.sha256,
                duration_seconds=probe.duration_seconds, sample_rate=probe.sample_rate,
                channels=probe.channels, codec=probe.codec,
                quality_flags=list(probe.quality_flags), enabled=True,
            )
            assets.append(asset)
            profile.source_asset_ids.append(asset.id)
        if assets:
            self.store.save_source_assets(self.project, assets)
        self.store.save_profile(self.project, profile)
        self.notify("voices.changed", {"profile_id": profile.id})
        return {
            "profile_id": profile.id, "added": len(assets), "duplicates": duplicates,
            "message": f"已导入 {len(assets)} 个素材" + (f"，跳过 {duplicates} 个重复文件" if duplicates else ""),
            "state": self.state(profile.id),
        }

    def remove_asset(self, profile_id: str, asset_id: str) -> dict[str, Any]:
        if self._scanning or (self.controller and (self.controller.requests or self.controller.background)):
            raise ValueError('素材正在处理，请结束任务后删除')
        self.store.remove_source_assets(self.project, {str(asset_id)})
        self.notify("voices.changed", {"profile_id": str(profile_id)})
        return self.state(profile_id)

    def start(self, profile_id: str, *, smart: bool = True, asset_ids: list[str] | None = None,
              manual_review=False, quality='standard', train_tts=True, train_singing=False) -> dict[str, Any]:
        if self.controller is None:
            raise RuntimeError("本地工作进程未连接，无法开始训练")
        if self._scanning or self.controller.requests or self.controller.background or self._tasks:
            raise ValueError('已有训练或素材检查正在运行')
        if quality not in {'quick', 'standard', 'extended'}:
            raise ValueError('不支持的训练时长档位')
        if not train_tts and not train_singing:
            raise ValueError('请选择至少一种训练能力')
        profile = self._profile(profile_id, name="", consent=False, create=False)
        if not profile.consent_confirmed:
            raise ValueError("请先确认这是本人声音，或已经取得明确授权")
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        if asset_ids:
            assets = [item for item in assets if item.id in set(str(item) for item in asset_ids)]
        if not assets:
            raise ValueError("没有可用的授权素材")
        if not train_tts:
            return self.train_singing(profile_id)
        workflow = self.controller.start(profile, [item.id for item in assets], bool(smart), auto_continue=not manual_review, quality=quality)
        workflow.processing_options['train_singing'] = bool(train_singing)
        self.store.save_workflow(self.project, workflow)
        self._workflow_id = workflow.id
        return {"workflow_id": workflow.id, "state": self.state(profile.id)}

    def edit_draft(self, draft_id, edits):
        draft = self.store.load_draft(self.project, draft_id)
        workflow = self.store.load_workflow(self.project, draft.workflow_id)
        if workflow.stage != WorkflowStage.REVIEW_REQUIRED:
            raise ValueError('当前任务不处于人工审核阶段')
        if not isinstance(edits, list): raise ValueError('片段编辑格式错误')
        segments = {s.id: s for s in draft.segments}
        if any(not isinstance(edit, dict) or edit.get('id') not in segments for edit in edits):
            raise ValueError('片段不属于当前草稿')
        for edit in edits:
            segment = segments[edit['id']]
            segment.text = str(edit.get('text', segment.text)).strip()
            segment.included = bool(edit.get('included', segment.included))
            segment.override_reason = str(edit.get('override_reason', segment.override_reason)).strip()
            segment.human_confirmed = False
            segment.auto_accepted = False
        self.store.save_draft(self.project, draft)
        return self._draft_dict(draft)

    def _review_options(self, workflow, options):
        if workflow.stage != WorkflowStage.REVIEW_REQUIRED or not options:
            return
        quality = str(options.get('quality', workflow.processing_options.get('quality', 'standard')))
        if quality not in {'quick', 'standard', 'extended'}:
            raise ValueError('不支持的训练时长档位')
        workflow.processing_options['quality'] = quality
        workflow.processing_options['train_singing'] = bool(options.get('train_singing', workflow.processing_options.get('train_singing', False)))
        self.store.save_workflow(self.project, workflow)

    def confirm(self, draft_id: str, *, include: list[str] | None = None, exclude: list[str] | None = None, options=None) -> dict[str, Any]:
        if self.controller is None:
            raise RuntimeError("本地工作进程未连接")
        draft = self.store.load_draft(self.project, str(draft_id))
        include_set = {str(item) for item in include or []}
        exclude_set = {str(item) for item in exclude or []}
        for segment in draft.segments:
            if segment.id in include_set:
                segment.included = True
            if segment.id in exclude_set:
                segment.included = False
        self.store.save_draft(self.project, draft)
        workflow = self.store.load_workflow(self.project, draft.workflow_id)
        self._review_options(workflow, options)
        self.controller.confirm_and_train(workflow, draft)
        self._draft_id = ""
        return {"workflow_id": workflow.id, "state": self.state(workflow.voice_profile_id)}

    def resume(self, workflow_id: str, *, options=None) -> dict[str, Any]:
        if self.controller is None:
            raise RuntimeError("本地工作进程未连接")
        workflow = self.store.load_workflow(self.project, str(workflow_id))
        self._review_options(workflow, options)
        self._workflow_id = workflow.id
        if workflow.stage == WorkflowStage.REVIEW_REQUIRED and workflow.draft_id:
            self.controller.confirm_and_train(workflow, self.store.load_draft(self.project, workflow.draft_id), automatic=True)
        else:
            self.controller.resume(workflow)
        return {"workflow_id": workflow.id, "state": self.state(workflow.voice_profile_id)}

    def cancel(self, workflow_id: str) -> dict[str, Any]:
        if self.controller is None:
            raise RuntimeError("本地工作进程未连接")
        workflow = self.store.load_workflow(self.project, str(workflow_id))
        self.controller.cancel(workflow)
        return {"workflow_id": workflow.id, "message": "已取消训练"}

    def train_singing(self, profile_id: str) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("本地工作进程未连接")
        if self._tasks:
            raise ValueError('已有歌唱训练在运行')
        profile = self._profile(profile_id, name="", consent=False, create=False)
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        total = sum(float(item.duration_seconds or 0) for item in assets)
        if total < SINGING_MIN_SECONDS:
            raise ValueError(f"歌唱模型训练需要至少 {SINGING_MIN_SECONDS / 60:.0f} 分钟素材，当前 {total:.1f} 秒")
        payload = {
            "project_path": str(self.project), "profile_id": profile.id,
            "source_asset_ids": [item.id for item in assets], "engine": "rvc_v2",
        }
        return self.start_task('train_singing_model', payload, kind='singing', stage='train',
                               title=f'{profile.name} · 歌唱模型训练', profile_id=profile.id)

    # ---------------------------------------------------------------- events
    def _on_workflow(self, workflow) -> None:
        self._workflow_id = workflow.id
        self.notify("training.workflow", {"workflow": self._workflow_dict(workflow), "profile_id": workflow.voice_profile_id})
        if workflow.stage == WorkflowStage.SAVED and workflow.processing_options.get('train_singing') and not workflow.processing_options.get('singing_started'):
            workflow.processing_options['singing_started'] = True
            self.store.save_workflow(self.project, workflow)
            try:
                self.train_singing(workflow.voice_profile_id)
            except Exception as exc:
                self.notify('training.error', {'message': f'文字声音已完成；歌唱训练未启动：{exc}'})

    def _on_draft(self, draft) -> None:
        self._draft_id = draft.id
        self.notify("training.draft", {"draft": self._draft_dict(draft), "profile_id": draft.voice_profile_id})

    def set_project(self, project: Path) -> None:
        super().set_project(project)
        self._workflow_id = ''
        self._draft_id = ''
        if self.controller is not None:
            self.controller.project = self.project

    def close(self) -> None:
        if self.controller is not None:
            self.controller._closing = True
            try:
                self.controller.client.event.disconnect(self.controller._on_event)
            except (RuntimeError, TypeError):
                pass
        super().close()
