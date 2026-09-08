from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from .waveform import WaveformWidget


class TrackStatus(str, Enum):
    EMPTY = "empty"
    READY = "ready"
    PROCESSING = "processing"
    ERROR = "error"


_STATUS_LABELS = {
    TrackStatus.EMPTY: "未就绪",
    TrackStatus.READY: "就绪",
    TrackStatus.PROCESSING: "处理中",
    TrackStatus.ERROR: "错误",
}


class StemTrackWidget(QWidget):
    mute_changed = Signal(bool)
    solo_changed = Signal(bool)
    volume_changed = Signal(int)
    seek_requested = Signal(int)

    def __init__(self, name: str = "音轨", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stemTrack")
        self.setMinimumHeight(48)
        self.setMaximumHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 8, 4)
        self.name_label = QLabel(name)
        self.name_label.setFixedWidth(76)
        self.name_label.setToolTip(name)
        self.mute = QPushButton("M")
        self.solo = QPushButton("S")
        for button, label in ((self.mute, "静音"), (self.solo, "独奏")):
            button.setCheckable(True)
            button.setFixedSize(19, 19)
            button.setAccessibleName(f"{name} · {label}")
            button.setToolTip(label)
            button.setStyleSheet("""
                QPushButton {min-width:17px; min-height:17px; padding:0; background:#fbf9f6;
                    color:#a2968b; border:1px solid #eee7df; border-radius:6px; font-size:8px;}
                QPushButton:hover {background:#fff7ef; border-color:#ffd8ba;}
                QPushButton:checked {color:#d56816; background:#fff0e4; border-color:#ffd8ba;}
            """)
        self.volume = QSlider(Qt.Horizontal, self)
        self.volume.hide()
        self.volume.setMinimumWidth(72)
        self.volume.setMaximumWidth(130)
        self.volume.setRange(0, 100); self.volume.setValue(80)
        self.status = TrackStatus.EMPTY
        self.status_label = QLabel()
        self.status_label.setObjectName("trackStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setMinimumWidth(58)
        self.status_label.setMaximumWidth(76)
        self.waveform = WaveformWidget(self)
        self.waveform.wave_color = QColor({"原曲": "#9a8e84", "原唱人声": "#ff8c35", "伴奏": "#e2a15d", "AI 人声": "#f06f1a", "最终混音": "#2cab74"}.get(name, "#ff8c35"))
        self.waveform.setMinimumHeight(44)
        self.waveform.seek_requested.connect(self.seek_requested)
        layout.addWidget(self.name_label); layout.addWidget(self.mute); layout.addWidget(self.solo); layout.addWidget(self.waveform, 1); layout.addWidget(self.status_label)
        self.mute.toggled.connect(self.mute_changed)
        self.solo.toggled.connect(self.solo_changed)
        self.volume.valueChanged.connect(self.volume_changed)

    def set_name(self, name: str) -> None:
        self.name_label.setText(self.name_label.fontMetrics().elidedText(str(name), Qt.ElideRight, self.name_label.width()))
        self.name_label.setToolTip(str(name))
        self.mute.setAccessibleName(f"{name} · 静音")
        self.solo.setAccessibleName(f"{name} · 独奏")
    def set_status(self, status: TrackStatus | str) -> None:
        if not isinstance(status, TrackStatus):
            aliases = {"Empty": TrackStatus.EMPTY, "Ready": TrackStatus.READY, "Processing": TrackStatus.PROCESSING, "Error": TrackStatus.ERROR}
            status = aliases.get(str(status), TrackStatus(str(status).lower()))
        self.status = status
        self.status_label.setText(_STATUS_LABELS[status])
        self.status_label.setVisible(status != TrackStatus.READY)
        self.waveform.setToolTip(f"{self.name_label.toolTip()} · {_STATUS_LABELS[status]}")
        self.status_label.setProperty("state", status.value)
        self.status_label.style().unpolish(self.status_label); self.status_label.style().polish(self.status_label)
    def set_volume(self, value: int) -> None: self.volume.setValue(max(0, min(100, int(value))))
    def set_waveform(self, peaks, duration_ms: int) -> None: self.waveform.set_waveform(peaks, duration_ms)
    def set_position(self, position_ms: int) -> None: self.waveform.set_position(position_ms)
