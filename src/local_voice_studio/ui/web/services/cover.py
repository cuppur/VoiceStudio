"""AI cover workflow service (import → rights → separate → convert → mix → export)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ....application.jobs import JobStage, ProductJobStatus
from ....cover.application.service import CoverApplicationService
from ....cover.mixing.models import CoverMixSettings, GainScale
from ....cover.project import RIGHTS_ATTESTATION_TEXT, CoverProject
from ....cover.separation import RoFormerRuntimeStatus, UVR5RuntimeStatus
from .base import WebService
from ...cover_session import probe_audio_metadata, parse_lrc, write_lrc

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")
SEPARATION_MODES = ("uvr5", "roformer")

# ProductJob stage name -> CoverProject stage field.  The job coordinator and
# the cover manifest track progress independently; without this bridge the
# manifest statuses stay wherever they were left and the page can never tell
# that a stage finished.
STAGE_FIELD_BY_TASK = {
    "separation": "separation",
    "voice_conversion": "ai_vocal",
    "mix": "mix",
    "export": "export",
}

# Music-app downloads are encrypted containers, not decodable audio.  Naming
# them here is the only way a user understands why import refuses the file.
ENCRYPTED_SUFFIXES = (".mgg", ".mflac", ".ncm", ".qmc0", ".qmc3", ".qmcflac", ".qmcogg", ".tm0", ".tm2", ".tm3", ".bkp")


class CoverService(WebService):
    """Real cover orchestration, free of Qt widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cover_runs: dict[str, str] = {}
        for job in self.store.list_product_jobs():
            if (job.kind == 'ai_cover' and Path(str(job.payload.get('project_path',''))).resolve() == self.project.resolve()
                    and job.status in {ProductJobStatus.RUNNING,ProductJobStatus.PREPARING,ProductJobStatus.CANCELLING}):
                job.status = ProductJobStatus.RECOVERABLE
                job.error = '上次进程已结束，可从当前阶段继续'
                self.store.save_product_job(job)

    # ------------------------------------------------------------------ state
    def _service(self) -> CoverApplicationService:
        return CoverApplicationService(self.project, paths=self.paths, store=self.store)

    def _cover(self, cover_id: str) -> CoverProject:
        return CoverProject.load(self.project, str(cover_id))

    # ---------------------------------------------------------- stage status
    def _mark_stage(self, cover_id: str, stage: str, status: str) -> None:
        """Mirror a task transition onto the cover manifest's stage status.

        Failures here must never break the task itself: the manifest status is
        a display/progress concern, while the product job already records the
        authoritative outcome.
        """
        field = STAGE_FIELD_BY_TASK.get(str(stage or ""))
        if not field or not cover_id:
            return
        try:
            self._cover(str(cover_id)).set_stage_status(field, status)
        except Exception:
            return

    def start_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().start_task(*args, **kwargs)
        self._mark_stage(kwargs.get("cover_id", ""), kwargs.get("stage", ""), "running")
        return result

    def engines(self) -> dict[str, Any]:
        uvr5 = UVR5RuntimeStatus.detect(self.paths, deep=False)
        roformer = RoFormerRuntimeStatus.detect(self.paths, deep=False)
        return {
            "uvr5": {"ready": bool(uvr5.ready), "detail": str(getattr(uvr5, "message", "") or getattr(uvr5, "error", "") or "")},
            "roformer": {"ready": bool(roformer.ready), "detail": str(getattr(roformer, "message", "") or getattr(roformer, "error", "") or "")},
        }

    def state(self, cover_id: str) -> dict[str, Any]:
        cover = self._cover(cover_id)
        assets = {str(asset.role): asset for asset in cover.assets}
        active_mix = cover.get_asset(role='final_mix')
        if active_mix:
            assets['final_mix'] = active_mix
        return {
            "cover_id": cover.id,
            "title": cover.title,
            "rights_confirmed": bool(cover.rights_confirmed),
            "rights_text": RIGHTS_ATTESTATION_TEXT,
            "separation_status": str(cover.separation_status),
            "ai_vocal_status": str(cover.ai_vocal_status),
            "mix_status": str(cover.mix_status),
            "export_status": str(cover.export_status),
            "has_vocal": "vocal" in assets,
            "has_instrumental": "instrumental" in assets,
            "has_ai_vocal": "ai_vocal" in assets,
            "has_final_mix": "final_mix" in assets,
            "final_asset_id": assets["final_mix"].id if "final_mix" in assets else "",
            "engines": self.engines(),
            "active_request": self.active_request(cover.id),
            "takes": [self._take(cover, asset) for asset in cover.assets if asset.role == 'final_mix'],
            "active_take_id": active_mix.id if active_mix else '',
            "pipeline": self._pipeline_state(cover.id),
        }

    PIPELINE_STAGE_LABELS = {
        "separation": "分离人声与伴奏",
        "voice_conversion": "生成 AI 人声",
        "mix": "生成最终混音",
    }

    def _pipeline_state(self, cover_id: str) -> dict[str, Any]:
        """Expose the one-click pipeline's stage progress to the page.

        Previously the button was a black box: the manifest statuses never left
        "running", so the only way to see progress was to watch files appear.
        """
        finished = {ProductJobStatus.SUCCEEDED, ProductJobStatus.FAILED,
                    ProductJobStatus.CANCELLED, ProductJobStatus.PUBLISHED}
        for job in self.store.list_product_jobs():
            if job.kind != 'ai_cover':
                continue
            if str(job.payload.get('cover_id', '')) != str(cover_id):
                continue
            stages = [{
                "name": item.name,
                "label": self.PIPELINE_STAGE_LABELS.get(item.name, item.name),
                "status": str(item.status.value if hasattr(item.status, 'value') else item.status),
                "progress": round(float(item.progress or 0) * 100, 1),
                "error": str(item.error or ''),
            } for item in job.stages]
            return {
                "job_id": job.id,
                "status": str(job.status.value if hasattr(job.status, 'value') else job.status),
                "current_stage": str(job.current_stage or ''),
                "current_label": self.PIPELINE_STAGE_LABELS.get(str(job.current_stage or ''), ''),
                "progress": round(float(job.progress or 0) * 100, 1),
                "error": str(job.error or ''),
                "stages": stages,
                "finished": job.status in finished,
            }
        return {}

    @staticmethod
    def _take(cover: CoverProject, asset) -> dict[str, Any]:
        return {"id": asset.id, "path": str(cover.root / asset.relative_path),
                "created_at": asset.created_at, "sha256": asset.sha256,
                "settings": dict(asset.metadata.get('settings') or {}),
                "model_id": asset.model_id,
                "source_asset_ids": list(asset.source_asset_ids)}

    def select_take(self, cover_id: str, take_id: str) -> dict[str, Any]:
        from ....audio import sha256_file
        cover = self._cover(cover_id)
        asset = cover.get_asset(take_id)
        if asset is None or asset.role != 'final_mix':
            raise ValueError('找不到该成品版本')
        path = cover.root / asset.relative_path
        if not path.is_file() or sha256_file(path) != asset.sha256:
            raise ValueError('成品文件缺失或已被修改，无法恢复')
        cover.active_take_id = asset.id
        cover.save()
        self.notify('songs.changed', {'cover_id':cover.id})
        return self.state(cover_id)

    def start_cover(self, cover_id: str, profile_id: str, *, mode='uvr5', pitch_shift=0,
                    settings=None, mix=None) -> dict[str, Any]:
        cover = self._cover(cover_id)
        if not cover.rights_confirmed:
            raise ValueError('请先确认歌曲处理权利')
        if self.active_request(cover_id):
            raise ValueError('歌曲已有任务正在进行')
        profile = next((p for p in self.store.list_profiles(self.project) if p.id == profile_id), None)
        if profile is None or not profile.active_singing_model_id:
            raise ValueError('请先选择已就绪的歌唱声音')
        if mode not in SEPARATION_MODES:
            raise ValueError('不支持的分离方式')
        payload = {'project_path':str(self.project), 'cover_id':cover_id,
                   'profile_id':profile_id, 'mode':mode, 'pitch_shift':int(pitch_shift),
                   'settings':dict(settings or {}), 'mix':dict(mix or {}), 'title':cover.title}
        job = self.coordinator.start('ai_cover', payload, ['separation','voice_conversion','mix'])
        if cover.get_asset(role='vocal') and cover.get_asset(role='instrumental'):
            job.mark_stage_running('separation')
            job.mark_stage_succeeded('separation')
        self.store.save_product_job(job)
        return self._advance_cover(job.id)

    def resume_cover(self, job_id: str) -> dict[str, Any]:
        job = self.store.load_product_job(job_id)
        if job.kind != 'ai_cover' or Path(str(job.payload.get('project_path',''))).resolve() != self.project.resolve():
            raise ValueError('找不到当前工程的一键翻唱任务')
        if job.status == ProductJobStatus.SUCCEEDED:
            raise ValueError('一键翻唱已经完成')
        if self.active_request(str(job.payload['cover_id'])):
            raise ValueError('该歌曲已有任务正在进行')
        self._cover(str(job.payload['cover_id']))
        for stage in ('separation','voice_conversion','mix'):
            item = job.stage(stage)
            if item.status not in {ProductJobStatus.SUCCEEDED,ProductJobStatus.PUBLISHED}:
                item.status = ProductJobStatus.QUEUED; item.progress = 0
        job.error = ''; job.status = ProductJobStatus.QUEUED
        self.store.save_product_job(job)
        return self._advance_cover(job.id)

    def _advance_cover(self, job_id: str) -> dict[str, Any]:
        job = self.store.load_product_job(job_id)
        payload = job.payload; cover_id = str(payload['cover_id']); profile_id = str(payload['profile_id'])
        ready = job.ready_stages()
        if not ready:
            job.status = ProductJobStatus.SUCCEEDED; job.current_stage = ''
            self.store.save_product_job(job)
            self.notify('cover.pipeline.complete', {'job_id':job_id, 'cover_id':cover_id})
            return {'job_id':job_id, 'completed':True}
        stage = ready[0].name
        try:
            if stage == 'separation':
                task = self.separate(cover_id, str(payload['mode']))
            elif stage == 'voice_conversion':
                task = self.convert_vocal(cover_id, profile_id, int(payload['pitch_shift']), payload['settings'])
            else:
                task = self.render(cover_id, profile_id, payload['mix'])
        except Exception as exc:
            job.status = ProductJobStatus.RECOVERABLE; job.error = str(exc)
            self.store.save_product_job(job)
            raise
        job.mark_stage_running(stage)
        self.store.save_product_job(job)
        self._cover_runs[task['request_id']] = job_id
        self.notify('cover.pipeline.stage', {'job_id':job_id,'stage':stage,'request_id':task['request_id']})
        return {**task, 'job_id':job_id, 'stage':stage}

    def handle_worker_event(self, request_id: str, event: str, payload: dict[str, Any]) -> bool:
        parent_id = self._cover_runs.get(str(request_id))
        # Base pops the task registration on result/error, so snapshot it first.
        info = dict(self._tasks.get(str(request_id)) or {})
        handled = super().handle_worker_event(request_id,event,payload)
        if info and event in {'result', 'error'}:
            if event == 'result':
                settled = 'completed'
            elif payload.get('cancelled') or payload.get('status') == 'cancelled':
                settled = 'cancelled'
            else:
                settled = 'failed'
            self._mark_stage(info.get('cover_id', ''), info.get('stage', ''), settled)
        if parent_id and event in {'result','error'}:
            self._cover_runs.pop(str(request_id),None)
            job = self.store.load_product_job(parent_id)
            if event == 'result':
                job.mark_stage_succeeded(job.current_stage)
                self.store.save_product_job(job)
                try: self._advance_cover(parent_id)
                except Exception as exc:
                    self.notify('cover.pipeline.error',{'job_id':parent_id,'message':str(exc)})
            else:
                job.status = ProductJobStatus.RECOVERABLE
                job.error = str(payload.get('message','本阶段失败'))
                job.stage(job.current_stage).error = job.error
                self.store.save_product_job(job)
                self.notify('cover.pipeline.error',{'job_id':parent_id,'message':job.error})
        return handled

    # ---------------------------------------------------------------- actions
    def delete_song(self, cover_id: str) -> dict[str, Any]:
        from ....paths import ensure_within
        if self.active_request(cover_id):
            raise ValueError('歌曲正在处理，请先取消或等待任务完成')
        cover = self._cover(cover_id)
        source = ensure_within(self.project / 'covers', cover.root)
        trash = ensure_within(self.project, self.project / '.trash' / 'songs' / cover.id)
        trash.parent.mkdir(parents=True, exist_ok=True)
        source.rename(trash)
        self.notify('songs.changed', {'deleted_id': cover.id})
        return {'cover_id': cover.id, 'message': '歌曲已移入工程回收目录，外部原始文件不受影响'}

    def rename_song(self, cover_id: str, title: str) -> dict[str, Any]:
        """Rename a song project's display title; audio and files stay put."""
        label = str(title or "").strip()
        if not label:
            raise ValueError("歌曲名称不能为空")
        if len(label) > 120:
            raise ValueError("歌曲名称过长（最多 120 个字符）")
        if any(ch in label for ch in '/\\:*?"<>|'):
            raise ValueError('歌曲名称不能包含 / \\ : * ? " < > | 这些字符')
        cover = self._cover(cover_id)
        if self.active_request(cover.id):
            raise ValueError('歌曲正在处理，请先取消或等待任务完成')
        cover.title = label
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return {"cover_id": cover.id, "title": cover.title}

    def import_song(self, source: Path) -> dict[str, Any]:
        source = Path(source)
        if not source.is_file():
            raise ValueError(f"文件不存在：{source}")
        if source.suffix.lower() in ENCRYPTED_SUFFIXES:
            raise ValueError(
                f"「{source.name}」是音乐播放器下载的加密文件，无法直接导入。"
                "请先在播放器内导出或转换为 WAV、MP3、FLAC、M4A、AAC 或 OGG 后再试。"
            )
        if source.suffix.lower() not in AUDIO_SUFFIXES:
            raise ValueError(f"不支持的音频格式：{source.suffix}")
        metadata = probe_audio_metadata(source, paths=self.paths)
        if metadata.duration_seconds <= 0 or metadata.sample_rate <= 0:
            raise ValueError('文件中没有可解码的音频')
        cover = CoverProject.create(self.project, title=source.stem or "未命名翻唱")
        cover.copy_source(source)
        cover.duration_ms = round(metadata.duration_seconds * 1000)
        lrc = source.with_suffix('.lrc')
        if lrc.is_file():
            try:
                self.import_lrc(cover.id, lrc)
                cover = self._cover(cover.id)
                cover.duration_ms = round(metadata.duration_seconds * 1000)
            except (OSError, UnicodeError, ValueError):
                pass  # An invalid optional sidecar must not reject valid audio.
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return {"cover_id": cover.id, "title": cover.title}

    def import_lrc(self, cover_id: str, source: Path) -> dict[str, Any]:
        cover = self._cover(cover_id)
        if source.suffix.lower() != '.lrc' or source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('请选择不超过 2 MB 的 LRC 歌词文件')
        try:
            text = source.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            text = source.read_text(encoding='gb18030')
        lines = parse_lrc(text)
        if not lines: raise ValueError('LRC 中没有有效的时间标签和歌词')
        target = cover.root / 'lyrics' / 'manual.lrc'
        write_lrc(target, lines)
        cover.lyrics_path = target.relative_to(cover.root).as_posix()
        cover.lyrics_origin = 'manual'
        cover.save()
        self.notify('songs.changed', {'cover_id': cover.id})
        return {'line_count': len(lines)}

    def attest_rights(self, cover_id: str, confirmed: bool = True) -> dict[str, Any]:
        cover = self._cover(cover_id)
        cover.attest_rights(bool(confirmed))
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return self.state(cover.id)

    def separate(self, cover_id: str, mode: str = "uvr5") -> dict[str, Any]:
        if str(mode) not in SEPARATION_MODES:
            raise ValueError("不支持的分离方式（当前仅 UVR5 与 RoFormer）")
        # Rights and source integrity are checked before any engine probing.
        command = self._service().prepare_separation(cover_id, mode=str(mode))
        engines = self.engines()
        if not engines.get(str(mode), {}).get("ready"):
            raise RuntimeError(f"{str(mode).upper()} 分离引擎未安装或未就绪")
        return self.start_task(
            "separate_song", command.to_worker_payload(),
            kind="separate", stage="separation", title=self._cover(cover_id).title,
            cover_id=cover_id,
        )

    def cleanup_vocal(self, cover_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"mode": "denoise", **(settings or {})}
        command = self._service().prepare_vocal_cleanup(cover_id, payload)
        return self.start_task(
            "cleanup_vocal", command.to_worker_payload(),
            kind="cleanup", stage="post_process", title=self._cover(cover_id).title,
            cover_id=cover_id,
        )

    def suggest_transpose(self, cover_id: str, profile_id: str) -> dict[str, Any]:
        command = self._service().prepare_transpose_suggestion(cover_id, profile_id)
        return self.start_task(
            "suggest_transpose", command.to_worker_payload(),
            kind="transpose", stage="analysis", title=self._cover(cover_id).title,
            cover_id=cover_id, profile_id=profile_id,
        )

    def convert_vocal(self, cover_id: str, profile_id: str, pitch_shift: int = 0,
                      settings: dict[str, Any] | None = None, cleanup: dict[str, Any] | None = None) -> dict[str, Any]:
        command = self._service().prepare_ai_vocal(
            cover_id, profile_id, pitch_shift=int(pitch_shift), inference_settings=dict(settings or {}),
        )
        payload = command.to_worker_payload()
        cover_id = str(cover_id)
        if cleanup:
            # Optional vocal cleanup first; the AI vocal command runs after it.
            cleanup_command = self._service().prepare_vocal_cleanup(cover_id, dict(cleanup))
            task = self.start_task(
                "cleanup_vocal", cleanup_command.to_worker_payload(),
                kind="cleanup", stage="post_process", title=self._cover(cover_id).title,
                cover_id=cover_id, profile_id=profile_id,
            )
            self._tasks[task["request_id"]]["on_result"] = (
                lambda _payload, payload=payload, cover_id=cover_id, profile_id=profile_id:
                self._start_convert(payload, cover_id, profile_id)
            )
            return task
        return self._start_convert(payload, cover_id, profile_id)

    def _start_convert(self, payload: dict[str, Any], cover_id: str, profile_id: str) -> dict[str, Any]:
        return self.start_task(
            "convert_vocal", payload, kind="convert", stage="voice_conversion",
            title=self._cover(cover_id).title, cover_id=cover_id, profile_id=profile_id,
        )

    def render(self, cover_id: str, profile_id: str, mix: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.mix_settings(mix or {})
        command = self._service().prepare_render(cover_id, profile_id, settings)
        return self.start_task(
            "render_cover", command.to_worker_payload(), kind="render", stage="mix",
            title=self._cover(cover_id).title, cover_id=cover_id, profile_id=profile_id,
        )

    def export(self, cover_id: str, *, format: str, file_name: str, destination: Path,
               existing_policy: str = "reject") -> dict[str, Any]:
        state = self.state(cover_id)
        command = self._service().prepare_export(
            cover_id, final_asset_id=state["final_asset_id"], format=str(format),
            file_name=str(file_name), destination=Path(destination),
            existing_policy=str(existing_policy), publication_rights_acknowledged=True,
        )
        return self.start_task(
            "export_cover", command.to_worker_payload(), kind="export", stage="export",
            title=self._cover(cover_id).title, cover_id=cover_id,
        )

    def transcribe_lyrics(self, cover_id: str, language: str = "zh") -> dict[str, Any]:
        command = self._service().prepare_lyrics_transcription(cover_id, language=str(language))
        return self.start_task(
            "transcribe_lyrics", command.to_worker_payload(), kind="lyrics", stage="lyrics",
            title=self._cover(cover_id).title, cover_id=cover_id,
        )

    def cancel(self, request_id: str = "") -> dict[str, Any]:
        return self.cancel_task(request_id)

    # ------------------------------------------------------------------ mix
    @staticmethod
    def mix_settings(mix: dict[str, Any]) -> CoverMixSettings:
        """Translate prototype slider values (0-100) into canonical dB settings."""
        def db(key: str, default_slider: float) -> float:
            value = mix.get(key)
            slider = default_slider if value is None else float(value)
            return GainScale.slider_to_db(slider)

        return CoverMixSettings(
            ai_gain_db=db("ai", 80.0),
            instrumental_gain_db=db("instrumental", 80.0),
            original_vocal_gain_db=db("original", 0.0),
            master_gain_db=float(mix.get("master_db", 0.0) or 0.0),
            normalize=bool(mix.get("normalize", True)),
            limiter=bool(mix.get("limiter", True)),
            fade_in_ms=int(mix.get("fade_in_ms", 0) or 0),
            fade_out_ms=int(mix.get("fade_out_ms", 0) or 0),
        )
