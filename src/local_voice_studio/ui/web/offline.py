"""Offline worker stand-in for visual verification and contract tests.

It has the same signals as :class:`~local_voice_studio.ui.worker_client.WorkerClient`
but never starts a process and never reports a task as successful, so screenshots
and DOM probes can run without touching the GPU.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from uuid import uuid4


class OfflineWorkerClient(QObject):
    """Worker-shaped adapter that reports processing is unavailable."""

    event = Signal(str, str, dict)
    stderr_line = Signal(str)
    state_changed = Signal(str)
    ready_changed = Signal(bool)
    request_started = Signal(str, str)
    request_finished = Signal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.ready = False
        self.pending: dict[str, str] = {}

    def start(self) -> None:
        QTimer.singleShot(0, lambda: self.state_changed.emit("离线预览 · 未连接本地 Worker"))

    def shutdown(self) -> None:
        self.pending.clear()

    def restart(self) -> None:
        self.start()

    def send(self, command: str, payload: dict | None = None, request_id: str | None = None) -> str:
        request_id = request_id or f"offline-{uuid4().hex}"
        self.pending[request_id] = command
        self.request_started.emit(request_id, command)

        def report() -> None:
            self.pending.pop(request_id, None)
            self.request_finished.emit(request_id, command)
            self.event.emit(request_id, "error", {
                "status": "offline_preview", "command": command,
                "message": "离线预览不执行本地模型或音频任务。",
            })

        QTimer.singleShot(120, report)
        return request_id

    def attach_pipeline_controller(self, _controller: object) -> None:
        return

    def detach_pipeline_controller(self, _controller: object) -> None:
        return

    def retry_product_job(self, _job_id: str) -> str:
        raise RuntimeError("离线预览不重试任务；请在实际运行中操作。")
