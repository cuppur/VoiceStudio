from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_voice_studio.ui.preview import PreviewWorkerClient, create_preview_window
from local_voice_studio.ui.simple_pages import TaskCenterDialog


def _app():
    return QApplication.instance() or QApplication([])


def test_visual_preview_isolated_and_renders_sample_cover():
    _app()
    window = create_preview_window()
    assert window.ui_preview is True
    assert window.navigation.count() == 8
    assert window.cover_page.song_title.text() == "落日信号"
    assert "无真实音频" in window.cover_page.song_meta.text()
    assert window.cover_page.take_selector.currentText().startswith("Take 03")
    assert "voicestudio-ui-preview-" in str(window.paths.data_root)
    window.close()


def test_preview_song_filters_only_change_visual_song_library():
    _app()
    window = create_preview_window()
    page = window.cover_page
    assert page.song_list.count() == 4
    page.song_filters["pending"].click()
    assert page.song_list.count() == 2
    assert all("待处理" in page.song_list.item(index).text() for index in range(page.song_list.count()))
    page.song_filters["complete"].click()
    assert page.song_list.count() == 1
    assert "已完成" in page.song_list.item(0).text()
    window.close()


def test_preview_model_import_only_updates_its_visual_notice():
    _app()
    window = create_preview_window()
    window.voice_page.import_models.click()
    assert "视觉预览不打开文件选择器" in window.voice_page.detail_note.text()
    window.close()


def test_preview_task_drawer_has_explicit_nonpersistent_visual_states():
    _app()
    window = create_preview_window()
    window.show(); window._open_task_center()
    cards = [window.task_drawer.cards_layout.itemAt(index).widget() for index in range(2)]
    assert all(card is not None and card.objectName() == "taskDrawerCard" for card in cards)
    assert any("视觉样例" in card.findChildren(type(window.cover_page.song_title))[0].text() for card in cards)
    window.close()


def test_preview_separation_and_exports_show_only_labelled_visual_rows():
    _app()
    window = create_preview_window()
    assert window.separator_page.stems.count() == 4
    assert "不会读取音频" in window.separator_page.summary.text()
    assert window.exports_page.rows.count() == 5
    assert "不占用本地存储" in window.exports_page.storage.text()
    window.close()


def test_preview_worker_never_reports_task_success():
    _app()
    client = PreviewWorkerClient()
    received = []
    client.event.connect(lambda request_id, event, payload: received.append((request_id, event, payload)))
    client.send("synthesize", {"text": "preview"})
    QTest.qWait(200)
    assert received[0][1] == "error"
    assert received[0][2]["status"] == "visual_preview"


def test_preview_task_center_labels_its_isolated_empty_history():
    _app()
    window = create_preview_window()
    dialog = TaskCenterDialog(window.store, window.client, window)
    note = dialog.findChild(type(window.cover_page.song_title), "taskPreviewNote")
    assert note is not None
    assert "未连接本地 Worker" in note.text()
    assert dialog.table.rowCount() == 0
    assert not dialog.empty_state.isHidden()
    empty_text = dialog.findChild(type(note), "taskEmptyText")
    assert empty_text is not None
    assert "视觉预览不会创建" in empty_text.text()
    dialog.close()
    window.close()
