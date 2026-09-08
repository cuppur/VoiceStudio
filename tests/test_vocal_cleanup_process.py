from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.cleanup import FFmpegVocalCleanupBackend, VocalCleanupService, VocalCleanupSettings
from local_voice_studio.cover.process import ManagedProcess
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db.sqlite")


def _real_backend(tmp_path: Path) -> FFmpegVocalCleanupBackend:
    ffmpeg = _ffmpeg_path()
    backend = FFmpegVocalCleanupBackend.__new__(FFmpegVocalCleanupBackend)
    backend.executable = ffmpeg
    backend.process = None
    return backend


def _ffmpeg_path() -> Path:
    candidates = []
    resolved = shutil.which("ffmpeg")
    if resolved:
        candidates.append(Path(resolved))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local = Path(local_app_data)
        candidates.extend((
            local / "LocalVoiceStudio" / "runtime" / "ffmpeg.exe",
            local / "LocalVoiceStudio" / "runtime" / "bin" / "ffmpeg.exe",
            local / "Programs" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("real FFmpeg is not installed or discoverable")


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _wait_pid_exit(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.05)
    assert not _pid_is_running(pid), f"FFmpeg PID {pid} remained alive"


def _cover(tmp_path: Path) -> tuple[AppPaths, Path, CoverProject]:
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "project"
    cover = CoverProject.create(project, cover_id="c" * 32)
    source = cover.root / "stems" / "vocals.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        # Long input keeps the real cleanup process alive long enough for the
        # cancellation path to be exercised reliably on slower CI runners.
        stream.writeframes(b"\0\0" * (16000 * 30))
    cover.set_stem("vocal", source)
    return paths, project, cover


def test_managed_process_real_child_drains_large_stderr_and_bounds_tail() -> None:
    code = "import sys; sys.stderr.write('x' * 2000000); sys.stderr.flush()"
    process = ManagedProcess([sys.executable, "-c", code], stderr_limit=1024)
    started = time.monotonic()
    assert process.run() == 0
    assert time.monotonic() - started < 5
    assert len(process.stderr_tail.encode("utf-8")) <= 1024


def test_real_ffmpeg_realtime_cancel_exits_and_pid_is_reaped(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_path()
    cancel = threading.Event()
    managed = ManagedProcess([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "30",
        "-f", "null", "-",
    ], cancel=cancel)
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["code"] = managed.run()
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    pid = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and pid is None:
        process = managed.process
        if process is not None:
            pid = process.pid
            break
        time.sleep(0.01)
    assert pid is not None
    cancel.set()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), InterruptedError)
    _wait_pid_exit(pid)


def test_cleanup_real_ffmpeg_cancel_reaps_process_and_staging(tmp_path: Path) -> None:
    paths, project, cover = _cover(tmp_path)
    backend = _real_backend(tmp_path)
    service = VocalCleanupService(project, paths=paths, backend=backend)
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(InterruptedError, match="人声清理已取消"):
            service.cleanup(cover.id, VocalCleanupSettings(denoise=True), cancel=cancel)
    finally:
        timer.cancel()
    assert backend.process is None
    assert not list(cover.root.glob("stems/cleanup/*.staging.wav"))


def test_cleanup_real_ffmpeg_nonzero_keeps_bounded_tail(tmp_path: Path) -> None:
    backend = _real_backend(tmp_path)
    with pytest.raises(RuntimeError) as raised:
        backend.cleanup(tmp_path / "missing.wav", tmp_path / "out.wav", VocalCleanupSettings(denoise=True))
    assert len(str(raised.value)) < 9000
    assert backend.process is None


def test_cleanup_real_ffmpeg_repeated_runs_include_cancel_retry(tmp_path: Path) -> None:
    paths, project, cover = _cover(tmp_path)
    backend = _real_backend(tmp_path)
    service = VocalCleanupService(project, paths=paths, backend=backend)
    for run in range(10):
        settings = VocalCleanupSettings(denoise=True, dereverb="strong", highpass_hz=80 + run)
        output_id = f"run-{run}"
        if run in {3, 7}:
            cancel = threading.Event()
            timer = threading.Timer(0.2, cancel.set)
            timer.start()
            try:
                with pytest.raises(InterruptedError):
                    service.cleanup(cover.id, settings, output_id=output_id, cancel=cancel)
            finally:
                timer.cancel()
            assert backend.process is None
        result = service.cleanup(cover.id, settings, output_id=output_id)
        assert Path(result["output_path"]).is_file()
        assert backend.process is None
