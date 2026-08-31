from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget


class TransportWidget(QWidget):
    play_requested = Signal()
    seek_relative_requested = Signal(int)
    volume_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self.play_button = QPushButton("▶")
        self.back_button = QPushButton("−10s")
        self.forward_button = QPushButton("+10s")
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.time_label = QLabel("00:00 / 00:00")
        for button in (self.play_button, self.back_button, self.forward_button):
            layout.addWidget(button)
        layout.addWidget(self.timeline, 1)
        layout.addWidget(self.time_label)
        layout.addWidget(QLabel("音量"))
        self.volume.setFixedWidth(90)
        layout.addWidget(self.volume)
        self.play_button.clicked.connect(self.play_requested)
        self.back_button.clicked.connect(lambda: self.seek_relative_requested.emit(-10_000))
        self.forward_button.clicked.connect(lambda: self.seek_relative_requested.emit(10_000))
        self.volume.valueChanged.connect(self.volume_changed)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("❚❚" if playing else "▶")

    def set_timeline(self, position_ms: int, duration_ms: int) -> None:
        duration = max(0, int(duration_ms))
        position = max(0, min(duration, int(position_ms)))
        self.timeline.setRange(0, duration)
        self.timeline.setValue(position)
        def fmt(value: int) -> str:
            seconds = max(0, value // 1000)
            return f"{seconds // 60:02d}:{seconds % 60:02d}"
        self.time_label.setText(f"{fmt(position)} / {fmt(duration)}")
