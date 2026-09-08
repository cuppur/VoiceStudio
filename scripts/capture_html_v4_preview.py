"""Capture isolated HTML-v4 preview screenshots at the acceptance sizes."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from local_voice_studio.ui.main_window import STYLE
from local_voice_studio.ui.preview import create_preview_window
from local_voice_studio.ui.simple_pages import TaskCenterDialog
from local_voice_studio.ui.theme import install_font_fallback


def main() -> int:
    app = QApplication([])
    app.setStyle("Fusion")
    install_font_fallback()
    app.setStyleSheet(STYLE)
    target = Path(__file__).resolve().parents[1] / "docs" / "screenshots" / "html-v4-preview"
    target.mkdir(parents=True, exist_ok=True)
    def capture(width: int, height: int, page_index: int, filename: str) -> None:
        # A fresh preview window prevents a hidden page's minimum size from
        # affecting the raster bounds of the next acceptance screenshot.
        window = create_preview_window()
        window.resize(width, height)
        window.navigation.setCurrentRow(page_index)
        window.show()
        app.processEvents()
        QTest.qWait(30)
        app.processEvents()
        window.grab().save(str(target / filename))
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    for width, height in ((1440, 900), (1280, 720), (1920, 1080)):
        capture(width, height, 0, f"cover-{width}x{height}.png")
    for index, page in enumerate(("cover", "tts", "voices", "train", "separator", "exports", "recent", "settings")):
        capture(1440, 900, index, f"{page}-1440x900.png")
    # Major global overlay, using the same isolated preview store.  It makes
    # the absence of local Worker history visible instead of fabricating rows.
    window = create_preview_window()
    window.resize(1440, 900)
    window.show()
    dialog = TaskCenterDialog(window.store, window.client, window)
    dialog.show()
    app.processEvents()
    QTest.qWait(30)
    app.processEvents()
    dialog.grab().save(str(target / "task-center-880x520.png"))
    dialog.close()
    dialog.deleteLater()
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    window = create_preview_window()
    window.resize(1440, 900); window.show(); window._open_task_center()
    app.processEvents(); QTest.qWait(30); app.processEvents()
    window.task_drawer.grab().save(str(target / "task-drawer-360x728.png"))
    window.close(); window.deleteLater(); app.sendPostedEvents(None, QEvent.DeferredDelete); app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
