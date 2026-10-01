"""Out-of-process probe for the QtWebEngine shell.

QtWebEngine cannot run inside the pytest process: its GPU/utility child
processes keep the interpreter alive after the test session finishes.  This
helper boots the real shell against an isolated temporary store, reads the
rendered DOM, prints one ``VS_PROBE {...}`` line and force-exits.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from local_voice_studio.cover.project import CoverProject  # noqa: E402
from local_voice_studio.models import VoiceProfile  # noqa: E402
from local_voice_studio.paths import AppPaths  # noqa: E402
from local_voice_studio.storage import StudioStore  # noqa: E402
from local_voice_studio.ui.web.offline import OfflineWorkerClient  # noqa: E402
from local_voice_studio.ui.web.shell import WebStudioWindow  # noqa: E402

PROBE_SCRIPT = """(() => {
    const q = (selector) => document.querySelector(selector);
    const result = {
      viewport: [window.innerWidth, window.innerHeight],
      topbarRect: Math.round(q('.topbar').getBoundingClientRect().width),
      coverRect: (() => { const r = q('.cover-layout').getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })(),
      cardRect: (() => { const r = q('.library').getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })(),
      pages: document.querySelectorAll('.page').length,
      nav: document.querySelectorAll('.nav-item').length,
      topbar: Math.round(q('.topbar').getBoundingClientRect().height),
      transport: Math.round(q('.transport').getBoundingClientRect().height),
      brand: q('.brand-name').textContent,
      columns: getComputedStyle(q('.cover-layout')).gridTemplateColumns.split(' ').length,
      radius: getComputedStyle(q('.card')).borderTopLeftRadius,
      background: getComputedStyle(document.body).backgroundColor,
      titleSize: getComputedStyle(q('.hero-title')).fontSize,
      titleWeight: getComputedStyle(q('.hero-title')).fontWeight,
      songs: document.querySelectorAll('#songList .song-row').length,
      count: q('#songCount').textContent,
      hero: q('#heroTitle').textContent,
      voices: document.querySelectorAll('#voiceGrid .voice-card-grid').length,
      tts: document.querySelectorAll('#ttsVoiceList .voice-row').length,
      detail: q('#detailName').textContent,
      outputPath: (q('#settingsContent .path') || {}).textContent,
      adapter: !!window.__vsBridge,
      fileUrl: window.__vsBridge ? window.__vsBridge.fileUrl(String.raw`C:\\Users\\测试 声音\\a.wav`) : '',
      error: (window.__vsBridge && window.__vsBridge.error) || '',
    };
    // interaction parity with the prototype (its own JS handlers stay in place)
    q('[data-page="tts"]').click();
    result.ttsVisible = !q('[data-page-view="tts"]').classList.contains('hidden');
    result.coverHidden = q('[data-page-view="cover"]').classList.contains('hidden');
    q('[data-page="cover"]').click();
    result.coverVisible = !q('[data-page-view="cover"]').classList.contains('hidden');
    q('#gpuBtn').click();
    result.drawerOpen = q('#taskDrawer').classList.contains('show');
    q('#closeTaskDrawer').click();
    result.drawerClosed = !q('#taskDrawer').classList.contains('show');
    result.toolbarComparison = !!q('.timeline .card-head #versionCompareToggle') && !!q('.timeline #compareA') && !!q('.timeline #abSeg') && !q('.library #versionComparePanel');
    result.globalImportRemoved = !q('#quickImportBtn');
    result.importDemoRemoved = !q('#importModal');
    return JSON.stringify(result);
})()"""


def main() -> int:
    app = QApplication([])
    temp = TemporaryDirectory()
    root = Path(temp.name)
    data = root / "data"
    paths = AppPaths(
        data_root=data, projects_root=root / "projects", runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS", models_root=data / "models",
        logs_root=data / "logs", database=data / "studio.sqlite3", cache_directory=data / "cache",
    )
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("测试工程")
    store.save_profile(project, VoiceProfile(name="测试声音", consent_confirmed=True))
    CoverProject.create(project, title="落日信号").save()

    window = WebStudioWindow(paths, store, client=OfflineWorkerClient())
    window.resize(1440, 900)
    window.show()
    QTest.qWait(1500)

    box: dict[str, object] = {}
    loop = QEventLoop()
    window.view.page().runJavaScript(PROBE_SCRIPT, lambda value: (box.update(probe=value), loop.quit()))
    QTimer.singleShot(20000, loop.quit)
    loop.exec()

    print("VS_PROBE " + json.dumps({"probe": box.get("probe"), "project": str(project)}), flush=True)
    temp.cleanup()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
