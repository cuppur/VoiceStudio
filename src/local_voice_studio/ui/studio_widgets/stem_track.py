from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QSlider, QWidget


class StemTrackWidget(QWidget):
    mute_changed = Signal(bool)
    solo_changed = Signal(bool)
    volume_changed = Signal(int)

    def __init__(self, name: str = "音轨", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stemTrack")
        self.setFixedHeight(42)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 8, 4)
        self.name_label = QLabel(name)
        self.mute = QCheckBox("M")
        self.solo = QCheckBox("S")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setFixedWidth(100)
        self.volume.setRange(0, 100); self.volume.setValue(80)
        self.status = QComboBox(); self.status.addItems(["Ready", "Empty", "等待分离", "等待生成", "处理中", "错误"]); self.status.setFixedWidth(90)
        layout.addWidget(self.name_label, 1); layout.addWidget(self.mute); layout.addWidget(self.solo); layout.addWidget(self.volume); layout.addWidget(self.status)
        self.mute.toggled.connect(self.mute_changed)
        self.solo.toggled.connect(self.solo_changed)
        self.volume.valueChanged.connect(self.volume_changed)

    def set_name(self, name: str) -> None: self.name_label.setText(str(name))
    def set_status(self, status: str) -> None: self.status.setCurrentText(str(status))
    def set_volume(self, value: int) -> None: self.volume.setValue(max(0, min(100, int(value))))
