from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


def main() -> int:
    source = Path(sys.argv[1]).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    app = QCoreApplication(sys.argv)
    audio = QAudioOutput()
    audio.setVolume(0.2)
    player = QMediaPlayer()
    player.setAudioOutput(audio)
    result = {"source": str(source), "playing_seen": False, "duration_ms": 0, "error": ""}

    def state_changed(state) -> None:
        if state == QMediaPlayer.PlayingState:
            result["playing_seen"] = True
            QTimer.singleShot(1500, player.stop)
        elif state == QMediaPlayer.StoppedState and result["playing_seen"]:
            result["duration_ms"] = player.duration()
            app.quit()

    def failed(error, message) -> None:
        result["error"] = message or str(error)
        app.exit(2)

    player.playbackStateChanged.connect(state_changed)
    player.errorOccurred.connect(failed)
    player.setSource(QUrl.fromLocalFile(str(source)))
    player.play()
    QTimer.singleShot(15000, lambda: app.exit(3))
    code = app.exec()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return code if code else (0 if result["playing_seen"] and result["duration_ms"] > 0 else 4)


if __name__ == "__main__":
    raise SystemExit(main())
