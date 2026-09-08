"""QtWebEngine window that renders the approved v4 HTML shell.

The prototype markup in ``assets/index.html`` is used verbatim; only the
``studio-bridge.js`` adapter is appended.  The adapter keeps the prototype's
own demo behaviour when no channel is available, so the same file can still be
opened in a browser for visual reference.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QFile, Qt, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from ...paths import AppPaths
from ...storage import StudioStore
from ..project_session import ProjectSession
from ..worker_client import WorkerClient
from .bridge import StudioBridge

# QtWebEngine needs the shared OpenGL context attribute before the application
# object exists; importing this module first (see app.py) guarantees that.
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
try:  # Qt 6.5+ requires the explicit initialisation for the Widgets binding.
    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    QtWebEngineQuick.initialize()
except Exception:  # noqa: BLE001 - older bindings initialise implicitly
    pass

ASSETS = Path(__file__).resolve().parent / "assets"
INDEX = ASSETS / "index.html"
QWEBCHANNEL_JS = ":/qtwebchannel/qwebchannel.js"


def _channel_source() -> str:
    """Return the official QWebChannel client library as a string."""
    handle = QFile(QWEBCHANNEL_JS)
    if handle.open(QFile.ReadOnly):
        try:
            return bytes(handle.readAll()).decode("utf-8", errors="replace")
        finally:
            handle.close()
    return ""


class WebStudioWindow(QMainWindow):
    """Main window whose entire UI is the HTML v4 prototype."""

    def __init__(self, paths: AppPaths, store: StudioStore, client: WorkerClient | None = None, parent=None):
        super().__init__(parent)
        self.paths, self.store = paths, store
        self.setWindowTitle("VoiceStudio · 本地 AI 声音创作工作室")
        self.resize(1440, 900)
        self.setMinimumSize(1280, 720)

        self.session = ProjectSession(store, self)
        self.project = self.session.current
        self.client = client or WorkerClient(paths, self)

        self.view = QWebEngineView(self)
        self.view.setObjectName("webShell")
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, False)
        self.setCentralWidget(self.view)

        self.channel = QWebChannel(self.view.page())
        self.bridge = StudioBridge(paths, store, self.project, self.client, self)
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        source = _channel_source()
        if source:
            script = QWebEngineScript()
            script.setName("qwebchannel")
            script.setSourceCode(source)
            script.setInjectionPoint(QWebEngineScript.DocumentCreation)
            script.setWorldId(QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(False)
            self.view.page().scripts().insert(script)

        self.session.project_changed.connect(self._project_changed)
        self.client.state_changed.connect(self._worker_state)
        self.client.ready_changed.connect(self._worker_ready)
        self.client.event.connect(self._worker_event)

        self.view.load(QUrl.fromLocalFile(str(INDEX)))
        self.client.start()

    # ------------------------------------------------------------------
    def _project_changed(self, project: Path) -> None:
        self.project = Path(project)
        self.bridge.set_project(self.project)
        self.bridge.notify("project.changed", {"path": str(self.project), "data": self.bridge.snapshot.state()})

    def _worker_state(self, state: str) -> None:
        self.bridge.notify("worker.state", {"state": str(state)})

    def _worker_ready(self, ready: bool) -> None:
        self.bridge.notify("worker.ready", {"ready": bool(ready)})

    def _worker_event(self, request_id: str, event: str, payload: dict) -> None:
        try:
            self.bridge.handle_worker_event(request_id, event, dict(payload or {}))
        except Exception as exc:  # noqa: BLE001 - a bad event must not kill the shell
            print(f"[web] worker event error: {exc}", file=sys.stderr, flush=True)
        if str(event) in {"progress", "result", "error", "stage", "log"}:
            self.bridge.notify("worker.event", {"id": str(request_id), "event": str(event), "payload": dict(payload or {})})

    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        try:
            self.client.shutdown()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        self.bridge.cleanup()
        super().closeEvent(event)


def assets_dir() -> Path:
    """Return the packaged asset directory (used by tests and packagers)."""
    return ASSETS


def main(paths: AppPaths, store: StudioStore, argv: list[str] | None = None) -> int:
    """Small standalone entry point kept for smoke tests."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(argv or sys.argv)
    window = WebStudioWindow(paths, store)
    window.show()
    return app.exec()
