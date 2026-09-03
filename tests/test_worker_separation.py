from __future__ import annotations

import threading
from types import SimpleNamespace
import pytest
from pathlib import Path

from local_voice_studio.paths import AppPaths
from local_voice_studio.protocol import COMMANDS, Message
from local_voice_studio.worker import WorkerService
from local_voice_studio.singing.base import EngineReadiness
from local_voice_studio.cover.application.commands import PrepareSeparationCommand


def paths(tmp_path: Path) -> AppPaths:
    root = tmp_path / "data"
    return AppPaths(root, tmp_path / "projects", root / "runtime", root / "engine",
                    root / "models", root / "logs", root / "db")


def test_protocol_exposes_separate_song():
    assert "separate_song" in COMMANDS
    assert Message.decode(Message("separate_song", {"input_path": "x"}).encode()).type == "separate_song"


def test_worker_dispatches_separate_song_and_passes_cancel(monkeypatch, tmp_path):
    seen = {}

    class FakePipeline:
        def __init__(self, project, *, paths):
            seen["project"] = project
        def separate(self, cover_id, source_relative_path, source_sha256, **kwargs):
            seen.update(cover_id=cover_id, source_relative_path=source_relative_path, source_sha256=source_sha256, cancel=kwargs["cancel"])
            return {"status": "completed", "outputs": {}}
        def cancel(self):
            seen["pipeline_cancel"] = True

    monkeypatch.setattr("local_voice_studio.worker.SongSeparationPipeline", FakePipeline)
    monkeypatch.setattr(
        "local_voice_studio.worker.CoverApplicationService.prepare_separation",
        lambda self, cover_id, *, mode: PrepareSeparationCommand(
            self.project, cover_id, "source/song.wav", "a" * 64, mode
        ),
    )
    service = WorkerService(paths(tmp_path))
    service.emit = lambda *args, **kwargs: None
    project = service.paths.projects_root / "p1"; project.mkdir(parents=True)
    service.handle(Message("separate_song", {"project_path": str(project), "cover_id": "c1", "source_relative_path": "source/song.wav", "source_sha256": "a" * 64, "mode": "uvr5"}))
    assert service.current_thread is not None
    service.current_thread.join(2)
    assert seen["project"] == project.resolve()
    assert seen["cover_id"] == "c1"
    assert seen["source_relative_path"] == "source/song.wav"
    assert seen["cancel"] is service.cancel_event


def test_worker_never_bypasses_application_separation_validation(monkeypatch, tmp_path):
    service = WorkerService(paths(tmp_path))
    project = service.paths.projects_root / "p1"
    project.mkdir(parents=True)
    monkeypatch.setattr(
        "local_voice_studio.worker.CoverApplicationService.prepare_separation",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("manifest missing")),
    )
    with pytest.raises(FileNotFoundError, match="manifest missing"):
        service._separate_song("request", {
            "project_path": str(project), "cover_id": "c1",
            "source_relative_path": "source/song.wav", "source_sha256": "a" * 64,
            "mode": "uvr5",
        })


def test_cancel_forwards_to_active_separation(monkeypatch, tmp_path):
    service = WorkerService(paths(tmp_path))
    service.emit = lambda *args, **kwargs: None
    called = []
    service.separation = type("P", (), {"cancel": lambda self: called.append(True)})()
    service.current_request_id = "work"
    service.handle(Message("cancel", {"target_request_id": "work"}))
    assert called == [True]


def test_worker_dispatches_singing_conversion(monkeypatch, tmp_path):
    seen = {}

    class FakeSinging:
        def convert(self, payload, cancel=None):
            seen["payload"] = payload; seen["cancel"] = cancel
            return {"output_path": "x.wav", "content_origin": "ai_generated"}

    service = WorkerService(paths(tmp_path), singing_engine=FakeSinging())
    service._singing = lambda _request_id: FakeSinging()
    service.emit = lambda *args, **kwargs: None
    project = service.paths.projects_root / "p"; project.mkdir(parents=True)
    (project / "project.json").write_text("{}", encoding="utf-8")
    service.handle(Message("convert_vocal", {"project_path": str(project), "profile_id": "p", "cover_id": "c"}))
    assert service.current_thread is not None
    service.current_thread.join(2)
    assert seen["cancel"] is service.cancel_event
    assert seen["payload"]["profile_id"] == "p"


def test_worker_rejects_external_singing_project_and_client_controlled_paths(tmp_path):
    service = WorkerService(paths(tmp_path), singing_engine=object())
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "project.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="超出"):
        service._validated_singing_payload({"project_path": str(outside), "profile_id": "p", "source_asset_ids": ["a"]}, training=True)
    project = service.paths.projects_root / "p"; project.mkdir(parents=True); (project / "project.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="不得提供"):
        service._validated_singing_payload({"project_path": str(project), "profile_id": "p", "source_asset_ids": ["a"], "dataset_dir": str(tmp_path)}, training=True)
    with pytest.raises(ValueError, match="SourceAsset"):
        service._validated_singing_payload({"project_path": str(project), "profile_id": "p", "source_asset_ids": []}, training=True)


def test_worker_health_reports_real_singing_readiness(monkeypatch, tmp_path):
    class FakeSinging:
        config = SimpleNamespace(commit="pinned", torch_version="2.7.1+cu128")
        def readiness(self): return EngineReadiness(False, ("RMVPE SHA-256 不匹配",), {"commit": "pinned", "torch_version": "2.7.1+cu128"})
    service = WorkerService(paths(tmp_path), singing_engine=FakeSinging())
    monkeypatch.setattr(service.engine, "gpu_health", lambda: {"compatible": True})
    monkeypatch.setattr(service.engine, "readiness", lambda: {"ready": True})
    monkeypatch.setattr("local_voice_studio.worker.EngineRuntimeResolver.verify_install_manifest", lambda _self: SimpleNamespace(valid=True, errors=()))
    seen = {}; service.emit = lambda _id, event, payload: seen.update(event=event, payload=payload)
    service.handle(Message("health", {}))
    assert seen["payload"]["rvc_ready"] is False
    assert seen["payload"]["rmvpe_ready"] is False
    assert seen["payload"]["rvc_commit"] == "pinned"
