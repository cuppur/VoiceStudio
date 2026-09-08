"""Font fallback for headless rendering.

The Qt offscreen platform ships without fonts; registering an installed CJK
font keeps screenshot verification legible without bundling a third-party font.
The HTML shell brings its own styling, so no Qt style sheet is needed any more.
"""
from __future__ import annotations

import os
from pathlib import Path


def install_font_fallback() -> None:
    from PySide6.QtGui import QFontDatabase

    font_directory = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("Noto Sans SC (TrueType).otf", "msyh.ttc", "Deng.ttf"):
        candidate = font_directory / name
        if candidate.is_file() and QFontDatabase.addApplicationFont(str(candidate)) >= 0:
            return
