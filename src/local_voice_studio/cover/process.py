"""Minimal cancellable subprocess helper shared by audio backends.

The child process writes diagnostics continuously (FFmpeg is especially
verbose), so stderr must be drained independently of the process lifecycle.
Only a bounded tail is retained for error messages.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from .cancellation import as_cancellation_token


# Shared by all in-process FFmpeg callers so diagnostics remain bounded and
# useful without allowing routine progress output to flood stderr.
FFMPEG_QUIET_ARGS = ("-hide_banner", "-nostats", "-loglevel", "error")


class ManagedProcess:
    """Run one trusted local process with cancellable, bounded diagnostics."""

    def __init__(self, command: Sequence[str | Path], *, cancel: Any = None, stderr_limit: int = 8_192, capture_stdout: bool = False) -> None:
        self.command = [str(item) for item in command]
        self.cancel = as_cancellation_token(cancel)
        self.stderr_limit = max(1, int(stderr_limit))
        self.capture_stdout = bool(capture_stdout)
        self._stdout_data = bytearray()
        self.process: subprocess.Popen[bytes] | None = None
        self._stderr_stream: BinaryIO | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_buffer = bytearray()
        self._state_lock = threading.RLock()
        self._stop_requested = threading.Event()

    @property
    def stderr_tail(self) -> str:
        """Return the bounded UTF-8-decoded tail of child stderr."""

        with self._state_lock:
            data = bytes(self._stderr_buffer)
        return data.decode("utf-8", errors="replace")

    @property
    def stdout(self) -> bytes:
        with self._state_lock:
            return bytes(self._stdout_data)

    def stop(self) -> None:
        self._stop_requested.set()
        with self._state_lock:
            process = self.process
            stream = self._stderr_stream

        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        self._close_stream(stream)

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            return

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                return

        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    return

    @staticmethod
    def _close_stream(stream: BinaryIO | None) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _wait_process(process: subprocess.Popen[bytes], timeout: float | None = None) -> int | None:
        """Reap a real Popen, while remaining friendly to tiny test fakes."""

        waiter = getattr(process, "wait", None)
        if not callable(waiter):
            return process.poll()
        try:
            if timeout is None:
                return waiter()
            return waiter(timeout=timeout)
        except TypeError:
            # A minimal fake may expose wait() without Popen's timeout kwarg.
            return waiter()

    def _drain_stderr(self, stream: BinaryIO) -> None:
        """Drain stderr until EOF while retaining only the final N bytes."""

        try:
            while True:
                try:
                    chunk = stream.read(8_192)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                with self._state_lock:
                    self._stderr_buffer.extend(chunk)
                    overflow = len(self._stderr_buffer) - self.stderr_limit
                    if overflow > 0:
                        del self._stderr_buffer[:overflow]
        finally:
            self._close_stream(stream)

    @staticmethod
    def _join_reader(reader: threading.Thread | None, timeout: float = 1.0) -> None:
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=max(0.0, timeout))

    def run(self, *, cwd: Path | None = None) -> int:
        with self._state_lock:
            if self.process is not None:
                raise RuntimeError("ManagedProcess 已在运行")
            self._stderr_buffer.clear()
            self._stdout_data.clear()
            self._stop_requested.clear()

        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE if self.capture_stdout else subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(self.command, **popen_kwargs)
        stream = getattr(process, "stderr", None)
        reader: threading.Thread | None = None
        if stream is not None:
            reader = threading.Thread(target=self._drain_stderr, args=(stream,),
                                       name="voicestudio-stderr-reader", daemon=True)
        with self._state_lock:
            self.process = process
            self._stderr_stream = stream
            self._stderr_thread = reader
        if reader is not None:
            reader.start()

        returncode: int | None = None
        try:
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                if self.cancel.is_cancelled() or self._stop_requested.is_set():
                    self.stop()
                    raise InterruptedError("任务已取消")
                self.cancel.wait(0.05)

            self._join_reader(reader)
            if reader is not None and reader.is_alive():
                self._close_stream(stream)
                self._join_reader(reader)
            if self.capture_stdout:
                stdout = getattr(process, "stdout", None)
                if stdout is not None:
                    try:
                        data = stdout.read()
                    except (OSError, ValueError):
                        data = b""
                    if data:
                        with self._state_lock:
                            self._stdout_data.extend(data)
                    self._close_stream(stdout)
            return int(returncode)
        finally:
            if process.poll() is None:
                self.stop()
            try:
                self._wait_process(process, timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                try:
                    self._wait_process(process, timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        self._wait_process(process, timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            self._close_stream(stream)
            self._join_reader(reader)
            with self._state_lock:
                if self.process is process:
                    self.process = None
                if self._stderr_thread is reader:
                    self._stderr_thread = None
                if self._stderr_stream is stream:
                    self._stderr_stream = None
            self._stop_requested.clear()
