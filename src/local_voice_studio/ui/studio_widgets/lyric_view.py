from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem


class LyricView(QListWidget):
    seek_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("lyricView")
        self.setAlternatingRowColors(False)
        self.setUniformItemSizes(True)
        self.lines: list[tuple[int, str]] = []
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._seek_item)

    def set_lyrics(self, lines) -> None:
        self.clear()
        self.lines = []
        for line in lines or []:
            if isinstance(line, dict):
                position = int(line.get("start_ms", line.get("time_ms", line.get("timestamp", 0))))
                text = str(line.get("text", ""))
            elif isinstance(line, (tuple, list)) and len(line) >= 2:
                position, text = int(line[0]), str(line[1])
            else:
                position = round(float(getattr(line, "timestamp_seconds", 0)) * 1000)
                text = str(getattr(line, "text", line))
            self.lines.append((max(0, position), text))
            seconds = max(0, position) // 1000
            item = QListWidgetItem(f"{seconds // 60:02d}:{seconds % 60:02d}   {text}")
            item.setData(Qt.UserRole, max(0, position))
            item.setData(Qt.UserRole + 1, text)
            self.addItem(item)

    def set_position(self, ms) -> None:
        if not self.lines:
            return
        target = max(0, int(ms))
        index = max(0, min(len(self.lines) - 1, next((i for i in range(len(self.lines) - 1, -1, -1) if self.lines[i][0] <= target), 0)))
        self.setCurrentRow(index)
        self.scrollToItem(self.item(index), QListWidget.PositionAtCenter)

    def _seek_item(self, item: QListWidgetItem) -> None:
        self.seek_requested.emit(int(item.data(Qt.UserRole) or 0))
