from __future__ import annotations

import math
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

from ..models import Job
from ..text import split_text


def format_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        return parsed.strftime("%m月%d日 %H:%M")
    except (TypeError, ValueError):
        return value[:16].replace("T", " ") if value else "时间未知"


def estimate_text_work(text: str, max_chars: int = 120) -> tuple[int, int]:
    clean = text.strip()
    return len(clean), len(split_text(clean, max_chars)) if clean else 0


def audio_duration(path: str | Path) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getnframes() / max(1, stream.getframerate())
    except (OSError, EOFError, wave.Error):
        return 0.0


def format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


class AudioWaveform(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.samples: list[float] = []
        self.setMinimumHeight(42)
        self.setMaximumHeight(56)

    def set_audio(self, path: str | Path | None) -> None:
        self.samples = []
        if path:
            try:
                with wave.open(str(path), "rb") as stream:
                    width, channels, frames = stream.getsampwidth(), stream.getnchannels(), stream.getnframes()
                    if width == 2 and frames:
                        bucket = max(1, frames // 120)
                        for _ in range(math.ceil(frames / bucket)):
                            values = array("h", stream.readframes(bucket))
                            if values:
                                peak = max(abs(value) for value in values[::max(1, channels)]) / 32768
                                self.samples.append(min(1.0, peak))
            except (OSError, EOFError, wave.Error):
                self.samples = []
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f4f7fb"))
        if not self.samples:
            painter.setPen(QPen(QColor("#cbd5e1"), 2))
            painter.drawLine(8, self.height() // 2, self.width() - 8, self.height() // 2)
            return
        middle = self.height() / 2
        step = max(1.0, (self.width() - 16) / max(1, len(self.samples) - 1))
        painter.setPen(QPen(QColor("#4f7edb"), 1.5))
        for index, value in enumerate(self.samples):
            x = 8 + index * step
            half = max(1.0, value * (middle - 5))
            painter.drawLine(QPointF(x, middle - half), QPointF(x, middle + half))


class InlineAudioPlayer(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inlinePlayer")
        self.path = ""
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self._audio_attached = True
        layout = QHBoxLayout(self); layout.setContentsMargins(6, 3, 6, 3)
        self.play = QPushButton("▶"); self.play.setFixedWidth(38); self.play.clicked.connect(self.toggle)
        self.slider = QSlider(Qt.Horizontal); self.slider.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("00:00 / 00:00"); self.time.setObjectName("hint")
        layout.addWidget(self.play); layout.addWidget(self.slider, 1); layout.addWidget(self.time)
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(lambda value: self.slider.setRange(0, max(0, value)))
        self.player.playbackStateChanged.connect(lambda state: self.play.setText("❚❚" if state == QMediaPlayer.PlayingState else "▶"))

    def set_source(self, path: str | Path | None) -> None:
        # On Windows the multimedia backend keeps the source file open until the
        # source itself is cleared.  That prevents re-record/delete flows from
        # replacing a previewed WAV, so always release the previous handle first.
        self.player.stop()
        self.player.setSource(QUrl())
        self.path = str(path or "")
        valid = bool(self.path and Path(self.path).is_file())
        self.play.setEnabled(valid); self.slider.setEnabled(valid)
        if valid and not self._audio_attached:
            self.player.setAudioOutput(self.audio_output)
            self._audio_attached = True
        self.player.setSource(QUrl.fromLocalFile(self.path) if valid else QUrl())
        duration = audio_duration(self.path) if valid else 0
        self.time.setText(f"00:00 / {format_duration(duration)}")

    def release(self) -> None:
        """Stop playback and release any Windows file handle."""
        self.player.stop()
        self.player.setSource(QUrl())
        if self._audio_attached:
            self.player.setAudioOutput(None)
            self._audio_attached = False
        self.path = ""
        self.play.setEnabled(False); self.slider.setEnabled(False)
        self.slider.setValue(0); self.time.setText("00:00 / 00:00")
        # Qt's FFmpeg backend releases its demuxer on the next event turn.
        # Flushing it here is important before a caller deletes/replaces a WAV.
        QApplication.processEvents()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release()
        super().closeEvent(event)

    def toggle(self) -> None:
        if not self.path:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _position(self, value: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(value)
        self.time.setText(f"{format_duration(value / 1000)} / {format_duration(self.player.duration() / 1000)}")


class StepTimeline(QFrame):
    def __init__(self, names: tuple[str, ...], parent: QWidget | None = None):
        super().__init__(parent)
        self.names = names
        self.labels: list[QLabel] = []
        self.results: list[QLabel] = []
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        for number, name in enumerate(names, 1):
            row = QFrame(); row_layout = QHBoxLayout(row); row_layout.setContentsMargins(10, 5, 10, 5)
            label = QLabel(f"○  {number}. {name}"); label.setObjectName("stepPending")
            result = QLabel(""); result.setObjectName("hint"); result.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row_layout.addWidget(label); row_layout.addWidget(result, 1)
            layout.addWidget(row); self.labels.append(label); self.results.append(result)

    def update_state(self, current: int, results: dict[int, str] | None = None, failed: bool = False) -> None:
        results = results or {}
        for index, (label, result) in enumerate(zip(self.labels, self.results)):
            done = index < current
            active = index == current and current < len(self.labels)
            symbol = "✓" if done else "!" if active and failed else "●" if active else "○"
            label.setText(f"{symbol}  {index + 1}. {self.names[index]}")
            label.setObjectName("stepDone" if done else "stepFailed" if active and failed else "stepActive" if active else "stepPending")
            label.style().unpolish(label); label.style().polish(label)
            result.setText(results.get(index, ""))


class ReviewSegmentCard(QFrame):
    changed = Signal(int, object)
    confirmed = Signal(int)
    navigate = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("reviewCard"); self.index = -1; self._loading = False
        layout = QVBoxLayout(self)
        top = QHBoxLayout(); self.counter = QLabel("片段"); self.counter.setObjectName("cardTitle"); self.duration = QLabel(""); self.duration.setObjectName("hint"); top.addWidget(self.counter); top.addStretch(); top.addWidget(self.duration); layout.addLayout(top)
        self.waveform = AudioWaveform(); layout.addWidget(self.waveform)
        self.player = InlineAudioPlayer(); layout.addWidget(self.player)
        layout.addWidget(QLabel("识别文字")); self.text = QLineEdit(); self.text.editingFinished.connect(self._emit_change); layout.addWidget(self.text)
        self.issue = QLabel(""); self.issue.setWordWrap(True); layout.addWidget(self.issue)
        bottom = QHBoxLayout(); self.include = QPushButton("纳入训练"); self.include.setCheckable(True); self.include.toggled.connect(self._emit_change); previous = QPushButton("← 上一条"); previous.clicked.connect(lambda: self.navigate.emit(-1)); next_button = QPushButton("确认并下一条 →"); next_button.setObjectName("primaryButton"); next_button.clicked.connect(self._confirm)
        bottom.addWidget(self.include); bottom.addStretch(); bottom.addWidget(previous); bottom.addWidget(next_button); layout.addLayout(bottom)
        self._shortcuts = []
        for key, callback in (("Space", self._play_shortcut), ("Return", self._confirm), ("Delete", self._exclude_shortcut), ("Up", lambda: self.navigate.emit(-1)), ("Down", lambda: self.navigate.emit(1))):
            shortcut = QShortcut(QKeySequence(key), self); shortcut.setContext(Qt.WidgetWithChildrenShortcut); shortcut.activated.connect(callback); self._shortcuts.append(shortcut)

    def set_segment(self, index: int, segment, project: Path, position: int, total: int) -> None:
        self._loading = True; self.index = index
        path = project / str(segment.audio_relative_path)
        self.counter.setText(f"片段 {position + 1} / {total}")
        self.duration.setText(f"{segment.duration_seconds:.1f} 秒")
        self.text.setText(segment.text); self.include.setChecked(bool(segment.included))
        flags = list(getattr(segment, "quality_flags", []))
        self.issue.setText("⚠ " + " · ".join(flags) if flags else "✓ 未发现明显问题")
        self.issue.setObjectName("issueWarning" if flags else "issueGood")
        self.player.set_source(path); self.waveform.set_audio(path); self._loading = False

    def _emit_change(self) -> None:
        if not self._loading and self.index >= 0:
            self.changed.emit(self.index, {"text": self.text.text().strip(), "included": self.include.isChecked()})

    def _confirm(self) -> None:
        if self.index < 0:
            return
        self._emit_change(); self.confirmed.emit(self.index); self.navigate.emit(1)

    def _play_shortcut(self) -> None:
        if QApplication.focusWidget() is not self.text:
            self.player.toggle()

    def _exclude_shortcut(self) -> None:
        if QApplication.focusWidget() is not self.text:
            self.include.setChecked(False); self.navigate.emit(1)

    def release_resources(self) -> None:
        self.player.release()
        self.waveform.set_audio(None)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources()
        super().closeEvent(event)


@dataclass(frozen=True)
class GenerationRecord:
    job: Job
    profile_name: str
    text: str
    audio_path: str
    duration_seconds: float

    @classmethod
    def from_job(cls, job: Job, profile_name: str) -> "GenerationRecord":
        audio = next((item for item in job.outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
        return cls(job, profile_name, str(job.payload.get("text", "")), audio, audio_duration(audio))


class GenerationRecordCard(QFrame):
    retry_requested = Signal(object)

    def __init__(self, record: GenerationRecord, parent: QWidget | None = None):
        super().__init__(parent); self.record = record; self.setObjectName("historyCard")
        layout = QVBoxLayout(self); top = QHBoxLayout(); title = QLabel(f"{format_timestamp(record.job.updated_at)} · {record.profile_name}"); title.setObjectName("cardTitleSmall"); top.addWidget(title); top.addStretch(); top.addWidget(QLabel(format_duration(record.duration_seconds))); layout.addLayout(top)
        excerpt = record.text.replace("\n", " "); excerpt = excerpt[:90] + ("…" if len(excerpt) > 90 else ""); label = QLabel(f"“{excerpt}”"); label.setWordWrap(True); layout.addWidget(label)
        self.player = InlineAudioPlayer(); self.player.set_source(record.audio_path); layout.addWidget(self.player)
        row = QHBoxLayout(); open_button = QPushButton("打开文件"); open_button.clicked.connect(self._open); copy = QPushButton("复制文本"); copy.clicked.connect(lambda: QApplication.clipboard().setText(record.text)); retry = QPushButton("再次生成"); retry.clicked.connect(lambda: self.retry_requested.emit(record.job)); row.addWidget(open_button); row.addWidget(copy); row.addWidget(retry); row.addStretch(); layout.addLayout(row)

    def _open(self) -> None:
        if self.record.audio_path:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.record.audio_path))

    def release_resources(self) -> None:
        self.player.release()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.release_resources()
        super().closeEvent(event)
