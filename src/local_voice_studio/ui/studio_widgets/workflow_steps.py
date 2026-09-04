from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel


class WorkflowSteps(QFrame):
    """Compact 8-step cover workflow indicator with a highlighted next step.

    The steps mirror the v1 user path from the product spec:

        1 导入歌曲  2 确认权利  3 分离  4 选择声音
        5 生成 AI 人声  6 调整  7 生成最终混音  8 导出

    The label never pretends a step is done: ``set_step`` highlights the
    current step and describes what comes next.
    """

    STEPS = ("导入歌曲", "确认权利", "分离", "选择声音", "生成 AI 人声", "调整", "生成最终混音", "导出")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workflowSteps")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.chips: list[QLabel] = []
        for index, name in enumerate(self.STEPS):
            chip = QLabel(f"{index + 1}. {name}")
            chip.setObjectName("stepChip")
            chip.setAlignment(Qt.AlignCenter)
            self.chips.append(chip)
            layout.addWidget(chip)
            if index < len(self.STEPS) - 1:
                arrow = QLabel("›")
                arrow.setObjectName("stepArrow")
                layout.addWidget(arrow)
        self.set_step(0)

    def set_step(self, index: int) -> None:
        """Highlight the step at *index* (0-based); others stay dimmed."""
        index = max(0, min(len(self.chips) - 1, int(index)))
        for chip_index, chip in enumerate(self.chips):
            chip.setProperty("state", "current" if chip_index == index else "")
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        next_index = index + 1
        self.setToolTip(
            f"当前步骤：{self.STEPS[index]}"
            + (f"\n下一步：{self.STEPS[next_index]}" if next_index < len(self.STEPS) else "\n工作流已完成")
        )
