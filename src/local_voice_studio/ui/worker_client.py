from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ..paths import AppPaths
from ..runtime import EngineRuntimeResolver


class WorkerClient(QObject):
    event = Signal(str, str, dict)
    stderr_line = Signal(str)
    state_changed = Signal(str)
    ready_changed = Signal(bool)
    request_started = Signal(str, str)
    request_finished = Signal(str, str)

    def __init__(self, paths: AppPaths, parent: QObject | None = None):
        super().__init__(parent)
        self.paths = paths
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._stdout)
        self.process.readyReadStandardError.connect(self._stderr)
        self.process.started.connect(self._started)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._buffer = b""
        self.ready = False
        self.pending: dict[str, str] = {}

    def _diagnostic(self, message: str) -> None:
        try:
            self.paths.logs_root.mkdir(parents=True, exist_ok=True)
            with (self.paths.logs_root / "worker-client.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now().isoformat()} {message}\n")
        except OSError:
            pass

    def start(self) -> None:
        if self.process.state() != QProcess.NotRunning:
            return
        try:
            launch = EngineRuntimeResolver(self.paths).worker_launch()
        except Exception as exc:
            self._diagnostic(str(exc))
            self.state_changed.emit(str(exc))
            return
        env = QProcessEnvironment.systemEnvironment()
        source_root = str(launch.source_root)
        old_pythonpath = env.value("PYTHONPATH")
        env.insert("PYTHONPATH", source_root + (os.pathsep + old_pythonpath if old_pythonpath else ""))
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)
        self.process.setProgram(str(launch.program))
        self.process.setArguments(launch.arguments)
        self._diagnostic(f"starting program={str(launch.program)!r} args={launch.arguments!r} source={source_root!r}")
        self.process.start()

    def send(self, command: str, payload: dict | None = None, request_id: str | None = None) -> str:
        if self.process.state() != QProcess.Running:
            self.start()
            if not self.process.waitForStarted(5000):
                raise RuntimeError("无法启动本地工作进程。请进入“设置”安装或修复本地引擎。")
        request_id = request_id or uuid4().hex
        line = json.dumps({"id": request_id, "type": command, "payload": payload or {}}, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        accepted = self.process.write(encoded)
        self._diagnostic(f"send id={request_id} command={command} bytes={len(encoded)} accepted={accepted}")
        if accepted < 0:
            raise RuntimeError("无法向本地工作进程发送任务")
        self.pending[request_id] = command
        self.request_started.emit(request_id, command)
        deadline_ms = 5000
        while self.process.bytesToWrite() and deadline_ms > 0:
            if not self.process.waitForBytesWritten(min(250, deadline_ms)):
                break
            deadline_ms -= 250
        if self.process.bytesToWrite():
            self._diagnostic(f"send pending id={request_id} bytes={self.process.bytesToWrite()}")
        return request_id

    def restart(self) -> None:
        self.shutdown(); self.start()

    def shutdown(self) -> None:
        if self.process.state() == QProcess.Running:
            try:
                self.send("shutdown")
                self.process.waitForFinished(3000)
            finally:
                if self.process.state() != QProcess.NotRunning:
                    self.process.kill()

    def _stdout(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                item = json.loads(line.decode("utf-8"))
                request_id, event = str(item.get("id", "")), str(item.get("type", ""))
                if request_id == "worker" and event == "ready":
                    self.ready = True; self.ready_changed.emit(True)
                if event in {"result", "error"} and request_id in self.pending:
                    command = self.pending.pop(request_id); self.request_finished.emit(request_id, command)
                self.event.emit(request_id, event, dict(item.get("payload") or {}))
            except Exception:
                self.stderr_line.emit("无法解析工作进程消息: " + line.decode("utf-8", errors="replace"))

    def _stderr(self) -> None:
        value = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in value.splitlines():
            if line.strip():
                self._diagnostic("stderr: " + line)
                self.stderr_line.emit(line)

    def _started(self) -> None:
        self._diagnostic(f"started pid={self.process.processId()}")
        self.state_changed.emit("running")

    def _finished(self, *_args) -> None:
        self._diagnostic(f"finished code={self.process.exitCode()} status={self.process.exitStatus()}")
        self.ready = False; self.pending.clear(); self.ready_changed.emit(False)
        self.state_changed.emit("stopped")

    def _process_error(self, *_args) -> None:
        self._diagnostic("process error: " + self.process.errorString())
        self.state_changed.emit(self.process.errorString())
