"""Contract tests for the headless workflow services behind the HTML shell."""
from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_voice_studio.cover.mixing.models import GainScale
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import ReferenceAsset, VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.services.cover import CoverService
from local_voice_studio.ui.web.services.training import TrainingService
from local_voice_studio.ui.web.services.tts import TtsService
from local_voice_studio.ui.web.services.voices import VoicesService


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


# ---------------------------------------------------------------------- TTS
def _ready_profile(tmp_path: Path, store: StudioStore, project: Path, name: str = "可用声音"):
    checkpoint = tmp_path / "gpt.ckpt"
    sovits = tmp_path / "sovits.pth"
    reference = _wav(tmp_path / f"ref-{name}.wav", 3.0)
    checkpoint.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    profile = VoiceProfile(
        name=name, consent_confirmed=True, consent_record="本人确认",
        consent_confirmed_at="2026-01-01T00:00:00+00:00",
        active_gpt_checkpoint=str(checkpoint), active_sovits_checkpoint=str(sovits),
    )
    profile.reference_assets = [ReferenceAsset(path=str(reference), sha256="a" * 64, transcript="你好", approved=True)]
    store.save_profile(project, profile)
    return profile


def test_tts_generate_requires_text_and_voice(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    tts = TtsService(paths, store, project, _worker)
    profile = _ready_profile(tmp_path, store, project)
    with pytest.raises(ValueError):
        tts.generate(profile.id, "   ")
    with pytest.raises(ValueError):
        tts.generate("missing", "你好")


def test_tts_generate_requires_consent_and_reference(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    tts = TtsService(paths, store, project, _worker)
    bare = VoiceProfile(name="未授权", consent_confirmed=False)
    store.save_profile(project, bare)
    with pytest.raises(ValueError) as error:
        tts.generate(bare.id, "你好")
    assert "授权" in str(error.value) or "训练" in str(error.value)
    profile = _ready_profile(tmp_path, store, project)
    profile.reference_assets = []
    store.save_profile(project, profile)
    with pytest.raises(ValueError) as error:
        tts.generate(profile.id, "你好")
    assert "参考音频" in str(error.value)


def test_tts_two_step_flow_records_generation(tmp_path: Path):
    paths, store, project, worker, _service = _fixture(tmp_path)
    tts = TtsService(paths, store, project, worker)
    profile = _ready_profile(tmp_path, store, project)
    events = _events(tts)
    task = tts.generate(profile.id, "你好世界", speed=1.0, pause=0.3)
    assert worker.sent[-1][1] == "load_profile"
    assert events[0][0] == "job.started"
    # load_profile finished → synthesize is dispatched
    tts.handle_worker_event(task["request_id"], "result", {"outputs": []})
    assert worker.sent[-1][1] == "synthesize"
    synthesis = worker.sent[-1][2]
    assert synthesis["text"] == "你好世界"
    assert synthesis["profile_id"] == profile.id
    assert Path(synthesis["output_dir"]).is_dir()
    synth_request = worker.sent[-1][0]
    tts.handle_worker_event(synth_request, "progress", {"progress": 0.5, "message": "生成第 1/2 段"})
    assert any(name == "job.progress" for name, _payload in events)
    wav = tmp_path / "out.wav"
    wav.write_bytes(b"RIFF")
    tts.handle_worker_event(synth_request, "result", {"outputs": [str(wav)]})
    records = store.list_generation_records(project)
    assert records and records[0].text == "你好世界"
    assert records[0].wav_path == str(wav)


def test_tts_rejects_empty_outputs(tmp_path: Path):
    paths, store, project, worker, _service = _fixture(tmp_path)
    tts = TtsService(paths, store, project, worker)
    profile = _ready_profile(tmp_path, store, project)
    events = _events(tts)
    task = tts.generate(profile.id, "你好")
    tts.handle_worker_event(task["request_id"], "result", {"outputs": []})
    synth_request = worker.sent[-1][0]
    tts.handle_worker_event(synth_request, "result", {"outputs": [str(tmp_path / "missing.wav")]})
    assert events[-1][0] == "job.error"
    assert not store.list_generation_records(project)


def test_tts_cancel_marks_job_cancelled(tmp_path: Path):
    paths, store, project, worker, _service = _fixture(tmp_path)
    tts = TtsService(paths, store, project, worker)
    profile = _ready_profile(tmp_path, store, project)
    events = _events(tts)
    task = tts.generate(profile.id, "你好")
    tts.cancel(task["request_id"])
    tts.handle_worker_event(task["request_id"], "error", {"message": "任务已取消", "cancelled": True})
    assert events[-1][0] == "job.cancelled"
    assert store.list_jobs()[0].status.value == "cancelled"


# ------------------------------------------------------------------- voices
def test_voices_create_requires_consent(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    voices = VoicesService(paths, store, project, _worker)
    with pytest.raises(ValueError):
        voices.create("新声音", consent=False)
    detail = voices.create("新声音", consent=True)
    assert detail["consent"] is True
    assert detail["name"] == "新声音"
    assert store.list_profiles(project)


def test_voices_rename_and_archive(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    voices = VoicesService(paths, store, project, _worker)
    detail = voices.create("旧名字", consent=True)
    renamed = voices.rename(detail["id"], "新名字")
    assert renamed["name"] == "新名字"
    with pytest.raises(ValueError):
        voices.rename(detail["id"], "  ")
    voices.archive(detail["id"])
    assert voices._profile(detail["id"]).archived is True


def test_voices_import_model_never_silently_enables(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    voices = VoicesService(paths, store, project, _worker)
    model = tmp_path / "voice.pth"
    model.write_bytes(b"x")
    result = voices.import_model([str(model)])
    assert result["registered"] == [str(model)]
    assert "不会直接启用" in result["message"]
    assert not store.list_profiles(project)


def test_voices_detail_lists_versions(tmp_path: Path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    voices = VoicesService(paths, store, project, _worker)
    profile = _ready_profile(tmp_path, store, project)
    detail = voices.detail(profile.id)
    assert detail["name"] == "可用声音"
    assert detail["preview"].endswith(".wav")
    # legacy checkpoints are migrated into a real model version by the store
    assert detail["versions"] and detail["versions"][0]["kind"] == "tts"


# ------------------------------------------------------------------ training
def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_training_import_registers_source_assets(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    received: dict[str, dict] = {}
    training.event.connect(lambda name, payload: received.update({name: json.loads(payload)}))
    source = _wav(tmp_path / "voice.wav", 2.0)
    training.import_assets("", [str(source)], name="训练测试声音", consent=True)
    deadline = time.time() + 60
    while time.time() < deadline and "training.scanned" not in received and "training.error" not in received:
        QApplication.processEvents()
        time.sleep(0.01)
    assert "training.scanned" in received, received
    payload = received["training.scanned"]
    assert payload["added"] == 1
    assets = store.list_source_assets(project)
    assert len(assets) == 1
    assert Path(assets[0].project_path).is_file()
    state = training.state(payload["profile_id"])
    assert state["profile"]["consent"] is True
    assert "voice.wav" in state["assets"][0]["name"]
    assert Path(state['assets'][0]['path']).is_file()
    assert Path(state['assets'][0]['path']).is_relative_to(project)


def test_training_import_requires_consent_for_new_voice(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    with pytest.raises(ValueError):
        training.import_assets("", [str(_wav(tmp_path / "v.wav"))], name="新声音", consent=False)


def test_training_start_requires_assets_and_consent(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    profile = VoiceProfile(name="未授权", consent_confirmed=False)
    store.save_profile(project, profile)
    with pytest.raises(ValueError):
        training.start(profile.id)


def test_training_singing_requires_three_minutes(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    profile = _ready_profile(tmp_path, store, project, name="短素材声音")
    with pytest.raises(ValueError) as error:
        training.train_singing(profile.id)
    assert "至少" in str(error.value)


def test_training_state_reports_empty_project(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    state = training.state("")
    assert state["profile"] is None
    assert state["assets"] == []
    assert state["workflow"] == {}
    assert state["singing_min_seconds"] == 180.0


def _imported(training: TrainingService, name: str, *files: Path) -> dict:
    """Import material as a new voice and wait for the background scan."""
    received: dict[str, dict] = {}
    training.event.connect(lambda event, payload: received.update({event: json.loads(payload)}))
    training.import_assets("", [str(item) for item in files], name=name, consent=True)
    deadline = time.time() + 60
    while time.time() < deadline and "training.scanned" not in received and "training.error" not in received:
        QApplication.processEvents()
        time.sleep(0.01)
    assert "training.scanned" in received, received
    return received["training.scanned"]


def test_training_state_lists_profiles_with_ready_flags(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    trained = _ready_profile(tmp_path, store, project, name="已训练声音")
    pending = VoiceProfile(name="待训练声音", consent_confirmed=True, consent_record="本人确认",
                           consent_confirmed_at="2026-01-01T00:00:00+00:00")
    store.save_profile(project, pending)
    state = training.state(pending.id)
    rows = {item["id"]: item for item in state["profiles"]}
    assert set(rows) == {trained.id, pending.id}
    assert rows[trained.id]["ready"] is True
    assert rows[trained.id]["tts_ready"] is True
    assert rows[trained.id]["current"] is False
    assert rows[pending.id]["ready"] is False
    assert rows[pending.id]["current"] is True
    assert state["profile"]["id"] == pending.id


def test_training_state_does_not_fall_back_to_first_profile(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    _ready_profile(tmp_path, store, project, name="唯一声音")
    state = training.state("missing-profile")
    assert state["profile"] is None
    assert len(state["profiles"]) == 1


def test_training_import_rejects_duplicate_voice_name(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    _imported(training, "重名声音", _wav(tmp_path / "first.wav", 2.0))
    before = len(store.list_source_assets(project))
    with pytest.raises(ValueError) as error:
        training.import_assets("", [str(_wav(tmp_path / "second.wav", 2.0))], name="重名声音", consent=True)
    assert "同名" in str(error.value)
    assert len(store.list_source_assets(project)) == before
    assert len([item for item in store.list_profiles(project) if not item.archived]) == 1


def test_training_import_appends_to_selected_profile(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    profile_id = _imported(training, "追加声音", _wav(tmp_path / "a.wav", 2.0))["profile_id"]
    before = len(store.list_profiles(project))
    received: dict[str, dict] = {}
    training.event.connect(lambda event, payload: received.update({event: json.loads(payload)}))
    # 不同时长保证 sha256 不同，否则会被重复文件检测跳过
    training.import_assets(profile_id, [str(_wav(tmp_path / "b.wav", 3.0))])
    deadline = time.time() + 60
    while time.time() < deadline and "training.scanned" not in received and "training.error" not in received:
        QApplication.processEvents()
        time.sleep(0.01)
    assert "training.scanned" in received, received
    assert len(store.list_profiles(project)) == before
    assert len(store.list_source_assets(project, profile_id)) == 2


def test_training_start_requires_a_selected_voice(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    _ready_profile(tmp_path, store, project, name="已备好素材")
    with pytest.raises(ValueError) as error:
        training.start("")
    assert "选择" in str(error.value)


def test_training_remove_assets_deletes_in_batch(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    profile_id = _imported(training, "批量删除声音", _wav(tmp_path / "a.wav", 2.0), _wav(tmp_path / "b.wav", 3.0))["profile_id"]
    rows = store.list_source_assets(project, profile_id)
    assert len(rows) == 2
    result = training.remove_assets(profile_id, [rows[0].id, rows[1].id])
    assert result["removed"] == 2
    assert result["state"]["assets"] == []
    assert store.list_source_assets(project, profile_id) == []


def test_training_remove_assets_rejects_an_empty_selection(tmp_path: Path):
    _app()
    paths, store, project, worker, _service = _fixture(tmp_path)
    training = TrainingService(paths, store, project, worker)
    profile_id = _imported(training, "空选择声音", _wav(tmp_path / "a.wav", 2.0))["profile_id"]
    with pytest.raises(ValueError) as error:
        training.remove_assets(profile_id, [])
    assert "没有" in str(error.value)
