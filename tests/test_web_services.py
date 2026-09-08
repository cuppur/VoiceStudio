"""Contract tests for the headless workflow services behind the HTML shell."""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from local_voice_studio.cover.mixing.models import GainScale
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.services.cover import CoverService


class FakeWorker(QObject):
    """Worker-shaped stand-in that records commands and can emit events."""

    event = Signal(str, str, dict)
    state_changed = Signal(str)
    ready_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str, dict]] = []
        self._counter = 0
        self.controllers: list[object] = []

    def send(self, command: str, payload: dict | None = None, request_id: str | None = None) -> str:
        self._counter += 1
        identifier = request_id or f"req-{self._counter}"
        self.sent.append((identifier, command, dict(payload or {})))
        return identifier

    def attach_pipeline_controller(self, controller: object) -> None:
        self.controllers.append(controller)

    def detach_pipeline_controller(self, controller: object) -> None:
        return

    def shutdown(self) -> None:
        return


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        data_root=data,
        projects_root=root / "projects",
        runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS",
        models_root=data / "models",
        logs_root=data / "logs",
        database=data / "studio.sqlite3",
        cache_directory=data / "cache",
    )


def _wav(path: Path, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def _fixture(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("服务测试工程")
    worker = FakeWorker()
    service = CoverService(paths, store, project, worker)
    return paths, store, project, worker, service


def _events(service: CoverService) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    service.event.connect(lambda name, payload: captured.append((name, json.loads(payload))))
    return captured


def test_import_song_copies_into_the_project(tmp_path: Path):
    paths, store, project, _worker, service = _fixture(tmp_path)
    source = _wav(tmp_path / "song.wav")
    result = service.import_song(source)
    covers = CoverProject.list(project)
    assert len(covers) == 1
    assert covers[0].id == result["cover_id"]
    assert Path(covers[0].source_path or (covers[0].root / covers[0].source_relative_path)).is_file()
    assert covers[0].source_sha256


def test_import_rejects_non_audio_suffix(tmp_path: Path):
    _paths_, _store, _project, _worker, service = _fixture(tmp_path)
    other = tmp_path / "notes.txt"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        service.import_song(other)


def test_state_reports_engines_and_rights(tmp_path: Path):
    _paths_, _store, _project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    state = service.state(cover_id)
    assert state["rights_confirmed"] is False
    assert state["has_vocal"] is False
    assert set(state["engines"]) == {"uvr5", "roformer"}
    assert "我确认自己拥有" in state["rights_text"]


def test_separation_requires_rights_confirmation(tmp_path: Path):
    _paths_, _store, _project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    with pytest.raises(Exception) as error:
        service.separate(cover_id, "uvr5")
    assert "权利" in str(error.value)


def test_separation_requires_ready_engine(tmp_path: Path):
    _paths_, _store, _project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    with pytest.raises(RuntimeError):
        service.separate(cover_id, "uvr5")


def test_separation_sends_trusted_command_and_tracks_job(tmp_path: Path, monkeypatch):
    paths, store, project, worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    monkeypatch.setattr(CoverService, "engines", lambda self: {
        "uvr5": {"ready": True, "detail": ""}, "roformer": {"ready": False, "detail": ""},
    })
    events = _events(service)
    task = service.separate(cover_id, "uvr5")
    assert worker.sent and worker.sent[-1][1] == "separate_song"
    payload = worker.sent[-1][2]
    assert payload["cover_id"] == cover_id
    assert "source_relative_path" in payload and "source_sha256" in payload
    assert task["job_id"]
    assert events[0][0] == "job.started"
    jobs = store.list_product_jobs()
    assert jobs and jobs[0].kind == "separate"


def test_worker_events_reach_the_page_and_update_the_job(tmp_path: Path, monkeypatch):
    paths, store, project, worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    monkeypatch.setattr(CoverService, "engines", lambda self: {
        "uvr5": {"ready": True, "detail": ""}, "roformer": {"ready": False, "detail": ""},
    })
    events = _events(service)
    task = service.separate(cover_id, "uvr5")
    request_id = task["request_id"]
    assert service.handle_worker_event(request_id, "progress", {"progress": 0.5, "stage": "separating", "message": "分离中"}) is True
    assert service.handle_worker_event(request_id, "result", {"outputs": ["a.wav"], "vocal_path": "x"}) is True
    assert service.handle_worker_event(request_id, "progress", {"progress": 1}) is False
    names = [name for name, _payload in events]
    assert names == ["job.started", "job.progress", "job.result"]
    assert events[1][1]["percent"] == 50.0
    assert events[1][1]["message"] == "分离中"


def test_worker_error_is_reported(tmp_path: Path, monkeypatch):
    _paths_, _store, _project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    monkeypatch.setattr(CoverService, "engines", lambda self: {
        "uvr5": {"ready": True, "detail": ""}, "roformer": {"ready": False, "detail": ""},
    })
    events = _events(service)
    task = service.separate(cover_id, "uvr5")
    service.handle_worker_event(task["request_id"], "error", {"message": "分离失败"})
    assert events[-1][0] == "job.error"
    assert events[-1][1]["message"] == "分离失败"
    assert service.active_request(cover_id) == ""


def test_cancel_requires_an_active_task(tmp_path: Path):
    _paths_, _store_, _project, _worker, service = _fixture(tmp_path)
    with pytest.raises(ValueError):
        service.cancel("")


def test_mix_settings_translate_sliders_to_db():
    settings = CoverService.mix_settings({"ai": 80, "instrumental": 80, "original": 0})
    assert settings.ai_gain_db == 0.0
    assert settings.instrumental_gain_db == 0.0
    assert settings.original_vocal_gain_db == float("-inf")
    assert GainScale.slider_to_db(0) == "-inf"
    assert CoverService.mix_settings({}).normalize is True


def test_convert_requires_a_verified_singing_model(tmp_path: Path):
    paths, store, project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    profile = VoiceProfile(name="测试声音", consent_confirmed=True, consent_record="本人确认", consent_confirmed_at="2026-01-01T00:00:00+00:00")
    store.save_profile(project, profile)
    with pytest.raises(Exception) as error:
        service.convert_vocal(cover_id, profile.id, 0, {}, None)
    assert "模型" in str(error.value) or "验证" in str(error.value)


def test_lyrics_requires_rights(tmp_path: Path):
    _paths_, _store_, _project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    with pytest.raises(Exception) as error:
        service.transcribe_lyrics(cover_id, "zh")
    assert "权利" in str(error.value)


def test_export_requires_final_mix(tmp_path: Path):
    _paths_, store, project, _worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path / "song.wav"))["cover_id"]
    service.attest_rights(cover_id, True)
    with pytest.raises(Exception):
        service.export(cover_id, format="wav", file_name="out", destination=tmp_path / "exports")
