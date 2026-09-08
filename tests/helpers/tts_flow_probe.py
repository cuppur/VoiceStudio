"""Out-of-process probe for the TTS and voice-library pages in QtWebEngine."""
from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from local_voice_studio.models import ReferenceAsset, VoiceProfile  # noqa: E402
from local_voice_studio.paths import AppPaths  # noqa: E402
from local_voice_studio.storage import StudioStore  # noqa: E402
from local_voice_studio.ui.web.shell import WebStudioWindow  # noqa: E402


class FakeClient(QObject):
    event = Signal(str, str, dict)
    state_changed = Signal(str)
    ready_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str, dict]] = []
        self._counter = 0

    def send(self, command, payload=None, request_id=None):
        self._counter += 1
        identifier = request_id or f"fake-{self._counter}"
        self.sent.append((identifier, command, dict(payload or {})))
        return identifier

    def start(self):
        QTimer.singleShot(0, lambda: self.state_changed.emit("running"))

    def shutdown(self):
        return

    def attach_pipeline_controller(self, controller):
        return

    def detach_pipeline_controller(self, controller):
        return


def _wav(path: Path, seconds: float = 3.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def main() -> int:
    app = QApplication([])
    temporary = TemporaryDirectory()
    root = Path(temporary.name)
    data = root / "data"
    paths = AppPaths(
        data_root=data, projects_root=root / "projects", runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS", models_root=data / "models",
        logs_root=data / "logs", database=data / "studio.sqlite3", cache_directory=data / "cache",
    )
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("web-tts-flow")
    checkpoint = root / "gpt.ckpt"; checkpoint.write_bytes(b"gpt")
    sovits = root / "sovits.pth"; sovits.write_bytes(b"sovits")
    reference = _wav(root / "ref.wav")
    profile = VoiceProfile(
        name="测试声音", consent_confirmed=True, consent_record="本人确认",
        consent_confirmed_at="2026-01-01T00:00:00+00:00",
        active_gpt_checkpoint=str(checkpoint), active_sovits_checkpoint=str(sovits),
    )
    profile.reference_assets = [ReferenceAsset(path=str(reference), sha256="a" * 64, transcript="你好", approved=True)]
    store.save_profile(project, profile)

    client = FakeClient()
    window = WebStudioWindow(paths, store, client=client)
    window.resize(1440, 900)
    window.show()
    result: dict[str, object] = {}

    def evaluate(script: str, timeout: int = 8000):
        loop = QEventLoop()
        box: dict[str, object] = {}
        window.view.page().runJavaScript(script, lambda value: (box.update(value=value), loop.quit()))
        QTimer.singleShot(timeout, loop.quit)
        loop.exec()
        return box.get("value")

    def flow() -> None:
        # TTS page
        evaluate("document.querySelector('[data-page=\"tts\"]').click(); 1")
        QTest.qWait(700)
        result["ttsHidden"] = json.loads(evaluate("""JSON.stringify({
            emotions: document.querySelectorAll('[data-page-view="tts"] .emotion-grid.hidden').length,
            pitchRows: Array.from(document.querySelectorAll('[data-page-view="tts"] .setting-row.hidden'))
                .filter(r => r.textContent.includes('音高')).length,
            outputCard: document.querySelectorAll('[data-page-view="tts"] .param-card.hidden').length,
            rows: document.querySelectorAll('#ttsVoiceList .voice-row').length,
            activeVoice: (document.querySelector('#ttsVoiceList .voice-row.active')||{dataset:{}}).dataset.voiceId || '',
        })"""))
        evaluate("""(() => {
            const area = document.querySelector('#scriptText');
            area.value = '这是新界面生成的测试文本。';
            area.dispatchEvent(new Event('input'));
            document.querySelector('#ttsGenerate').click();
            return 1;
        })()""")
        QTest.qWait(900)
        result["ttsSent"] = [list(item) for item in client.sent]
        result["toast"] = evaluate("(document.querySelector('#toast')||{}).textContent||''")
        result["error"] = evaluate("(window.__vsBridge && window.__vsBridge.error) || ''")
        result["taskPopup"] = json.loads(evaluate(
            "JSON.stringify({shown: !!document.querySelector('#taskPop.show'),"
            "title: (document.querySelector('#taskTitle')||{}).textContent||''})"))
        result["charCount"] = evaluate("(document.querySelector('#charCount')||{}).textContent")
        # Voices page
        evaluate("document.querySelector('#settingsBtn').click(); 1")
        QTest.qWait(200)
        evaluate("document.querySelector('[data-page=\"voices\"]') ? document.querySelector('[data-page=\"voices\"]').click() : 1")
        QTest.qWait(700)
        result["voices"] = json.loads(evaluate("""JSON.stringify({
            cards: document.querySelectorAll('#voiceGrid .voice-card-grid').length,
            detail: (document.querySelector('#detailName')||{}).textContent||'',
            versions: document.querySelectorAll('[data-page-view="voices"] .model-version').length,
            actions: document.querySelectorAll('[data-page-view="voices"] .detail-actions .btn').length,
        })"""))
        evaluate("document.querySelector('[data-page-view=\"voices\"] .detail-actions .btn').click(); 1")
        QTest.qWait(700)
        result["auditionSent"] = [list(item) for item in client.sent[-2:]]
        print("VS_TTS " + json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(0)

    window.view.loadFinished.connect(lambda ok: (QTimer.singleShot(1800, flow) if ok else os._exit(3)))
    QTimer.singleShot(90000, lambda: os._exit(2))
    code = app.exec()
    temporary.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
