from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import AppPaths


class EngineRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerLaunch:
    program: Path
    arguments: list[str]
    source_root: Path


class EngineRuntimeResolver:
    """Resolve the real private interpreter. A frozen GUI executable is never Python."""

    def __init__(self, paths: AppPaths, frozen: bool | None = None, executable: str | None = None, bundle_root: Path | None = None):
        self.paths = paths
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.executable = Path(executable or sys.executable)
        self.bundle_root = bundle_root or Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))

    def candidates(self) -> list[Path]:
        return [
            self.paths.runtime_root / "env" / "python.exe",
            self.paths.runtime_root / "python.exe",
            self.paths.data_root / "engine" / "venv" / "Scripts" / "python.exe",
        ]

    def resolve_private_python(self) -> Path:
        for candidate in self.candidates():
            if candidate.is_file():
                return candidate.resolve()
        if not self.frozen and self.executable.is_file():
            return self.executable.resolve()
        raise EngineRuntimeError("本地引擎尚未安装完整。请进入“设置”并点击“安装/修复本地引擎”。")

    def worker_launch(self) -> WorkerLaunch:
        python = self.resolve_private_python()
        if self.frozen and python == self.executable.resolve():
            raise EngineRuntimeError("打包程序不能作为 GPU 工作进程解释器，请修复本地引擎。")
        source = self.bundle_root / "worker_source" if self.frozen else Path(__file__).resolve().parents[1]
        return WorkerLaunch(python, ["-X", "utf8", "-u", "-m", "local_voice_studio.worker"], source)


def utf8_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
