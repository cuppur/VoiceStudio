from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from local_voice_studio.models import DatasetDraft, DatasetDraftSegment, TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow
from local_voice_studio.ui.worker_client import WorkerClient
from local_voice_studio.workflow import freeze_draft, parse_asr_result


def paths_for(root: Path) -> AppPaths:
    return AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data" / "studio.sqlite3")


def wav(path: Path, seconds: float = 6.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b"\0\0" * int(8000 * seconds))


def test_workflow_and_draft_roundtrip_and_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); project = store.create_project("小白模式")
        profile = VoiceProfile("我的声音", True, consent_record="已授权", consent_confirmed_at="2026-01-01T00:00:00Z"); store.save_profile(project, profile)
        workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.TRAINING, status=WorkflowStatus.RUNNING)
        store.save_workflow(project, workflow)
        draft = DatasetDraft(workflow.id, profile.id, "prep", [DatasetDraftSegment("processed/a.wav", 0, 6, asr_text="你好", text="已修改", included=True, human_confirmed=False)])
        store.save_draft(project, draft)
        assert store.load_draft(project, draft.id).segments[0].text == "已修改"
        recovered = store.recover_workflows(project)[0]
        assert recovered.status == WorkflowStatus.INTERRUPTED


def test_asr_music_and_empty_tags_are_not_silently_accepted():
    language, text, flags = parse_asr_result("<|zh|><|BGM|><|nospeech|>")
    assert language == "zh" and not text
    assert "疑似配乐" in flags and "ASR 检测到异常" in flags and "空文本" in flags
    language, text, flags = parse_asr_result("down on you <|en|><|Speech|>that")
    assert language == "en" and text == "down on you that" and not flags


def test_freeze_requires_confirmation_and_hashes_audio():
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); project = store.create_project("冻结")
        profile = VoiceProfile("声音", True, consent_record="已授权", consent_confirmed_at="2026-01-01T00:00:00Z"); store.save_profile(project, profile)
        segments = []
        for index in range(10):
            path = project / "processed" / f"clip-{index}.wav"; wav(path)
            segments.append(DatasetDraftSegment(path.relative_to(project).as_posix(), 0, 6, text=f"句子{index}", asr_text=f"句子{index}", included=True, human_confirmed=index > 0))
        draft = DatasetDraft("workflow", profile.id, "prep", segments)
        try:
            freeze_draft(store, project, profile, draft)
            assert False, "59 秒以下应被阻止"
        except ValueError:
            pass
        segments[0].human_confirmed = True
        dataset = freeze_draft(store, project, profile, draft)
        assert dataset.approved_seconds == 60
        assert all(item.source_sha256 and item.audio_relative_path for item in dataset.segments)
        assert store.load_dataset_snapshot(project, dataset.id).snapshot_sha256 == dataset.snapshot_sha256


def test_main_navigation_exposes_cover_studio_and_legacy_pages():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); store.create_project("界面")
        with patch.object(WorkerClient, "start", lambda self: None), patch.object(WorkerClient, "shutdown", lambda self: None), patch.object(WorkerClient, "send", lambda self, command, payload=None: f"fake-{command}"):
            window = MainWindow(store.paths, store)
            labels = [window.navigation.item(i).text() for i in range(window.navigation.count())]
            assert [label.split()[-1] for label in labels] == ["翻唱", "文字生成", "我的声音", "训练声音", "设置"]
            assert window.stack.currentWidget() is window.cover_page
            assert window.cover_page.import_button.text() == "导入歌曲"
            assert window.minimumWidth() <= 1280 and window.minimumHeight() <= 720
            window.close()
    app.processEvents()
