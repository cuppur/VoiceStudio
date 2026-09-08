"""Shared five-column export rows for real files and labelled visual samples."""
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


@dataclass(frozen=True)
class ExportRowData:
    name: str
    source: str
    format: str
    size: str
    time: str
    path: Path | None = None

    @classmethod
    def from_file(cls, path: Path, source: str):
        stat = path.stat()
        return cls(path.name, source, path.suffix[1:].upper(),
                   f"{stat.st_size / 1024**2:.1f} MB",
                   datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"), path)


class ExportTableRow(QFrame):
    open_requested = Signal(str)

    def __init__(self, data: ExportRowData | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("exportTableHeader" if data is None else "exportTableRow")
        row = QHBoxLayout(self); row.setContentsMargins(10, 9, 10, 9); row.setSpacing(12)
        if data is None:
            row.addWidget(QLabel("文件"), 1)
            for title, width in (("来源", 76), ("格式", 62), ("时间", 86), ("操作", 74)):
                label = QLabel(title); label.setFixedWidth(width); row.addWidget(label)
        else:
            file_box = QFrame(); file_layout = QHBoxLayout(file_box)
            file_layout.setContentsMargins(0, 0, 0, 0); file_layout.setSpacing(9)
            icon = QLabel(data.format); icon.setObjectName("exportFileIcon")
            icon.setFixedSize(38, 38); icon.setAlignment(Qt.AlignCenter); file_layout.addWidget(icon)
            copy = QVBoxLayout(); copy.setSpacing(3)
            title = QLabel(data.name); title.setWordWrap(True); title.setObjectName("exportFileName")
            copy.addWidget(title)
            detail = QLabel(data.size if data.path else "视觉样例 · 无本地文件")
            detail.setObjectName("exportFileDetail"); copy.addWidget(detail)
            file_layout.addLayout(copy, 1); row.addWidget(file_box, 1)
            for text, width in ((data.source, 76), (data.format, 62), (data.time, 86)):
                label = QLabel(text); label.setFixedWidth(width); row.addWidget(label)
            self.action = QPushButton("打开" if data.path else "视觉预览")
            self.action.setFixedWidth(74); self.action.setEnabled(data.path is not None)
            if data.path:
                def open_existing():
                    if not data.path.is_file():
                        detail.setText("文件已移动或删除，请刷新导出列表")
                        self.action.setEnabled(False)
                        return
                    self.open_requested.emit(str(data.path))
                self.action.clicked.connect(open_existing)
            row.addWidget(self.action)
        self.setStyleSheet("""
            QFrame#exportTableHeader {background:#fbf8f4;border:0;border-bottom:1px solid #eee5dc;}
            QFrame#exportTableRow {background:white;border:0;border-bottom:1px solid #f0e9e2;}
            QLabel {background:transparent;color:#8d8176;font-size:10px;border:0;}
            QLabel#exportFileName {color:#453a31;font-weight:600;font-size:11px;}
            QLabel#exportFileDetail {color:#9b8f84;font-size:9px;}
            QLabel#exportFileIcon {background:#fff0e4;color:#d76a18;border-radius:10px;font-size:9px;font-weight:700;}
            QPushButton {background:#fffaf6;color:#827468;border:1px solid #e9e1d8;border-radius:7px;padding:5px;font-size:10px;}
            QPushButton:hover {background:#fff1e5;border-color:#ffc99d;}
            QPushButton:disabled {color:#b3a79b;}
        """)
