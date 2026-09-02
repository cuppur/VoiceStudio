from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSlider, QWidget

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
        self.name_label.setMinimumWidth(76)
        self.name_label.setMaximumWidth(112)
        self.mute = QCheckBox("M")
        self.solo = QCheckBox("S")
        self.volume = QSlider(Qt.Horizontal)
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
        self.waveform.setMinimumHeight(44)
        self.waveform.seek_requested.connect(self.seek_requested)
        layout.addWidget(self.name_label); layout.addWidget(self.waveform, 1); layout.addWidget(self.mute); layout.addWidget(self.solo); layout.addWidget(self.volume); layout.addWidget(self.status_label)
        self.mute.toggled.connect(self.mute_changed)
        self.solo.toggled.connect(self.solo_changed)
        self.volume.valueChanged.connect(self.volume_changed)

    def set_name(self, name: str) -> None: self.name_label.setText(str(name))
    def set_status(self, status: TrackStatus | str) -> None:
        if not isinstance(status, TrackStatus):
            aliases = {"Empty": TrackStatus.EMPTY, "Ready": TrackStatus.READY, "Processing": TrackStatus.PROCESSING, "Error": TrackStatus.ERROR}
            status = aliases.get(str(status), TrackStatus(str(status).lower()))
        self.status = status
        self.status_label.setText(_STATUS_LABELS[status])
        self.status_label.setProperty("state", status.value)
        self.status_label.style().unpolish(self.status_label); self.status_label.style().polish(self.status_label)
    def set_volume(self, value: int) -> None: self.volume.setValue(max(0, min(100, int(value))))
    def set_waveform(self, peaks, duration_ms: int) -> None: self.waveform.set_waveform(peaks, duration_ms)
    def set_position(self, position_ms: int) -> None: self.waveform.set_position(position_ms)
