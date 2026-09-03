from __future__ import annotations

from pathlib import Path

from local_voice_studio.cover.application import (
    ExportCoverCommand,
    PrepareRenderCommand,
    PrepareSeparationCommand,
)
from local_voice_studio.cover.exporting import ExportFormat, OverwritePolicy
from local_voice_studio.cover.mixing import CoverMixSettings
from local_voice_studio.paths import AppPaths
from local_voice_studio.worker import WorkerService


def _assert_ipc(value: object) -> None:
    assert isinstance(value, dict)
    assert all(not isinstance(item, (Path, ExportFormat, OverwritePolicy)) for item in value.values())


def test_render_command_serializes_only_ipc_primitives() -> None:
    command = PrepareRenderCommand(Path("C:/projects/demo"), "cover", "profile", "model", CoverMixSettings())
    payload = command.to_worker_payload()
    _assert_ipc(payload)
    assert payload["project_path"] == "C:\\projects\\demo"
    assert payload["mix_settings"]["version"] == "cover-mix-v1"
    assert not hasattr(payload["mix_settings"], "ai_vocal_gain_db")


def test_export_command_serializes_path_and_enum_boundaries() -> None:
    command = ExportCoverCommand(
        Path("C:/projects/demo"), "cover", "mix", ExportFormat.BOTH,
        "我的翻唱", Path("C:/exports"), OverwritePolicy.REJECT, True,
    )
    payload = command.to_worker_payload()
    _assert_ipc(payload)
    assert payload == {
        "project_path": "C:\\projects\\demo",
        "cover_id": "cover",
        "final_asset_id": "mix",
        "format": "both",
        "file_name": "我的翻唱",
        "destination": "C:\\exports",
        "existing_policy": "reject",
        "publication_rights_acknowledged": True,
    }


def test_legacy_project_id_read_alias_does_not_change_ipc_name() -> None:
    command = PrepareSeparationCommand("C:/projects/demo", "cover", "source/a.wav", "a" * 64)
    assert command.project_id == "C:\\projects\\demo"
    assert "project_id" not in command.to_payload()
    assert command.to_payload()["project_path"] == "C:\\projects\\demo"


def test_worker_cancel_error_is_structured_and_recoverable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    paths = AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine",
                     data / "models", data / "logs", data / "db.sqlite3")
    worker = WorkerService(paths)
    events: list[dict] = []
    worker.emit = lambda _id, _event, payload: events.append(payload)
    worker.current_request_id = "request"
    worker._request_context = {"command": "render_cover"}
    worker.cancel_event.set()
    def raises(*_args):
        raise RuntimeError("backend stopped")
    worker._run_guarded("request", raises, {})
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["code"] == "cover.cancelled"
    assert events[-1]["recoverable"] is True
