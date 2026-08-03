from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import wave
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal, QUrl, Qt
from PySide6.QtWidgets import QApplication

from local_voice_studio.audio import AudioProbe, sha256_file
from local_voice_studio.models import Job, JobKind, JobStatus, ReferenceAsset, SourceAsset, VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.training import TrainingPipeline
from local_voice_studio.ui.pages import GeneratePage, TrainingPage, _friendly_error, _parse_asr_result
from local_voice_studio.worker import WorkerService


def write_wav(path: Path, seconds: float = 0.1, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate); stream.writeframes(b"\0\0" * int(seconds * rate))


class FakeClient(QObject):
    event = Signal(str, str, dict); stderr_line = Signal(str)
    def send(self, _command, _payload=None): return "request"


class RecordingClient(QObject):
    event = Signal(str, str, dict); stderr_line = Signal(str)
    def __init__(self):
        super().__init__(); self.commands = []
    def send(self, command, payload=None):
        request = f"request-{len(self.commands) + 1}"; self.commands.append((request, command, payload or {})); return request


def make_paths(root: Path) -> AppPaths:
    return AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3")


def make_generate_page(root: Path):
    paths = make_paths(root); store = StudioStore(paths); project = store.create_project("生成测试"); client = RecordingClient()
    ref = root / "reference.wav"; write_wav(ref, 6)
    profile = VoiceProfile("测试声音", True, reference_assets=[ReferenceAsset(str(ref), "hash", "参考文本", duration_seconds=6, approved=True)])
    store.save_profile(project, profile); page = GeneratePage(store, project, client); page.text.setPlainText("第一句。第二句。")
    return store, page, client


def test_generate_state_machine_preview_happy_path():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        store, page, client = make_generate_page(Path(tmp)); page._preview_generate()
        load_id = client.commands[-1][0]; assert [item[1] for item in client.commands] == ["load_profile"]
        page._on_event(load_id, "result", {}); synth_id = client.commands[-1][0]; assert [item[1] for item in client.commands] == ["load_profile", "synthesize"]
        preview = Path(tmp) / "preview.wav"; write_wav(preview); page._on_event(synth_id, "result", {"preview": True, "outputs": [str(preview)]})
        assert not page.pending and page.active_job is None and len(client.commands) == 2
        assert store.list_jobs()[0].status == JobStatus.COMPLETED
        page.player.stop(); page.player.setSource(QUrl()); app.processEvents(); page.deleteLater(); app.processEvents()


def test_generate_state_machine_formal_happy_path():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        store, page, client = make_generate_page(Path(tmp)); page._generate(); load_id = client.commands[-1][0]
        page._on_event(load_id, "result", {}); synth_id = client.commands[-1][0]; wav = Path(tmp) / "out.wav"; mp3 = Path(tmp) / "out.mp3"; write_wav(wav); mp3.write_bytes(b"ID3")
        page._on_event(synth_id, "result", {"preview": False, "outputs": [str(wav), str(mp3)]})
        assert [item[1] for item in client.commands] == ["load_profile", "synthesize"] and not page.pending
        job = store.list_jobs()[0]; assert job.status == JobStatus.COMPLETED and job.outputs == [str(wav), str(mp3)]
        page.deleteLater(); app.processEvents()


@pytest.mark.parametrize("failure_stage", ["load_profile", "synthesize"])
def test_generate_state_machine_errors_finish_once(monkeypatch, failure_stage):
    app = QApplication.instance() or QApplication([]); shown = []; monkeypatch.setattr("local_voice_studio.ui.pages._show_error", lambda _parent, message: shown.append(message))
    with tempfile.TemporaryDirectory() as tmp:
        store, page, client = make_generate_page(Path(tmp)); page._generate(); request = client.commands[-1][0]
        if failure_stage == "synthesize": page._on_event(request, "result", {}); request = client.commands[-1][0]
        page._on_event(request, "error", {"message": "模拟失败"}); page._on_event(request, "error", {"message": "重复错误"})
        assert not page.pending and page.active_job is None and store.list_jobs()[0].status == JobStatus.FAILED and shown == ["模拟失败"]
        page.deleteLater(); app.processEvents()


def test_preview_replacement_while_loading_never_synthesizes_old_text():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        _store, page, client = make_generate_page(Path(tmp)); page._preview_generate(); old_load = client.commands[-1][0]
        page.text.setPlainText("替换后的文字"); page._preview_generate(); assert [item[1] for item in client.commands] == ["load_profile"]
        page._on_event(old_load, "result", {}); app.processEvents()
        assert [item[1] for item in client.commands] == ["load_profile", "load_profile"]
        assert all(command != "synthesize" for _, command, _ in client.commands)
        page.deleteLater(); app.processEvents()


def test_preview_replacement_while_synthesizing_cancels_then_restarts():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        _store, page, client = make_generate_page(Path(tmp)); page._preview_generate(); page._on_event(client.commands[-1][0], "result", {}); old_synth = client.commands[-1][0]
        page.text.setPlainText("更新试听"); page._preview_generate(); assert client.commands[-1][1] == "cancel"
        page._on_event(old_synth, "error", {"message": "任务已取消", "status": "cancelled"}); app.processEvents()
        assert [item[1] for item in client.commands] == ["load_profile", "synthesize", "cancel", "load_profile"]
        page.deleteLater(); app.processEvents()


def test_two_real_preparation_flows_only_process_current_selection(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); paths = make_paths(root); paths.ensure(); paths.private_python.parent.mkdir(parents=True, exist_ok=True); paths.private_python.write_bytes(b"python"); ffmpeg = paths.data_root / "tools" / "ffmpeg.exe"; ffmpeg.parent.mkdir(parents=True); ffmpeg.write_bytes(b"ffmpeg")
        project = paths.projects_root / "project"; project.mkdir(parents=True); sources = []
        for name in ("a", "b", "c"):
            audio = root / f"{name}.wav"; write_wav(audio); sources.append(SourceAsset("voice", str(audio), str(audio), sha256_file(audio), id=name).to_dict())
        real_run = subprocess.run
        def fake_run(command, *args, **kwargs):
            if command[-1] == "-version": return subprocess.CompletedProcess(command, 0)
            write_wav(Path(command[-1])); return subprocess.CompletedProcess(command, 0, "", "")
        monkeypatch.setattr(subprocess, "run", fake_run)
        pipeline = TrainingPipeline(paths)
        def fake_stage(command, _env, _log, _cancel):
            if "slice_audio.py" in command[2]:
                source_dir, target_dir = Path(command[3]), Path(command[4]); target_dir.mkdir(parents=True, exist_ok=True)
                for source in source_dir.glob("*.wav"): shutil.copy2(source, target_dir / source.name)
            elif "local_voice_studio.asr_cli" in command:
                source_dir, target_dir = Path(command[command.index("-i") + 1]), Path(command[command.index("-o") + 1]); target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "result.list").write_text("\n".join(f"{item}|speaker|zh|文本" for item in source_dir.glob("*.wav")) + "\n", encoding="utf-8")
        pipeline._run = fake_stage
        common = {"action": "pipeline", "profile_id": "voice", "source_assets": sources, "project_path": str(project), "processing_options": {"language": "zh"}}
        first = pipeline.prepare({**common, "preparation_id": "run-abc", "source_asset_ids": ["a", "b", "c"]}, lambda *_: None, threading.Event())
        second = pipeline.prepare({**common, "preparation_id": "run-a", "source_asset_ids": ["a"]}, lambda *_: None, threading.Event())
        first_data = json.loads(first.read_text(encoding="utf-8")); second_data = json.loads(second.read_text(encoding="utf-8"))
        assert len(list(Path(first_data["normalized_dir"]).glob("*.wav"))) == 3
        assert [item.stem for item in Path(second_data["normalized_dir"]).glob("*.wav")] == ["a"]
        assert len(list(Path(second_data["segments_dir"]).glob("*.wav"))) == 1 and first.parent != second.parent
        monkeypatch.setattr(subprocess, "run", real_run)


def test_failed_preparation_does_not_replace_last_success(monkeypatch):
    app = QApplication.instance() or QApplication([]); monkeypatch.setattr("local_voice_studio.ui.pages._show_error", lambda *_: None)
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp)); store = StudioStore(paths); project = store.create_project("准备失败"); client = RecordingClient(); profile = VoiceProfile("声音", True, current_preparation_id="good", current_preparation_manifest="good.json"); store.save_profile(project, profile); page = TrainingPage(store, project, client)
        job = Job(JobKind.PREPARE_DATASET, {"action": "pipeline", "profile_id": profile.id, "preparation_id": "bad"}); page.active_requests["failed"] = job; page._on_event("failed", "error", {"message": "失败"})
        loaded = store.list_profiles(project)[0]; assert loaded.current_preparation_id == "good" and loaded.current_preparation_manifest == "good.json"
        page.deleteLater(); app.processEvents()


def test_candidate_and_ab_state_restore_after_page_restart():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); paths = make_paths(root); store = StudioStore(paths); project = store.create_project("候选恢复"); gpt = root / "candidate.ckpt"; sovits = root / "candidate.pth"; gpt.write_bytes(b"gpt"); sovits.write_bytes(b"sovits")
        profile = VoiceProfile("声音", True, candidate_gpt_checkpoint=str(gpt), candidate_sovits_checkpoint=str(sovits), candidate_training_run_id="run", candidate_dataset_snapshot_id="snapshot", candidate_snapshot_sha256="sha", ab_status="awaiting_ab"); store.save_profile(project, profile)
        first = TrainingPage(store, project, RecordingClient()); assert first.ab_button.isEnabled(); first.deleteLater(); app.processEvents()
        second = TrainingPage(store, project, RecordingClient()); assert second.ab_button.isEnabled() and second.latest_checkpoints == {"gpt": str(gpt), "sovits": str(sovits)}
        second.deleteLater(); app.processEvents()


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
