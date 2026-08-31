from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class WaveformWidget(QWidget):
    """Small waveform display with mouse seeking and clamped positions."""

    seek_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.peaks: list[tuple[float, float]] = []
        self.duration_ms = 0
        self.position_ms = 0
        self.setMinimumHeight(48)
        self.setCursor(Qt.PointingHandCursor)

    def set_waveform(self, peaks, duration_ms) -> None:
        normalized = []
        for value in peaks or []:
            if isinstance(value, (tuple, list)) and len(value) >= 2:
                low, high = float(value[0]), float(value[1])
                scale = 32768.0 if max(abs(low), abs(high)) > 1 else 1.0
                normalized.append((max(-1.0, low / scale), min(1.0, high / scale)))
            else:
                amplitude = max(0.0, min(1.0, abs(float(value))))
                normalized.append((-amplitude, amplitude))
        self.peaks = normalized
        self.duration_ms = max(0, int(duration_ms))
        self.position_ms = min(self.position_ms, self.duration_ms)
        self.update()

    def set_position(self, ms) -> None:
        self.position_ms = max(0, min(self.duration_ms, int(ms)))
        self.update()

    def _position_from_x(self, x: int) -> int:
        width = max(1, self.width())
        return max(0, min(self.duration_ms, round(max(0, min(width, x)) / width * self.duration_ms)))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            value = self._position_from_x(round(event.position().x()))
            self.set_position(value)
            self.seek_requested.emit(value)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            value = self._position_from_x(round(event.position().x()))
            self.set_position(value)
            self.seek_requested.emit(value)
        super().mouseMoveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        mid = self.height() / 2
        painter.fillRect(self.rect(), QColor("#151821"))
        painter.setPen(QPen(QColor("#2A303C"), 1))
        painter.drawLine(0, round(mid), self.width(), round(mid))
        if not self.peaks:
            return
        played = self.position_ms / self.duration_ms if self.duration_ms else 0.0
        columns = max(1, self.width())
        for x in range(columns):
            left = x * len(self.peaks) // columns
            right = max(left + 1, (x + 1) * len(self.peaks) // columns)
            low = min(value[0] for value in self.peaks[left:right])
            high = max(value[1] for value in self.peaks[left:right])
            color = QColor("#7C89FF") if x / columns <= played else QColor("#596171")
            painter.setPen(QPen(color, 2))
            radius = (self.height() - 8) / 2
            painter.drawLine(x, round(mid - high * radius), x, round(mid - low * radius))
