"""Regenerate the shipped HTML shell from the approved prototype file.

``assets/index.html`` must stay a byte-for-byte copy of the approved prototype
plus one adapter script tag.  Run this script after the prototype is updated,
then re-run ``tests/test_web_shell.py`` (it asserts the copy stays verbatim) and
``scripts/capture_web_baseline.py`` for fresh visual baselines.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "local_voice_studio" / "ui" / "web" / "assets"
DEFAULT_PROTOTYPE = ROOT / "VoiceStudio_Full_UI_Prototype_v4.html"
NEEDLE = "</script>\n</body></html>"
ADAPTER = '<script src="studio-bridge.js"></script>\n<script src="studio-media.js"></script>\n<script src="studio-pages.js"></script>'


def sync(prototype: Path, target: Path) -> tuple[int, int]:
    source = prototype.read_text(encoding="utf-8")
    if NEEDLE not in source:
        raise SystemExit(f"原型文件结构不符合预期（缺少 {NEEDLE!r}）：{prototype}")
    updated = source.replace(NEEDLE, f"</script>\n{ADAPTER}\n</body></html>", 1)
    # The reference keeps its demo interactions in a normal browser or static
    # capture. The desktop product must never execute simulated work or data.
    updated = updated.replace('<script>\n', '<script>\nif (!window.qt || window.__VS_STATIC__) {\n', 1)
    updated = updated.replace(f'</script>\n{ADAPTER}', f'}}\n</script>\n{ADAPTER}', 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8", newline="")
    return len(updated.encode("utf-8")), len(updated) - len(source)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prototype", default=str(DEFAULT_PROTOTYPE))
    parser.add_argument("--target", default=str(ASSETS / "index.html"))
    options = parser.parse_args()
    size, delta = sync(Path(options.prototype), Path(options.target))
    print(f"synced {options.target}: {size} bytes (+{delta} chars for the adapter tag)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
