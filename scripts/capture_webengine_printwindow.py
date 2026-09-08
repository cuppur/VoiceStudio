"""Capture the QtWebEngine shell with the Win32 PrintWindow API.

``QWebEngineView.grab()`` returns an empty surface and ``QScreen.grabWindow``
returns a corrupt one, but ``PrintWindow(PW_RENDERFULLCONTENT)`` renders the
real composited window.  The window frame offset is subtracted so the saved
image is exactly the client area, which makes it directly comparable with the
Chromium baseline from ``scripts/capture_web_baseline.py --scale <dpr>``.

Usage:
    python scripts/capture_webengine_printwindow.py --out docs/screenshots/web-shell-engine
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWebEngineCore import QWebEngineScript  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from local_voice_studio.paths import AppPaths  # noqa: E402
from local_voice_studio.storage import StudioStore  # noqa: E402
from local_voice_studio.ui.web.offline import OfflineWorkerClient  # noqa: E402
from local_voice_studio.ui.web.shell import WebStudioWindow  # noqa: E402

PW_RENDERFULLCONTENT = 2
PAGES = (
    ("cover", "[data-page=\"cover\"]"),
    ("tts", "[data-page=\"tts\"]"),
    ("voices", "[data-page=\"voices\"]"),
    ("train", "[data-page=\"train\"]"),
    ("separator", "[data-page=\"separator\"]"),
    ("exports", "[data-page=\"exports\"]"),
    ("recent", "#recentBtn"),
    ("settings", "#settingsBtn"),
)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def force_redraw(hwnd: int) -> None:
    """Make sure DWM has the latest frame before PrintWindow reads it."""
    try:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - foreground changes can be denied
        pass
    try:
        ctypes.windll.user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080)  # RDW_INVALIDATE|RDW_ALLCHILDREN
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.dwmapi.DwmFlush()
    except Exception:  # noqa: BLE001 - dwmapi is optional
        pass


def capture_client_area(hwnd: int) -> QImage | None:
    """Render the window with PrintWindow and crop it to the client area."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    client, window = wintypes.RECT(), wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    user32.GetWindowRect(hwnd, ctypes.byref(window))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    width, height = client.right - client.left, client.bottom - client.top
    full_w, full_h = window.right - window.left, window.bottom - window.top
    if width <= 0 or height <= 0 or full_w <= 0 or full_h <= 0:
        return None
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, full_w, full_h)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            return None
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth = full_w
        header.biHeight = -full_h
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(full_w * full_h * 4)
        if gdi32.GetDIBits(memory_dc, bitmap, 0, full_h, buffer, ctypes.byref(header), 0) != full_h:
            return None
        full = QImage(buffer, full_w, full_h, QImage.Format_RGB32).copy()
        return full.copy(origin.x - window.left, origin.y - window.top, width, height)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def isolated_paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        data_root=data, projects_root=root / "projects", runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS", models_root=data / "models",
        logs_root=data / "logs", database=data / "studio.sqlite3", cache_directory=data / "cache",
    )


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("PrintWindow 仅适用于 Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots" / "web-shell-engine"))
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--pages", default="")
    options = parser.parse_args()

    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = {name.strip() for name in options.pages.split(",") if name.strip()}

    app = QApplication(sys.argv)
    temp = TemporaryDirectory()
    paths = isolated_paths(Path(temp.name))
    paths.ensure()
    store = StudioStore(paths)
    store.create_project("视觉验收工程")
    window = WebStudioWindow(paths, store, client=OfflineWorkerClient())
    window.resize(options.width, options.height)
    script = QWebEngineScript()
    script.setName("vs-static")
    script.setSourceCode("window.__VS_STATIC__ = true;")
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    window.view.page().scripts().insert(script)
    window.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    window.show()
    window.raise_()
    window.activateWindow()

    saved: list[str] = []

    def capture(index: int) -> None:
        if index >= len(PAGES):
            print(f"captured {len(saved)} pages -> {out}")
            QTimer.singleShot(150, app.quit)
            return
        name, selector = PAGES[index]
        if wanted and name not in wanted:
            capture(index + 1)
            return

        def after_switch(_value=None) -> None:
            # The prototype repaints canvases 30 ms after showPage(); give the
            # compositor time and force one resize so nothing stays half drawn.
            def force_repaint(_again=None) -> None:
                def publish() -> None:
                    # First PrintWindow makes DWM publish the freshly rendered
                    # frame; the second one reads it.  Without this the capture
                    # lags one page behind.
                    force_redraw(int(window.winId()))
                    capture_client_area(int(window.winId()))
                    QTimer.singleShot(400, shoot)

                def shoot() -> None:
                    force_redraw(int(window.winId()))
                    image = capture_client_area(int(window.winId()))
                    target = out / f"{name}-{options.width}x{options.height}.png"
                    ok = bool(image) and image.save(str(target))
                    print(f"{name}: {image.width() if image else 0}x{image.height() if image else 0} saved={ok}")
                    if ok:
                        saved.append(name)
                    capture(index + 1)

                QTimer.singleShot(600, publish)

            QTimer.singleShot(600, lambda: window.view.page().runJavaScript(
                "window.dispatchEvent(new Event('resize')); 1", force_repaint))

        window.view.page().runJavaScript(f"document.querySelector('{selector}').click(); 1", after_switch)

    def start(ok: bool) -> None:
        if not ok:
            print("page failed to load", file=sys.stderr)
            app.quit()
            return
        QTimer.singleShot(900, lambda: capture(0))

    window.view.loadFinished.connect(start)
    QTimer.singleShot(180000, app.quit)
    code = app.exec()
    temp.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
