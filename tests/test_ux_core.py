from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from local_voice_studio.models import (
    DatasetDraft, DatasetDraftSegment, Job, JobKind, JobStatus, ReferenceAsset,
    TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus,
)
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.project_session import ProjectSession
from local_voice_studio.ui.recording import analyse_pcm_quality
from local_voice_studio.ui.simple_pages import OneClickGeneratePage, OneClickTrainingPage, RecordingDialog
from local_voice_studio.ui.widgets import GenerationRecordCard, StepTimeline, estimate_text_work


class FakeClient(QObject):
    event = Signal(str, str, dict)

    def send(self, command, payload=None, request_id=None):
        return request_id or f"fake-{command}"


def paths_for(root: Path) -> AppPaths:
    return AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data" / "studio.sqlite3")


def write_wav(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b"\0\0" * round(8000 * seconds))


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_project_session_remembers_switch_and_display_rename():
    app()
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); first = store.create_project("小说"); second = store.create_project("游戏配音")
        session = ProjectSession(store); session.activate(second)
        restored = ProjectSession(store); assert restored.current == second.resolve()
        assert restored.rename("角色对白") == "角色对白"
        assert store.load_project(second)["name"] == "角色对白"
        assert restored.projects()[0]["path"] == str(second.resolve())
        session.activate(first); assert store.get_setting(ProjectSession.LAST_PROJECT_KEY) == str(first.resolve())


def test_step_timeline_exposes_six_results_not_only_progress():
    app(); timeline = StepTimeline(("检查", "切片", "识别", "确认", "训练", "验证")); timeline.update_state(3, {0: "33 个文件", 1: "150 个片段", 2: "8 个需确认"})
    assert len(timeline.labels) == 6
    assert timeline.labels[0].text().startswith("✓") and timeline.labels[3].text().startswith("●")
    assert timeline.results[2].text() == "8 个需确认"


def test_review_card_edits_are_saved_immediately():
    app()
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); project = store.create_project("审核"); profile = VoiceProfile("声音", True); store.save_profile(project, profile)
        audio = project / "processed" / "clip.wav"; write_wav(audio, 3)
        workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.REVIEW_REQUIRED, status=WorkflowStatus.WAITING)
        draft = DatasetDraft(workflow.id, profile.id, "prep", [DatasetDraftSegment(audio.relative_to(project).as_posix(), 0, 3, text="旧文本", asr_text="旧文本", quality_flags=["疑似配乐"], included=False)])
        store.save_workflow(project, workflow); store.save_draft(project, draft); workflow.draft_id = draft.id
        page = OneClickTrainingPage(store, project, FakeClient()); page.workflow = workflow; page._show_draft(draft)
        page.review_card.text.setText("已经人工修改"); page.review_card.include.setChecked(True); page.review_card.text.editingFinished.emit()
        stored = store.load_draft(project, draft.id).segments[0]
        assert stored.text == "已经人工修改" and stored.included is True
        page.release_resources(); QApplication.processEvents()


def test_generation_history_cards_and_segment_estimate_use_saved_jobs():
    app()
    with tempfile.TemporaryDirectory() as tmp:
        store = StudioStore(paths_for(Path(tmp))); project = store.create_project("生成历史"); reference = project / "raw" / "ref.wav"; output = project / "exports" / "voice.wav"; write_wav(reference, 6); write_wav(output, 2)
        profile = VoiceProfile("旁白", True, reference_assets=[ReferenceAsset(str(reference), "hash", "你好", duration_seconds=6, approved=True)]); store.save_profile(project, profile)
        job = Job(JobKind.SYNTHESIZE, {"profile_id": profile.id, "text": "今天测试生成历史", "speed_factor": 1.0}, status=JobStatus.COMPLETED, outputs=[str(output)]); store.save_job(job)
        page = OneClickGeneratePage(store, project, FakeClient())
        assert len(page.history.findChildren(GenerationRecordCard)) == 1
        page.text.setPlainText("测试。" * 100); QApplication.processEvents(); characters, segments = estimate_text_work(page.text.toPlainText())
        assert characters == 300 and segments > 1 and "将自动分成" in page.text_info.text()
        page.release_resources(); QApplication.processEvents()


def test_recording_quality_and_sixty_second_gate():
    app(); quiet = analyse_pcm_quality(b"\0\0" * 4800); clipped = analyse_pcm_quality((32767).to_bytes(2, "little", signed=True) * 4800)
    assert quiet["volume"] == "偏低" and clipped["clipping"] is True
    with tempfile.TemporaryDirectory() as tmp:
        dialog = RecordingDialog(Path(tmp));
        for index in range(10):
            path = Path(tmp) / f"{index}.wav"; write_wav(path, 6); dialog._saved(str(path), 6)
        assert dialog.finish_button.isEnabled() and dialog.total.value() == 600
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No): dialog.reject(); assert all(path.exists() for path in dialog.paths)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes): dialog.reject(); assert not any(path.exists() for path in dialog.paths)
