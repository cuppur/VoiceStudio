from pathlib import Path
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer


def prototype_icon(name, size=16):
    path = Path(__file__).with_name("resources") / "prototype" / f"{name}.svg"
    if not path.is_file(): return QIcon()
    source = path.read_text(encoding="utf-8")
    icon = QIcon()
    for mode, color in ((QIcon.Normal, "#82776e"), (QIcon.Selected, "#ff7a1a"), (QIcon.Active, "#ff7a1a")):
        renderer = QSvgRenderer(QByteArray(source.replace("currentColor", color).encode("utf-8")))
        pixmap = QPixmap(size * 2, size * 2); pixmap.setDevicePixelRatio(2); pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap); renderer.render(painter); painter.end(); icon.addPixmap(pixmap, mode)
    return icon
