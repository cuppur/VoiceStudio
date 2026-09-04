from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QVBoxLayout


class LyricLineEditDialog(QDialog):
    """Small editor for one lyric line: text plus timestamp in seconds."""

    def __init__(self, position_ms: int, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑歌词行")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.text_edit = QLineEdit(text)
        form.addRow("歌词", self.text_edit)
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setRange(0.0, 3600.0)
        self.time_spin.setDecimals(2)
        self.time_spin.setSingleStep(0.1)
        self.time_spin.setSuffix(" 秒")
        self.time_spin.setValue(max(0, position_ms) / 1000.0)
        form.addRow("时间", self.time_spin)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, str]:
        text = self.text_edit.text().strip()
        if not text:
            raise ValueError("歌词不能为空")
        return int(round(self.time_spin.value() * 1000)), text


class LyricView(QListWidget):
    seek_requested = Signal(int)
    edit_requested = Signal(int, int, str)  # old_position_ms, new_position_ms, text

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("lyricView")
        self.setAlternatingRowColors(False)
        self.setUniformItemSizes(True)
        self.lines: list[tuple[int, str]] = []
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._seek_item)
        self.itemDoubleClicked.connect(self._edit_item)
        self._editable = False

    def set_lyrics(self, lines, *, editable: bool = False) -> None:
        self._editable = bool(editable)
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
        if self._editable:
            self.setToolTip("双击歌词行可编辑文本或时间")

    def set_position(self, ms) -> None:
        if not self.lines:
            return
        target = max(0, int(ms))
        index = max(0, min(len(self.lines) - 1, next((i for i in range(len(self.lines) - 1, -1, -1) if self.lines[i][0] <= target), 0)))
        self.setCurrentRow(index)
        self.scrollToItem(self.item(index), QListWidget.PositionAtCenter)

    def move_previous(self) -> None:
        """Jump to the previous lyric line and emit its position."""
        if not self.lines:
            return
        row = max(0, self.currentRow() - 1) if self.currentRow() > 0 else 0
        self.setCurrentRow(row)
        self.scrollToItem(self.item(row), QListWidget.PositionAtCenter)
        self.seek_requested.emit(int(self.item(row).data(Qt.UserRole) or 0))

    def move_next(self) -> None:
        """Jump to the next lyric line and emit its position."""
        if not self.lines:
            return
        row = min(len(self.lines) - 1, self.currentRow() + 1)
        self.setCurrentRow(row)
        self.scrollToItem(self.item(row), QListWidget.PositionAtCenter)
        self.seek_requested.emit(int(self.item(row).data(Qt.UserRole) or 0))

    def edit_current(self) -> None:
        """Open the editor for the currently selected line."""
        row = self.currentRow()
        if not self.lines or row < 0 or row >= len(self.lines):
            QMessageBox.information(self, "编辑歌词", "请先选择一行歌词再编辑。")
            return
        self._edit_item(self.item(row))

    def _edit_item(self, item: QListWidgetItem) -> None:
        if not self._editable:
            self._seek_item(item)
            return
        position = int(item.data(Qt.UserRole) or 0)
        text = str(item.data(Qt.UserRole + 1) or "")
        dialog = LyricLineEditDialog(position, text, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            new_position, new_text = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "编辑歌词", str(exc))
            return
        if (new_position, new_text) == (position, text):
            return
        self.edit_requested.emit(position, new_position, new_text)

    def _seek_item(self, item: QListWidgetItem) -> None:
        self.seek_requested.emit(int(item.data(Qt.UserRole) or 0))
