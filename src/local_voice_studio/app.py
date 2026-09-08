from __future__ import annotations

import multiprocessing
import sys
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer, Qt
from PySide6.QtWidgets import QApplication

from .paths import AppPaths
from .storage import StudioStore
from .ui.main_window import MainWindow, STYLE


def main() -> int:
    multiprocessing.freeze_support()
    QCoreApplication.setApplicationName("本地声音工坊")
    QCoreApplication.setOrganizationName("LocalVoiceStudio")
    # The HTML shell renders the approved v4 prototype inside QtWebEngine.
    # `--ui-qt` keeps the classic native widgets available as a fallback, and
    # `--ui-preview` never initialises QtWebEngine at all.
    preview = "--ui-preview" in sys.argv
    use_web = "--ui-qt" not in sys.argv and not preview
    if use_web:
        from .ui.web import shell as _web_shell  # noqa: F401 - must run before QApplication
    # Phase 6.1: keep UI crisp at Windows 100/125/150 % DPI. Qt 6 high-DPI
    # scaling is on by default; PassThrough keeps fractional rounding exact.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from .ui.theme import install_font_fallback
    install_font_fallback()
    if not use_web:
        app.setStyleSheet(STYLE)
    if "--ui-preview" in sys.argv:
        from .ui.preview import create_preview_window
        window = create_preview_window(); window.show()
        if "--smoke-test" in sys.argv:
            QTimer.singleShot(1500, app.quit)
        return app.exec()
    paths = AppPaths.default(); paths.ensure(); _cleanup_preview_cache(paths.cache_root / "preview"); store = StudioStore(paths)
    if not use_web:
        from .ui.theme import apply_preferences
        apply_preferences(store)
    window = _web_shell.WebStudioWindow(paths, store) if use_web else MainWindow(paths, store)
    window.show()
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(1500, app.quit)
    return app.exec()


def _cleanup_preview_cache(root: Path, max_age_seconds: int = 7 * 86400) -> None:
    if not root.exists(): return
    cutoff = time.time() - max_age_seconds
    for item in root.iterdir():
        try:
            if item.stat().st_mtime < cutoff:
                if item.is_dir(): shutil.rmtree(item)
                else: item.unlink()
        except OSError:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
