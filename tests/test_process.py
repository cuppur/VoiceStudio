"""Regression tests for the cancellable subprocess boundary."""
from __future__ import annotations

import sys
import threading
import time

from local_voice_studio.cover.process import FFMPEG_QUIET_ARGS, ManagedProcess


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _run_in_thread(managed: ManagedProcess) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["returncode"] = managed.run()
        except BaseException as exc:  # preserve the exception for assertions
            outcome["exception"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_ffmpeg_diagnostics_are_centrally_configured() -> None:
    assert FFMPEG_QUIET_ARGS == ("-hide_banner", "-nostats", "-loglevel", "error")


def test_large_stderr_is_drained_without_deadlock_and_tail_is_bounded() -> None:
    marker = b"\nLARGE_STDERR_TAIL_MARKER\n"
    code = (
        "import sys\n"
        "chunk = b'X' * 4096\n"
        "for _ in range(1536):\n"  # 6 MiB before the marker
        "    sys.stderr.buffer.write(chunk)\n"
        "    sys.stderr.buffer.flush()\n"
        f"sys.stderr.buffer.write({marker!r})\n"
        "sys.stderr.buffer.flush()\n"
    )
    managed = ManagedProcess(_python(code), stderr_limit=8192)
    thread, outcome = _run_in_thread(managed)

    thread.join(10)
    if thread.is_alive():
        managed.stop()
        thread.join(2)
    assert not thread.is_alive(), "child/reader deadlocked while writing >5 MiB stderr"
    assert outcome.get("exception") is None
    assert outcome.get("returncode") == 0
    assert len(managed.stderr_tail.encode("utf-8")) <= 8192
    assert managed.stderr_tail.endswith(marker.decode())
    assert managed.process is None
    assert managed._stderr_thread is None


def test_cancel_under_continuous_large_stderr_stops_child_and_reader() -> None:
    cancel = threading.Event()
    code = (
        "import sys\n"
        "chunk = b'C' * 4096\n"
        "while True:\n"
        "    sys.stderr.buffer.write(chunk)\n"
        "    sys.stderr.buffer.flush()\n"
    )
    managed = ManagedProcess(_python(code), cancel=cancel, stderr_limit=8192)
    thread, outcome = _run_in_thread(managed)

    deadline = time.monotonic() + 3
    while managed.process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert managed.process is not None

    started_cancel = time.monotonic()
    cancel.set()
    thread.join(2)
    elapsed = time.monotonic() - started_cancel
    if thread.is_alive():
        managed.stop()
        thread.join(2)
    assert not thread.is_alive(), "cancel left the process/reader blocked"
    assert elapsed < 2
    assert isinstance(outcome.get("exception"), InterruptedError)
    assert managed.process is None
    assert managed._stderr_thread is None
    assert len(managed.stderr_tail.encode("utf-8")) <= 8192
