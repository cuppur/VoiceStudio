from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import wave
from pathlib import Path

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal, QUrl, Qt
from PySide6.QtWidgets import QApplication

from local_voice_studio.audio import AudioProbe
from local_voice_studio.models import ReferenceAsset, VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.pages import GeneratePage, TrainingPage, _friendly_error, _parse_asr_result
from local_voice_studio.worker import WorkerService


def write_wav(path: Path, seconds: float = 0.1, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate); stream.writeframes(b"\0\0" * int(seconds * rate))


class FakeClient(QObject):
    event = Signal(str, str, dict); stderr_line = Signal(str)
    def send(self, _command, _payload=None): return "request"


def make_paths(root: Path) -> AppPaths:
    return AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3")


def test_profile_dropdown_refreshes_immediately():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp)); store = StudioStore(paths); project = store.create_project("中文项目"); client = FakeClient(); page = GeneratePage(store, project, client)
        ref = Path(tmp) / "参考.wav"; write_wav(ref, 6); profile = VoiceProfile("新声音", True, reference_assets=[ReferenceAsset(str(ref), "h", "你好", duration_seconds=6, approved=True)])
        store.save_profile(project, profile); page.refresh_profiles()
        assert page.profile.findData(profile.id) >= 0
        page.player.setSource(QUrl.fromLocalFile(str(ref))); assert Path(page.player.source().toLocalFile()).resolve() == ref.resolve(); page.player.stop(); page.player.setSource(QUrl()); app.processEvents(); page.deleteLater(); app.processEvents()
    app.processEvents()


def test_preview_only_writes_temporary_wav_and_export_writes_mp3():
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp)); service = WorkerService(paths); service.profile = {"id": "p"}; events = []
        service.emit = lambda request, event, payload: events.append((request, event, payload))
        service.engine.load = lambda *_args, **_kwargs: None
        service.engine.synthesize_segment = lambda _payload, destination: (write_wav(destination) or (16000, destination, 1600))
        def merge(wavs, merged, _pause): write_wav(merged); return [merged]
        def encode(wavs, merged, mp3, _pause): write_wav(merged); mp3.write_bytes(b"ID3-real-export"); return [merged, mp3]
        service.engine.merge_wavs = merge; service.engine.merge_and_encode = encode
        service._synthesize("preview", {"text": "中文试听", "preview": True, "output_dir": str(Path(tmp) / "exports")})
        preview = events[-1][2]; assert preview["preview"] is True; assert len(preview["outputs"]) == 1; assert Path(preview["outputs"][0]).suffix == ".wav"; assert not list((paths.data_root / "cache" / "preview").rglob("*.mp3"))
        service._synthesize("export", {"text": "正式导出", "preview": False, "output_dir": str(Path(tmp) / "exports")})
        outputs = events[-1][2]["outputs"]; assert {Path(item).suffix for item in outputs} == {".wav", ".mp3"}


def test_missing_torch_message_is_chinese_and_actionable():
    value = _friendly_error("ModuleNotFoundError: No module named 'torch'")
    assert "设置" in value and "安装/修复" in value and "PyTorch" in value


def test_training_gate_uses_human_confirmation_not_import_duration():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp)); store = StudioStore(paths); project = store.create_project("训练项目"); client = FakeClient(); profile = VoiceProfile("训练声音", True, dataset_snapshot_id="snapshot"); store.save_profile(project, profile); page = TrainingPage(store, project, client)
        audio = Path(tmp) / "训练.wav"; write_wav(audio, 61); page._append_probe(AudioProbe(str(audio), "hash", 61, 16000, 1, "pcm"), "准确文本")
        page._update_duration(); assert not page.train.isEnabled()
        page.dataset_table.item(0, 2).setCheckState(Qt.Checked); page._update_duration(); assert page.train.isEnabled()
        page.review_player.stop(); page.review_player.setSource(QUrl()); page.deleteLater(); app.processEvents()


def test_imported_source_assets_reach_prepare_dataset_without_recording():
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp)); service = WorkerService(paths); seen = {}; events = []
        service.training.prepare = lambda payload, _progress, _cancel: (seen.update(payload) or Path(tmp) / "preparation.json")
        service.emit = lambda request, event, payload: events.append((request, event, payload))
        service._prepare_dataset("prepare", {"action": "pipeline", "profile_id": "p", "source_asset_ids": ["imported"], "source_assets": [{"id": "imported"}]})
        assert seen["source_asset_ids"] == ["imported"]; assert events[-1][1] == "result"


def test_sensevoice_tags_become_language_and_quality_flags():
    language, text, flags = _parse_asr_result("<|en|><|HAPPY|><|BGM|>hello world", "zh")
    assert language == "en"
    assert text == "hello world"
    assert flags == ["疑似伴奏"]
