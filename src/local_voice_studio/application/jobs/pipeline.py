"""Declarative AI-cover orchestration independent of the desktop UI."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable
from . import ProductJob, ProductJobStatus

STAGES = ("analyze", "waveform", "separation", "lyrics", "voice_conversion", "post_process", "mix", "export")
COMMANDS = {"analyze": "health", "waveform": "health", "separation": "separate_song", "lyrics": "transcribe_lyrics", "voice_conversion": "convert_vocal", "post_process": "cleanup_vocal", "mix": "render_cover", "export": "export_cover"}

@dataclass
class CoverPipeline:
    job: ProductJob
    send: Callable[[str, dict[str, Any]], str]
    request_ids: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, job: ProductJob, send: Callable[[str, dict[str, Any]], str]) -> "CoverPipeline":
        if job.kind != "ai_cover": raise ValueError("仅支持 ai_cover")
        return cls(job, send)

    def next_stage(self) -> str | None:
        ready = self.job.ready_stages()
        return ready[0].name if ready else None

    def dispatch_next(self) -> str | None:
        stage = self.next_stage()
        if stage is None: return None
        command = COMMANDS[stage]
        payload = dict(self.job.payload)
        payload.update({"product_job_id": self.job.id, "stage": stage})
        request_id = self.send(command, payload)
        self.request_ids[stage] = request_id
        self.job.stage(stage).status = ProductJobStatus.PREPARING
        self.job.current_stage = stage
        self.job.status = ProductJobStatus.RUNNING
        return stage

    def accept_result(self, stage: str, outputs: list[str] | None = None) -> str | None:
        self.job.mark_stage_succeeded(stage, outputs)
        return self.dispatch_next()

    def accept_error(self, stage: str, message: str, recoverable: bool = True) -> None:
        item = self.job.stage(stage); item.error = message
        self.job.error = message; self.job.status = ProductJobStatus.RECOVERABLE if recoverable else ProductJobStatus.FAILED
