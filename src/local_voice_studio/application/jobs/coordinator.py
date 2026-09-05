"""Persistence bridge between Worker JSONL events and ProductJob state."""
from __future__ import annotations

from typing import Any

from ...storage import StudioStore
from . import JobStage, ProductJob, ProductJobStatus

DEFAULT_STAGES = ("analyze", "waveform", "separation", "lyrics", "voice_conversion", "post_process", "mix", "export")


class JobCoordinator:
    def __init__(self, store: StudioStore):
        self.store = store
        self.jobs: dict[str, ProductJob] = {}

    def start(self, kind: str, payload: dict[str, Any], stages: list[str] | None = None) -> ProductJob:
        names = stages or list(DEFAULT_STAGES if kind == "ai_cover" else (kind,))
        stage_objects = [JobStage(name, [names[index - 1]] if index else []) for index, name in enumerate(names)]
        job = ProductJob(kind, dict(payload), stage_objects)
        self.jobs[job.id] = job
        self.store.save_product_job(job)
        return job

    def handle_progress(self, job_id: str, stage: str, progress: float, message: str = "") -> ProductJob:
        job = self._get(job_id)
        item = job.stage(stage)
        if item.status == ProductJobStatus.QUEUED:
            job.mark_stage_running(stage)
        item.progress = max(0.0, min(1.0, float(progress)))
        if message:
            item.error = ""  # progress messages are not failure state
        job.progress = sum(stage_item.progress for stage_item in job.stages) / max(1, len(job.stages))
        self.store.save_product_job(job)
        return job

    def handle_result(self, job_id: str, stage: str, outputs: list[str] | None = None) -> ProductJob:
        job = self._get(job_id)
        item = job.stage(stage)
        if item.status == ProductJobStatus.QUEUED:
            job.mark_stage_running(stage)
        job.mark_stage_succeeded(stage, outputs)
        self.store.save_product_job(job)
        return job

    def handle_error(self, job_id: str, error: str, recoverable: bool = True) -> ProductJob:
        job = self._get(job_id)
        job.error = str(error)
        job.status = ProductJobStatus.RECOVERABLE if recoverable else ProductJobStatus.FAILED
        if job.current_stage:
            job.stage(job.current_stage).error = str(error)
        self.store.save_product_job(job)
        return job

    def _get(self, job_id: str) -> ProductJob:
        job = self.jobs.get(job_id)
        if job is None:
            job = self.store.load_product_job(job_id)
            self.jobs[job_id] = job
        return job
