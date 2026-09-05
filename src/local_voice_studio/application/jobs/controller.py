"""Bridge a CoverPipeline to a request sender and event stream."""
from __future__ import annotations
from typing import Any, Callable
from .pipeline import CoverPipeline
from . import ProductJob

class CoverPipelineController:
    def __init__(self, job: ProductJob, send: Callable[[str, dict[str, Any]], str], persist: Callable[[ProductJob], None] | None = None):
        self.pipeline=CoverPipeline.create(job, send); self.persist=persist or (lambda _job: None)
    @property
    def job(self): return self.pipeline.job
    def complete_local(self, stage: str, outputs: list[str] | None = None) -> str | None:
        result=self.pipeline.complete_local_stage(stage, outputs); self.persist(self.job); return result
    def handle(self, request_id: str, event: str, payload: dict[str, Any]) -> str | None:
        stage=next((name for name,rid in self.pipeline.request_ids.items() if rid==request_id), None)
        if stage is None: return None
        if event == "result":
            outputs=payload.get("outputs") or payload.get("output_path") or payload.get("output_paths") or []
            if isinstance(outputs,str): outputs=[outputs]
            result=self.pipeline.accept_result(stage, list(outputs) if isinstance(outputs,list) else [])
        elif event == "error":
            self.pipeline.accept_error(stage, str(payload.get("message", "Worker 失败")), bool(payload.get("recoverable", True))); result=None
        else: return stage
        self.persist(self.job); return result
