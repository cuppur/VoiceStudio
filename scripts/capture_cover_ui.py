from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.main_window import MainWindow, STYLE
from local_voice_studio.ui.worker_client import WorkerClient


def sample_song(root: Path) -> Path:
    path = root / "星光练习曲.wav"
    rate = 22_050
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", ""))
        frames = bytearray()
        for index in range(rate * 12):
            envelope = 0.25 + 0.65 * abs(math.sin(index / rate * math.pi / 2))
            value = int(17_000 * envelope * math.sin(2 * math.pi * (180 + index / rate * 18) * index / rate))
            frames.extend(struct.pack("<h", value))
        stream.writeframes(frames)
    path.with_suffix(".lrc").write_text("[00:00.00]夜色落在安静的窗台\n[00:03.00]让声音沿着星光醒来\n[00:06.00]这一刻只属于音乐\n[00:09.00]下一段旅程即将展开\n", encoding="utf-8")
    return path


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="voice-studio-capture-"))
    paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data" / "studio.sqlite3")
    # Reuse the installed private media tools without copying them.
    installed = AppPaths.default()
    paths = AppPaths(installed.data_root, paths.projects_root, installed.runtime_root, installed.engine_root, installed.models_root, installed.logs_root, paths.database)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    store = StudioStore(paths); project = store.create_project("AI 翻唱演示")
    store.save_profile(project, VoiceProfile("澄澈女声", True))
    app = QApplication.instance() or QApplication(sys.argv); app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    WorkerClient.start = lambda self: None; WorkerClient.shutdown = lambda self: None
    window = MainWindow(paths, store); window.show(); window.cover_page.set_song(str(sample_song(root)))
    loop = QEventLoop(); QTimer.singleShot(5000, loop.quit)
    if window.cover_page._thread: window.cover_page._thread.finished.connect(loop.quit)
    loop.exec(); app.processEvents()
    output = Path(__file__).resolve().parents[1] / "docs" / "screenshots"; output.mkdir(parents=True, exist_ok=True)
    for width, height in ((1440, 900), (1280, 720)):
        window.resize(width, height); app.processEvents(); window.grab().save(str(output / f"ai-cover-{width}x{height}.png"))
    window.close(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
