"""Capture the HTML shell in Chromium for visual comparison.

QtWebEngine renders the same file, but its composited surface cannot be grabbed
reliably on Windows (BitBlt returns a corrupt surface).  Chromium is therefore
used as the pixel-level reference, and the WebEngine side is verified through
computed-style and geometry assertions in ``tests/test_web_shell.py``.

Each page capture is produced by copying ``assets/index.html`` and appending a
one-line script that clicks the prototype's own navigation button, so the
captured markup is byte-identical to the shipped asset.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "local_voice_studio" / "ui" / "web" / "assets"
INDEX = ASSETS / "index.html"

PAGES = {
    "cover": "[data-page=\"cover\"]",
    "tts": "[data-page=\"tts\"]",
    "voices": "[data-page=\"voices\"]",
    "train": "[data-page=\"train\"]",
    "separator": "[data-page=\"separator\"]",
    "exports": "[data-page=\"exports\"]",
    "recent": "#recentBtn",
    "settings": "#settingsBtn",
}

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("没有找到 Chromium 内核浏览器（Chrome/Edge）")


def capture(browser: str, url: str, target: Path, width: int, height: int, profile: Path, scale: float = 1.0) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        browser, "--headless=new", "--disable-gpu",
        f"--force-device-scale-factor={scale}", f"--window-size={width},{height}",
        f"--user-data-dir={profile}", f"--screenshot={target}", url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    return target.is_file() and result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots" / "web-shell"))
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--scale", type=float, default=1.0, help="device scale factor (match the WebEngine devicePixelRatio)")
    parser.add_argument("--pages", default="")
    options = parser.parse_args()

    browser = chrome()
    out = Path(options.out)
    wanted = [name.strip() for name in options.pages.split(",") if name.strip()] or list(PAGES)
    source = INDEX.read_text(encoding="utf-8")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        shutil.copy2(ASSETS / "studio-bridge.js", workspace / "studio-bridge.js")
        profile = workspace / "profile"
        for name in wanted:
            selector = PAGES[name]
            page = workspace / f"page-{name}.html"
            # The cover page is the prototype's default view: capturing it with
            # no injected script keeps it byte-identical to the prototype file.
            script = "" if name == "cover" else f"<script>document.querySelector('{selector}').click();</script>\n"
            page.write_text(source.replace("</body></html>", script + "</body></html>"), encoding="utf-8")
            target = out / f"{name}-{options.width}x{options.height}.png"
            ok = capture(browser, page.as_uri(), target, options.width, options.height, profile, options.scale)
            print(f"{name}: {'ok' if ok else 'FAILED'} -> {target.name}")
            if not ok:
                failures.append(name)
    if failures:
        print("failed pages: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
