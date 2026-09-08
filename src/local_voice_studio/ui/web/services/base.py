"""Headless service layer for the HTML shell.

Services own the real workflow orchestration (worker commands, product jobs,
state validation) and never import ``QtWidgets``.  They talk to the page only
through ``event(name, json)``; the bridge forwards those events to JavaScript.
This keeps the classic Qt pages and the HTML shell on one implementation of the
business rules while the Qt pages are retired.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ....application.jobs.coordinator import JobCoordinator
from ....paths import AppPaths
from ....storage import StudioStore


class WebService(QObject):
    """Base class: task tracking, worker event routing and error translation."""

    event = Signal(str, str)

    def __init__(self, paths: AppPaths, store: StudioStore, project: Path, client, parent: QObject | None = None):
        super().__init__(parent)
        self.paths = paths
        self.store = store
        self.project = Path(project)
        self.client = client
        self.coordinator = JobCoordinator(store)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._closing = False

    # ------------------------------------------------------------------
    def set_project(self, project: Path) -> None:
        self.project = Path(project)

    def close(self) -> None:
        self._closing = True
        self._tasks.clear()

    def notify(self, name: str, payload: dict[str, Any] | None = None) -> None:
        if not self._closing:
            self.event.emit(str(name), json.dumps(payload or {}, ensure_ascii=False))

    # ------------------------------------------------------------------
    def start_task(self, command: str, payload: dict[str, Any], *, kind: str, stage: str,
                   title: str, cover_id: str = "", profile_id: str = "",
                   on_result: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Send one worker command and register it as a persisted product job."""
        if self.client is None:
            raise RuntimeError("本地工作进程未连接，无法执行该任务")
        job = self.coordinator.start(kind, {**payload, "title": title}, [stage])
        request_id = self.client.send(command, payload)
        self._tasks[str(request_id)] = {
            "job_id": job.id, "kind": kind, "stage": stage, "title": title,
            "cover_id": cover_id, "profile_id": profile_id, "on_result": on_result,
        }
        job.payload["active_request_id"] = request_id
        self.store.save_product_job(job)
        self.notify("job.started", {
            "request_id": str(request_id), "job_id": job.id, "kind": kind,
            "stage": stage, "title": title, "cover_id": cover_id,
        })
        return {"request_id": str(request_id), "job_id": job.id}

    def cancel_task(self, request_id: str = "") -> dict[str, Any]:
        target = str(request_id or "")
        if not target:
            target = next(iter(self._tasks), "")
        if not target:
            raise ValueError("没有正在执行的本地任务")
        if self.client is None:
            raise RuntimeError("本地工作进程未连接")
        self.client.send("cancel", {"target_request_id": target})
        info = self._tasks.get(target)
        if info:
            self.coordinator.mark_cancelling(info["job_id"])
        self.notify("job.cancelling", {"request_id": target})
        return {"request_id": target}

    # ------------------------------------------------------------------
    def handle_worker_event(self, request_id: str, event: str, payload: dict[str, Any]) -> bool:
        """Return True when this service owns the request."""
        info = self._tasks.get(str(request_id))
        if info is None:
            return False
        job_id = info["job_id"]
        stage = info["stage"]
        if event == "progress":
            percent = float(payload.get("progress", 0) or 0)
            message = str(payload.get("message", ""))
            # The job tracks the registered stage; the worker's finer-grained
            # stage name is forwarded to the page only.
            self.coordinator.handle_progress(job_id, stage, percent, message)
            self.notify("job.progress", {
                "request_id": str(request_id), "job_id": job_id, "kind": info["kind"],
                "stage": str(payload.get("stage") or stage), "percent": round(percent * 100, 1),
                "message": message, "cover_id": info["cover_id"],
            })
        elif event == "result":
            self._tasks.pop(str(request_id), None)
            self.coordinator.handle_result(job_id, stage, list(payload.get("outputs") or []))
            self.notify("job.result", {
                "request_id": str(request_id), "job_id": job_id, "kind": info["kind"],
                "stage": stage, "title": info["title"], "cover_id": info["cover_id"],
                "data": payload,
            })
            callback = info.get("on_result")
            if callback:
                callback(payload)
        elif event == "error":
            self._tasks.pop(str(request_id), None)
            message = str(payload.get("message", "本地任务失败"))
            self.coordinator.handle_error(job_id, message)
            self.notify("job.error", {
                "request_id": str(request_id), "job_id": job_id, "kind": info["kind"],
                "stage": stage, "message": message, "cover_id": info["cover_id"],
            })
        return True

    def active_request(self, cover_id: str = "", kind: str = "") -> str:
        for request_id, info in self._tasks.items():
            if cover_id and info.get("cover_id") != cover_id:
                continue
            if kind and info.get("kind") != kind:
                continue
            return request_id
        return ""

    # ------------------------------------------------------------------
    @staticmethod
    def translate_error(exc: Exception) -> str:
        """Turn domain/OS errors into actionable Chinese text."""
        text = str(exc).strip() or type(exc).__name__
        lowered = text.lower()
        if "out of memory" in lowered or "cuda" in lowered and "memory" in lowered:
            return "显存不足：请关闭其它 GPU 程序，或在设置中降低精度后重试。"
        if "no space" in lowered or "disk" in lowered and "full" in lowered:
            return "磁盘空间不足：请清理缓存或更换输出目录。"
        return text
