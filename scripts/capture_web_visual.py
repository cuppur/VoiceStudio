"""Capture the HTML shell with QtWebEngine for visual verification.

``--static`` (default)
    Renders ``assets/index.html`` exactly as the prototype does (the adapter
    returns before touching the DOM), so captures can be compared with the same
    page in a plain Chromium browser.

``--live``
    Boots the real shell against an isolated temporary store so the capture
    shows real local data instead of the prototype's demo values.

``--pdf``
    QtWebEngine's composited surface cannot be grabbed on Windows, but
    ``printToPdf`` renders the page in the real engine.  The PDF is converted to
    PNG with PyMuPDF so WebEngine pixels can be compared with the Chromium
    baseline from ``scripts/capture_web_baseline.py``.

The script never reads the user's database, projects or models: it builds an
isolated :class:`AppPaths` under a temporary directory.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--force-device-scale-factor=1 --disable-gpu")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QMarginsF, QSizeF, QTimer  # noqa: E402
from PySide6.QtGui import QPageLayout, QPageSize  # noqa: E402
from PySide6.QtWebEngineCore import QWebEngineScript  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from local_voice_studio.paths import AppPaths  # noqa: E402
from local_voice_studio.storage import StudioStore  # noqa: E402
from local_voice_studio.ui.web.offline import OfflineWorkerClient  # noqa: E402
from local_voice_studio.ui.web.shell import WebStudioWindow  # noqa: E402

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


def isolated_paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        data_root=data,
        projects_root=root / "projects",
        runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS",
        models_root=data / "models",
        logs_root=data / "logs",
        database=data / "studio.sqlite3",
        cache_directory=data / "cache",
    )


def pdf_to_png(pdf: Path, png: Path, dpi: int = 96) -> bool:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return False
    document = fitz.open(str(pdf))
    try:
        if document.page_count == 0:
            return False
        pixmap = document[0].get_pixmap(dpi=dpi)
        pixmap.save(str(png))
        return png.is_file()
    finally:
        document.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots" / "web-shell"))
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--live", dest="static", action="store_false", help="show real local data instead of the prototype demo data")
    parser.add_argument("--grab", dest="pdf", action="store_false", help="use QWidget.grab() instead of printToPdf (returns a blank surface on Windows)")
    parser.add_argument("--pages", default="", help="comma separated page names")
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
    if options.static:
        script = QWebEngineScript()
        script.setName("vs-static")
        script.setSourceCode("window.__VS_STATIC__ = true;")
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        window.view.page().scripts().insert(script)
    window.show()

    saved: list[str] = []
    # CSS pixels are 1/96 inch, PDF points are 1/72 inch: the page must be
    # laid out in points and rasterised at 96 dpi to reproduce screen pixels.
    layout = QPageLayout(
        QPageSize(QSizeF(options.width * 0.75, options.height * 0.75), QPageSize.Point),
        QPageLayout.Portrait,
        QMarginsF(0, 0, 0, 0),
        QPageLayout.Point,
    )

    def capture(index: int) -> None:
        if index >= len(PAGES):
            print(f"captured {len(saved)} pages -> {out}")
            QTimer.singleShot(150, app.quit)
            return
        name, selector = PAGES[index]
        if wanted and name not in wanted:
            capture(index + 1)
            return
        target = out / f"{name}-{options.width}x{options.height}.png"
        pdf = out / f"{name}-{options.width}x{options.height}.pdf"

        def after_switch(_value=None) -> None:
            if options.pdf:
                def printed(path: str, ok: bool) -> None:
                    if ok:
                        ok = pdf_to_png(Path(path), target)
                    print(f"{name}: pdf={'ok' if ok else 'FAILED'}")
                    if ok:
                        saved.append(name)
                    pdf.unlink(missing_ok=True)
                    capture(index + 1)

                window.view.page().pdfPrintingFinished.connect(printed)
                window.view.page().printToPdf(str(pdf), layout)
                return

            def after_paint(_ignored=None) -> None:
                pixmap = window.grab()
                ok = pixmap.save(str(target))
                print(f"{name}: {pixmap.width()}x{pixmap.height()} saved={ok}")
                if ok:
                    saved.append(name)
                capture(index + 1)

            QTimer.singleShot(420, lambda: window.view.page().runJavaScript("1", after_paint))

        window.view.page().runJavaScript(f"document.querySelector('{selector}').click(); 1", after_switch)

    def start(ok: bool) -> None:
        if not ok:
            print("page failed to load", file=sys.stderr)
            app.quit()
            return
        QTimer.singleShot(700, lambda: capture(0))

    window.view.loadFinished.connect(start)
    QTimer.singleShot(180000, app.quit)
    code = app.exec()
    temp.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
