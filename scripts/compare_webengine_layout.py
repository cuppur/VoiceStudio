"""Compare the eight page layouts between Chromium and QtWebEngine.

Both engines render the *same* ``assets/index.html``; this script proves the
resulting layout is identical (positions, sizes and card counts) by measuring
``getBoundingClientRect`` for every page in both engines and diffing the JSON.

    python scripts/compare_webengine_layout.py            # full comparison
    python scripts/compare_webengine_layout.py --engine-only --out engine.json

The WebEngine half always runs in a child process: QtWebEngine keeps the
interpreter alive after the measurement, so the child force-exits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "local_voice_studio" / "ui" / "web" / "assets"
INDEX = ASSETS / "index.html"
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
PAGES = (
    ("cover", '[data-page="cover"]'), ("tts", '[data-page="tts"]'), ("voices", '[data-page="voices"]'),
    ("train", '[data-page="train"]'), ("separator", '[data-page="separator"]'), ("exports", '[data-page="exports"]'),
    ("recent", "#recentBtn"), ("settings", "#settingsBtn"),
)

CHROME_MEASURE = """
<pre id="vsout">pending</pre>
<script>
const PAGES = %s;
function signature(view) {
  const rect = (el) => { const b = el.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
  const layout = view.querySelector('[class$="-layout"]');
  return {
    hidden: view.classList.contains('hidden'),
    viewport: [window.innerWidth, window.innerHeight],
    topbar: rect(document.querySelector('.topbar')),
    transport: rect(document.querySelector('.transport')),
    layout: layout ? rect(layout) : null,
    cardCount: view.querySelectorAll('.card').length,
    cards: Array.from(view.querySelectorAll('.card')).map(rect),
  };
}
(async () => {
  const out = {};
  for (const [name, selector] of PAGES) {
    document.querySelector(selector).click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    out[name] = signature(document.querySelector('[data-page-view="' + name + '"]'));
  }
  document.querySelector('[data-page="cover"]').click();
  document.getElementById('vsout').textContent = 'RESULT ' + JSON.stringify(out);
})();
</script>
"""

ENGINE_MEASURE = """(() => {
  const view = document.querySelector('[data-page-view="%s"]');
  const rect = (el) => { const b = el.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
  const layout = view.querySelector('[class$="-layout"]');
  return JSON.stringify({
    hidden: view.classList.contains('hidden'),
    viewport: [window.innerWidth, window.innerHeight],
    topbar: rect(document.querySelector('.topbar')),
    transport: rect(document.querySelector('.transport')),
    layout: layout ? rect(layout) : null,
    cardCount: view.querySelectorAll('.card').length,
    cards: Array.from(view.querySelectorAll('.card')).map(rect),
  });
})()"""


def chrome_binary() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("没有找到 Chromium 内核浏览器（Chrome/Edge）")


def measure_chrome(width: int, height: int) -> dict:
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as temporary:
        page = Path(temporary) / "measure.html"
        page.write_text(
            INDEX.read_text(encoding="utf-8").replace(
                "</body></html>",
                CHROME_MEASURE % json.dumps(PAGES) + "</body></html>",
            ),
            encoding="utf-8",
        )
        # window-size includes the frame; add the decoration so the viewport matches.
        command = [
            chrome_binary(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={width + 28},{height + 101}", "--virtual-time-budget=30000",
            f"--user-data-dir={Path(temporary) / 'profile'}", "--dump-dom", page.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    text = result.stdout + result.stderr
    marker = 'id="vsout">RESULT '
    position = text.find(marker)
    if position < 0:
        raise SystemExit("Chromium 测量失败：\n" + text[-800:])
    payload = text[position + len(marker):]
    return json.loads(payload[:payload.find("</pre>")])


def measure_engine(width: int, height: int) -> dict:
    """Run the WebEngine half in a child process (see --engine-only)."""
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "engine.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--engine-only",
                   "--width", str(width), "--height", str(height), "--out", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if not target.is_file():
            raise SystemExit("WebEngine 测量失败：\n" + (result.stdout + result.stderr)[-800:])
        return json.loads(target.read_text(encoding="utf-8"))


def engine_only(width: int, height: int, out: Path) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    sys.path.insert(0, str(ROOT / "src"))

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWidgets import QApplication

    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    from local_voice_studio.ui.preview import PreviewWorkerClient
    from local_voice_studio.ui.web.shell import WebStudioWindow

    app = QApplication([])
    root = Path(tempfile.mkdtemp())
    data = root / "data"
    paths = AppPaths(
        data_root=data, projects_root=root / "projects", runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS", models_root=data / "models",
        logs_root=data / "logs", database=data / "studio.sqlite3", cache_directory=data / "cache",
    )
    paths.ensure()
    store = StudioStore(paths)
    store.create_project("布局对照工程")
    window = WebStudioWindow(paths, store, client=PreviewWorkerClient())
    window.resize(width, height)
    static = QWebEngineScript()
    static.setName("vs-static")
    static.setSourceCode("window.__VS_STATIC__ = true;")
    static.setInjectionPoint(QWebEngineScript.DocumentCreation)
    static.setWorldId(QWebEngineScript.MainWorld)
    window.view.page().scripts().insert(static)
    window.show()
    QTest.qWait(1500)

    def evaluate(script: str):
        loop = QEventLoop()
        box: dict[str, object] = {}
        window.view.page().runJavaScript(script, lambda value: (box.update(value=value), loop.quit()))
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        return box.get("value")

    result: dict[str, object] = {}
    for name, selector in PAGES:
        evaluate(f"document.querySelector('{selector}').click(); 1")
        QTest.qWait(500)
        raw = evaluate(ENGINE_MEASURE % name)
        result[name] = json.loads(raw) if raw else None
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"engine layout written to {out}", flush=True)
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--engine-only", action="store_true")
    parser.add_argument("--out", default="")
    options = parser.parse_args()
    if options.engine_only:
        if not options.out:
            raise SystemExit("--engine-only 需要 --out")
        return engine_only(options.width, options.height, Path(options.out))

    chrome = measure_chrome(options.width, options.height)
    engine = measure_engine(options.width, options.height)
    mismatched = 0
    for name, _selector in PAGES:
        left, right = chrome.get(name), engine.get(name)
        if right is None:
            print(f"{name}: MISSING in engine")
            mismatched += 1
            continue
        fields = {
            "viewport": (left["viewport"], right["viewport"]),
            "topbar": (left["topbar"], right["topbar"]),
            "transport": (left["transport"], right["transport"]),
            "layout": (left["layout"], right["layout"]),
            "cardCount": (left["cardCount"], right["cardCount"]),
        }
        bad = {key: value for key, value in fields.items() if value[0] != value[1]}
        if bad or left["cards"] != right["cards"]:
            mismatched += 1
            print(f"{name}: MISMATCH {bad}")
            for index, (a, b) in enumerate(zip(left["cards"], right["cards"])):
                if a != b:
                    print(f"   card[{index}] chrome={a} engine={b}")
        else:
            print(f"{name}: identical (layout={left['layout']}, cards={left['cardCount']})")
    print(f"pages measured: {len(PAGES)}, mismatched: {mismatched}")
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
