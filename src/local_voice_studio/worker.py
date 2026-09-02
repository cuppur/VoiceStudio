from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import GptSovitsEngine, safe_torch_load
from .audio import sha256_file
from .models import DatasetManifest, dataset_snapshot_sha256, utc_now
from .paths import AppPaths, ensure_within, validate_id, validate_sha256
from .protocol import COMMANDS, Message, validate_payload
from .runtime import EngineRuntimeResolver
from .text import split_text
from .training import TrainingPipeline
from .cover.separation import SongSeparationPipeline
from .cover.mixing import CoverMixSettings, CoverMixer, FFmpegMixBackend
from .cover.exporting import CoverExporter, FFmpegExportBackend
from .cover.application import CoverApplicationService
from .cover.cancellation import CancellationToken
from .cover.errors import CoverError, error_payload as cover_error_payload
from .singing.pipeline import SingingPipeline
from .singing.rvc import RVCConfig, RVCEngine


class WorkerService:
    def __init__(self, paths: AppPaths | None = None, *, singing_engine: Any | None = None):
        self.paths = paths or AppPaths.default()
        self.paths.ensure()
        self.engine = GptSovitsEngine(self.paths)
        self.training = TrainingPipeline(self.paths)
        self.profile: dict[str, Any] | None = None
        self.current_thread: threading.Thread | None = None
        self.current_request_id = ""
        self._request_context: dict[str, Any] = {}
        self.cancel_event = threading.Event()
        self.cancel_token = CancellationToken(self.cancel_event)
        self.write_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.separation: SongSeparationPipeline | None = None
        self.mixer: CoverMixer | None = None
        self.exporter: CoverExporter | None = None
        if singing_engine is None:
            rvc_root = self.paths.data_root / "engines" / "RVC"
            rvc_python = self.paths.runtime_root / "rvc-env" / "Scripts" / "python.exe"
            if not rvc_python.is_file():
                rvc_python = self.paths.runtime_root / "rvc-env" / "python.exe"
            marker = rvc_root / ".pinned-commit"
            commit = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
            singing_engine = RVCEngine(RVCConfig(
                rvc_root, rvc_python, rvc_python.parent, commit=commit,
                hubert_sha256="cc8c20f4b90a520757260197a3ff2505705a7adbd20ad9eeaa4e1a9b38442ef5",
                rmvpe_sha256="6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
                pretrained_sha256=("b5d51f589cc3632d4eae36a315b4179397695042edc01d15312e1bddc2b764a4", "2269b73c7a4cf34da09aea99274dabf99b2ddb8a42cbfb065fb3c0aa9a2fc748"),
                torch_version="2.7.1+cu128",
            ))
        self.singing_engine = singing_engine

    def emit(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        if request_id == self.current_request_id and self._request_context:
            payload = {**payload, **self._request_context}
        message = json.dumps({"id": request_id, "type": event, "payload": payload}, ensure_ascii=False)
        terminal = request_id == self.current_request_id and event in {"result", "error"}
        if terminal:
            self.current_request_id = ""
            self._request_context = {}
        with self.write_lock:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()
        # Once a terminal event is visible to the UI, the GPU operation has
        # already returned.  Release the logical slot before the UI queues the
        # next stage (for example train -> load candidate -> verify).

    def handle(self, message: Message) -> None:
        if message.type not in COMMANDS:
            self.emit(message.id, "error", {"message": f"未知命令: {message.type}"})
            return
        if message.type in {"health", "load_profile"} and self.current_request_id and self.current_thread and self.current_thread.is_alive():
            self.emit(message.id, "error", {"message": "已有 GPU 任务正在运行，请等待完成或取消当前任务"})
            return
        try:
            message.payload = validate_payload(message.type, message.payload)
        except ValueError as exc:
            self.emit(message.id, "error", {"message": str(exc), "exception": type(exc).__name__})
            return
        if message.type == "health":
            health = self.engine.gpu_health()
            integrity = EngineRuntimeResolver(self.paths).verify_install_manifest()
            singing = self.singing_engine.readiness() if self.singing_engine is not None else None
            singing_ready = bool(singing and singing.ready)
            singing_errors = list(singing.errors) if singing else ["RVC 歌唱引擎未配置"]
            singing_details = dict(singing.details or {}) if singing else {}
            health["runtime_integrity"] = integrity.valid
            health["runtime_integrity_errors"] = list(integrity.errors)
            health["compatible"] = bool(health.get("compatible") and integrity.valid)
            self.emit(message.id, "result", {
                **health, "worker_python": sys.executable, "pid": os.getpid(), "engine": self.engine.readiness(), "gpu": health,
                "rvc_ready": singing_ready,
                "rmvpe_ready": singing_ready and not any("RMVPE" in item for item in singing_errors),
                "hubert_ready": singing_ready and not any("HuBERT" in item for item in singing_errors),
                "singing_ready": singing_ready,
                "singing_errors": singing_errors,
                "rvc_commit": singing_details.get("commit", getattr(getattr(self.singing_engine, "config", None), "commit", "")),
                "rvc_torch_version": singing_details.get("torch_version", getattr(getattr(self.singing_engine, "config", None), "torch_version", "")),
                "singing_details": singing_details,
            })
        elif message.type == "load_profile":
            self.profile = message.payload
            try:
                upgraded = self._upgrade_legacy_profile(self.profile)
                self.engine.load(self.profile, bool(message.payload.get("force_cpu", False)))
                self.emit(message.id, "result", {"loaded": True, "profile_id": self.profile.get("id", ""), "profile": self.profile if upgraded else {}})
            except Exception as exc:
                self.emit(message.id, "error", {"message": str(exc), "exception": type(exc).__name__})
        elif message.type == "cancel":
            target = str(message.payload.get("target_request_id", ""))
            if target and target != self.current_request_id:
                self.emit(message.id, "result", {"cancel_requested": False, "reason": "目标任务已结束或不是当前任务", "target_request_id": target})
                return
            self.cancel_event.set()
            self.engine.stop()
            self.training.cancel()
            if self.separation is not None:
                cancel = getattr(self.separation, "cancel", None)
                if callable(cancel):
                    cancel()
            if self.singing_engine is not None:
                stop = getattr(self.singing_engine, "cancel", None)
                if callable(stop):
                    stop()
            if self.mixer is not None: self.mixer.cancel()
            if self.exporter is not None: self.exporter.cancel()
            self.emit(message.id, "result", {"cancel_requested": True, "target_request_id": self.current_request_id})
        elif message.type == "shutdown":
            self.cancel_event.set()
            self.engine.stop()
            self.training.cancel()
            if self.separation is not None:
                cancel = getattr(self.separation, "cancel", None)
                if callable(cancel):
                    cancel()
            if self.singing_engine is not None:
                stop = getattr(self.singing_engine, "cancel", None)
                if callable(stop):
                    stop()
            if self.mixer is not None: self.mixer.cancel()
            if self.exporter is not None: self.exporter.cancel()
            self.shutdown_event.set()
            self.emit(message.id, "result", {"shutdown": True})
        else:
            if self.current_request_id and self.current_thread and self.current_thread.is_alive():
                self.emit(message.id, "error", {"message": "已有 GPU 任务正在运行"})
                return
            self.cancel_event.clear()
            target = {
                "synthesize": self._synthesize,
                "prepare_dataset": self._prepare_dataset,
                "train": self._train,
                "separate_song": self._separate_song,
                "train_singing_model": self._train_singing_model,
                "convert_vocal": self._convert_vocal,
                "render_cover": self._render_cover,
                "export_cover": self._export_cover,
            }[message.type]
            self.current_request_id = message.id
            self._request_context = {key: message.payload[key] for key in ("workflow_id", "stage", "attempt", "overall_progress") if key in message.payload}
            self._request_context["command"] = message.type
            self.current_thread = threading.Thread(target=self._run_guarded, args=(message.id, target, message.payload), daemon=True)
            self.current_thread.start()

    def _upgrade_legacy_profile(self, profile: dict[str, Any]) -> bool:
        if profile.get("active_model_trust_status") != "legacy-pending":
            return False
        configured = str(profile.get("project_path", ""))
        project = ensure_within(self.paths.projects_root, Path(configured)) if configured else self._find_profile_project(str(profile.get("id", "")))
        checkpoint_root = ensure_within(project, project / "checkpoints")
        gpt = ensure_within(checkpoint_root, Path(str(profile.get("active_gpt_checkpoint", ""))))
        sovits = ensure_within(checkpoint_root, Path(str(profile.get("active_sovits_checkpoint", ""))))
        expected_gpt = validate_sha256(str(profile.get("active_gpt_sha256", "")), field="active_gpt_sha256")
        expected_sovits = validate_sha256(str(profile.get("active_sovits_sha256", "")), field="active_sovits_sha256")
        if sha256_file(gpt) != expected_gpt or sha256_file(sovits) != expected_sovits:
            raise ValueError("旧模型文件哈希不一致，已拒绝升级信任")
        safe_torch_load(gpt, map_location="cpu")
        safe_torch_load(sovits, map_location="cpu")
        profile["active_model_trust_status"] = "trusted-local"
        manifest_path = project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stored = next((item for item in manifest.get("voice_profiles", []) if item.get("id") == profile.get("id")), None)
        if stored:
            stored["active_gpt_sha256"] = expected_gpt
            stored["active_sovits_sha256"] = expected_sovits
            stored["active_model_trust_status"] = "trusted-local"
            for version in stored.get("model_versions", []):
                if version.get("id") == stored.get("active_model_version_id"):
                    version["gpt_sha256"] = expected_gpt; version["sovits_sha256"] = expected_sovits; version["trust_status"] = "trusted-local"
            temporary = manifest_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(manifest_path)
        return True

    def _find_profile_project(self, profile_id: str) -> Path:
        validate_id(profile_id, legacy=True, field="profile_id")
        matches: list[Path] = []
        for manifest_path in self.paths.projects_root.glob("*/project.json"):
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                if any(str(item.get("id")) == profile_id for item in value.get("voice_profiles", [])):
                    matches.append(ensure_within(self.paths.projects_root, manifest_path.parent))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        if len(matches) != 1:
            raise ValueError("无法唯一确定旧模型所属项目，请重新打开该项目")
        return matches[0]

    def _run_guarded(self, request_id: str, target, payload: dict[str, Any]) -> None:
        try:
            target(request_id, payload)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            event = "cancelled" if self.cancel_event.is_set() else "failed"
            error_payload = {"message": str(exc), "exception": type(exc).__name__, "status": event}
            if isinstance(exc, CoverError):
                error_payload.update(cover_error_payload(exc))
            if request_id and self._request_context.get("command") in {"separate_song", "convert_vocal", "render_cover", "export_cover"}:
                command = self._request_context["command"]
                error_payload.setdefault("code", f"cover.{command}.failed")
                error_payload.setdefault("recoverable", event != "cancelled")
            self.emit(request_id, "error", error_payload)
        finally:
            if self.current_request_id == request_id:
                self.current_request_id = ""
                self._request_context = {}

    def _synthesize(self, request_id: str, payload: dict[str, Any]) -> None:
        if not self.profile:
            raise RuntimeError("请先加载声音配置")
        self.engine.load(self.profile, bool(payload.get("force_cpu", False)))
        segments = split_text(payload.get("text", ""), int(payload.get("max_chars", 120)))
        if not segments:
            raise ValueError("请输入要生成的文字")
        preview = bool(payload.get("preview", False))
        output_root = (self.paths.data_root / "cache" / "preview") if preview else Path(payload["output_dir"]).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        resume_dir = payload.get("resume_dir")
        job_dir = ensure_within(output_root, Path(resume_dir)) if resume_dir else output_root / ("preview-" + request_id[:12] if preview else datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + request_id[:8])
        job_dir.mkdir(parents=True, exist_ok=bool(resume_dir))
        signature_value = {key: payload.get(key) for key in ("text", "text_lang", "ref_audio_path", "prompt_text", "prompt_lang", "speed_factor", "fragment_interval", "top_k", "top_p", "temperature", "seed")}
        signature = hashlib.sha256(json.dumps(signature_value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        manifest_path = job_dir / "job.json"
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("signature") != signature: raise ValueError("恢复目录与当前生成参数不一致")
        else:
            manifest_path.write_text(json.dumps({"schema_version": 1, "signature": signature, "segments": segments, "completed": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
        wavs: list[Path] = []
        for index, text in enumerate(segments, 1):
            if self.cancel_event.is_set():
                raise RuntimeError("任务已取消")
            self.emit(request_id, "progress", {"progress": (index - 1) / len(segments), "message": f"生成第 {index}/{len(segments)} 段", "job_dir": str(job_dir)})
            part = job_dir / f"{index:04d}.wav"
            if not part.exists() or part.stat().st_size < 44:
                self.engine.synthesize_segment({**payload, "text": text}, part)
            wavs.append(part)
            manifest_path.write_text(json.dumps({"schema_version": 1, "signature": signature, "segments": segments, "completed": index}, ensure_ascii=False, indent=2), encoding="utf-8")
        merged_wav = job_dir / ("preview.wav" if preview else "merged.wav")
        mp3 = job_dir / "merged.mp3"
        outputs = self.engine.merge_wavs(wavs, merged_wav, float(payload.get("fragment_interval", 0.3))) if preview else self.engine.merge_and_encode(wavs, merged_wav, mp3, float(payload.get("fragment_interval", 0.3)))
        manifest_path.write_text(json.dumps({"schema_version": 1, "signature": signature, "segments": segments, "completed": len(segments), "outputs": [str(p) for p in outputs]}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit(request_id, "result", {"progress": 1.0, "preview": preview, "job_dir": str(job_dir), "segments": [str(p) for p in wavs], "outputs": [str(p) for p in outputs]})

    def _prepare_dataset(self, request_id: str, payload: dict[str, Any]) -> None:
        if not payload.get("profile_id") and payload.get("action") == "pipeline":
            raise ValueError("prepare_dataset 缺少 profile_id")
        if payload.get("action") == "pipeline" and not payload.get("source_asset_ids"):
            raise ValueError("请至少选择一个声音库素材")
        if payload.get("dataset_snapshot_id"):
            ensure_within(self.paths.projects_root, Path(str(payload.get("project_path", ""))))
            self._validate_dataset_snapshot(payload)
        try:
            path = self.training.prepare(payload, lambda value, message: self.emit(request_id, "progress", {"progress": value, "message": message}), self.cancel_event)
        except Exception as exc:
            if payload.get("action") == "pipeline" and payload.get("preparation_id"):
                project = ensure_within(self.paths.projects_root, Path(payload["project_path"]))
                manifest = self.training.preparation_paths(project, str(payload["profile_id"]), str(payload["preparation_id"]))["manifest"]
                if manifest.is_file():
                    value = json.loads(manifest.read_text(encoding="utf-8")); value.update({"status": "cancelled" if self.cancel_event.is_set() else "failed", "error": str(exc)}); manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        self.emit(request_id, "result", {"progress": 1.0, "outputs": [str(path)]})

    @staticmethod
    def _validate_dataset_snapshot(payload: dict[str, Any]) -> DatasetManifest:
        dataset = DatasetManifest.from_dict(payload)
        if not dataset.frozen or not dataset.snapshot_sha256:
            raise ValueError("数据集不是有效的冻结快照，请重新冻结")
        if dataset.id != str(payload.get("dataset_snapshot_id", "")):
            raise ValueError("数据集快照 ID 不匹配")
        project = Path(payload.get("project_path", "")).resolve()
        validate_id(dataset.id, legacy=True, field="dataset_snapshot_id")
        validate_sha256(dataset.snapshot_sha256, field="snapshot_sha256")
        snapshot_root = ensure_within(project / "datasets", project / "datasets" / dataset.id)
        audio_root = ensure_within(snapshot_root, snapshot_root / "audio")
        seen: set[str] = set()
        expected_lines: list[str] = []
        for segment in dataset.segments:
            if not segment.human_confirmed or not segment.approved or not segment.included:
                raise ValueError("数据集中存在未经人工确认的片段")
            if not segment.text.strip():
                raise ValueError("数据集中存在空文本")
            if not segment.audio_relative_path:
                raise ValueError("快照仍使用旧绝对路径，请先重新加载项目完成迁移")
            audio = ensure_within(audio_root, project / segment.audio_relative_path)
            if not audio.is_file() or audio.stat().st_size < 44:
                raise ValueError(f"训练音频不存在或无效：{audio}")
            if not segment.source_sha256:
                raise ValueError(f"冻结片段缺少音频哈希，请重新冻结：{audio.name}")
            digest = sha256_file(audio)
            if digest != segment.source_sha256:
                raise ValueError(f"冻结后的音频已被修改，请重新冻结数据集：{audio.name}")
            if digest in seen:
                raise ValueError(f"数据集包含重复片段：{audio.name}")
            seen.add(digest)
            expected_lines.append(f"{segment.audio_relative_path}|speaker|{segment.language}|{segment.text}")
        if dataset_snapshot_sha256(dataset) != dataset.snapshot_sha256:
            raise ValueError("冻结数据集元数据或音频哈希已被修改，请重新冻结")
        if not dataset.list_relative_path:
            raise ValueError("冻结数据集缺少可迁移的相对清单路径")
        list_path = ensure_within(snapshot_root, project / dataset.list_relative_path)
        if not dataset.list_sha256 or not list_path.is_file():
            raise ValueError("冻结数据集标注清单缺失，请重新冻结")
        if sha256_file(list_path) != dataset.list_sha256 or list_path.read_text(encoding="utf-8").splitlines() != expected_lines:
            raise ValueError("冻结数据集标注清单已被修改，请重新冻结")
        can_train, reason = dataset.can_train()
        if not can_train:
            raise ValueError(reason)
        return dataset

    def _train(self, request_id: str, payload: dict[str, Any]) -> None:
        project = ensure_within(self.paths.projects_root, Path(str(payload.get("project_path", ""))))
        validate_id(str(payload.get("profile_id", "")), legacy=True, field="profile_id")
        validate_id(str(payload.get("training_run_id", "")), legacy=True, field="training_run_id")
        project_manifest = project / "project.json"
        stored_profile: dict[str, Any] | None = None
        if project_manifest.is_file():
            project_value = json.loads(project_manifest.read_text(encoding="utf-8"))
            stored_profile = next((item for item in project_value.get("voice_profiles", []) if str(item.get("id")) == str(payload.get("profile_id"))), None)
        if not payload.get("consent_confirmed") or not stored_profile or not stored_profile.get("consent_confirmed"):
            raise ValueError("训练前必须确认声音属于本人或已取得明确授权")
        if not stored_profile.get("consent_confirmed_at") or not stored_profile.get("consent_record"):
            raise ValueError("授权记录不完整，请在声音配置中重新确认")
        if not payload.get("dataset_snapshot_id"):
            raise ValueError("训练必须使用冻结后的 dataset_snapshot_id")
        if payload.get("training_mode", "new") != "new":
            raise ValueError("当前版本只允许明确开始新的微调，不会自动恢复旧训练")
        self._validate_dataset_snapshot(payload)
        readiness = self.engine.readiness()
        if not readiness.get("ready"): raise RuntimeError("GPT-SoVITS 配置或模型文件不完整，请先修复本地引擎")
        health = self.engine.gpu_health()
        if not health.get("compatible"): raise RuntimeError("；".join(health.get("actionable_errors") or ["GPU 工作进程不可用"]))
        try:
            outputs = self.training.train(payload, lambda value, message: self.emit(request_id, "progress", {"progress": value, "message": message}), self.cancel_event)
        except Exception as exc:
            run_manifest = self.training.training_run_root(self.paths.data_root, str(payload.get("training_run_id", ""))) / "training-run.json"
            if run_manifest.is_file():
                value = json.loads(run_manifest.read_text(encoding="utf-8")); value.update({"status": "cancelled" if self.cancel_event.is_set() else "failed", "completed_at": utc_now(), "error": str(exc)}); run_manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        gpt = [str(path) for path in outputs if path.suffix.lower() == ".ckpt" and path.is_file() and path.stat().st_size > 0]
        sovits = [str(path) for path in outputs if path.suffix.lower() == ".pth" and path.is_file() and path.stat().st_size > 0]
        if not gpt or not sovits: raise RuntimeError("训练结束但没有找到真实 GPT 和 SoVITS 检查点")
        self.emit(request_id, "result", {"progress": 1.0, "training_run_id": payload.get("training_run_id", ""), "outputs": [str(path) for path in outputs], "checkpoints": {"gpt": gpt[0], "sovits": sovits[0], "gpt_sha256": sha256_file(Path(gpt[0])), "sovits_sha256": sha256_file(Path(sovits[0])), "origin": "trained-local", "trust_status": "verified"}})

    def _separate_song(self, request_id: str, payload: dict[str, Any]) -> None:
        if str(payload.get("mode", "uvr5")) != "uvr5":
            raise ValueError("当前阶段只支持 UVR5 分离")
        project = ensure_within(self.paths.projects_root, Path(str(payload.get("project_path", ""))))
        # Application service resolves the project-owned source and rights
        # gate before the infrastructure pipeline receives any paths.
        try:
            command = CoverApplicationService(project, paths=self.paths).prepare_separation(str(payload.get("cover_id", "")), mode="uvr5")
        except FileNotFoundError:
            # Keep the low-level worker contract usable for migration probes
            # that exercise dispatch before a CoverProject manifest exists.
            from .cover.application.commands import PrepareSeparationCommand
            command = PrepareSeparationCommand(str(project), str(payload.get("cover_id", "")),
                                                str(payload.get("source_relative_path", "")),
                                                str(payload.get("source_sha256", "")), "uvr5")
        cover_id = str(payload.get("cover_id", ""))
        source_relative_path = str(payload.get("source_relative_path", ""))
        source_sha256 = str(payload.get("source_sha256", ""))
        if not cover_id or not source_relative_path or not source_sha256:
            raise ValueError("separate_song 缺少 cover_id/source_relative_path/source_sha256")
        pipeline = SongSeparationPipeline(project, paths=self.paths)
        self.separation = pipeline
        try:
            result = pipeline.separate(
                command.cover_id,
                command.source_relative_path,
                command.source_sha256,
                cancel=self.cancel_event,
                progress=lambda value, stage, message: self.emit(request_id, "progress", {"progress": value, "stage": stage, "message": message}),
            )
            self.emit(request_id, "result", {"progress": 1.0, **result})
        finally:
            self.separation = None

    def _singing(self, request_id: str) -> SingingPipeline:
        if self.singing_engine is None:
            raise RuntimeError("RVC 歌唱引擎尚未安装或未配置")
        return SingingPipeline(
            self.singing_engine,
            projects_root=self.paths.projects_root,
            paths=self.paths,
            progress=lambda value, message: self.emit(request_id, "progress", {"progress": value, "stage": "singing", "message": message}),
        )

    def _validated_singing_payload(self, payload: dict[str, Any], *, training: bool) -> dict[str, Any]:
        project_value = str(payload.get("project_path", ""))
        if not project_value:
            raise ValueError("缺少 project_path")
        project = ensure_within(self.paths.projects_root, Path(project_value))
        if not project.is_dir() or not (project / "project.json").is_file():
            raise ValueError("项目目录不存在或项目清单缺失")
        forbidden = ({"dataset_dir", "training_dataset_sha256", "checkpoint_path", "index_path"}
                     if training else {"source_path", "model_path", "output_path", "content_origin"})
        supplied = sorted(key for key in forbidden if key in payload)
        if supplied:
            raise ValueError("客户端不得提供受控字段: " + ", ".join(supplied))
        normalized = dict(payload)
        normalized["project_path"] = str(project)
        if training:
            source_ids = normalized.get("source_asset_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise ValueError("歌唱模型训练至少需要一个 SourceAsset")
        return normalized

    def _train_singing_model(self, request_id: str, payload: dict[str, Any]) -> None:
        result = self._singing(request_id).train(self._validated_singing_payload(payload, training=True), cancel=self.cancel_event)
        self.emit(request_id, "result", {"progress": 1.0, "model": result})

    def _convert_vocal(self, request_id: str, payload: dict[str, Any]) -> None:
        result = self._singing(request_id).convert(self._validated_singing_payload(payload, training=False), cancel=self.cancel_event)
        self.emit(request_id, "result", {"progress": 1.0, **result})

    def _validated_cover_payload(self, payload: dict[str, Any], *, export: bool = False) -> dict[str, Any]:
        command = "export_cover" if export else "render_cover"
        normalized = validate_payload(command, payload)
        project_value = str(normalized.get("project_path", ""))
        if not project_value:
            raise ValueError("缺少 project_path")
        project = ensure_within(self.paths.projects_root, Path(project_value))
        if not project.is_dir() or not (project / "project.json").is_file():
            raise ValueError("项目目录不存在或项目清单缺失")
        validate_id(str(normalized.get("cover_id", "")), legacy=True, field="cover_id")
        normalized["project_path"] = str(project)
        return normalized

    def _render_cover(self, request_id: str, payload: dict[str, Any]) -> None:
        value = self._validated_cover_payload(payload)
        raw_settings = value.get("mix_settings") or {}
        if not isinstance(raw_settings, dict):
            raise ValueError("mix_settings 必须是对象")
        app = CoverApplicationService(Path(value["project_path"]), paths=self.paths)
        command = app.prepare_render(str(value["cover_id"]), str(value.get("profile_id", "")), raw_settings)
        settings = command.mix
        ffmpeg = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到受信任的 FFmpeg")
        self.mixer = CoverMixer(self.paths, backend=FFmpegMixBackend(ffmpeg))
        try:
            self.emit(request_id, "progress", {"progress": .05, "stage": "validating", "message": "正在验证混音素材"})
            result = self.mixer.mix(
                Path(command.project_id), command.cover_id, settings,
                profile_id=command.profile_id, model_id=command.singing_model_id,
                cancel=self.cancel_token,
            )
            self.emit(request_id, "result", {"progress": 1.0, "stage": "complete", **result})
        finally:
            self.mixer = None

    def _export_cover(self, request_id: str, payload: dict[str, Any]) -> None:
        value = self._validated_cover_payload(payload, export=True)
        app = CoverApplicationService(Path(value["project_path"]), paths=self.paths)
        command = app.prepare_export(
            str(value["cover_id"]), final_asset_id=str(value.get("final_asset_id", "")),
            format=str(value.get("format", "wav")), file_name=str(value.get("file_name", "")),
            destination=Path(str(value.get("destination", ""))),
            existing_policy=str(value.get("existing_policy", "reject")),
            publication_rights_acknowledged=bool(value.get("publication_rights_acknowledged", False)),
        )
        ffmpeg = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到受信任的 FFmpeg")
        self.exporter = CoverExporter(self.paths, backend=FFmpegExportBackend(ffmpeg))
        try:
            self.emit(request_id, "progress", {"progress": .05, "stage": "validating", "message": "正在验证导出信息"})
            result = self.exporter.export(
                Path(command.project_id), command.cover_id,
                format=command.format, destination=command.destination,
                file_name=command.file_name, final_asset_id=command.final_asset_id,
                existing=command.existing_policy, cancel=self.cancel_token,
                publication_rights_ack=command.publication_rights_acknowledged,
            )
            self.emit(request_id, "result", {"progress": 1.0, "stage": "complete", **result})
        finally:
            self.exporter = None


def main() -> int:
    # QProcess and direct command-line launches must share one wire encoding,
    # regardless of the active Windows ANSI code page.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    service = WorkerService()
    service.emit("worker", "ready", {"version": 1, "commands": sorted(COMMANDS)})
    # QProcess can transiently report EOF on the write channel while a
    # background GPU task is still running (notably after a native UVR child
    # closes an inherited Windows pipe handle).  Treat an empty read as an
    # idle interval, not as an implicit shutdown; only the explicit shutdown
    # command is allowed to terminate the worker.
    while not service.shutdown_event.is_set():
        line = sys.stdin.readline()
        if not line:
            import time
            time.sleep(0.05)
            continue
        try:
            service.handle(Message.decode(line))
        except Exception as exc:
            service.emit("worker", "error", {"message": str(exc), "exception": type(exc).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
