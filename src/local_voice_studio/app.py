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
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    paths = AppPaths.default(); paths.ensure(); _cleanup_preview_cache(paths.data_root / "cache" / "preview"); store = StudioStore(paths)
    window = MainWindow(paths, store); window.show()
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
