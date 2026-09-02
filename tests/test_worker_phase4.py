"""Worker Phase 4 protocol hardening contracts."""
from __future__ import annotations

from pathlib import Path

import pytest

from local_voice_studio.paths import AppPaths
from local_voice_studio.worker import WorkerService


def service(tmp_path: Path) -> WorkerService:
    root = tmp_path / "data"
    paths = AppPaths(root, tmp_path / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "db.sqlite3")
    return WorkerService(paths)


def test_phase4_payload_allowlist_rejects_client_controlled_fields(tmp_path):
    worker = service(tmp_path); project = worker.paths.projects_root / "p"; project.mkdir(parents=True)
    (project / "project.json").write_text('{"schema_version": 1, "id": "c", "assets": []}', encoding="utf-8")
    payload = {"project_path": str(project), "cover_id": "c", "mix_settings": {"ai_gain_db": 0.0}}
    worker._validated_cover_payload(payload)
    for key in ("input_path", "model_path", "ffmpeg", "output_path", "rights_confirmed"):
        bad = dict(payload); bad[key] = "client-controlled"
        with pytest.raises(ValueError): worker._validated_cover_payload(bad)


def test_phase4_payload_rejects_project_escape_and_busy_worker(tmp_path):
    worker = service(tmp_path); outside = tmp_path / "outside"; outside.mkdir()
    with pytest.raises(ValueError): worker._validated_cover_payload({"project_path": str(outside), "cover_id": "c"})
    worker.current_request_id = "busy"
    worker.current_thread = type("Busy", (), {"is_alive": lambda self: True})()
    events = []; worker.emit = lambda *args: events.append(args)
    worker.handle(type("Msg", (), {"type": "render_cover", "id": "new", "payload": {}})())
    assert events and events[-1][1] == "error"
