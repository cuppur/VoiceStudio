from __future__ import annotations

import os
import hashlib
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, Qt, QObject, Signal
from PySide6.QtWidgets import QApplication

from local_voice_studio.models import SourceAsset, VoiceProfile
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.simple_pages import OneClickTrainingPage
from local_voice_studio.ui.cover_page import CoverPage
from local_voice_studio.ui.main_window import MainWindow
from local_voice_studio.ui.studio_widgets.voice_selector import VoiceSelector
from local_voice_studio.ui.worker_client import WorkerClient


class FakeClient(QObject):
    event = Signal(str, str, dict)
    state_changed = Signal(str)
    ready_changed = Signal(bool)
    def __init__(self): super().__init__(); self.sent = []
    def send(self, command, payload=None, request_id=None):
        self.sent.append((command, dict(payload or {}), request_id)); return request_id or "request"


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(data, root / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "studio.sqlite3")


def test_voice_selector_uses_project_root_for_strict_singing_gate(tmp_path: Path):
    QApplication.instance() or QApplication([])
    profile = VoiceProfile("歌唱声", True)
    selector = VoiceSelector(project_root=tmp_path)
    with patch.object(profile, "singing_status", return_value="training") as status:
        selector.set_profiles([profile])
        status.assert_called_once_with(tmp_path)
    assert not selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert "不可用" in selector.itemText(0)


def test_worker_exit_finishes_pending_requests_with_error(tmp_path: Path):
    client = WorkerClient.__new__(WorkerClient)
    client.pending = {"request-1": "convert_vocal"}
    client.ready = True
    client.process = type("Process", (), {"exitCode": lambda self: 17, "exitStatus": lambda self: "crashed"})()
    events = []
    client.event = type("Signal", (), {"emit": lambda self, *args: events.append(args)})()
    client.ready_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client.state_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client._diagnostic = lambda _message: None
    client._finished()
    assert client.pending == {}
    assert events[0][0:2] == ("request-1", "error")
    assert events[0][2]["status"] == "worker_stopped"


def test_worker_process_error_flushes_pending_on_failed_to_start():
    client = WorkerClient.__new__(WorkerClient)
    client.pending = {"request-2": "separate_song"}
    client.ready = True
    client.process = type("Process", (), {
        "errorString": lambda self: "cannot start",
        "error": lambda self: QProcess.FailedToStart,
    })()
    events = []
    client.event = type("Signal", (), {"emit": lambda self, *args: events.append(args)})()
    client.ready_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client.state_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client._diagnostic = lambda _message: None
    client._process_error()
    assert client.pending == {}
    assert events[0][0:2] == ("request-2", "error")
    assert events[0][2]["status"] == "worker_stopped"


def test_formal_training_page_sends_only_product_singing_schema(tmp_path: Path):
    QApplication.instance() or QApplication([])
    store = StudioStore(_paths(tmp_path)); project = store.create_project("phase31")
    profile = VoiceProfile("授权声音", True, consent_record="本人授权", consent_confirmed_at="now")
    store.save_profile(project, profile)
    source = project / "raw" / profile.id / "voice.wav"; source.parent.mkdir(parents=True); source.write_bytes(b"owned")
    asset = SourceAsset(profile.id, str(source), str(source), "0" * 64, duration_seconds=603.2, sample_rate=48000, channels=1, codec="pcm")
    store.save_source_assets(project, [asset]); profile.source_asset_ids = [asset.id]; store.save_profile(project, profile)
    client = FakeClient(); page = OneClickTrainingPage(store, project, client)
    assert hasattr(page, "singing_profile") and hasattr(page, "singing_status") and hasattr(page, "singing_train_button")
    page._start_singing_training()
    command, payload, _request_id = client.sent[-1]
    assert command == "train_singing_model"
    assert set(payload) == {"project_path", "profile_id", "source_asset_ids", "training_run_id", "engine"}
    assert payload["source_asset_ids"] == [asset.id]
    assert not {"dataset_dir", "training_dataset_sha256", "checkpoint_path", "index_path"} & payload.keys()


def _wav(path: Path, seconds: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(100); stream.writeframes(b"\x01\x00" * seconds * 100)


def test_cover_generate_gate_uses_real_assets_hashes_rights_and_model(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    store = StudioStore(_paths(tmp_path)); project = store.create_project("cover-gate")
    checkpoint = project / "models" / "singing" / "profile" / "run" / "model.pth"; checkpoint.parent.mkdir(parents=True); checkpoint.write_bytes(b"checkpoint")
    index = checkpoint.with_suffix(".index"); index.write_bytes(b"index")
    profile = VoiceProfile("授权声音", True, id="profile", consent_record="本人授权", consent_confirmed_at="now")
    model = SingingModelVersion(profile_id=profile.id, engine="rvc_v2", checkpoint_relative_path=checkpoint.relative_to(project).as_posix(), checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(), index_relative_path=index.relative_to(project).as_posix(), index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(), trust_status="verified")
    profile.singing_models = [model]; profile.active_singing_model_id = model.id; store.save_profile(project, profile)
    original = tmp_path / "song.wav"; _wav(original)
    cover = CoverProject.create(project, title="song"); cover.copy_source(original)
    vocal = cover.root / "stems" / "vocals.wav"; _wav(vocal); cover.set_stem("vocal", vocal); cover.attest_rights()
    page = CoverPage(store.paths, store, project, FakeClient()); page.cover_project = CoverProject.load(project, cover.id); page.refresh_profiles(); page._update_cover_button()
    assert page.cover_button.isEnabled() is True
    page.cover_project.attest_rights(False); page._update_cover_button(); assert page.cover_button.isEnabled() is False
    page.cover_project.attest_rights(True); vocal.write_bytes(b"tampered"); page._update_cover_button(); assert page.cover_button.isEnabled() is False
    _wav(vocal); page.cover_project.set_stem("vocal", vocal); page._update_cover_button(); assert page.cover_button.isEnabled() is True
    checkpoint.write_bytes(b"tampered"); page.refresh_profiles(); page._update_cover_button(); assert page.cover_button.isEnabled() is False
    page.release_resources(); app.processEvents()


def test_main_window_uses_the_single_formal_singing_training_page(tmp_path: Path):
    QApplication.instance() or QApplication([])
    store = StudioStore(_paths(tmp_path)); store.create_project("formal-ui")
    with patch.object(WorkerClient, "start", lambda self: None), patch.object(WorkerClient, "shutdown", lambda self: None), patch.object(WorkerClient, "send", lambda self, command, payload=None, request_id=None: request_id or "request"):
        window = MainWindow(store.paths, store)
        assert isinstance(window.training_page, OneClickTrainingPage)
        assert hasattr(window.training_page, "singing_profile")
        assert hasattr(window.training_page, "singing_status")
        assert hasattr(window.training_page, "singing_train_button")
        assert window.training_page.train_singing is window.training_page.singing_train_button
        window.close()


def test_key_controls_expose_accessible_names(tmp_path: Path):
    """Phase 6.3 accessibility: primary actions must be reachable by name."""
    QApplication.instance() or QApplication([])
    store = StudioStore(_paths(tmp_path)); project = store.create_project("a11y")
    client = FakeClient()
    page = CoverPage(store.paths, store, project, client)
    assert page.import_button.accessibleName() == "导入歌曲"
    assert page.cover_button.accessibleName() == "开始 AI 翻唱"
    assert page.render_button.accessibleName() == "生成最终翻唱"
    assert page.export_button.accessibleName() == "导出最终混音"
    page.release_resources(); app = QApplication.instance(); app.processEvents()
    with patch.object(WorkerClient, "start", lambda self: None), patch.object(WorkerClient, "shutdown", lambda self: None), patch.object(WorkerClient, "send", lambda self, command, payload=None, request_id=None: request_id or "request"):
        window = MainWindow(store.paths, store)
        assert window.navigation.accessibleName() == "主导航"
        window.close(); app.processEvents()
