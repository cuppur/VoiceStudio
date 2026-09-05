"""Single-GPU scheduler for product jobs.

The scheduler owns execution order, while Worker/engines own model lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Callable, Any
from uuid import uuid4

class SchedulerStatus(str, Enum):
    QUEUED="queued"; RUNNING="running"; CANCELLING="cancelling"; SUCCEEDED="succeeded"; CANCELLED="cancelled"; RECOVERABLE="recoverable"; FAILED="failed"

@dataclass
class GpuTask:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: SchedulerStatus = SchedulerStatus.QUEUED
    error: str = ""
    result: Any = None

class GpuJobScheduler:
    def __init__(self, runner: Callable[[GpuTask, Callable[[], bool]], Any], *, max_history: int = 200):
        self.runner=runner; self.max_history=max(1,int(max_history)); self._queue:list[GpuTask]=[]; self._active:GpuTask|None=None; self._history:list[GpuTask]=[]; self._cancel=False; self._lock=RLock()
    def submit(self, kind: str, payload: dict[str, Any]|None=None) -> GpuTask:
        task=GpuTask(kind,dict(payload or {}))
        with self._lock: self._queue.append(task); self._start_next_locked()
        return task
    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if self._active and self._active.id==task_id:
                self._active.status=SchedulerStatus.CANCELLING; self._cancel=True; return True
            for task in self._queue:
                if task.id==task_id: task.status=SchedulerStatus.CANCELLED; self._queue.remove(task); self._record_locked(task); return True
        return False
    def active(self) -> GpuTask|None:
        with self._lock: return self._active
    def queued(self) -> list[GpuTask]:
        with self._lock: return list(self._queue)
    def history(self) -> list[GpuTask]:
        with self._lock: return list(self._history)
    def _start_next_locked(self) -> None:
        if self._active or not self._queue: return
        task=self._queue.pop(0); self._active=task; task.status=SchedulerStatus.RUNNING; self._cancel=False
        import threading
        threading.Thread(target=self._run,args=(task,),daemon=True).start()
    def _run(self, task: GpuTask) -> None:
        try:
            task.result=self.runner(task, lambda: self._cancel)
            task.status=SchedulerStatus.CANCELLED if self._cancel else SchedulerStatus.SUCCEEDED
        except Exception as exc:
            task.error=str(exc); task.status=SchedulerStatus.CANCELLED if self._cancel else SchedulerStatus.RECOVERABLE
        finally:
            with self._lock:
                self._record_locked(task); self._active=None; self._start_next_locked()
    def _record_locked(self, task: GpuTask) -> None:
        self._history.append(task); del self._history[:-self.max_history]
