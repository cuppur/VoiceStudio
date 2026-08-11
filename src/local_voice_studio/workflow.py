from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from .audio import copy_original, scan_audio_files, sha256_file
from .models import (
    DatasetDraft, DatasetDraftSegment, DatasetManifest, DatasetSegment, Job, JobKind,
    JobStatus, ModelVersion, ReferenceAsset, TrainingWorkflow, VoiceProfile,
    WorkflowStage, WorkflowStatus, dataset_snapshot_sha256, utc_now,
)
from .paths import ensure_within
from .storage import StudioStore


VERIFY_TEXTS = (
    "清晨的阳光穿过窗帘，今天的声音训练已经完成。",
    "你好，欢迎使用 VoiceStudio. Please check the latest game build.",
)


def parse_asr_result(raw_text: str, fallback_language: str = "zh") -> tuple[str, str, list[str]]:
    tags = re.findall(r"<\|[^|]+\|>", raw_text)
    text = re.sub(r"<\|[^|]+\|>", "", raw_text).strip()
    language = fallback_language
    for tag in tags:
        if tag in {"<|zh|>", "<|yue|>"}: language = "zh"
        elif tag == "<|en|>": language = "en"
    flags: list[str] = []
    if "<|BGM|>" in tags: flags.append("疑似配乐")
    if any(tag in {"<|nospeech|>", "<|Event|>", "<|Event_UNK|>"} for tag in tags): flags.append("ASR 检测到异常")
    if not text.strip(): flags.append("空文本")
    return language, text.strip(), flags


def draft_from_preparation(project: Path, workflow: TrainingWorkflow, manifest_path: Path) -> DatasetDraft:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(value.get("preparation_id")) != workflow.preparation_id:
        raise ValueError("准备结果与当前任务不匹配")
    list_path = Path(value["asr_list"])
    recognized: dict[str, tuple[str, str, list[str]]] = {}
    for line in list_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            recognized[Path(parts[0]).name] = parse_asr_result(parts[3], parts[2] or "zh")
    probes = [item for item in scan_audio_files([Path(value["segments_dir"])]) if not item.duplicate_of]
    segments: list[DatasetDraftSegment] = []
    for probe in probes:
        audio = ensure_within(project, Path(probe.path))
        language, text, asr_flags = recognized.get(audio.name, ("zh", "", ["空文本"]))
        flags = list(dict.fromkeys([*probe.quality_flags, *asr_flags]))
        if probe.duration_seconds < 1.5: flags.append("片段过短")
        if len("".join(text.split())) < 2: flags.append("文本过短")
        flags = list(dict.fromkeys(flags))
        segments.append(DatasetDraftSegment(
            audio_relative_path=audio.relative_to(project.resolve()).as_posix(), start_seconds=0,
            end_seconds=probe.duration_seconds, language=language, asr_text=text, text=text,
            quality_flags=flags, included=not flags, human_confirmed=False,
        ))
    return DatasetDraft(workflow.id, workflow.voice_profile_id, workflow.preparation_id, segments)


def freeze_draft(store: StudioStore, project: Path, profile: VoiceProfile, draft: DatasetDraft) -> DatasetManifest:
    valid = [item for item in draft.segments if item.included and item.human_confirmed and item.text.strip() and not item.quality_flags]
    seconds = sum(item.duration_seconds for item in valid)
    if seconds < 60:
        raise ValueError(f"已确认的合格素材不足 60 秒，还差 {60 - seconds:.1f} 秒")
    dataset = DatasetManifest(f"{profile.id}-snapshot", profile.id, frozen=True)
    final_root = ensure_within(project / "datasets", project / "datasets" / dataset.id)
    temporary = ensure_within(project / "datasets", project / "datasets" / f".{dataset.id}.tmp")
    if temporary.exists(): shutil.rmtree(temporary)
    audio_root = temporary / "audio"; audio_root.mkdir(parents=True)
    lines: list[str] = []
    try:
        for item in valid:
            source = ensure_within(project, project / item.audio_relative_path)
            copied = copy_original(source, audio_root)
            relative = f"datasets/{dataset.id}/audio/{copied.name}"
            digest = sha256_file(copied)
            lines.append(f"{relative}|speaker|{item.language}|{item.text}")
            dataset.segments.append(DatasetSegment(
                digest, str(project / relative), item.start_seconds, item.end_seconds,
                item.language, item.text, item.asr_text, None, [], True, True, True,
                item.id, relative,
            ))
        list_file = temporary / "dataset.list"
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        dataset.list_path = str(final_root / "dataset.list")
        dataset.wav_dir = str(final_root / "audio")
        dataset.list_relative_path = f"datasets/{dataset.id}/dataset.list"
        dataset.list_sha256 = sha256_file(list_file)
        dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
        temporary.replace(final_root)
        store.save_dataset_snapshot(project, dataset)
    except Exception:
        if temporary.exists(): shutil.rmtree(temporary)
        raise
    profile.dataset_snapshot_id = dataset.id
    if not any(item.approved and item.transcript.strip() and 5 <= item.duration_seconds <= 10 for item in profile.reference_assets):
        reference = next((item for item in dataset.segments if 5 <= item.duration_seconds <= 10), None)
        if reference:
            profile.reference_assets = [ReferenceAsset(reference.audio_path, reference.source_sha256, reference.text, reference.language, reference.duration_seconds, True, [])]
    store.save_profile(project, profile)
    return dataset


class TrainingWorkflowController(QObject):
    workflow_changed = Signal(object)
    draft_ready = Signal(object)
    profile_changed = Signal(str)
    job_created = Signal(object)

    def __init__(self, store: StudioStore, project: Path, client, parent=None):
        super().__init__(parent); self.store, self.project, self.client = store, project, client
        self.requests: dict[str, tuple[str, str, Job]] = {}
        self.verify_index: dict[str, int] = {}
        self.client.event.connect(self._on_event)
        self.store.recover_workflows(project)

    def start(self, profile: VoiceProfile, source_asset_ids: list[str], smart_optimization: bool = True) -> TrainingWorkflow:
        if not profile.consent_confirmed: raise ValueError("请先确认声音属于本人或已取得明确授权")
        workflow = TrainingWorkflow(
            profile.id, profile.name, list(source_asset_ids), status=WorkflowStatus.WAITING,
            processing_options={"language": "zh", "separate_vocals": smart_optimization, "denoise": smart_optimization},
            consent_record=profile.consent_record, consent_confirmed_at=profile.consent_confirmed_at,
        )
        profile.current_workflow_id = workflow.id; profile.last_workflow_id = workflow.id
        self.store.save_profile(self.project, profile); self.store.save_workflow(self.project, workflow)
        self.process(workflow)
        return workflow

    def process(self, workflow: TrainingWorkflow) -> None:
        workflow.stage = WorkflowStage.PREPROCESSING; workflow.status = WorkflowStatus.RUNNING
        workflow.preparation_id = uuid4().hex; workflow.attempt += 1; workflow.progress = .03
        workflow.message = "正在检查、清理、切片并识别文字"; workflow.error = ""
        self._save(workflow)
        assets = self.store.list_source_assets(self.project, workflow.voice_profile_id)
        payload = self._context(workflow, {
            "action": "pipeline", "preparation_id": workflow.preparation_id,
            "profile_id": workflow.voice_profile_id, "source_asset_ids": workflow.source_asset_ids,
            "source_assets": [item.to_dict() for item in assets], "project_path": str(self.project),
            "processing_options": workflow.processing_options,
        })
        self._send(workflow, "prepare_dataset", "pipeline", JobKind.PREPARE_DATASET, payload)

    def confirm_and_train(self, workflow: TrainingWorkflow, draft: DatasetDraft) -> None:
        profile = self._profile(workflow.voice_profile_id)
        if not profile.consent_confirmed: raise ValueError("授权记录已失效，请重新确认")
        for item in draft.segments:
            item.human_confirmed = bool(item.included and item.text.strip() and not item.quality_flags)
        self.store.save_draft(self.project, draft)
        if draft.confirmed_seconds < 60:
            workflow.stage = WorkflowStage.REVIEW_REQUIRED; workflow.status = WorkflowStatus.WAITING
            workflow.waiting_reason = f"还差 {60 - draft.confirmed_seconds:.1f} 秒合格素材"
            self._save(workflow); raise ValueError(workflow.waiting_reason)
        if not any(5 <= item.duration_seconds <= 10 for item in draft.segments if item.included and item.human_confirmed and not item.quality_flags):
            workflow.stage = WorkflowStage.REVIEW_REQUIRED; workflow.status = WorkflowStatus.WAITING
            workflow.waiting_reason = "需要至少一个 5–10 秒的干净参考片段"
            self._save(workflow); raise ValueError(workflow.waiting_reason)
        workflow.stage = WorkflowStage.FREEZING; workflow.status = WorkflowStatus.RUNNING; workflow.progress = .52; workflow.message = "正在锁定已确认数据"; self._save(workflow)
        dataset = freeze_draft(self.store, self.project, profile, draft)
        workflow.dataset_snapshot_id = dataset.id; workflow.snapshot_sha256 = dataset.snapshot_sha256
        self._prepare_features(workflow, dataset)

    def resume(self, workflow: TrainingWorkflow) -> None:
        if workflow.stage == WorkflowStage.REVIEW_REQUIRED and workflow.draft_id:
            self.draft_ready.emit(self.store.load_draft(self.project, workflow.draft_id)); self._save(workflow); return
        if workflow.stage in {WorkflowStage.IMPORTING, WorkflowStage.PREPROCESSING}: self.process(workflow); return
        if workflow.dataset_snapshot_id:
            dataset = self.store.load_dataset_snapshot(self.project, workflow.dataset_snapshot_id)
            if workflow.stage in {WorkflowStage.FREEZING, WorkflowStage.FEATURE_PREPARING}: self._prepare_features(workflow, dataset); return
            if workflow.stage == WorkflowStage.TRAINING: self._train(workflow, dataset); return
            if workflow.stage == WorkflowStage.VERIFYING: self._verify(workflow); return
        raise ValueError("上次任务缺少可恢复的数据，请重新处理素材")

    def cancel(self, workflow: TrainingWorkflow) -> None:
        request = next((rid for rid, value in self.requests.items() if value[0] == workflow.id), "")
        if request: self.client.send("cancel", {"target_request_id": request})
        workflow.status = WorkflowStatus.CANCELLED; workflow.message = "任务已取消，可稍后重试"; self._save(workflow)

    def _prepare_features(self, workflow: TrainingWorkflow, dataset: DatasetManifest) -> None:
        workflow.stage = WorkflowStage.FEATURE_PREPARING; workflow.status = WorkflowStatus.RUNNING; workflow.progress = .58; workflow.message = "正在准备训练"; workflow.error = ""; self._save(workflow)
        payload = self._dataset_payload(workflow, dataset)
        manifest = self._matching_feature_manifest(payload)
        if manifest:
            workflow.feature_manifest = str(manifest); self._save(workflow); self._train(workflow, dataset); return
        self._send(workflow, "prepare_dataset", "features", JobKind.PREPARE_DATASET, payload)

    def _train(self, workflow: TrainingWorkflow, dataset: DatasetManifest) -> None:
        profile = self._profile(workflow.voice_profile_id)
        if not profile.consent_confirmed: raise ValueError("训练前授权记录已失效")
        workflow.stage = WorkflowStage.TRAINING; workflow.status = WorkflowStatus.RUNNING; workflow.training_run_id = uuid4().hex; workflow.attempt += 1; workflow.progress = .68; workflow.message = "正在训练声音模型"; self._save(workflow)
        payload = self._dataset_payload(workflow, dataset)
        payload.update({"training_run_id": workflow.training_run_id, "training_mode": "new"})
        self._send(workflow, "train", "train", JobKind.TRAIN, payload)

    def _verify(self, workflow: TrainingWorkflow) -> None:
        workflow.stage = WorkflowStage.VERIFYING; workflow.status = WorkflowStatus.RUNNING; workflow.progress = .9; workflow.message = "正在验证新声音"; self._save(workflow)
        profile = self._profile(workflow.voice_profile_id); candidate = profile.to_dict()
        candidate["active_gpt_checkpoint"] = workflow.candidate_gpt_checkpoint
        candidate["active_sovits_checkpoint"] = workflow.candidate_sovits_checkpoint
        request = uuid4().hex
        self.requests[request] = (workflow.id, "verify_load", Job(JobKind.SYNTHESIZE, candidate))
        self.client.send("load_profile", self._context(workflow, candidate), request_id=request)

    def _verify_synthesize(self, workflow: TrainingWorkflow, index: int) -> None:
        profile = self._profile(workflow.voice_profile_id)
        ref = next((item for item in profile.reference_assets if item.approved and item.transcript.strip() and Path(item.path).is_file()), None)
        if not ref: raise ValueError("缺少可用的 5–10 秒参考片段")
        root = self.project / "exports" / "model-versions" / workflow.training_run_id
        payload = self._context(workflow, {"text": VERIFY_TEXTS[index], "text_lang": "auto", "ref_audio_path": ref.path, "prompt_text": ref.transcript, "prompt_lang": ref.language, "speed_factor": 1.0, "fragment_interval": .3, "output_dir": str(root)})
        job = Job(JobKind.SYNTHESIZE, payload); self.store.save_job(job); self.job_created.emit(job)
        request = uuid4().hex; self.requests[request] = (workflow.id, f"verify_{index}", job)
        self.client.send("synthesize", payload, request_id=request)

    def _finish(self, workflow: TrainingWorkflow) -> None:
        if len(workflow.verification_outputs) < 2 or any(not Path(item).is_file() or Path(item).stat().st_size < 44 for item in workflow.verification_outputs if item.lower().endswith(".wav")):
            raise RuntimeError("新声音验证文件不完整，旧声音未被替换")
        profile = self._profile(workflow.voice_profile_id)
        for item in profile.model_versions:
            if item.status == "active": item.status = "available"
        version = ModelVersion(
            name=f"训练版本 {len(profile.model_versions) + 1}", training_run_id=workflow.training_run_id,
            dataset_snapshot_id=workflow.dataset_snapshot_id, snapshot_sha256=workflow.snapshot_sha256,
            gpt_checkpoint=workflow.candidate_gpt_checkpoint, sovits_checkpoint=workflow.candidate_sovits_checkpoint,
            preview_outputs=workflow.verification_outputs, status="active",
        )
        profile.model_versions.append(version); profile.active_model_version_id = version.id
        profile.active_gpt_checkpoint = version.gpt_checkpoint; profile.active_sovits_checkpoint = version.sovits_checkpoint
        profile.default_model_mode = "fine_tuned"; profile.training_state = ""; profile.current_workflow_id = ""
        self.store.save_profile(self.project, profile)
        workflow.stage = WorkflowStage.SAVED; workflow.status = WorkflowStatus.COMPLETED; workflow.progress = 1; workflow.message = "新声音已验证并启用"; workflow.waiting_reason = ""; self._save(workflow); self.profile_changed.emit(profile.id)

    def _on_event(self, request_id: str, event: str, payload: dict) -> None:
        context = self.requests.get(request_id)
        if not context: return
        workflow = self.store.load_workflow(self.project, context[0]); operation, job = context[1], context[2]
        if event == "progress":
            job.status = JobStatus.RUNNING; job.progress = float(payload.get("progress", 0)); job.message = str(payload.get("message", "")); self.store.save_job(job)
            bases = {"pipeline": (.05, .42), "features": (.58, .1), "train": (.68, .21), "verify_0": (.9, .04), "verify_1": (.94, .05)}
            base, span = bases.get(operation, (workflow.progress, 0)); workflow.progress = min(.99, base + span * job.progress); workflow.message = job.message; self._save(workflow); return
        if event == "error":
            self.requests.pop(request_id, None); job.status = JobStatus.CANCELLED if payload.get("status") == "cancelled" else JobStatus.FAILED; job.error = str(payload.get("message", "")); self.store.save_job(job)
            workflow.status = WorkflowStatus.CANCELLED if job.status == JobStatus.CANCELLED else WorkflowStatus.FAILED; workflow.error = job.error; workflow.message = "已取消" if job.status == JobStatus.CANCELLED else "本阶段失败，可点击重试"; self._save(workflow); return
        if event != "result": return
        self.requests.pop(request_id, None); job.status = JobStatus.COMPLETED; job.progress = 1; job.outputs = list(payload.get("outputs", [])); self.store.save_job(job)
        try:
            if operation == "pipeline":
                draft = draft_from_preparation(self.project, workflow, Path(job.outputs[0])); self.store.save_draft(self.project, draft)
                workflow.draft_id = draft.id; workflow.stage = WorkflowStage.REVIEW_REQUIRED; workflow.status = WorkflowStatus.WAITING; workflow.progress = .48
                workflow.waiting_reason = f"已识别 {len(draft.segments)} 个片段，请确认合格数据"; workflow.message = workflow.waiting_reason; self._save(workflow); self.draft_ready.emit(draft)
            elif operation == "features":
                workflow.feature_manifest = job.outputs[0]; self._save(workflow)
                QTimer.singleShot(50, lambda workflow_id=workflow.id: self._train(
                    self.store.load_workflow(self.project, workflow_id),
                    self.store.load_dataset_snapshot(self.project, workflow.dataset_snapshot_id),
                ))
            elif operation == "train":
                checkpoints = dict(payload.get("checkpoints") or {}); workflow.candidate_gpt_checkpoint = str(checkpoints.get("gpt", "")); workflow.candidate_sovits_checkpoint = str(checkpoints.get("sovits", ""))
                if not Path(workflow.candidate_gpt_checkpoint).is_file() or not Path(workflow.candidate_sovits_checkpoint).is_file(): raise RuntimeError("本次训练没有生成完整模型")
                self._save(workflow); QTimer.singleShot(50, lambda workflow_id=workflow.id: self._verify(self.store.load_workflow(self.project, workflow_id)))
            elif operation == "verify_load": QTimer.singleShot(50, lambda workflow_id=workflow.id: self._verify_synthesize(self.store.load_workflow(self.project, workflow_id), 0))
            elif operation.startswith("verify_"):
                outputs = list(payload.get("outputs", [])); wavs = [item for item in outputs if item.lower().endswith(".wav") and Path(item).is_file() and Path(item).stat().st_size >= 44]
                if not wavs: raise RuntimeError("固定试听生成失败")
                workflow.verification_outputs.extend(outputs); self._save(workflow)
                if operation == "verify_0": QTimer.singleShot(50, lambda workflow_id=workflow.id: self._verify_synthesize(self.store.load_workflow(self.project, workflow_id), 1))
                else: self._finish(workflow)
        except Exception as exc:
            workflow.status = WorkflowStatus.FAILED; workflow.error = str(exc); workflow.message = "验证失败，旧声音未被替换" if workflow.stage == WorkflowStage.VERIFYING else "本阶段失败，可点击重试"; self._save(workflow)

    def _send(self, workflow: TrainingWorkflow, command: str, operation: str, kind: JobKind, payload: dict) -> None:
        job = Job(kind, payload); self.store.save_job(job); self.job_created.emit(job)
        request = uuid4().hex; self.requests[request] = (workflow.id, operation, job)
        try:
            self.client.send(command, payload, request_id=request)
        except Exception:
            self.requests.pop(request, None)
            raise

    def _save(self, workflow: TrainingWorkflow) -> None:
        self.store.save_workflow(self.project, workflow); self.workflow_changed.emit(workflow)

    def _profile(self, profile_id: str) -> VoiceProfile:
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == profile_id and not item.archived), None)
        if not profile: raise ValueError("声音配置不存在或已删除")
        return profile

    def _context(self, workflow: TrainingWorkflow, payload: dict) -> dict:
        return {**payload, "workflow_id": workflow.id, "stage": workflow.stage.value, "attempt": workflow.attempt, "overall_progress": workflow.progress}

    def _dataset_payload(self, workflow: TrainingWorkflow, dataset: DatasetManifest) -> dict:
        profile = self._profile(workflow.voice_profile_id)
        return self._context(workflow, {**dataset.to_dict(), "approved_seconds": dataset.approved_seconds, "project_path": str(self.project), "profile_id": profile.id, "dataset_snapshot_id": dataset.id, "consent_confirmed": profile.consent_confirmed, "consent_record": profile.consent_record, "experiment_name": f"{self.project.name}-{profile.id[:8]}-{dataset.snapshot_sha256[:12]}", "checkpoint_dir": str(self.project / "checkpoints" / profile.id)})

    def _matching_feature_manifest(self, payload: dict) -> Path | None:
        root = self.store.paths.data_root / "training" / str(payload["profile_id"]) / str(payload["snapshot_sha256"]) / "features" / "feature-manifest.json"
        if not root.is_file(): return None
        try:
            value = json.loads(root.read_text(encoding="utf-8"))
            if all(str(value.get(key, "")) == str(payload.get(key, "")) for key in ("profile_id", "dataset_snapshot_id", "snapshot_sha256", "list_sha256")) and all(Path(item).is_file() and Path(item).stat().st_size for item in value.get("feature_files", {}).values()): return root
        except (OSError, ValueError, TypeError, json.JSONDecodeError): pass
        return None
