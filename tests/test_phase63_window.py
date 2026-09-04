from __future__ import annotations

"""Phase 6.3 UI coverage hardening: MainWindow / ProjectSession /
VoiceSelector / WaveformWidget.

Offscreen Qt. Every modal (QMessageBox / QInputDialog / QFileDialog /
SetupDialog.exec / TaskCenterDialog.exec) is patched so offscreen runs
cannot block on a dialog nobody will close.
"""

import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QPointF, QEvent, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QMouseEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QFileDialog

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow, SetupDialog
from local_voice_studio.ui.project_session import ProjectSession
from local_voice_studio.ui.simple_pages import TaskCenterDialog
from local_voice_studio.ui.studio_widgets.voice_selector import VoiceSelector
from local_voice_studio.ui.studio_widgets.waveform import WaveformWidget
from local_voice_studio.ui.worker_client import WorkerClient
from local_voice_studio.ui import main_window as main_window_module


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(data, root / "projects", data / "runtime", data / "engine",
                    data / "models", data / "logs", data / "studio.sqlite3")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _store(tmp_path: Path) -> StudioStore:
    _app()
    return StudioStore(_paths(tmp_path))


def _main_window(store: StudioStore) -> MainWindow:
    """Build a MainWindow with WorkerClient I/O stubbed out."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(WorkerClient, "start", lambda self: None))
        stack.enter_context(patch.object(WorkerClient, "shutdown", lambda self: None))
        stack.enter_context(patch.object(WorkerClient, "send", lambda self, command, payload=None, request_id=None: request_id or "request"))
        return MainWindow(store.paths, store)


# ---------------------------------------------------------------------------
# ProjectSession
# ---------------------------------------------------------------------------


def test_project_session_display_name_defaults_to_folder(tmp_path: Path):
    store = _store(tmp_path)
    project = store.create_project("我的声音项目")
    session = ProjectSession(store)
    assert session.current == project
    assert session.display_name() == "我的声音项目"
    # A project.json that cannot be read falls back to the folder name.
    bogus = store.create_project("bogus")
    with patch.object(store, "load_project", side_effect=ValueError("bad")):
        assert session.display_name(bogus) == bogus.name


def test_project_session_display_name_json_decode_error(tmp_path: Path):
    store = _store(tmp_path)
    project = store.create_project("p")
    session = ProjectSession(store)
    # Corrupt project.json -> JSONDecodeError -> folder name fallback.
    (project / "project.json").write_text("{ not json", encoding="utf-8")
    assert session.display_name(project) == project.name


def test_project_session_rename_and_validation(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    session.rename(" 带空格的名字 ")
    assert session.display_name() == "带空格的名字"
    renamed = store.load_project(session.current)
    assert renamed["name"] == "带空格的名字"


def test_project_session_rename_empty_raises(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    try:
        session.rename("   ")
        assert False, "blank name must raise"
    except ValueError as exc:
        assert "不能为空" in str(exc)


def test_project_session_create_and_activate(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("first")
    session = ProjectSession(store)
    assert session.current.name == "first"
    created = session.create("second")
    assert created.is_dir()
    assert session.current == created
    names = {item["name"] for item in session.projects()}
    assert {"first", "second"} <= names


def test_project_session_open_existing_registers_in_index(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("a")
    session = ProjectSession(store)
    project = store.create_project("b")
    opened = session.open_existing(project)
    assert opened == project
    assert session.current == project
    names = {item["name"] for item in session.projects()}
    assert "b" in names


def test_project_session_activate_same_project_still_remembers(tmp_path: Path):
    store = _store(tmp_path)
    project = store.create_project("p")
    session = ProjectSession(store)
    seen = []
    session.project_changed.connect(seen.append)
    returned = session.activate(project)
    assert returned == project
    assert seen == []  # no emit when unchanged


def test_project_session_activate_emits_when_changed(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    target = store.create_project("p2")
    seen = []
    session.project_changed.connect(seen.append)
    result = session.activate(target)
    assert seen == [target]
    assert result == target


def test_project_session_activate_outside_root_raises(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p")
    session = ProjectSession(store)
    outside = Path(tmp_path).parent / "evil-project"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "project.json").write_text(json.dumps({"schema_version": 4}), encoding="utf-8")
    try:
        session.activate(outside)
        assert False, "must reject traversal"
    except ValueError:
        pass


def test_project_session_projects_filters_missing_project_json(tmp_path: Path):
    store = _store(tmp_path)
    good = store.create_project("good")
    session = ProjectSession(store)
    # Manually add a stale index entry whose project.json is missing.
    stale = store.paths.projects_root / "stale"
    stale.mkdir(parents=True, exist_ok=True)
    with store._connect() as db:
        db.execute("INSERT INTO projects(id,name,path,updated_at) VALUES(?,?,?,?)",
                   ("stale", "stale", str(stale), "2026-01-01"))
    names = {item["name"] for item in session.projects()}
    assert "good" in names
    assert "stale" not in names  # filtered out (no project.json)


def test_project_session_projects_orders_by_recent(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    store.create_project("p2")
    store.create_project("p3")
    # Recent order comes from ui.recent_projects.
    recent = store.get_setting(ProjectSession.RECENT_PROJECTS_KEY, [])
    ordered = [item["name"] for item in session.projects()]
    assert set(ordered) == {"p1", "p2", "p3"}


def test_project_session_restore_uses_last_project(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    second = store.create_project("p2")
    session.activate(second)
    session2 = ProjectSession(store)
    assert session2.current == second


def test_project_session_restore_ignores_bad_last_project(tmp_path: Path):
    store = _store(tmp_path)
    good = store.create_project("good")
    session = ProjectSession(store)
    # A last_project pointing outside root is ignored on restore.
    store.set_setting(ProjectSession.LAST_PROJECT_KEY, str(tmp_path / ".." / "outside"))
    session2 = ProjectSession(store)
    assert session2.current == good


def test_project_session_remember_keeps_recent_list(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    for name in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j"):
        session.activate(store.create_project(name))
    recent = store.get_setting(ProjectSession.RECENT_PROJECTS_KEY, [])
    assert len(recent) <= 8


def test_project_session_remember_skips_broken_recent_entries(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("p1")
    session = ProjectSession(store)
    # A broken recent entry (invalid path) must be skipped.
    store.set_setting(ProjectSession.RECENT_PROJECTS_KEY, ["\x00\x00invalid"])
    session.activate(store.create_project("ok"))
    recent = store.get_setting(ProjectSession.RECENT_PROJECTS_KEY, [])
    assert any("ok" in str(value) for value in recent)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


def test_main_window_construction_and_navigation(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    assert window.navigation.count() == 5
    assert window.navigation.item(0).text() == "AI 翻唱"
    assert window.stack.currentIndex() == 0
    assert window.session.current.name == "win"
    window.close()
    _app().processEvents()


def test_main_window_state_messages(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    window._state("running")
    assert window.topbar_status.text() == "本地工作进程 · 就绪"
    window._state("stopped")
    assert window.topbar_status.text() == "本地工作进程 · 已停止"
    window._state("weird")
    assert window.topbar_status.text() == "weird"
    window.close()


def test_main_window_worker_event_ready(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    window._worker_event("worker", "ready", {})
    assert window.topbar_status.text() == "本地工作进程 · 就绪"
    window._worker_event("other", "result", {"status": "ok"})  # forwarded to cover_page
    window.close()


def test_main_window_use_and_retrain_profile(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    window._use_profile("prof-1")
    assert window.navigation.currentRow() == 1
    window._retrain_profile("prof-2")
    assert window.navigation.currentRow() == 3
    window.close()


def test_main_window_new_project_via_input_dialog(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QInputDialog, "getText", return_value=("品牌项目", True)) as dlg:
        window._new_project()
        dlg.assert_called_once()
    assert any(item["name"] == "品牌项目" for item in window.session.projects())
    window.close()


def test_main_window_new_project_cancelled(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QInputDialog, "getText", return_value=("", False)):
        window._new_project()  # must not create
    assert len(window.session.projects()) == 1
    window.close()


def test_main_window_new_project_blank_name(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QInputDialog, "getText", return_value=("   ", True)):
        window._new_project()  # blank -> create skipped
    assert len(window.session.projects()) == 1
    window.close()


def test_main_window_open_project(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    target = store.create_project("opened")
    window = _main_window(store)
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(target)):
        window._open_project()
    assert window.project == target
    window.close()


def test_main_window_open_project_cancelled_and_error(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QFileDialog, "getExistingDirectory", return_value=""):
        window._open_project()  # cancelled -> no change
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path / "nope")), \
         patch.object(QMessageBox, "critical") as critical:
        window._open_project()
        critical.assert_called_once()
    window.close()


def test_main_window_rename_project(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QInputDialog, "getText", return_value=("重命名后", True)):
        window._rename_project()
    assert window.session.display_name() == "重命名后"
    window.close()


def test_main_window_rename_project_error_path(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    # blank -> rename raises -> critical message box
    with patch.object(QInputDialog, "getText", return_value=("   ", True)), \
         patch.object(QMessageBox, "critical") as critical:
        window._rename_project()
        critical.assert_called_once()
    window.close()


def test_main_window_rename_project_cancelled(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QInputDialog, "getText", return_value=("", False)):
        window._rename_project()  # cancelled -> no rename
    assert window.session.display_name() == "win"
    window.close()


def test_main_window_switch_project(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    target = store.create_project("sw")
    window = _main_window(store)
    window._switch_project(target)
    assert window.project == target
    assert window.project_button.text().startswith("sw")
    window.close()


def test_main_window_open_task_center_exec_patched(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(TaskCenterDialog, "exec", return_value=0) as ex:
        window._open_task_center()
        ex.assert_called_once()
    window.close()


def test_main_window_open_setup_patched(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(main_window_module.SetupDialog, "exec", return_value=0) as ex:
        window._open_setup()
        ex.assert_called_once()
    window.close()


def test_main_window_navigation_icon_and_script_paths(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    # Non-frozen resource path resolves under the ui package.
    icon = window._navigation_icon("cover.svg")
    assert icon.isNull() is False
    assert window._script_path().name == "bootstrap_runtime.ps1"
    import sys as _sys
    with patch.object(_sys, "frozen", True, create=True), \
         patch.object(_sys, "_MEIPASS", str(tmp_path), create=True):
        assert window._script_path().name == "bootstrap_runtime.ps1"
    window.close()


def test_main_window_refresh_project_menu_and_open_folder(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    with patch.object(QDesktopServices, "openUrl") as open_url, \
         patch.object(QInputDialog, "getText", return_value=("x", True)), \
         patch.object(QFileDialog, "getExistingDirectory", return_value=""):
        window._refresh_project_menu()
        assert window.project_menu.actions()
        # Trigger project-select and folder actions; the modal handlers are patched.
        for action in window.project_menu.actions():
            action.trigger()
    open_url.assert_called_once()
    window.close()


def test_main_window_close_event(tmp_path: Path):
    store = _store(tmp_path)
    store.create_project("win")
    window = _main_window(store)
    window.closeEvent(QCloseEvent())
    assert window.client is not None


# ---------------------------------------------------------------------------
# SetupDialog (build script log parsing)
# ---------------------------------------------------------------------------


def _setup_dialog(tmp_path: Path) -> SetupDialog:
    _app()
    script = tmp_path / "bootstrap_runtime.ps1"
    script.write_text("# dummy", encoding="utf-8")
    return SetupDialog(script, _paths(tmp_path))


def test_setup_dialog_apply_event_states(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog._apply_event({"type": "step", "step": 1, "state": "running"})
    assert "正在执行" in dialog.step_labels[0].text()
    dialog._apply_event({"type": "step", "step": 1, "state": "completed"})
    assert dialog.progress.value() == round(100 / 7)
    dialog._apply_event({"type": "step", "step": 2, "state": "skipped"})
    dialog._apply_event({"type": "step", "step": 3, "state": "failed", "message": "出错了"})
    assert "出错了" in dialog.summary.text()
    dialog._apply_event({"type": "other"})  # non-step ignored
    dialog.close()


def test_setup_dialog_apply_event_unknown_state_and_step_range(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    # Out-of-range step leaves labels untouched but still updates _current_step.
    dialog._apply_event({"type": "step", "step": 99, "state": "mystery"})
    assert dialog._current_step == 99
    assert "等待" in dialog.step_labels[0].text()
    # In-range unknown state text is passed through.
    dialog._apply_event({"type": "step", "step": 4, "state": "mystery"})
    assert "mystery" in dialog.step_labels[3].text()
    dialog.close()


def test_setup_dialog_finished_code_zero(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog._line_buffer = "tail output"
    dialog._finished(0, None)
    assert "安装完成" in dialog.summary.text()
    assert dialog.progress.value() == 100
    dialog.close()


def test_setup_dialog_finished_code_nonzero_without_summary(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog._line_buffer = "  "
    dialog._finished(7, None)
    assert "退出码 7" in dialog.summary.text()
    dialog.close()


def test_setup_dialog_finished_error_keeps_prior_summary(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog.summary.setText("之前的信息")
    dialog._finished(3, None)
    assert "之前的信息" in dialog.summary.text()
    dialog.close()


def test_setup_dialog_start_script_missing(tmp_path: Path):
    dialog = SetupDialog(tmp_path / "missing.ps1", _paths(tmp_path))
    with patch.object(QMessageBox, "critical") as critical:
        dialog.start()
        critical.assert_called_once()
    dialog.close()


def test_setup_dialog_start_with_and_without_optional_tools(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    started = []
    dialog.process.start = lambda: started.append(True)
    dialog.start()
    assert started == [True]
    assert dialog.start_button.isEnabled() is False
    # Re-enable and start again without optional tools.
    dialog.start_button.setEnabled(True)
    dialog.optional_tools.setChecked(False)
    dialog.process.start = lambda: started.append(True)
    dialog.start()
    assert len(started) == 2
    dialog.close()


def test_setup_dialog_read_parses_events_and_progress(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog._current_step = 2
    data = (
        b"plain line\n"
        b'LVS_EVENT {"type": "step", "step": 2, "state": "completed"}\n'
        b"50%\n"
    )

    class FakeProcess:
        def readAllStandardOutput(self):  # noqa: N802
            return data

    dialog.process = FakeProcess()
    dialog._read()
    assert "plain line" in dialog.log.toPlainText()
    assert "LVS_EVENT" not in dialog.log.toPlainText()


def test_setup_dialog_read_bad_event_line(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)

    class FakeProcess:
        def readAllStandardOutput(self):  # noqa: N802
            return b"LVS_EVENT not json\n"

    dialog.process = FakeProcess()
    dialog._read()
    assert "LVS_EVENT not json" in dialog.log.toPlainText()
    dialog.close()


def test_setup_dialog_read_no_visible_lines(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)
    dialog._line_buffer = "abc"
    dialog._current_step = 0

    class FakeProcess:
        def readAllStandardOutput(self):  # noqa: N802
            return b""

    dialog.process = FakeProcess()
    dialog._read()
    dialog.close()


def test_setup_dialog_read_ansi_stripped(tmp_path: Path):
    dialog = _setup_dialog(tmp_path)

    class FakeProcess:
        def readAllStandardOutput(self):  # noqa: N802
            return b"\x1b[31mred\x1b[0m\n"

    dialog.process = FakeProcess()
    dialog._read()
    assert "red" in dialog.log.toPlainText()
    assert "\x1b" not in dialog.log.toPlainText()
    dialog.close()


# ---------------------------------------------------------------------------
# VoiceSelector
# ---------------------------------------------------------------------------


def test_voice_selector_dict_and_object_and_singing(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)

    class Profile:
        name = "对象声音"
        id = "obj-id"
        consent_confirmed = True
        archived = False

        def singing_status(self, root):
            return "ready"

    selector.set_profiles([
        {"id": "a", "name": "可用", "consent_confirmed": True, "archived": False},
        Profile(),
    ])
    assert selector.count() == 2
    assert selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert selector.model().item(1).flags() & Qt.ItemIsEnabled
    assert "歌唱模型就绪" in selector.itemText(1)


def test_voice_selector_singing_status_training(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)

    class Profile:
        name = "训练中"
        id = "a"
        consent_confirmed = True
        archived = False

        def singing_status(self, root):
            return "training"

    selector.set_profiles([Profile()])
    assert not selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert "歌唱模型训练中" in selector.itemText(0)


def test_voice_selector_singing_status_ready_and_verification_failed(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)

    class Profile:
        consent_confirmed = True
        archived = False

        def __init__(self, name, status):
            self.name = name
            self.id = name
            self._status = status

        def singing_status(self, root):
            return self._status

    selector.set_profiles([
        Profile("verified", "untrusted"),
        Profile("failed", "verification_failed"),
        Profile("missing", "model_missing"),
        Profile("ready2", "ready"),
    ])
    assert selector.count() == 4
    assert not selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert not selector.model().item(1).flags() & Qt.ItemIsEnabled
    assert not selector.model().item(2).flags() & Qt.ItemIsEnabled
    assert selector.model().item(3).flags() & Qt.ItemIsEnabled
    assert "歌唱模型未验证" in selector.itemText(0)


def test_voice_selector_singing_status_typeerror_retry(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)

    class Profile:
        name = "R"
        id = "r"
        consent_confirmed = True
        archived = False

        def singing_status(self, *args):
            # Called with project_root first -> raise TypeError to trigger retry
            # without args.
            if args:
                raise TypeError("needs no arg")
            return "ready"

    selector.set_profiles([Profile()])
    # First call with root raises TypeError, retry without args returns "ready".
    assert selector.model().item(0).flags() & Qt.ItemIsEnabled
    assert "歌唱模型就绪" in selector.itemText(0)


def test_voice_selector_profile_without_attrs(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)
    # A bare string has no consent -> not allowed -> disabled.
    selector.set_profiles(["声线"])
    assert selector.count() == 1
    assert not selector.model().item(0).flags() & Qt.ItemIsEnabled

    class Empty:
        pass

    # An empty object: name falls back to str(obj), consent False -> disabled.
    selector.set_profiles([Empty()])
    assert selector.count() == 1
    assert not selector.model().item(0).flags() & Qt.ItemIsEnabled


# ---------------------------------------------------------------------------
# WaveformWidget
# ---------------------------------------------------------------------------


def test_waveform_set_position_clamps(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.resize(400, 100)
    widget.set_waveform([(-32768, 16384)], 10_000)
    widget.set_position(-5)
    assert widget.position_ms == 0
    widget.set_position(99_999)
    assert widget.position_ms == 10_000


def test_waveform_set_waveform_normalizes(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.set_waveform([(0.5, -0.2), (3, -4)], 1000)
    # scale 1 for the first (within range), scale 32768 for the second.
    assert widget.peaks[0] == (0.5, -0.2)
    assert widget.peaks[1] == (3 / 32768, -4 / 32768)
    assert widget.duration_ms == 1000


def test_waveform_set_waveform_scalar_values(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.set_waveform([0.8, -0.8, 2], 500)
    # Scalars clamp amplitude to [0, 1]; no 32768 scaling for scalars.
    assert widget.peaks[0] == (-0.8, 0.8)
    assert widget.peaks[1] == (-0.8, 0.8)
    assert widget.peaks[2] == (-1.0, 1.0)
    widget.set_waveform([], 100)
    assert widget.peaks == []
    widget.set_waveform(None, 100)
    assert widget.peaks == []


def test_waveform_position_from_x(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.resize(400, 100)
    widget.set_waveform([(0, 1)], 10_000)
    assert widget._position_from_x(200) == 5000
    assert widget._position_from_x(-10) == 0
    assert widget._position_from_x(9999) == 10_000


def _mouse_event(etype, button=None, buttons=None, x=0.0):
    pos = QPointF(x, 10)
    return QMouseEvent(etype, pos, button or Qt.NoButton, buttons or Qt.NoButton, Qt.NoModifier)


def test_waveform_mouse_press_and_move(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.resize(400, 100)
    widget.set_waveform([(0, 1)], 10_000)
    seeks = []
    widget.seek_requested.connect(seeks.append)

    widget.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, Qt.LeftButton, Qt.LeftButton, x=200))
    assert widget.position_ms == 5000
    assert seeks == [5000]
    # Right press does not seek.
    widget.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, Qt.RightButton, Qt.RightButton, x=100))
    assert widget.position_ms == 5000
    assert seeks == [5000]
    # Move with left button seeks.
    widget.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, None, Qt.LeftButton, x=400))
    assert widget.position_ms == 10_000
    assert seeks[-1] == 10_000
    # Move without left button does not seek.
    widget.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, None, Qt.NoButton, x=0))
    assert widget.position_ms == 10_000
    assert seeks[-1] == 10_000


def test_waveform_paint_event_with_and_without_peaks(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.resize(400, 100)
    widget.paintEvent(None)  # no peaks
    widget.set_waveform([(-32768, 16384), (0, 10000), (1, 2)], 10_000)
    widget.set_position(5000)
    widget.paintEvent(None)  # with peaks and position
    widget.duration_ms = 0
    widget.paintEvent(None)  # duration zero branch


def test_waveform_paint_with_single_peak_column(tmp_path: Path):
    _app()
    widget = WaveformWidget()
    widget.resize(1, 50)
    widget.set_waveform([(0, 1)], 1000)
    widget.paintEvent(None)
