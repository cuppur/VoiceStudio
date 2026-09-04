"""Phase 6.3 UI coverage: LyricView editing flows and VoiceSelector rendering.

Covers the paint/sizeHint paths of _VoiceCardDelegate and the editable
dialog flow of LyricView, which the page-level suites exercise only
indirectly. All modal entry points are patched for offscreen runs.
"""
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDialog, QListWidgetItem, QMessageBox, QStyle, QStyleOptionViewItem, QWidget

from local_voice_studio.ui.studio_widgets.lyric_view import LyricLineEditDialog, LyricView
from local_voice_studio.ui.studio_widgets.voice_selector import VoiceSelector, _VoiceCardDelegate


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _delegate_index(selector: VoiceSelector, row: int = 0) -> QModelIndex:
    return selector.model().index(row, 0)


def _paint(delegate, index, state=QStyle.State_None) -> None:
    """Paint one frame, guaranteeing QPainter ends before the QImage dies."""
    image = QImage(320, 96, QImage.Format_ARGB32)
    image.fill(QColor("#0D0F14"))
    painter = QPainter(image)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 320, 96)
        option.state = state
        option.font = QApplication.font()
        delegate.paint(painter, option, index)
    finally:
        painter.end()


def test_voice_selector_constructor_widget_first(tmp_path: Path):
    _app()
    parent = QWidget()
    selector = VoiceSelector(parent, None)
    assert selector.project_root is None
    assert selector.parent() is parent


def test_voice_selector_delegate_paint_selected_and_disabled(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)
    selector.set_profiles([
        {"name": "我的声音", "id": "p1", "consent_confirmed": True},
        {"name": "未授权声", "id": "p2", "consent_confirmed": False, "archived": False},
    ])
    delegate = _VoiceCardDelegate(selector.view())
    _paint(delegate, _delegate_index(selector, 0), QStyle.State_Selected)
    _paint(delegate, _delegate_index(selector, 1), QStyle.State_MouseOver)
    _paint(delegate, _delegate_index(selector, 1), QStyle.State_None)
    size = delegate.sizeHint(QStyleOptionViewItem(), _delegate_index(selector, 0))
    assert size.height() >= 54


def test_voice_selector_delegate_paint_missing_item(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)
    selector.set_profiles([{"name": "我的声音", "id": "p1", "consent_confirmed": True}])
    delegate = _VoiceCardDelegate(selector.view())
    image = QImage(16, 16, QImage.Format_ARGB32)
    painter = QPainter(image)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 16, 16)
        # Walk the defensive "no item" branch of paint().
        with patch.object(selector.model(), "item", return_value=None):
            delegate.paint(painter, option, selector.model().index(0, 0))
    finally:
        painter.end()


def test_lyric_view_empty_navigation():
    _app()
    view = LyricView()
    received = []
    view.seek_requested.connect(received.append)
    view.move_previous()
    view.move_next()
    assert received == []


def test_lyric_view_navigation_emits():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句"), (1500, "第二句")], editable=True)
    received = []
    view.seek_requested.connect(received.append)
    view.setCurrentRow(0)
    view.move_previous()
    view.move_next()
    view.move_next()
    assert received == [500, 1500, 1500]


def test_lyric_view_edit_current_no_selection():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=True)
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        view.edit_current()


def test_lyric_view_edit_non_editable_seeks():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=False)
    received = []
    view.seek_requested.connect(received.append)
    view.setCurrentRow(0)
    view.edit_current()
    assert received == [500]


def test_lyric_view_edit_flow_accepted_and_emits():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=True)
    edited = []
    view.edit_requested.connect(lambda old_pos, new_pos, text: edited.append((old_pos, new_pos, text)))
    view.setCurrentRow(0)
    with patch.object(LyricLineEditDialog, "exec", return_value=QDialog.Accepted), \
         patch.object(LyricLineEditDialog, "values", return_value=(700, "新句")):
        view.edit_current()
    assert edited == [(500, 700, "新句")]


def test_lyric_view_edit_flow_unchanged_no_emit():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=True)
    edited = []
    view.edit_requested.connect(lambda *_: edited.append(True))
    view.setCurrentRow(0)
    with patch.object(LyricLineEditDialog, "exec", return_value=QDialog.Accepted), \
         patch.object(LyricLineEditDialog, "values", return_value=(500, "第一句")):
        view.edit_current()
    assert edited == []


def test_lyric_view_edit_flow_rejected_no_emit():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=True)
    edited = []
    view.edit_requested.connect(lambda *_: edited.append(True))
    view.setCurrentRow(0)
    with patch.object(LyricLineEditDialog, "exec", return_value=QDialog.Rejected):
        view.edit_current()
    assert edited == []


def test_lyric_view_edit_flow_empty_text_warns():
    _app()
    view = LyricView()
    view.set_lyrics([(500, "第一句")], editable=True)
    view.setCurrentRow(0)
    with patch.object(LyricLineEditDialog, "exec", return_value=QDialog.Accepted), \
         patch.object(LyricLineEditDialog, "values", side_effect=ValueError("歌词不能为空")), \
         patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
        view.edit_current()


def test_lyric_line_edit_dialog_values():
    _app()
    dialog = LyricLineEditDialog(1250, "  新句  ")
    assert dialog.values() == (1250, "新句")


def test_lyric_line_edit_dialog_empty_raises():
    _app()
    dialog = LyricLineEditDialog(0, "  ")
    try:
        dialog.values()
    except ValueError:
        return
    raise AssertionError("empty text should raise ValueError")
