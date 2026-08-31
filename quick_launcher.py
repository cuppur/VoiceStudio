"""Portable one-click launcher for the packaged VoiceStudio application."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path


def _show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, "VoiceStudio 启动失败", 0x10)


def main() -> int:
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    candidates = (
        root / "dist" / "LocalVoiceStudio" / "LocalVoiceStudio.exe",
        root / "build" / "LocalVoiceStudio" / "LocalVoiceStudio.exe",
    )
    target = next((path for path in candidates if path.is_file()), None)
    if target is None:
        _show_error(
            "没有找到 VoiceStudio 主程序。\n\n"
            "请保留“一键启动”与项目文件夹在一起，或先运行 scripts\\build.ps1。"
        )
        return 2

    try:
        subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
    except OSError as exc:
        _show_error(f"主程序启动失败：\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
