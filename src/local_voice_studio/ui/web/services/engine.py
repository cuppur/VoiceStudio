"""Engine health and one-click install/repair service (streams the script log)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QProcessEnvironment

from .base import WebService


class EngineService(WebService):
    """Runs the pinned bootstrap script and reports real progress lines."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._process: QProcess | None = None
        self._buffer = ""

    # ------------------------------------------------------------------ state
    def health(self) -> dict[str, Any]:
        from .data import StudioSnapshot
        return StudioSnapshot(self.paths, self.store, self.project).engine()

    def script_path(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS")) / "scripts" / "bootstrap_runtime.ps1"
        return Path(__file__).resolve().parents[5] / "scripts" / "bootstrap_runtime.ps1"

    def running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    # ---------------------------------------------------------------- actions
    def install(self, *, tools: bool = True) -> dict[str, Any]:
        if self.running():
            raise ValueError("安装任务正在执行")
        script = self.script_path()
        if not script.is_file():
            raise ValueError(f"安装脚本不存在：{script}")
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._finished)
        arguments = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-DataRoot", str(self.paths.data_root)]
        if tools:
            arguments.extend(("-DownloadUVR5", "-DownloadRoFormer"))
        process.setProgram("powershell.exe")
        process.setArguments(arguments)
        self._process = process
        self._buffer = ""
        process.start()
        self.notify("engine.install.started", {"script": str(script)})
        return {"message": "已开始安装/修复本地引擎，请保持程序打开"}

    def cancel_install(self) -> dict[str, Any]:
        if not self.running():
            raise ValueError("没有正在执行的安装任务")
        self._process.kill()
        self.notify("engine.install.done", {"code": -1, "message": "安装已取消"})
        return {"message": "已取消安装"}

    # ---------------------------------------------------------------- process
    def _read(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.notify("engine.install.log", {"line": line})

    def _finished(self, code: int, _status) -> None:
        if self._buffer.strip():
            self.notify("engine.install.log", {"line": self._buffer.strip()})
        self._buffer = ""
        self._process = None
        message = "本地引擎安装完成" if code == 0 else f"本地引擎安装失败（退出码 {code}）"
        self.notify("engine.install.done", {"code": int(code), "message": message})

    def close(self) -> None:
        if self.running():
            self._process.kill()
            self._process.waitForFinished(3000)
        self._process = None
        super().close()
