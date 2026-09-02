"""Minimal cancellable subprocess helper shared by audio backends."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .cancellation import as_cancellation_token


class ManagedProcess:
    """Run one trusted local process and capture only bounded diagnostics."""

    def __init__(self, command: Sequence[str | Path], *, cancel: Any = None, stderr_limit: int = 8_192) -> None:
        self.command = [str(item) for item in command]
        self.cancel = as_cancellation_token(cancel)
        self.stderr_limit = max(512, int(stderr_limit))
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr_tail = ""

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            process.terminate()

    def run(self, *, cwd: Path | None = None) -> int:
        self.process = subprocess.Popen(self.command, cwd=str(cwd) if cwd else None,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
        try:
            while self.process.poll() is None:
                if self.cancel.is_cancelled():
                    self.stop()
                    raise InterruptedError("任务已取消")
                self.cancel.wait(0.1)
            data = self.process.stderr.read() if self.process.stderr else b""
            self.stderr_tail = data.decode("utf-8", errors="replace")[-self.stderr_limit:]
            return int(self.process.returncode or 0)
        finally:
            self.process = None
