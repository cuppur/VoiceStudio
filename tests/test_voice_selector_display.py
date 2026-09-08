"""The compact value survives Qt model refreshes without losing voice identity."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from local_voice_studio.ui.studio_widgets.voice_selector import VoiceSelector


def test_compact_value_survives_blocked_refresh_and_selection():
    app = QApplication.instance() or QApplication([])
    selector = VoiceSelector()
    selector.blockSignals(True)
    selector.set_profiles([
        {"id": "first", "name": "南栀", "consent_confirmed": True},
        {"id": "second", "name": "低沉男声", "consent_confirmed": True},
    ])
    selector.setCurrentIndex(1)
    selector.blockSignals(False)
    app.processEvents()
    assert selector.lineEdit().text() == "低沉男声"
    assert selector.currentData() == "second"
    assert "可用" in selector.toolTip()
    assert "可用" in selector.itemText(1)
    selector.setCurrentIndex(0)
    assert selector.lineEdit().text() == "南栀"
    assert selector.currentData() == "first"
    selector.clear()
    assert selector.lineEdit().text() == ""
    assert selector.toolTip() == ""


def test_cover_columns_follow_reference_breakpoint():
    from local_voice_studio.ui.preview import create_preview_window

    window = create_preview_window()
    window.resize(1440, 900)
    window.show()
    QApplication.processEvents()
    assert window.cover_page.song_library.width() == 242
    assert window.cover_page.cover_settings.width() == 320
    window.resize(1280, 720)
    QApplication.processEvents()
    assert window.cover_page.song_library.width() == 215
    assert window.cover_page.cover_settings.width() == 292
    window.close()
