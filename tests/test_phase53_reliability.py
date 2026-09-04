from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from local_voice_studio.cover.errors import classify_backend_error
from local_voice_studio.cover.lyrics import CoverLyricsService
from local_voice_studio.cover.project import CoverProject, CoverProjectError
from local_voice_studio.paths import AppPaths
from local_voice_studio.worker import WorkerService
from local_voice_studio.ui.worker_client import WorkerClient


def _paths(tmp_path: Path) -> AppPaths:
    root = tmp_path / "data"
    return AppPaths(root, tmp_path / "projects", root / "runtime", root / "engine",
                    root / "models", root / "logs", root / "db")


def _cover(tmp_path: Path) -> tuple[AppPaths, Path, CoverProject]:
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "p1"
    project.mkdir()
    cover = CoverProject.create(project, cover_id="c" * 32)
    return paths, project, cover


def test_stage_status_six_state_contract_persists(tmp_path):
    _, project, cover = _cover(tmp_path)
    assert cover.separation_status == "pending"
    assert cover.ai_vocal_status == "pending"
    assert cover.mix_status == "pending"
    assert cover.export_status == "pending"
    cover.set_stage_status("separation", "running")
    cover.set_stage_status("ai_vocal", "completed")
    cover.set_stage_status("mix", "cancelled")
    cover.set_stage_status("export", "failed")
    restored = CoverProject.load(project, "c" * 32)
    assert restored.separation_status == "running"
    assert restored.ai_vocal_status == "completed"
    assert restored.mix_status == "cancelled"
    assert restored.export_status == "failed"
    with pytest.raises(CoverProjectError):
        cover.set_stage_status("nope", "running")
    with pytest.raises(CoverProjectError):
        cover.set_stage_status("separation", "bogus")


def test_recover_interrupted_flips_stale_running(tmp_path):
    _, project, cover = _cover(tmp_path)
    cover.set_stage_status("separation", "running")
    cover.set_stage_status("ai_vocal", "running")
    recovered = CoverProject.recover_interrupted(project, "c" * 32)
    assert recovered.separation_status == "interrupted"
    assert recovered.ai_vocal_status == "interrupted"
    assert recovered.mix_status == "pending"


def test_load_rolls_back_to_backup_on_corrupt_manifest(tmp_path):
    _, project, cover = _cover(tmp_path)
    cover.set_stage_status("separation", "completed")
    manifest = cover.manifest_path
    manifest.write_text("{ not valid json", encoding="utf-8")
    restored = CoverProject.load(project, "c" * 32)
    assert restored.separation_status == "completed"
    # A backup must survive, and a torn manifest without backup reports clearly.
    cover.set_stage_status("mix", "running")
    manifest.write_text("{ nope", encoding="utf-8")
    (manifest.with_suffix(".json.bak")).unlink()
    with pytest.raises(CoverProjectError, match="损坏"):
        CoverProject.load(project, "c" * 32)


def test_save_never_overwrites_good_backup_with_corrupt_manifest(tmp_path):
    _, project, cover = _cover(tmp_path)
    cover.set_stage_status("separation", "completed")
    good = cover.manifest_path.with_suffix(".json.bak").read_bytes()
    cover.manifest_path.write_text("{ broken", encoding="utf-8")
    cover.save()  # must not copy the corrupt manifest over the good backup
    assert cover.manifest_path.with_suffix(".json.bak").read_bytes() == good


class _Noop:
    def __call__(self, request_id, payload):
        pass

    __name__ = "_noop"


def test_worker_stage_state_machine(tmp_path):
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "p1"
    project.mkdir()
    cover = CoverProject.create(project, cover_id="c" * 32)
    service = WorkerService(paths)
    service.emit = lambda *args, **kwargs: None
    service.current_request_id = "r1"
    service._request_context = {"command": "separate_song", "project_path": str(project), "cover_id": "c" * 32}
    service._run_guarded("r1", _Noop(), {})
    assert CoverProject.load(project, "c" * 32).separation_status == "completed"


def test_worker_stage_marks_failed_on_error(tmp_path):
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "p1"
    project.mkdir()
    cover = CoverProject.create(project, cover_id="c" * 32)

    class _Boom:
        __name__ = "_boom"
        def __call__(self, request_id, payload):
            raise RuntimeError("boom")

    service = WorkerService(paths)
    events = []
    service.emit = lambda *args, **kwargs: events.append(args)
    service.current_request_id = "r2"
    service._request_context = {"command": "convert_vocal", "project_path": str(project), "cover_id": "c" * 32}
    service._run_guarded("r2", _Boom(), {})
    assert CoverProject.load(project, "c" * 32).ai_vocal_status == "failed"
    assert events[0][2]["status"] == "failed"
    assert events[0][2]["code"] == "cover.convert_vocal.failed"


def test_lyrics_cancel_kills_child_process(tmp_path, monkeypatch):
    _, project, cover = _cover(tmp_path)
    killed = threading.Event()

    class FakePython:
        def __fspath__(self): return "python.exe"

    class FakeProcess:
        def poll(self): return None
        def wait(self): return 0
        def __init__(self, *args, **kwargs): self.stdout = iter(())

    monkeypatch.setattr(CoverLyricsService, "_engine_python", lambda self: FakePython())

    def _fake_kill(process):
        killed.set()

    monkeypatch.setattr(CoverLyricsService, "_kill", staticmethod(_fake_kill))
    monkeypatch.setattr("local_voice_studio.cover.lyrics.subprocess.Popen", lambda *a, **k: FakeProcess())
    service = CoverLyricsService(project, paths=_paths(tmp_path))
    service._process = FakeProcess()
    service.cancel()
    assert killed.is_set()


def test_classify_backend_error_detects_cuda_oom_and_disk_full():
    oom = classify_backend_error("FFmpeg 混音失败：bogus", "CUDA error: out of memory at /pytorch/aten")
    assert "显存" in oom and "CUDA" in oom
    disk = classify_backend_error("FFmpeg 导出失败", "av_interleaved_write_frame: No space left on device")
    assert "磁盘空间不足" in disk
    plain = classify_backend_error("FFmpeg 混音失败：broken pipe", "some detail")
    assert plain == "FFmpeg 混音失败：broken pipe"
