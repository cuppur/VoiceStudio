"""Out-of-process probe for the cover page interactions in QtWebEngine.

QtWebEngine cannot be torn down inside pytest, so the flow runs in a child
process and prints one ``VS_FLOW {...}`` line.  Engine readiness is patched so
the test does not depend on this machine's installed runtimes.
"""
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

from local_voice_studio.paths import AppPaths  # noqa: E402
from local_voice_studio.models import VoiceProfile  # noqa: E402
from local_voice_studio.storage import StudioStore  # noqa: E402
from local_voice_studio.ui.web.services.cover import CoverService  # noqa: E402
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


def _wav(path: Path, seconds: float = 0.6) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def main() -> int:
    CoverService.engines = lambda self: {  # type: ignore[method-assign]
        "uvr5": {"ready": True, "detail": ""},
        "roformer": {"ready": True, "detail": ""},
    }
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
    project = store.create_project("web-cover-flow")
    voice = VoiceProfile("UI 翻唱测试声音", True)
    voice.active_singing_model_id = "probe-singing-model"
    store.save_profile(project, voice)
    client = FakeClient()
    service = CoverService(paths, store, project, client)
    service.import_song(_wav(root / "song.wav"))

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

    def click_text(needle: str) -> None:
        evaluate("""(() => {
            const buttons = Array.from(document.querySelectorAll('.modal-mask.show .modal-actions button'));
            const target = buttons.find(b => b.textContent.includes(%s));
            if (target) target.click();
            return 1;
        })()""" % json.dumps(needle))
        QTest.qWait(700)

    def flow() -> None:
        evaluate("window.__vsBridge.voices = [{id:%s,name:'UI 翻唱测试声音',subtitle:'RVC 已验证',cover_ready:true}];"
                 "document.querySelector('#coverRender').click(); 1" % json.dumps(voice.id))
        QTest.qWait(800)
        result["rights"] = json.loads(evaluate(
            "JSON.stringify({shown: document.querySelectorAll('.modal-mask.show').length,"
            "title: (document.querySelector('.modal-mask.show .modal h3')||{}).textContent||''})"))
        click_text("我确认")
        result["voiceChoice"] = json.loads(evaluate(
            "JSON.stringify({shown: document.querySelectorAll('.modal-mask.show').length,"
            "title: (document.querySelector('.modal-mask.show .modal h3')||{}).textContent||'',"
            "count: document.querySelectorAll('.modal-mask.show .voice-row').length})"))
        click_text("使用该声音")
        QTest.qWait(500)
        result["afterVoiceChoice"] = json.loads(evaluate(
            "JSON.stringify({modal:!!document.querySelector('.modal-mask.show'),"
            "voiceId:window.VS_PAGES.cover.voiceId,songId:window.VS_PAGES.cover.songId,"
            "toast:(document.querySelector('#toast')||{}).textContent||''})"))
        result["sent"] = [list(item) for item in client.sent]
        result["taskPopup"] = json.loads(evaluate(
            "JSON.stringify({shown: !!document.querySelector('#taskPop.show'),"
            "title: (document.querySelector('#taskTitle')||{}).textContent||''})"))
        result["hidden"] = json.loads(evaluate("""JSON.stringify({
            presets: document.querySelectorAll('.recommend-row.hidden').length,
            advanced: document.querySelectorAll('#advancedBtn.hidden').length,
            takes: document.querySelectorAll('.take-strip.hidden').length,
            ab: document.querySelectorAll('#abCompare.hidden').length,
            preview: document.querySelectorAll('#previewRender.hidden').length,
            strength: document.querySelectorAll('.settings-body .setting-row.hidden').length,
            toggles: document.querySelectorAll('.settings-body .toggle-row.hidden').length,
        })"""))
        result["renderLabel"] = evaluate("document.querySelector('#coverRender').textContent")
        result["rightsState"] = evaluate("(document.querySelector('#heroStatus')||{}).textContent")
        request_id, command, payload = client.sent[-1]
        parent_job_id = window.bridge.cover._cover_runs.get(request_id, "")
        parent_payload = store.load_product_job(parent_job_id).payload if parent_job_id else {}
        result["oneClick"] = {"command": command, "cover_id": payload.get("cover_id"),
                               "profile_id": parent_payload.get("profile_id"),
                               "parent_job_id": parent_job_id}
        print("VS_FLOW " + json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(0)

    window.view.loadFinished.connect(lambda ok: (QTimer.singleShot(1800, flow) if ok else os._exit(3)))
    QTimer.singleShot(90000, lambda: os._exit(2))
    code = app.exec()
    temporary.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
