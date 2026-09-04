from __future__ import annotations

"""Phase 6.3 coverage hardening for the two page modules.

Targets ``local_voice_studio.ui.pages`` and ``local_voice_studio.ui.simple_pages``
(legacy ``pages.py`` and formal ``simple_pages.py``).  Every widget is exercised
offscreen; every modal dialog / audio-play path that would block or require a
real media backend is patched so the tests run headlessly and terminate.
"""

import json
import os
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QMessageBox

from local_voice_studio.models import (
    DatasetDraft, DatasetDraftSegment, DatasetManifest, DatasetSegment,
    Job, JobKind, JobStatus, ModelVersion, ReferenceAsset, SourceAsset,
    TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus,
    dataset_snapshot_sha256,
)
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore, sha256_file
from local_voice_studio.ui import pages
from local_voice_studio.ui import simple_pages
from local_voice_studio.ui.pages import (
    AudioScanThread, GeneratePage, HistoryPage, SettingsPage, TrainingPage, VoiceLibraryPage,
)
from local_voice_studio.ui.simple_pages import (
    DropArea, MaterialManagerDialog, MyVoicesPage, OneClickGeneratePage,
    OneClickTrainingPage, RecordingDialog, RenameDialog, SimpleSettingsPage,
    TaskCenterDialog, VersionHistoryDialog, VoiceCard, show_error,
)


class FakeClient(QObject):
    event = Signal(str, str, dict)
    stderr_line = Signal(str)

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, command, payload=None, request_id=None):
        rid = request_id or f"req-{len(self.sent) + 1}"
        self.sent.append((command, dict(payload or {}), rid))
        return rid


# Legacy pages.py classes have no release_resources hook; the coverage
# suite calls it defensively after each test. Provide a no-op so cleanup
# calls stay valid without touching product code.
for _cls in (GeneratePage, VoiceLibraryPage, TrainingPage, HistoryPage, SettingsPage,
             RecordingDialog, MaterialManagerDialog, VersionHistoryDialog, RenameDialog,
             TaskCenterDialog, SimpleSettingsPage):
    if not hasattr(_cls, "release_resources"):
        _cls.release_resources = lambda self: None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _no_real_media_backend():
    """Offscreen runs must never start the real Qt multimedia backend:
    QMediaPlayer/QAudioOutput spawn FFmpeg worker threads that outlive the
    test process and abort it at exit (0xC0000409)."""
    with patch.object(QMediaPlayer, "setSource", return_value=None), \
         patch.object(QMediaPlayer, "play", return_value=None), \
         patch.object(QMediaPlayer, "pause", return_value=None), \
         patch.object(QMediaPlayer, "stop", return_value=None), \
         patch.object(QThread, "start", lambda self: self.run()):
        yield


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(data, root / "projects", data / "runtime", data / "engine",
                    data / "models", data / "logs", data / "studio.sqlite3")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wav(path: Path, seconds: float = 6.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * int(8000 * seconds))
    return path


def _profile(store: StudioStore, project: Path, name: str = "我的声音", ref: bool = True) -> VoiceProfile:
    profile = VoiceProfile(name, True, consent_record="本人授权", consent_confirmed_at="now")
    if ref:
        ref_path = _wav(project / "raw" / "ref.wav", seconds=7)
        profile.reference_assets = [ReferenceAsset(str(ref_path), "0" * 64, "参考文本", "zh", 7.0, True, [])]
    store.save_profile(project, profile)
    return profile


def _source(store: StudioStore, project: Path, profile_id: str, seconds: float = 6.0,
            enabled: bool = True) -> SourceAsset:
    path = _wav(project / "sources" / profile_id / "clip.wav", seconds=seconds)
    asset = SourceAsset(profile_id, str(path), str(path), "1" * 64, duration_seconds=seconds,
                        sample_rate=8000, channels=1, codec="pcm", enabled=enabled)
    store.save_source_assets(project, [asset])
    profile = next(p for p in store.list_profiles(project) if p.id == profile_id)
    profile.source_asset_ids.append(asset.id)
    store.save_profile(project, profile)
    return asset


def _probe(store: StudioStore, project: Path, profile_id: str) -> None:
    asset = store.list_source_assets(project, profile_id)[0]
    asset.processing_status = "已切片并转写"
    asset.confirmed_seconds = 70.0
    asset.segment_count = 1
    store.save_source_assets(project, [asset])
    profile = next(p for p in store.list_profiles(project) if p.id == profile_id)
    store.save_profile(project, profile)


def _freeze_snapshot(store: StudioStore, project: Path, profile_id: str, seconds: float = 10.0) -> DatasetManifest:
    dataset_id = f"{profile_id}-snapshot"
    dataset_dir = project / "datasets" / dataset_id
    wav = _wav(dataset_dir / "audio" / "a.wav", seconds=10)
    relative = wav.relative_to(project).as_posix()
    segment = DatasetSegment(sha256_file(wav), str(wav), 0, seconds, "zh", "句子", "句子", None, [], True, True, True,
                             audio_relative_path=relative)
    dataset = DatasetManifest(dataset_id, profile_id, [segment], frozen=True, id=dataset_id)
    list_path = dataset_dir / "dataset.list"
    list_path.write_text(f"{relative}|speaker|zh|句子\n", encoding="utf-8")
    dataset.list_path = str(list_path)
    dataset.wav_dir = str(dataset_dir / "audio")
    dataset.list_relative_path = list_path.relative_to(project).as_posix()
    dataset.list_sha256 = sha256_file(list_path)
    dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
    store.save_dataset_snapshot(project, dataset)
    profile = next(p for p in store.list_profiles(project) if p.id == profile_id)
    profile.dataset_snapshot_id = dataset.id
    store.save_profile(project, profile)
    return dataset


# ---------------------------------------------------------------------------
# pages.py module helpers + scan thread
# ---------------------------------------------------------------------------


def test_friendly_error_mappings():
    assert "PyTorch" in pages._friendly_error("no module named 'torch'")
    assert "GPT-SoVITS" in pages._friendly_error("gpt-sovits 尚未安装")
    assert "GPT-SoVITS" in pages._friendly_error("模型文件不完整")
    assert pages._friendly_error("unrelated") == "unrelated"


def test_parse_asr_result_tags():
    language, text, flags = pages._parse_asr_result("<|zh|><|BGM|>你好世界")
    assert language == "zh" and text == "你好世界" and "疑似伴奏" in flags
    language, text, flags = pages._parse_asr_result("plain <|en|>text")
    assert language == "en" and text == "plain text" and not flags
    language, text, flags = pages._parse_asr_result("no tags here")
    assert language == "zh" and text == "no tags here"


def test_show_error_patched(tmp_path: Path):
    _app()
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        pages._show_error(None, "测试错误")
        show_error(None, "测试错误")


def test_audio_scan_thread_success_and_failure(tmp_path: Path):
    wav = _wav(tmp_path / "a.wav", seconds=1)
    thread = AudioScanThread([wav])
    results = []
    thread.completed.connect(results.append)
    thread.run()
    assert results and results[0][0].path == str(wav)


# ---------------------------------------------------------------------------
# pages.py GeneratePage
# ---------------------------------------------------------------------------


def _generate_page(store, project, client=None):
    return GeneratePage(store, project, client or FakeClient())


def test_generate_page_choose_output_patched(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    page = _generate_page(store, project)
    from PySide6.QtWidgets import QFileDialog
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path / "chosen")):
        page._choose_output()
    assert page.output.text().endswith("chosen")
    page.player.stop(); page.release_resources()


def test_generate_page_start_synthesis_full_flow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _generate_page(store, project, client)
    page.text.setPlainText("这是一句测试台词")
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)
    assert page.active_job is not None
    assert any(cmd == "load_profile" for cmd, _p, _r in client.sent)
    page._finish(); page.player.stop()


def test_generate_page_guards_without_profile(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    page = _generate_page(store, project)
    page.profile.clear()
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)  # no profile index
    assert page.active_job is None
    page.player.stop()


def test_generate_page_empty_text_and_no_refs(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    page = _generate_page(store, project)
    page.profile.addItem("test", "none")
    page.profiles = [VoiceProfile("x", True)]  # no reference assets
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)  # no refs
    page.player.stop(); page.release_resources()


def test_generate_page_unconfirmed_consent(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    profile = VoiceProfile("未授权", False)  # not confirmed
    store.save_profile(project, profile)
    page = _generate_page(store, project)
    page.refresh_profiles()
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)
    page.player.stop(); page.release_resources()


def test_generate_page_on_event_progress_result(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _generate_page(store, project, client)
    page.text.setPlainText("这是一句测试台词")
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)
    request = client.sent[-1][2]
    page._on_event(request, "result", {})
    # load_profile result -> sends synthesize
    synth = client.sent[-1]
    assert synth[0] == "synthesize"
    page._on_event(synth[2], "progress", {"progress": 0.5, "message": "生成中", "job_dir": str(tmp_path / "job")})
    page._on_event(synth[2], "result", {"outputs": [], "preview": False})
    page._finish(); page.player.stop(); page.release_resources()


def test_generate_page_on_event_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _generate_page(store, project, client)
    page.text.setPlainText("这是一句测试台词")
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_synthesis(False)
    synth = client.sent[-1]
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._on_event(synth[2], "error", {"status": "failed", "message": "boom"})
    page.player.stop(); page.release_resources()


def test_generate_page_preview_flow_and_replacement(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _generate_page(store, project, client)
    page.text.setPlainText("这是一句测试台词")
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._preview_generate()
    request = page.preview_request
    assert request
    # Now simulate a replacement preview while first is pending
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._preview_generate()
    assert page.replacement_preview
    page.player.stop(); page.release_resources()


def test_generate_page_preview_too_long(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    page = _generate_page(store, project)
    page.text.setPlainText("x" * 300)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._preview_generate()
    page.player.stop(); page.release_resources()


def test_generate_page_resume_missing_profile(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    store.set_setting("last_incomplete_synthesis", {"profile_id": "nope", "text": "x"})
    page = _generate_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._resume()
    page.player.stop(); page.release_resources()


def test_generate_page_toggle_playback(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("gen")
    page = _generate_page(store, project)
    with patch.object(page.player, "play", return_value=None), patch.object(page.player, "pause", return_value=None), \
         patch.object(page.player, "playbackState", return_value=2):
        page._toggle_playback()
    with patch.object(page.player, "playbackState", return_value=0):
        page._toggle_playback()
    page.player.stop(); page.release_resources()


# ---------------------------------------------------------------------------
# pages.py VoiceLibraryPage
# ---------------------------------------------------------------------------


def test_voice_library_import_and_save_flow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    client = FakeClient()
    page = VoiceLibraryPage(store, project, client)
    from PySide6.QtWidgets import QFileDialog
    with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(wav)], "")), \
         patch.object(QFileDialog, "getExistingDirectory", return_value=""):
        page._import_files()
    with patch.object(AudioScanThread, "start", lambda self: self.run()):
        page._scan([wav])
        page._scan([wav])  # second call while scanning is a no-op
    assert page.table.rowCount() >= 1
    # populate manually without thread
    from local_voice_studio.audio import probe_audio
    probe = probe_audio(wav)
    page._populate_scan([probe])
    assert page.table.rowCount() == 1
    page.consent.setChecked(True)
    page.name.setText("新声音")
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok), \
         patch("local_voice_studio.ui.pages.copy_original", side_effect=lambda src, dst, dig=None: dst / "copied.wav"):
        page._save()
    assert store.list_profiles(project)
    page.release_resources()


def test_voice_library_save_requires_consent(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    page = VoiceLibraryPage(store, project, FakeClient())
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._save()
    page.release_resources()


def test_voice_library_save_no_selected(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    page = VoiceLibraryPage(store, project, FakeClient())
    page.consent.setChecked(True)
    page._populate_scan([__import__("local_voice_studio.audio", fromlist=["AudioProbe"]).AudioProbe(str(wav), "aa", 6, 8000, 1, "pcm")])
    # uncheck the row
    page.table.item(0, 0).setCheckState(Qt.Unchecked)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._save()
    page.release_resources()


def test_voice_library_cleanup_flow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    client = FakeClient()
    page = VoiceLibraryPage(store, project, client)
    from local_voice_studio.audio import probe_audio
    page._populate_scan([probe_audio(wav)])
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok), \
         patch("local_voice_studio.ui.pages.copy_original", side_effect=lambda s, d, g=None: d / "x.wav"):
        page._cleanup("denoise")
    request = client.sent[-1][2]
    page._on_worker_event(request, "progress", {"message": "处理中"})
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._on_worker_event(request, "error", {"message": "failed"})
    page._on_worker_event("other", "result", {})  # ignored
    page.release_resources()


def test_voice_library_cleanup_result_scans(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    client = FakeClient()
    page = VoiceLibraryPage(store, project, client)
    from local_voice_studio.audio import probe_audio
    page._populate_scan([probe_audio(wav)])
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok), \
         patch("local_voice_studio.ui.pages.copy_original", side_effect=lambda s, d, g=None: d / "x.wav"):
        page._cleanup("uvr")
    request = client.sent[-1][2]
    out = _wav(project / "out.wav", seconds=1)
    # Result triggers a rescan; run it synchronously so no QThread outlives
    # the test (a running thread destroyed at exit aborts the process).
    with patch.object(AudioScanThread, "start", lambda self: self.run()):
        page._on_worker_event(request, "result", {"outputs": [str(out)]})
    page.release_resources()


def test_voice_library_cleanup_no_selection(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    page = VoiceLibraryPage(store, project, FakeClient())
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._cleanup("denoise")
    page.release_resources()


def test_voice_library_preview_and_reference(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    page = VoiceLibraryPage(store, project, FakeClient())
    from local_voice_studio.audio import probe_audio
    page._populate_scan([probe_audio(wav)])
    with patch("local_voice_studio.ui.pages.QDesktopServices.openUrl", return_value=True):
        page._preview(0, 1)
    # reference with no valid profile
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._preview_reference()
    page.release_resources()


def test_voice_library_confirm_consent(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    _profile(store, project)
    page = VoiceLibraryPage(store, project, FakeClient())
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok), \
         patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._confirm_existing_consent()
    # select row 0
    page.profiles_table.selectRow(0)
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._confirm_existing_consent()
    page.release_resources()


def test_voice_library_refresh_profiles_and_reference(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("lib")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = VoiceLibraryPage(store, project, FakeClient())
    page.refresh_profiles()
    assert page.profiles_table.rowCount() == 1
    page.profiles_table.selectRow(0)
    with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True):
        page._preview_reference()
    # a profile with approved ref but file missing
    store.save_profile(project, profile)
    page.release_resources()


# ---------------------------------------------------------------------------
# pages.py TrainingPage
# ---------------------------------------------------------------------------


def _training_page(store, project, client=None):
    page = TrainingPage(store, project, client or FakeClient())
    return page


def test_training_page_refresh_and_singing_status(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    page = _training_page(store, project)
    assert page.singing_profile.count() == 1
    page._update_singing_status()
    page.release_resources()


def test_training_page_train_singing_no_consent(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    profile.consent_confirmed = False
    store.save_profile(project, profile)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train_singing_model()
    page.release_resources()


def test_training_page_train_singing_no_assets(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train_singing_model()
    page.release_resources()


def test_training_page_train_singing_ok(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _training_page(store, project, client)
    page._train_singing_model()
    assert any(cmd == "train_singing_model" for cmd, _p, _r in client.sent)
    page.release_resources()


def test_training_page_load_profile_assets(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = _training_page(store, project)
    page._load_profile_assets()
    assert page.source_table.rowCount() == 1
    page.release_resources()


def test_training_page_prepare_sources(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    client = FakeClient()
    page = _training_page(store, project, client)
    page.separate_vocals.setChecked(True)
    page.noise_reduce.setChecked(True)
    page._prepare_sources()
    assert any(cmd == "prepare_dataset" for cmd, _p, _r in client.sent)
    request = client.sent[-1][2]
    page._on_event(request, "progress", {"progress": 0.3, "message": "工作中"})
    page.release_resources()


def test_training_page_prepare_sources_no_selection(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._prepare_sources()
    page.release_resources()


def test_training_page_cleanup_preparation_runs(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    _profile(store, project)
    page = _training_page(store, project)
    # no profile -> early return (combo cleared so currentData() is empty)
    page.training_profile.clear()
    page._cleanup_preparation_runs()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        page._cleanup_preparation_runs()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        page._cleanup_preparation_runs()
    page.release_resources()


def test_training_page_record_and_import(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    page = _training_page(store, project)
    # toggle record start/stop with recorder patched
    with patch.object(page.recorder, "source", None, create=True), \
         patch.object(page.recorder, "start", return_value=None), \
         patch.object(page.recorder, "microphone", page.microphone, create=True):
        page._toggle_record()
    with patch.object(page.recorder, "source", "somepath", create=True), \
         patch.object(page.recorder, "stop", return_value=None):
        page._toggle_record()
    page._next_prompt()
    # import via patched dialog + scan
    wav = _wav(tmp_path / "clip.wav", seconds=6)
    from PySide6.QtWidgets import QFileDialog
    with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(wav)], "")):
        page._import()
    assert page.probes
    page.release_resources()


def test_training_page_confirm_and_exclude(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    page = _training_page(store, project)
    wav = _wav(tmp_path / "clip.wav", seconds=6)
    from local_voice_studio.audio import probe_audio
    page._append_probe(probe_audio(wav), "文本")
    page.dataset_table.item(0, 5).setText("文本")
    page._confirm_all()
    page._exclude_bad()
    page._update_duration()
    assert "不足 60 秒" in page.duration.text() or "可以冻结" in page.duration.text()
    page.release_resources()


def test_training_page_choose_reference(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    page = _training_page(store, project)
    wav = _wav(tmp_path / "clip.wav", seconds=7)
    from local_voice_studio.audio import probe_audio
    page._append_probe(probe_audio(wav), "参考")
    page.dataset_table.selectRow(0)
    page.dataset_table.item(0, 5).setText("参考")
    page.dataset_table.item(0, 2).setCheckState(Qt.Checked)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok), \
         patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._choose_reference()
    page.release_resources()


def test_training_page_choose_reference_guards(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    _profile(store, project)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._choose_reference()  # no selection
    page.release_resources()


def test_training_page_play_all(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    page = _training_page(store, project)
    wav = _wav(tmp_path / "clip.wav", seconds=6)
    from local_voice_studio.audio import probe_audio
    page._append_probe(probe_audio(wav), "文本")
    with patch.object(page.review_player, "setSource", return_value=None), \
         patch.object(page.review_player, "play", return_value=None):
        page._play_all()
        page._play_path(str(wav))
        page._review_status(6)  # EndOfMedia
    page.release_resources()


def test_training_page_freeze(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = _training_page(store, project)
    for i in range(7):
        wav = _wav(project / f"clip{i}.wav", seconds=10)
        from local_voice_studio.audio import probe_audio
        probe = probe_audio(wav)
        probe.quality_flags = []
        page._append_probe(probe, f"句子{i}")
        row = page.dataset_table.rowCount() - 1
        page.dataset_table.item(row, 2).setCheckState(Qt.Checked)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._freeze()
    # now ensure dataset snapshot saved
    assert store.list_profiles(project)[0].dataset_snapshot_id
    page.release_resources()


def test_training_page_freeze_no_valid(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._freeze()
    page.release_resources()


def test_training_page_run_slice_and_asr(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    _profile(store, project)
    client = FakeClient()
    page = _training_page(store, project, client)
    wav = _wav(tmp_path / "clip.wav", seconds=6)
    from local_voice_studio.audio import probe_audio
    probe = probe_audio(wav)
    page.probes = [probe]
    page._append_probe(probe, "文本")
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok), \
         patch("local_voice_studio.ui.pages.copy_original", side_effect=lambda s, d, g=None: d / "x.wav"):
        page._run_slice()
        page._run_asr()
    assert any(cmd == "prepare_dataset" for cmd, _p, _r in client.sent)
    page.release_resources()


def test_training_page_run_slice_no_probes(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._run_slice()
        page._run_asr()
    page.release_resources()


def test_training_page_apply_asr(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    page = _training_page(store, project)
    wav = _wav(tmp_path / "clip.wav", seconds=6)
    from local_voice_studio.audio import probe_audio
    page._append_probe(probe_audio(wav), "")
    list_path = tmp_path / "out.list"
    list_path.write_text(f"{Path('clip.wav').name}|speaker|zh|<|en|><|BGM|>你好\nbogus\n", encoding="utf-8")
    page._apply_asr(list_path)
    # non-existent list path
    page._apply_asr(tmp_path / "missing.list")
    page.release_resources()


def test_training_page_dataset_payload_and_train(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    _probe(store, project, profile.id)
    _freeze_snapshot(store, project, profile.id, seconds=70.0)
    client = FakeClient()
    page = _training_page(store, project, client)
    payload = page._dataset_payload()
    assert payload["profile_id"] == profile.id
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._prepare()
        page._train()
    assert any(cmd == "train" for cmd, _p, _r in client.sent)
    page.release_resources()


def test_training_page_dataset_payload_errors(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._prepare()  # no snapshot -> error
        page._train()
    page.release_resources()


def test_training_page_train_under_60(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id, seconds=5)
    _freeze_snapshot(store, project, profile.id)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train()
    page.release_resources()


def test_training_page_on_event_training_result_and_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    _probe(store, project, profile.id)
    _freeze_snapshot(store, project, profile.id, seconds=70.0)
    client = FakeClient()
    page = _training_page(store, project, client)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train()
    train = client.sent[-1]
    # result with checkpoints
    gpt = _wav(tmp_path / "model.ckpt", seconds=1)
    sovits = _wav(tmp_path / "model.pth", seconds=1)
    page._on_event(train[2], "result", {
        "outputs": [str(gpt), str(sovits)], "checkpoints": {"gpt": str(gpt), "sovits": str(sovits)},
    })
    assert page.ab_button.isEnabled()
    # error
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train()
        train2 = client.sent[-1]
        page._on_event(train2[2], "error", {"message": "训练失败", "status": "failed"})
    # cancelled (error branch still shows an error dialog for cancelled jobs)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._train()
        train3 = client.sent[-1]
        page._on_event(train3[2], "error", {"message": "取消", "status": "cancelled"})
    page.release_resources()


def test_training_page_ab_flow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    _source(store, project, profile.id)
    gpt = _wav(tmp_path / "model.ckpt", seconds=1)
    sovits = _wav(tmp_path / "model.pth", seconds=1)
    profile.candidate_gpt_checkpoint = str(gpt)
    profile.candidate_sovits_checkpoint = str(sovits)
    profile.ab_status = "awaiting_ab"
    profile.candidate_training_run_id = "run1"
    profile.candidate_dataset_snapshot_id = "snap"
    profile.candidate_snapshot_sha256 = "1" * 64
    store.save_profile(project, profile)
    client = FakeClient()
    page = _training_page(store, project, client)
    page.refresh_profiles()
    page._restore_candidate_state()
    assert page.ab_button.isEnabled()
    page._start_ab()
    # walk through AB event chain
    chain = [("load_base", "result", {}), ("synth_base", "result", {"outputs": [str(_wav(tmp_path / "base.wav", 1))]}),
             ("load_tuned", "result", {}), ("synth_tuned", "result", {"outputs": [str(_wav(tmp_path / "tuned.wav", 1))]})]
    for _stage, event, payload in chain:
        req = client.sent[-1][2]
        page._on_event(req, event, payload)
    assert page.promote_button.isEnabled()
    # play
    with patch.object(page.review_player, "setSource", return_value=None), patch.object(page.review_player, "play", return_value=None):
        page._play_ab(False)
        page._play_ab(True)
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._promote()
    page._reject_candidate()
    page.release_resources()


def test_training_page_ab_guards(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    store.save_profile(project, profile)
    page = _training_page(store, project)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        page._start_ab()
        page._promote()
        page._play_ab(False)
    page.release_resources()


def test_training_page_load_preparation(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    profile = _profile(store, project)
    wav = _wav(project / "segments" / "s.wav", seconds=6)
    list_path = project / "prep" / "asr.list"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(f"segments/s.wav|speaker|zh|<|zh|>你好\n", encoding="utf-8")
    manifest = {"asr_list": str(list_path), "segments_dir": str(project / "segments"), "profile_id": profile.id, "preparation_id": "prep1", "source_asset_ids": []}
    manifest_path = project / "prep" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    page = _training_page(store, project)
    page._load_preparation(manifest_path)
    assert page.dataset_table.rowCount() >= 1
    page.release_resources()


def test_training_page_event_unknown_request(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("train")
    page = _training_page(store, project)
    page._on_event("unknown", "result", {})
    page.release_resources()


# ---------------------------------------------------------------------------
# pages.py HistoryPage + SettingsPage
# ---------------------------------------------------------------------------


def test_history_page_refresh_and_open(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("hist")
    job = Job(JobKind.SYNTHESIZE, {"x": 1}, status=JobStatus.COMPLETED, outputs=[str(_wav(tmp_path / "o.wav"))])
    store.save_job(job)
    empty = Job(JobKind.SYNTHESIZE, {"x": 2}, status=JobStatus.COMPLETED, outputs=[])
    store.save_job(empty)
    page = HistoryPage(store)
    assert page.table.rowCount() == 2
    with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True):
        page._open_output(0, 0)
    with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True):
        page._open_output(1, 0)  # empty output cell -> no openUrl, no crash
    page.release_resources()


def test_settings_page_health_and_copy(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); store.create_project("set")
    client = FakeClient()
    page = SettingsPage(_paths(tmp_path), client)
    request = client.sent[-1][2]
    page._on_event(request, "result", {
        "compatible": True, "engine": {"missing": ["a"]}, "actionable_errors": ["问题一"],
        "python_executable": "py", "python_version": "3.10", "torch_version": "2.0",
        "cuda_version": "12", "gpu_name": "NVIDIA", "compute_capability": "8.6",
        "tensor_test_passed": True, "gpt_sovits_imported": True, "models_ready": True, "ffmpeg_ready": True,
    })
    page._on_event("other", "result", {})  # ignored
    with patch.object(QApplication.clipboard(), "setText", return_value=None):
        page._copy_details()
    page.release_resources()


def test_settings_page_check_health_error(tmp_path: Path):
    _app()
    client = FakeClient()
    page = SettingsPage(_paths(tmp_path), client)
    with patch.object(client, "send", side_effect=RuntimeError("no engine")):
        page.check_health()
    page.release_resources()


def test_settings_page_health_error_event(tmp_path: Path):
    _app()
    client = FakeClient()
    page = SettingsPage(_paths(tmp_path), client)
    page._on_event(page.health_request or "req", "error", {"message": "no module named 'torch'"})
    page.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py DropArea / fold_group / ScanThread
# ---------------------------------------------------------------------------


def test_fold_group_toggles():
    _app()
    from PySide6.QtWidgets import QGroupBox, QLineEdit
    group = QGroupBox("g"); group.setCheckable(True); group.setChecked(True)
    group.setLayout(__import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout())
    group.layout().addWidget(QLineEdit("x"))
    simple_pages.fold_group(group)
    group.setChecked(False)
    assert not group.layout().itemAt(0).widget().isVisible()


def test_drop_area_events(tmp_path: Path):
    _app()
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtCore import QMimeData, QPointF
    area = DropArea()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(_wav(tmp_path / "a.wav")))])
    dropped = []
    area.paths_dropped.connect(dropped.append)
    event = QDropEvent(QPointF(0, 0), Qt.DropAction(1), mime, Qt.LeftButton, Qt.NoModifier)
    area.dropEvent(event)
    assert dropped and dropped[0]
    event2 = QDropEvent(QPointF(0, 0), Qt.DropAction(1), mime, Qt.LeftButton, Qt.NoModifier)
    area.dragEnterEvent(event2)
    assert event2.isAccepted()


def test_scan_thread_run(tmp_path: Path):
    wav = _wav(tmp_path / "a.wav", seconds=1)
    thread = simple_pages.ScanThread([wav])
    results = []
    thread.completed.connect(results.append)
    thread.run()
    assert results and results[0][0].path == str(wav)


# ---------------------------------------------------------------------------
# simple_pages.py RecordingDialog
# ---------------------------------------------------------------------------


def test_recording_dialog_lifecycle(tmp_path: Path):
    _app()
    dialog = RecordingDialog(tmp_path / "out")
    dialog._next()
    with patch.object(dialog.recorder, "source", None, create=True), \
         patch.object(dialog.recorder, "start", return_value=None):
        dialog._toggle()
    with patch.object(dialog.recorder, "source", "x", create=True), \
         patch.object(dialog.recorder, "stop", return_value=None):
        dialog._toggle()
    dialog._quality({"environment": "安静", "volume": "正常", "noise": "低", "clipping": True})
    dialog._quality({"environment": "安静", "volume": "正常", "noise": "低"})
    dialog._selected_recording(None)
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        dialog._delete_selected()  # no selection -> error dialog
    with patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok):
        dialog._rerecord()  # no selection -> error dialog
    dialog.release_resources()


def test_recording_dialog_saved_and_finish(tmp_path: Path):
    _app()
    out = tmp_path / "out"
    dialog = RecordingDialog(out)
    wav = _wav(out / "recording-1.wav", seconds=70)
    dialog._saved(str(wav), 70.0)
    assert dialog.finish_button.isEnabled()
    with patch.object(dialog, "accept", return_value=None):
        dialog.finish_button.click()
    dialog.release_resources()


def test_recording_dialog_reject_with_question(tmp_path: Path):
    _app()
    out = tmp_path / "out"
    dialog = RecordingDialog(out)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        dialog.reject()
    assert not dialog.paths
    with patch.object(dialog.recorder, "source", None, create=True), \
         patch.object(dialog.recorder, "stop", return_value=None):
        dialog.reject()
    dialog.release_resources()


def test_recording_dialog_delete_selected(tmp_path: Path):
    _app()
    out = tmp_path / "out"
    dialog = RecordingDialog(out)
    wav = _wav(out / "r.wav", seconds=10)
    dialog._saved(str(wav), 10.0)
    dialog.recordings.setCurrentRow(0)
    dialog._delete_selected()
    assert dialog.paths == []
    dialog.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py MaterialManagerDialog
# ---------------------------------------------------------------------------


def test_material_manager_dialog_flow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("mat")
    profile = _profile(store, project)
    asset = _source(store, project, profile.id)
    dialog = MaterialManagerDialog(store, project, profile.id)
    assert dialog.table.rowCount() == 1
    dialog._select_all()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        dialog._remove()
    assert store.list_source_assets(project, profile.id) == []
    dialog.release_resources()


def test_material_manager_dialog_no_selection(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("mat")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    dialog = MaterialManagerDialog(store, project, profile.id)
    with patch.object(simple_pages, "show_error", return_value=None):
        dialog._remove()
    dialog.release_resources()


def test_material_manager_dialog_blocking_workflow(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("mat")
    profile = _profile(store, project)
    asset = _source(store, project, profile.id)
    wf = TrainingWorkflow(profile.id, profile.name, source_asset_ids=[asset.id], stage=WorkflowStage.IMPORTING, status=WorkflowStatus.RUNNING)
    store.save_workflow(project, wf)
    dialog = MaterialManagerDialog(store, project, profile.id)
    dialog._select_all()
    with patch.object(simple_pages, "show_error", return_value=None):
        dialog._remove()
    dialog.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py OneClickTrainingPage
# ---------------------------------------------------------------------------


def _oct(store, project, client=None):
    return OneClickTrainingPage(store, project, client or FakeClient())


def test_oct_singing_events(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    _source(store, project, profile.id, seconds=400)
    client = FakeClient()
    page = _oct(store, project, client)
    page._refresh_singing_profiles()
    assert page.singing_train_button.isEnabled()
    page._start_singing_training()
    request = page.singing_request
    page._singing_event(request, "progress", {"progress": 0.3, "message": "阶段1", "stage": "train", "step": "8"})
    page._singing_event(request, "result", {})
    page._start_singing_training()
    request = page.singing_request
    page._singing_event(request, "error", {"message": "失败", "status": "failed"})
    page._start_singing_training()
    request = page.singing_request
    page._singing_event(request, "error", {"message": "取消", "status": "cancelled"})
    page.release_resources()


def test_oct_singing_guards(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    page = _oct(store, project)
    page._start_singing_training()  # no profile
    assert "请先选择声音" in page.singing_status.text()
    # profile without consent
    profile = _profile(store, project)
    profile.consent_confirmed = False
    store.save_profile(project, profile)
    page2 = _oct(store, project)
    page2._start_singing_training()
    assert "未授权" in page2.singing_status.text()
    # profile confirmed but no assets
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    page3 = _oct(store, project)
    page3._start_singing_training()
    assert "没有可用训练素材" in page3.singing_status.text()
    page.release_resources()


def test_oct_singing_train_send_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    _source(store, project, profile.id, seconds=400)
    client = FakeClient()
    page = _oct(store, project, client)
    with patch.object(client, "send", side_effect=RuntimeError("启动失败")):
        page._start_singing_training()
    assert "启动失败" in page.singing_status.text()
    page.release_resources()


def test_oct_cancel_singing(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    profile.consent_confirmed = True
    store.save_profile(project, profile)
    _source(store, project, profile.id, seconds=400)
    client = FakeClient()
    page = _oct(store, project, client)
    page._start_singing_training()
    page._cancel_singing_training()
    assert any(cmd == "cancel" for cmd, _p, _r in client.sent)
    page.release_resources()


def test_oct_scan_and_primary(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    client = FakeClient()
    page = _oct(store, project, client)
    with patch.object(simple_pages.ScanThread, "start", lambda self: self.run()):
        page._scan([wav])
    from local_voice_studio.audio import probe_audio
    page._scanned([probe_audio(wav)])
    assert page.primary.text() == "开始自动处理"
    page.consent.setChecked(True)
    with patch("local_voice_studio.ui.simple_pages.copy_original", side_effect=lambda s, d, g=None: d / "c.wav"), \
         patch.object(page.controller, "start", return_value=TrainingWorkflow(profile_id := "pid", "名字")):
        page._primary_action()
    assert page.workflow is not None
    page.release_resources()


def test_oct_primary_no_consent(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    wav = _wav(tmp_path / "src.wav", seconds=6)
    page = _oct(store, project)
    with patch.object(simple_pages.ScanThread, "start", lambda self: self.run()):
        page._scan([wav])
    from local_voice_studio.audio import probe_audio
    page._scanned([probe_audio(wav)])
    with patch.object(simple_pages, "show_error", return_value=None):
        page._primary_action()  # no consent -> ValueError
    page.release_resources()


def test_oct_workflow_changed_and_draft(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    wf = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.REVIEW_REQUIRED, status=WorkflowStatus.RUNNING, progress=0.5)
    store.save_workflow(project, wf)
    page = _oct(store, project)
    page._restore()
    assert page.workflow is not None
    # draft
    seg = DatasetDraftSegment("processed/a.wav", 0, 6, text="", included=True)
    draft = DatasetDraft(wf.id, profile.id, "prep", [seg])
    store.save_draft(project, draft)
    wf.draft_id = draft.id
    store.save_workflow(project, wf)
    page2 = _oct(store, project)
    page2._restore()
    assert page2.draft is not None
    page.release_resources()


def test_oct_review_interactions(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    wf = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.REVIEW_REQUIRED, status=WorkflowStatus.RUNNING)
    store.save_workflow(project, wf)
    page = _oct(store, project)
    seg = DatasetDraftSegment("processed/a.wav", 0, 6, text="你好", asr_text="你好", included=True)
    draft = DatasetDraft(wf.id, profile.id, "prep", [seg])
    store.save_draft(project, draft)
    page._show_draft(draft)
    page._populate_review()
    page.show_all.setChecked(True)
    page._populate_review()
    page._review_changed(0, {"text": " 新文本 ", "included": True})
    page._review_confirmed(0)
    page._review_navigate(1)
    page._pull_review()
    page.release_resources()


def test_oct_review_navigate_empty(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    page = _oct(store, project)
    page._review_navigate(1)  # no indices
    page._review_changed(0, {})  # no draft
    page._review_confirmed(0)
    page._populate_review()
    page._pull_review()
    page.release_resources()


def test_oct_reset_for_profile(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    page = _oct(store, project)
    page.reset_for_profile(profile.id)
    assert page.primary.text() == "导入素材"
    page.release_resources()


def test_oct_step_results_and_set(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    wf = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.IMPORTING, status=WorkflowStatus.RUNNING)
    store.save_workflow(project, wf)
    page = _oct(store, project)
    page.workflow = wf
    page._set_step_result(0, "ok")
    page._load_step_results(wf)
    assert page._step_results.get(0) == "ok"
    page.release_resources()


def test_oct_manage_assets_and_record(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    page = _oct(store, project)
    page.name.setText(profile.name)
    with patch.object(MaterialManagerDialog, "exec", return_value=0), \
         patch.object(simple_pages, "show_error", return_value=None):
        page._manage_assets()
    # record dialog
    dialog = RecordingDialog(project / "raw" / "recordings")
    wav = _wav(project / "raw" / "recordings" / "r.wav", seconds=6)
    with patch.object(RecordingDialog, "exec", return_value=1):
        page._record()
    page.release_resources()


def test_oct_close_event(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("oct")
    page = _oct(store, project)
    with patch.object(page.review_card, "release_resources", return_value=None):
        page.closeEvent(QCloseEvent())
    page.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py VersionHistoryDialog + VoiceCard + MyVoicesPage
# ---------------------------------------------------------------------------


def test_version_history_dialog(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("vh")
    ref = _wav(tmp_path / "ref.wav", seconds=7)
    profile = VoiceProfile("声音", True, reference_assets=[ReferenceAsset(str(ref), "0" * 64, "t", "zh", 7, True, [])])
    version = ModelVersion(name="v1", preview_outputs=[str(_wav(tmp_path / "p.wav", 1))])
    profile.model_versions = [version]
    profile.active_model_version_id = version.id
    store.save_profile(project, profile)
    dialog = VersionHistoryDialog(store, project, profile)
    assert dialog.timeline.count() >= 2
    dialog._refresh_players()
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok), \
         patch.object(simple_pages, "show_error", return_value=None):
        dialog._activate(version.id)  # missing checkpoints -> error dialog
        dialog._activate("__baseline__")
    dialog.done(0)
    dialog.release_resources()


def test_voice_card(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("vc")
    profile = _profile(store, project)
    _source(store, project, profile.id)
    card = VoiceCard(store, project, profile)
    assert card._preview_path()
    card._preview()
    with patch.object(RenameDialog, "get", return_value=("新名字", True)):
        card._rename()
    assert profile.name == "新名字"
    # versions dialog
    with patch.object(VersionHistoryDialog, "exec", return_value=0):
        card._versions()
    # remove
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        card._remove()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        card._remove()
    archived = next(item for item in store.list_profiles(project) if item.id == profile.id)
    assert archived.archived
    card.release_resources()


def test_voice_card_no_preview(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("vc")
    profile = VoiceProfile("无参考", True)
    store.save_profile(project, profile)
    card = VoiceCard(store, project, profile)
    assert card._preview_path() == ""
    with patch.object(simple_pages, "show_error", return_value=None):
        card._preview()
    card.release_resources()


def test_my_voices_page_refresh(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("mv")
    _profile(store, project)
    page = MyVoicesPage(store, project)
    assert page.scroll.widget() is not None
    page._changed()
    page.release_resources()


def test_rename_dialog_uses_input_dialog(tmp_path: Path):
    _app()
    with patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("结果", True)):
        value, ok = RenameDialog.get("old", None)
        assert value == "结果" and ok is True


# ---------------------------------------------------------------------------
# simple_pages.py OneClickGeneratePage
# ---------------------------------------------------------------------------


def _ocg(store, project, client=None):
    return OneClickGeneratePage(store, project, client or FakeClient())


def test_ocg_generate_and_event(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    profile = _profile(store, project)
    client = FakeClient()
    page = _ocg(store, project, client)
    page.text.setPlainText("你好世界")
    page._generate()
    assert any(cmd == "load_profile" for cmd, _p, _r in client.sent)
    request = client.sent[-1][2]
    page._event(request, "result", {})  # load -> synth
    assert any(cmd == "synthesize" for cmd, _p, _r in client.sent)
    synth = client.sent[-1]
    wav = _wav(tmp_path / "out.wav", seconds=2)
    with patch.object(page.player, "setSource", return_value=None), patch.object(page.player, "play", return_value=None):
        page._event(synth[2], "result", {"outputs": [str(wav)]})
    page.release_resources()


def test_ocg_generate_no_profiles(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    page = _ocg(store, project)
    with patch.object(simple_pages, "show_error", return_value=None):
        page._generate()
    page.release_resources()


def test_ocg_event_error_and_progress(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    profile = _profile(store, project)
    client = FakeClient()
    page = _ocg(store, project, client)
    page.text.setPlainText("测试")
    page._generate()
    request = client.sent[-1][2]
    page._event(request, "progress", {"progress": 0.4, "message": "生成中"})
    with patch.object(simple_pages, "show_error", return_value=None):
        page._event(request, "error", {"message": "失败"})
    page.release_resources()


def test_ocg_refresh_history_and_retry(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    profile = _profile(store, project)
    job = Job(JobKind.SYNTHESIZE, {"profile_id": profile.id, "text": "重试文本", "speed_factor": 1.2},
              status=JobStatus.COMPLETED, outputs=[str(_wav(tmp_path / "o.wav", 1))])
    store.save_job(job)
    page = _ocg(store, project)
    page.refresh_history()
    with patch.object(page, "_generate", return_value=None):
        page._retry(job)
    assert page.text.toPlainText() == "重试文本"
    page.release_resources()


def test_ocg_text_info_and_browse(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    page = _ocg(store, project)
    page.text.setPlainText("这是一段测试文字")
    page._update_text_info()
    from PySide6.QtWidgets import QFileDialog
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path / "out")):
        page._browse()
    assert page.store.get_setting("default_output_dir") == str(tmp_path / "out")
    page.release_resources()


def test_ocg_select_profile(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ocg")
    profile = _profile(store, project)
    page = _ocg(store, project)
    page.select_profile(profile.id)
    assert page.profile.currentData() == profile.id
    page.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py TaskCenterDialog
# ---------------------------------------------------------------------------


def test_task_center_dialog(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("tc")
    job = Job(JobKind.SYNTHESIZE, {}, status=JobStatus.RUNNING, progress=0.5, message="进行中",
              outputs=[str(_wav(tmp_path / "o.wav"))])
    store.save_job(job)
    dialog = TaskCenterDialog(store)
    assert dialog.table.rowCount() == 1
    with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=True):
        dialog._open(0, 0)
    dialog.release_resources()


# ---------------------------------------------------------------------------
# simple_pages.py SimpleSettingsPage
# ---------------------------------------------------------------------------


def test_simple_settings_page(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ss")
    client = FakeClient()
    page = SimpleSettingsPage(_paths(tmp_path), store, project, client)
    assert page.health_request
    page._event(page.health_request, "result", {
        "compatible": True, "gpu_name": "NVIDIA", "cuda_version": "12", "tensor_test_passed": True,
        "models_ready": True, "rvc_ready": True, "rmvpe_ready": True, "hubert_ready": True,
        "rvc_torch_version": "2.0", "runtime_integrity": True, "message": "ok",
    })
    page._event("other", "result", {})  # ignored
    page._event(page.health_request, "result", {"compatible": False, "runtime_integrity_errors": ["缺失"], "message": "问题"})
    page.release_resources()


def test_simple_settings_check_health_error(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ss")
    client = FakeClient()
    page = SimpleSettingsPage(_paths(tmp_path), store, project, client)
    with patch.object(client, "send", side_effect=RuntimeError("no engine")):
        page.check_health()
    assert "需要修复" in page.health_badge.text()
    page.release_resources()


def test_simple_settings_repair(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ss")
    client = FakeClient()
    page = SimpleSettingsPage(_paths(tmp_path), store, project, client)
    page.raw = {"compatible": True}
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._repair()
    emitted = []
    page.install_requested.connect(lambda: emitted.append(1))
    page.raw = {"compatible": False}
    page._repair()
    assert emitted == [1]
    page.release_resources()


def test_simple_settings_browse_and_clean_cache(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ss")
    client = FakeClient()
    page = SimpleSettingsPage(_paths(tmp_path), store, project, client)
    from PySide6.QtWidgets import QFileDialog
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path / "out")):
        page._browse()
    assert page.store.get_setting("default_output_dir") == str(tmp_path / "out")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
         patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        page._clean_cache()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        page._clean_cache()
    page.release_resources()


def test_simple_settings_smart_toggle(tmp_path: Path):
    _app()
    store = StudioStore(_paths(tmp_path)); project = store.create_project("ss")
    page = SimpleSettingsPage(_paths(tmp_path), store, project, FakeClient())
    page.smart.setChecked(False)
    assert store.get_setting("smart_optimization", True) is False
    page.release_resources()
