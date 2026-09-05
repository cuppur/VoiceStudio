"""Product-level job state and stage dependency primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from datetime import datetime, timezone

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductJobStatus(str, Enum):
    QUEUED = "queued"
    WAITING_DEPENDENCY = "waiting_dependency"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECOVERABLE = "recoverable"
    SUCCEEDED = "succeeded"
    PUBLISHED = "published"


@dataclass
class JobStage:
    name: str
    dependencies: list[str] = field(default_factory=list)
    status: ProductJobStatus = ProductJobStatus.QUEUED
    progress: float = 0.0
    error: str = ""
    outputs: list[str] = field(default_factory=list)


@dataclass
class ProductJob:
    kind: str
    payload: dict[str, Any]
    stages: list[JobStage]
    id: str = field(default_factory=lambda: uuid4().hex)
    status: ProductJobStatus = ProductJobStatus.QUEUED
    current_stage: str = ""
    progress: float = 0.0
    error: str = ""
    outputs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def stage(self, name: str) -> JobStage:
        for item in self.stages:
            if item.name == name:
                return item
        raise KeyError(name)

    def ready_stages(self) -> list[JobStage]:
        ready: list[JobStage] = []
        for item in self.stages:
            if item.status != ProductJobStatus.QUEUED:
                continue
            if all(self.stage(dep).status in {ProductJobStatus.SUCCEEDED, ProductJobStatus.PUBLISHED} for dep in item.dependencies):
                ready.append(item)
        return ready

    def mark_stage_running(self, name: str) -> None:
        item = self.stage(name)
        if item not in self.ready_stages():
            raise ValueError(f"任务阶段依赖未完成: {name}")
        item.status = ProductJobStatus.RUNNING
        self.current_stage = name
        self.status = ProductJobStatus.RUNNING
        self.updated_at = utc_now()

    def mark_stage_succeeded(self, name: str, outputs: list[str] | None = None) -> None:
        item = self.stage(name)
        if item.status not in {ProductJobStatus.RUNNING, ProductJobStatus.PREPARING}:
            raise ValueError(f"任务阶段未运行: {name}")
        item.status = ProductJobStatus.SUCCEEDED
        item.progress = 1.0
        item.outputs = list(outputs or [])
        self.progress = sum(stage.progress for stage in self.stages) / max(1, len(self.stages))
        if all(stage.status in {ProductJobStatus.SUCCEEDED, ProductJobStatus.PUBLISHED} for stage in self.stages):
            self.status = ProductJobStatus.SUCCEEDED
            self.current_stage = ""
        self.updated_at = utc_now()

    def cancel(self) -> None:
        if self.status in {ProductJobStatus.SUCCEEDED, ProductJobStatus.PUBLISHED, ProductJobStatus.CANCELLED}:
            return
        self.status = ProductJobStatus.CANCELLING
        self.updated_at = utc_now()

    def mark_cancelled(self) -> None:
        self.status = ProductJobStatus.CANCELLED
        if self.current_stage:
            self.stage(self.current_stage).status = ProductJobStatus.CANCELLED
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "payload": dict(self.payload),
            "stages": [{"name": item.name, "dependencies": list(item.dependencies), "status": item.status.value, "progress": item.progress, "error": item.error, "outputs": list(item.outputs)} for item in self.stages],
            "status": self.status.value, "current_stage": self.current_stage, "progress": self.progress,
            "error": self.error, "outputs": list(self.outputs), "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProductJob":
        stages = [JobStage(name=str(item["name"]), dependencies=list(item.get("dependencies", [])), status=ProductJobStatus(item.get("status", "queued")), progress=float(item.get("progress", 0)), error=str(item.get("error", "")), outputs=list(item.get("outputs", []))) for item in value.get("stages", [])]
        return cls(kind=str(value.get("kind", "")), payload=dict(value.get("payload", {})), stages=stages, id=str(value.get("id", uuid4().hex)), status=ProductJobStatus(value.get("status", "queued")), current_stage=str(value.get("current_stage", "")), progress=float(value.get("progress", 0)), error=str(value.get("error", "")), outputs=list(value.get("outputs", [])), created_at=str(value.get("created_at", utc_now())), updated_at=str(value.get("updated_at", utc_now())))


