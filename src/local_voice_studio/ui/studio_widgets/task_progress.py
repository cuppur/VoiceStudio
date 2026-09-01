from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


class TaskProgress(QWidget):
    STAGES = ("验证歌曲", "准备模型", "分离人声", "生成波形", "保存工程")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.labels: list[QLabel] = []
        layout = QHBoxLayout(self)
        for stage in self.STAGES:
            label = QLabel(stage)
            label.setAlignment(Qt.AlignCenter)
            self.labels.append(label)
            layout.addWidget(label, 1)
        self.set_stage(0)

    def set_stage(self, current: int) -> None:
        current = max(0, min(len(self.labels), int(current)))
        for index, label in enumerate(self.labels):
            label.setText(("✓ " if index < current else "● " if index == current else "○ ") + self.STAGES[index])
            label.setProperty("state", "done" if index < current else "active" if index == current else "pending")
            label.style().unpolish(label); label.style().polish(label)

    def set_progress(self, current: int) -> None:
        self.set_stage(current)
