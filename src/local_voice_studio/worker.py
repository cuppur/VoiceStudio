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

from .engine import GptSovitsEngine
from .audio import sha256_file
from .models import DatasetManifest, dataset_snapshot_sha256
from .paths import AppPaths
from .protocol import COMMANDS, Message
from .text import split_text
from .training import TrainingPipeline


class WorkerService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.default()
        self.paths.ensure()
        self.engine = GptSovitsEngine(self.paths)
        self.training = TrainingPipeline(self.paths)
        self.profile: dict[str, Any] | None = None
        self.current_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.write_lock = threading.Lock()
        self.shutdown_event = threading.Event()

    def emit(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"id": request_id, "type": event, "payload": payload}, ensure_ascii=False)
        with self.write_lock:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    def handle(self, message: Message) -> None:
        if message.type not in COMMANDS:
            self.emit(message.id, "error", {"message": f"未知命令: {message.type}"})
            return
        if message.type == "health":
            health = self.engine.gpu_health()
            self.emit(message.id, "result", {**health, "worker_python": sys.executable, "pid": os.getpid(), "engine": self.engine.readiness(), "gpu": health})
        elif message.type == "load_profile":
            self.profile = message.payload
            try:
                self.engine.load(self.profile, bool(message.payload.get("force_cpu", False)))
                self.emit(message.id, "result", {"loaded": True, "profile_id": self.profile.get("id", "")})
            except Exception as exc:
                self.emit(message.id, "error", {"message": str(exc), "exception": type(exc).__name__})
        elif message.type == "cancel":
            self.cancel_event.set()
            self.engine.stop()
            self.training.cancel()
            self.emit(message.id, "result", {"cancel_requested": True})
        elif message.type == "shutdown":
            self.cancel_event.set()
            self.engine.stop()
            self.training.cancel()
            self.shutdown_event.set()
            self.emit(message.id, "result", {"shutdown": True})
        else:
            if self.current_thread and self.current_thread.is_alive():
                self.emit(message.id, "error", {"message": "已有 GPU 任务正在运行"})
                return
            self.cancel_event.clear()
            target = {
                "synthesize": self._synthesize,
                "prepare_dataset": self._prepare_dataset,
                "train": self._train,
            }[message.type]
            self.current_thread = threading.Thread(target=self._run_guarded, args=(message.id, target, message.payload), daemon=True)
            self.current_thread.start()

    def _run_guarded(self, request_id: str, target, payload: dict[str, Any]) -> None:
        try:
            target(request_id, payload)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            event = "cancelled" if self.cancel_event.is_set() else "failed"
            self.emit(request_id, "error", {"message": str(exc), "exception": type(exc).__name__, "status": event})

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
        job_dir = Path(resume_dir).resolve() if resume_dir else output_root / ("preview-" + request_id[:12] if preview else datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + request_id[:8])
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
            self._validate_dataset_snapshot(payload)
        path = self.training.prepare(payload, lambda value, message: self.emit(request_id, "progress", {"progress": value, "message": message}), self.cancel_event)
        self.emit(request_id, "result", {"progress": 1.0, "outputs": [str(path)]})

    @staticmethod
    def _validate_dataset_snapshot(payload: dict[str, Any]) -> DatasetManifest:
        dataset = DatasetManifest.from_dict(payload)
        if not dataset.frozen or not dataset.snapshot_sha256:
            raise ValueError("数据集不是有效的冻结快照，请重新冻结")
        if dataset.id != str(payload.get("dataset_snapshot_id", "")):
            raise ValueError("数据集快照 ID 不匹配")
        seen: set[str] = set()
        expected_lines: list[str] = []
        for segment in dataset.segments:
            if not segment.human_confirmed or not segment.approved or not segment.included:
                raise ValueError("数据集中存在未经人工确认的片段")
            if not segment.text.strip():
                raise ValueError("数据集中存在空文本")
            audio = Path(segment.audio_path)
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
            expected_lines.append(f"{audio}|speaker|{segment.language}|{segment.text}")
        if dataset_snapshot_sha256(dataset) != dataset.snapshot_sha256:
            raise ValueError("冻结数据集元数据或音频哈希已被修改，请重新冻结")
        list_path = Path(dataset.list_path)
        if not dataset.list_sha256 or not list_path.is_file():
            raise ValueError("冻结数据集标注清单缺失，请重新冻结")
        if sha256_file(list_path) != dataset.list_sha256 or list_path.read_text(encoding="utf-8").splitlines() != expected_lines:
            raise ValueError("冻结数据集标注清单已被修改，请重新冻结")
        can_train, reason = dataset.can_train()
        if not can_train:
            raise ValueError(reason)
        return dataset

    def _train(self, request_id: str, payload: dict[str, Any]) -> None:
        if not payload.get("consent_confirmed"):
            raise ValueError("训练前必须确认声音属于本人或已取得明确授权")
        if not payload.get("dataset_snapshot_id"):
            raise ValueError("训练必须使用冻结后的 dataset_snapshot_id")
        self._validate_dataset_snapshot(payload)
        readiness = self.engine.readiness()
        if not readiness.get("ready"): raise RuntimeError("GPT-SoVITS 配置或模型文件不完整，请先修复本地引擎")
        health = self.engine.gpu_health()
        if not health.get("compatible"): raise RuntimeError("；".join(health.get("actionable_errors") or ["GPU 工作进程不可用"]))
        outputs = self.training.train(payload, lambda value, message: self.emit(request_id, "progress", {"progress": value, "message": message}), self.cancel_event)
        gpt = [str(path) for path in outputs if path.suffix.lower() == ".ckpt" and path.is_file() and path.stat().st_size > 0]
        sovits = [str(path) for path in outputs if path.suffix.lower() == ".pth" and path.is_file() and path.stat().st_size > 0]
        if not gpt or not sovits: raise RuntimeError("训练结束但没有找到真实 GPT 和 SoVITS 检查点")
        self.emit(request_id, "result", {"progress": 1.0, "outputs": [str(path) for path in outputs], "checkpoints": {"gpt": gpt[0], "sovits": sovits[0]}})


def main() -> int:
    # QProcess and direct command-line launches must share one wire encoding,
    # regardless of the active Windows ANSI code page.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    service = WorkerService()
    service.emit("worker", "ready", {"version": 1, "commands": sorted(COMMANDS)})
    for line in sys.stdin:
        if service.shutdown_event.is_set():
            break
        try:
            service.handle(Message.decode(line))
        except Exception as exc:
            service.emit("worker", "error", {"message": str(exc), "exception": type(exc).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
