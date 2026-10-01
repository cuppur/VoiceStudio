"""Out-of-process probe for the training page inside QtWebEngine."""
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

from local_voice_studio.models import SourceAsset, VoiceProfile  # noqa: E402
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

    def send(self, command, payload=None, request_id=None):
        identifier = request_id or f"fake-{len(self.sent) + 1}"
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


def _wav(path: Path, seconds: float) -> Path:
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
    project = store.create_project("web-training-flow")
    profile = VoiceProfile(name="训练声音", consent_confirmed=True, consent_record="本人确认",
                           consent_confirmed_at="2026-01-01T00:00:00+00:00")
    store.save_profile(project, profile)
    asset_file = _wav(root / "material.wav", 12.0)
    asset = SourceAsset(profile.id, str(asset_file), str(asset_file), "b" * 64, duration_seconds=12.0, sample_rate=8000, channels=1, codec="pcm_s16le")
    extra_file = _wav(root / "material2.wav", 7.0)
    extra = SourceAsset(profile.id, str(extra_file), str(extra_file), "c" * 64, duration_seconds=7.0, sample_rate=8000, channels=1, codec="pcm_s16le")
    store.save_source_assets(project, [asset, extra])
    # 第二个声音已训练完成：左栏「已训练完成」分组应当默认折叠
    (root / "gpt.ckpt").write_bytes(b"gpt")
    (root / "sovits.pth").write_bytes(b"sovits")
    trained = VoiceProfile(
        name="已训练声音", consent_confirmed=True, consent_record="本人确认",
        consent_confirmed_at="2026-01-01T00:00:00+00:00",
        active_gpt_checkpoint=str(root / "gpt.ckpt"), active_sovits_checkpoint=str(root / "sovits.pth"),
    )
    store.save_profile(project, trained)

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
        evaluate("document.querySelector('.nav-item[data-page=\"voices\"]').click(); document.querySelector('[data-page-view=\"voices\"] .page-actions .btn').click(); 1")
        QTest.qWait(1200)
        result["page"] = json.loads(evaluate("""JSON.stringify({
            rows: document.querySelectorAll('#sampleList .sample-row').length,
            firstRow: ((document.querySelector('#sampleList .sample-name')||{}).firstChild||{}).textContent||'',
            button: (document.querySelector('#trainStart')||{}).textContent||'',
            hiddenQuality: Array.from(document.querySelectorAll('[data-page-view="train"] .field.hidden'))
                .filter(n => n.textContent.includes('训练质量')).length,
            steps: document.querySelectorAll('[data-page-view="train"] .step-item').length,
            sampleTools: {
                all: !!document.querySelector('#sampleAll'),
                remove: (document.querySelector('#sampleRemove')||{}).textContent||'',
                clear: (document.querySelector('#sampleClear')||{}).textContent||'',
                checks: document.querySelectorAll('#sampleList .sample-check').length,
                deletes: document.querySelectorAll('#sampleList .sample-delete').length,
                times: document.querySelectorAll('#sampleList .sample-time').length,
            },
            voiceList: {
                newButton: (document.querySelector('#trainNewVoice')||{}).textContent||'',
                activeCount: (document.querySelector('#trainActiveCount')||{}).textContent||'',
                doneCount: (document.querySelector('#trainDoneCount')||{}).textContent||'',
                doneCollapsed: !!document.querySelector('#trainVoiceDone')
                    && document.querySelector('#trainVoiceDone').classList.contains('collapsed'),
                doneHidden: getComputedStyle(document.querySelector('#trainVoiceDone .tv-body')).display === 'none',
                activeRows: document.querySelectorAll('#trainActiveBody .tv-row').length,
                doneRows: document.querySelectorAll('#trainDoneBody .tv-row').length,
                activeFirst: (document.querySelector('#trainActiveBody .tv-row .tv-name')||{}).textContent||'',
                doneFirst: (document.querySelector('#trainDoneBody .tv-row .tv-name')||{}).textContent||'',
            },
        })"""))
        evaluate("document.querySelector('#sampleList .sample-check').click(); 1")
        QTest.qWait(250)
        result["afterPick"] = json.loads(evaluate("""JSON.stringify({
            picked: (document.querySelector('#samplePicked')||{}).textContent||'',
        })"""))
        evaluate("document.querySelector('#sampleRemove').click(); 1")
        QTest.qWait(350)
        evaluate('document.querySelector(\'.modal-mask.show .modal-actions button[data-index="1"]\').click(); 1')
        QTest.qWait(900)
        result["afterRemove"] = json.loads(evaluate("""JSON.stringify({
            rows: document.querySelectorAll('#sampleList .sample-row').length,
            picked: (document.querySelector('#samplePicked')||{}).textContent||'',
        })"""))
        evaluate("document.querySelector('#trainVoiceDone .tv-group-head').click(); 1")
        QTest.qWait(300)
        result["afterExpand"] = json.loads(evaluate("""JSON.stringify({
            doneCollapsed: document.querySelector('#trainVoiceDone').classList.contains('collapsed'),
            doneHidden: getComputedStyle(document.querySelector('#trainVoiceDone .tv-body')).display === 'none',
            doneRows: document.querySelectorAll('#trainDoneBody .tv-row').length,
            doneFirst: (document.querySelector('#trainDoneBody .tv-row .tv-name')||{}).textContent||'',
        })"""))
        evaluate("document.querySelector('#trainNewVoice').click(); 1")
        QTest.qWait(500)
        result["afterNew"] = json.loads(evaluate("""JSON.stringify({
            mask: document.querySelectorAll('.modal-mask.show').length,
            title: (document.querySelector('.modal-mask.show .modal h3')||{}).textContent||'',
            suggested: (document.querySelector('.modal-mask.show #voiceName')||{}).value||'',
        })"""))
        evaluate("document.querySelector('.modal-mask.show .modal-actions button').click(); 1")
        QTest.qWait(300)
        evaluate("document.querySelector('#trainStart').click(); 1")
        QTest.qWait(1200)
        result["afterClick"] = json.loads(evaluate("""JSON.stringify({
            mask: document.querySelectorAll('.modal-mask.show').length,
            title: (document.querySelector('.modal-mask.show .modal h3')||{}).textContent||'',
            toast: (document.querySelector('#toast')||{}).textContent||'',
            sent: %s,
        })""" % json.dumps([[item[1], item[2].get("profile_id", "")] for item in client.sent])))
        print("VS_TRAIN " + json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(0)

    window.view.loadFinished.connect(lambda ok: (QTimer.singleShot(1800, flow) if ok else os._exit(3)))
    QTimer.singleShot(90000, lambda: os._exit(2))
    code = app.exec()
    temporary.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
