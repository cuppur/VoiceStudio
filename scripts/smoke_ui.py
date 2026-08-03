from __future__ import annotations

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCAL_VOICE_STUDIO_HOME"] = tempfile.mkdtemp(prefix="voice-studio-ui-")
os.environ["LOCAL_VOICE_STUDIO_PROJECTS"] = tempfile.mkdtemp(prefix="voice-studio-projects-")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow, STYLE


app = QApplication([])
app.setStyleSheet(STYLE)
paths = AppPaths.default(); paths.ensure()
window = MainWindow(paths, StudioStore(paths)); window.show()
QTimer.singleShot(1200, window.close)
QTimer.singleShot(1600, app.quit)
raise SystemExit(app.exec())

