from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from local_voice_studio.models import GenerationRecord
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.html_pages import HtmlPage, RecentProjectsPage


def make_store(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = StudioStore(AppPaths(tmp_path, tmp_path / "projects", tmp_path / "runtime",
                                 tmp_path / "engine", tmp_path / "models", tmp_path / "logs",
                                 tmp_path / "studio.sqlite3"))
    return app, store, store.create_project("真实项目")


def test_recent_project_emits_saved_path(tmp_path):
    app, store, project = make_store(tmp_path)
    page = HtmlPage("工程", "", store, project, "recent")
    selected = []
    page.project_selected.connect(selected.append)
    assert page.items.count() == 1
    page._activate_item(page.items.item(0))
    assert selected == [str(project)]
    page.close()


def test_recent_projects_create_button_requests_actual_project_creation(tmp_path):
    app, store, project = make_store(tmp_path)
    page = RecentProjectsPage(store, project)
    requested = []
    page.project_create_requested.connect(lambda: requested.append(True))
    page.create_button.click()
    assert requested == [True]
    page.close()


def test_recent_projects_use_clickable_three_column_visual_cards(tmp_path):
    app, store, project = make_store(tmp_path)
    store.create_project("第二个真实工程")
    store.create_project("第三个真实工程")
    page = RecentProjectsPage(store, project)
    selected = []
    page.project_selected.connect(selected.append)
    first = page.grid.itemAtPosition(0, 0).widget()
    third = page.grid.itemAtPosition(0, 2).widget()
    assert first.objectName() == "recentProjectCard"
    assert third.objectName() == "recentProjectCard"
    QTest.mouseClick(first, Qt.LeftButton)
    assert selected == [first.toolTip()]
    page.close()


def test_exports_only_lists_existing_record_files(tmp_path):
    app, store, project = make_store(tmp_path)
    output = project / "test.wav"
    # Existence contract only: this fixture is never represented as playable audio.
    output.touch()
    store.save_generation_record(project, GenerationRecord(
        project_uid=store.load_project(project)["project_uid"], voice_profile_id="test", text="test",
        wav_path=str(output), mp3_path=str(project / "missing.mp3")))
    page = HtmlPage("导出", "", store, project, "export")
    assert page.items.count() == 1
    assert page.items.item(0).text() == output.name
    output.unlink()
    page.refresh()
    assert page.items.count() == 0
    page.close()


def test_separation_selection_and_cancel(tmp_path):
    app, store, project = make_store(tmp_path)
    page = HtmlPage("分离", "", store, project, "separation")
    selected = []
    page.audio_selected.connect(selected.append)
    with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
        page.import_button.click()
    assert selected == []
    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(tmp_path / "song.wav"), "")):
        page.import_button.click()
    assert selected == [str(tmp_path / "song.wav")]
    page.close()


def test_separation_checks_private_tool_not_system_path(tmp_path):
    app, store, project = make_store(tmp_path)
    page = HtmlPage("分离", "", store, project, "separation")
    assert page.state.text() == "未就绪"
    tool = store.paths.data_root / "tools" / "ffmpeg.exe"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.touch()  # Resolver existence fixture; no executable success is claimed.
    page.refresh()
    assert page.state.text() == "待检查模型"
    assert str(tool.resolve()) in page.runtime.body.text()
    page.close()


def test_exports_exclude_source_and_intermediate_audio(tmp_path):
    app, store, project = make_store(tmp_path)
    cover = project / "covers" / "song"
    for folder, filename in (("source", "original.wav"), ("stems", "vocals.wav"), ("exports", "final.wav")):
        target = cover / folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    page = HtmlPage("导出", "", store, project, "export")
    assert [page.items.item(i).text() for i in range(page.items.count())] == ["final.wav"]
    page.close()
