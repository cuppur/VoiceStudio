"""Text-to-speech service (two-step load_profile → synthesize, cancellable)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ....models import GenerationRecord, Job, JobKind, JobStatus
from .base import WebService


class TtsService(WebService):
    """Real text generation through the existing worker contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ state
    def usable_profiles(self) -> list[dict[str, Any]]:
        """Voices that can actually synthesize right now."""
        rows: list[dict[str, Any]] = []
        for profile in self.store.list_profiles(self.project):
            if profile.archived or not profile.consent_confirmed or not profile.consent_record or not profile.consent_confirmed_at:
                continue
            if not profile.active_gpt_checkpoint or not profile.active_sovits_checkpoint:
                continue
            if not Path(profile.active_gpt_checkpoint).is_file() or not Path(profile.active_sovits_checkpoint).is_file():
                continue
            reference = self.reference_for(profile)
            rows.append({
                "id": profile.id,
                "name": profile.name,
                "ready": bool(reference),
                "reason": "" if reference else "缺少已审核的参考音频与文本",
            })
        return rows

    @staticmethod
    def reference_for(profile) -> Any:
        return next((item for item in profile.reference_assets
                     if item.approved and item.transcript.strip() and Path(item.path).is_file()), None)

    def default_output_dir(self) -> str:
        return str(self.store.get_setting("default_output_dir", str(self.project / "exports")))

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        names = {item.id: item.name for item in self.store.list_profiles(self.project)}
        rows: list[dict[str, Any]] = []
        for job in self.store.list_jobs(200):
            if job.kind != JobKind.SYNTHESIZE:
                continue
            profile_id = str(job.payload.get("profile_id", ""))
            if profile_id not in names:
                continue
            outputs = [str(item) for item in job.outputs]
            rows.append({
                "id": job.id,
                "voice": names[profile_id],
                "text": str(job.payload.get("text", ""))[:80],
                "status": job.status.value,
                "outputs": outputs,
                "exists": bool(outputs) and all(Path(item).is_file() for item in outputs),
                "error": str(job.error or ""),
                "created_text": str(job.updated_at)[:16].replace("T", " "),
            })
            if len(rows) >= limit:
                break
        return rows

    # ---------------------------------------------------------------- actions
    def generate(self, profile_id: str, text: str, *, speed: float = 1.0, pause: float = 0.3,
                 seed: int = -1, language: str = "zh", output_dir: str = "",
                 max_chars: int = 120) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("本地工作进程未连接，无法生成语音")
        if self._pending:
            raise ValueError("已有生成任务正在执行，请等待或取消当前任务")
        body = str(text or "").strip()
        if not body:
            raise ValueError("请输入要生成的文字")
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == str(profile_id)), None)
        if profile is None or profile.archived:
            raise ValueError("声音不存在或已归档")
        if not profile.consent_confirmed or not profile.consent_record.strip() or not profile.consent_confirmed_at:
            raise ValueError("声音授权已失效，请重新确认素材与声音授权")
        if not profile.active_gpt_checkpoint or not profile.active_sovits_checkpoint:
            raise ValueError("声音尚未完成训练，请先训练或恢复模型版本")
        if not Path(profile.active_gpt_checkpoint).is_file() or not Path(profile.active_sovits_checkpoint).is_file():
            raise ValueError("声音模型文件已缺失，请重新训练或恢复版本")
        reference = self.reference_for(profile)
        if reference is None:
            raise ValueError("声音缺少已审核的参考音频与文本，请追加或修复素材")
        target = Path(output_dir).resolve() if str(output_dir).strip() else Path(self.default_output_dir())
        target.mkdir(parents=True, exist_ok=True)

        synthesis = {
            "text": body, "text_lang": str(language or "zh"), "ref_audio_path": reference.path,
            "prompt_text": reference.transcript, "prompt_lang": reference.language,
            "output_dir": str(target), "speed_factor": float(speed), "fragment_interval": float(pause),
            "seed": int(seed), "max_chars": int(max_chars), "profile_id": profile.id,
        }
        job = Job(JobKind.SYNTHESIZE, synthesis)
        self.store.save_job(job)
        payload = profile.to_dict()
        payload["project_path"] = str(self.project)
        request_id = self.client.send("load_profile", payload)
        self._pending[str(request_id)] = {"stage": "load", "job": job, "synthesis": synthesis}
        job.payload["active_request_id"] = request_id
        self.store.save_job(job)
        self.notify("job.started", {
            "request_id": str(request_id), "job_id": job.id, "kind": "tts",
            "stage": "load", "title": f"{profile.name} · {body[:16]}",
        })
        return {"request_id": str(request_id), "job_id": job.id}

    def cancel(self, request_id: str = "") -> dict[str, Any]:
        if not self._pending:
            raise ValueError("没有正在执行的生成任务")
        target = str(request_id) or next(iter(self._pending))
        info = self._pending.get(target)
        if info is None:
            raise ValueError("找不到该生成任务")
        if info["stage"] != "load":
            self.client.send("cancel", {"target_request_id": target})
        info["cancelled"] = True
        self.notify("job.cancelling", {"request_id": target})
        return {"request_id": target}

    # ------------------------------------------------------------------ events
    def handle_worker_event(self, request_id: str, event: str, payload: dict[str, Any]) -> bool:
        info = self._pending.get(str(request_id))
        if info is None:
            return False
        job: Job = info["job"]
        if event == "progress":
            job.status = JobStatus.RUNNING
            job.progress = float(payload.get("progress", 0) or 0)
            job.message = str(payload.get("message", ""))
            self.store.save_job(job)
            self.notify("job.progress", {
                "request_id": str(request_id), "job_id": job.id, "kind": "tts",
                "stage": info["stage"], "percent": round(job.progress * 100, 1), "message": job.message,
            })
            return True
        if event == "error":
            self._pending.pop(str(request_id), None)
            cancelled = bool(payload.get("cancelled")) or payload.get("status") == "cancelled" or info.get("cancelled")
            job.status = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
            job.error = str(payload.get("message", ""))
            self.store.save_job(job)
            self.notify("job.error" if not cancelled else "job.cancelled", {
                "request_id": str(request_id), "job_id": job.id, "kind": "tts",
                "message": "已取消" if cancelled else job.error,
            })
            return True
        if event != "result":
            return True
        self._pending.pop(str(request_id), None)
        if info["stage"] == "load":
            if info.get("cancelled"):
                job.status = JobStatus.CANCELLED
                self.store.save_job(job)
                self.notify("job.cancelled", {"request_id": str(request_id), "job_id": job.id, "kind": "tts", "message": "已取消"})
                return True
            try:
                next_request = self.client.send("synthesize", info["synthesis"])
            except Exception as exc:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = self.translate_error(exc)
                self.store.save_job(job)
                self.notify("job.error", {"request_id": str(request_id), "job_id": job.id, "kind": "tts", "message": job.error})
                return True
            self._pending[str(next_request)] = {"stage": "synth", "job": job, "synthesis": info["synthesis"]}
            job.payload["active_request_id"] = next_request
            self.store.save_job(job)
            self.notify("job.progress", {
                "request_id": str(next_request), "job_id": job.id, "kind": "tts",
                "stage": "synth", "percent": 0, "message": "声音模型已加载，开始合成",
            })
            return True

        outputs = [str(item) for item in payload.get("outputs", [])]
        if not outputs or any(not Path(item).is_file() or Path(item).stat().st_size == 0 for item in outputs):
            job.status = JobStatus.FAILED
            job.error = "生成输出不存在或为空，未标记为成功"
            self.store.save_job(job)
            self.notify("job.error", {"request_id": str(request_id), "job_id": job.id, "kind": "tts", "message": job.error})
            return True
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.outputs = outputs
        self.store.save_job(job)
        wav = next((item for item in outputs if item.lower().endswith(".wav")), "")
        mp3 = next((item for item in outputs if item.lower().endswith(".mp3")), "")
        record = GenerationRecord(
            project_uid=self.store.load_project(self.project)["project_uid"],
            voice_profile_id=str(job.payload.get("profile_id", "")),
            text=str(job.payload.get("text", "")),
            parameters={key: value for key, value in job.payload.items() if key != "active_request_id"},
            wav_path=wav, mp3_path=mp3, status="completed", id=job.id,
        )
        self.store.save_generation_record(self.project, record)
        self.notify("job.result", {
            "request_id": str(request_id), "job_id": job.id, "kind": "tts",
            "outputs": outputs, "wav": wav, "mp3": mp3,
            "data": {"outputs": outputs, "wav": wav, "mp3": mp3},
        })
        return True

    def close(self) -> None:
        self._pending.clear()
        super().close()
