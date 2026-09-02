from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSlider, QVBoxLayout


def _label(text: str, object_name: str) -> QLabel:
    label = QLabel(text); label.setObjectName(object_name); return label


class QuickMixerPanel(QFrame):
    """Compact audition mixer used beside synchronized lyrics."""

    volume_changed = Signal(int, int)
    _NAMES = ("AI 人声", "伴奏", "原唱")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("mixerCard")
        layout = QVBoxLayout(self); layout.setContentsMargins(14, 0, 14, 12); layout.setSpacing(8)
        header = QHBoxLayout(); header.addWidget(_label("快速混音", "cardTitle")); header.addStretch(); header.addWidget(_label("试听", "cardSub")); layout.addLayout(header)
        self.sliders: list[QSlider] = []
        for index, name in enumerate(self._NAMES):
            row = QHBoxLayout(); row.addWidget(_label(name, "mixerLabel")); slider = QSlider(); slider.setOrientation(Qt.Horizontal); slider.setRange(0, 100); slider.setValue((82, 72, 0)[index]); slider.valueChanged.connect(lambda value, i=index: self.volume_changed.emit(i, value)); value = QLabel(str(slider.value())); value.setObjectName("mixerValue"); slider.valueChanged.connect(value.setNum); row.addWidget(slider, 1); row.addWidget(value); layout.addLayout(row); self.sliders.append(slider)
        note = QLabel("仅影响试听混音，导出时按设置重新渲染"); note.setObjectName("mixerNote"); note.setWordWrap(True); layout.addWidget(note)
        self.setMinimumWidth(230); self.setMaximumWidth(300)
