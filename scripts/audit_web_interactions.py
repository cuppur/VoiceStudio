"""Read-only product audit using synthetic audio and an isolated QtWebEngine store.

Runs no GPU tasks and installs nothing. The native picker is replaced with a
synthetic WAV selection so this can run without touching personal audio.
Reports observations, rather than asserting that the current bugs are desired.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_voice_studio.ui.web.shell import WebStudioWindow
from local_voice_studio.ui.web.offline import OfflineWorkerClient
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.models import VoiceProfile
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog


def main():
    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(prefix="voicestudio-audit-")
    root = Path(temporary.name)
    data = root / "data"
    paths = AppPaths(data, root / "projects", data / "runtime", data / "engine",
                     data / "models", data / "logs", data / "studio.sqlite3", data / "cache")
    paths.ensure()
    store = StudioStore(paths)
    store.create_project("audit")
    window = WebStudioWindow(paths, store, client=OfflineWorkerClient())
    window.show()

    def evaluate(script):
        loop = QEventLoop()
        box = {}
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        window.view.page().runJavaScript(script, lambda value: (box.update(value=value), loop.quit()))
        timer.start(8000)
        loop.exec()
        timer.stop()
        if "value" not in box:
            raise RuntimeError("JavaScript evaluation timed out")
        return box["value"]

    def observe(expression):
        return json.loads(evaluate("JSON.stringify(" + expression + ")"))

    for _ in range(40):
        QTest.qWait(100)
        if evaluate("!!(window.__vsBridge && window.__vsBridge.data && window.VS_PAGES)"):
            break
    else:
        raise RuntimeError("Bridge did not initialize")
    QTest.qWait(300)
    selected = root / "outside-managed-roots" / "same-name.wav"
    selected.parent.mkdir()
    with wave.open(str(selected), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    picks = []
    def picker(*args, **kwargs):
        picks.append(str(selected))
        return str(selected), ""
    original_picker = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = picker
    result = {}
    try:
        evaluate("document.querySelector('#coverImport').click()")
        result["import_entry"] = observe("({modal:document.querySelector('#importModal').textContent, filePickerCalls:" + str(len(picks)) + "})")
        evaluate("document.querySelector('#confirmImport').click()")
        QTest.qWait(400)
        result["first_import"] = observe("({songs:__vsBridge.songs.length, selectedId:__vsBridge.song.id, operationId:VS_PAGES.cover.songId, duration:__vsBridge.song.duration_text, toast:document.querySelector('#toast').textContent})")
        result["first_import"]["filePickerCalls"] = len(picks)
        evaluate("document.querySelector('#coverRender').click()")
        QTest.qWait(100)
        result["first_render_toast"] = evaluate("document.querySelector('#toast').textContent")
        # Explicitly selecting the first row works around the initial sync bug.
        evaluate("document.querySelector('#songList .song-row').click()")
        QTest.qWait(200)
        evaluate("document.querySelector('#confirmImport').click()")
        QTest.qWait(300)
        evaluate("document.querySelectorAll('#songList .song-row')[1].click()")
        QTest.qWait(250)
        result["duplicate_title_selection"] = observe("({selectedId:__vsBridge.song.id, operationId:VS_PAGES.cover.songId, names:__vsBridge.songs.map(s=>s.title)})")
        evaluate("window.auditEvents=[];const handler=VS_PAGES.onEvent;VS_PAGES.onEvent=(name,data)=>{auditEvents.push(name);handler(name,data);}")
        for name in ("training.scanning", "training.error", "voices.changed", "engine.install.started", "engine.install.log", "engine.install.done", "job.progress"):
            window.bridge.notify(name, {"message": "audit-marker", "line": "audit-line"})
        QTest.qWait(250)
        result["forwarded_events"] = observe("auditEvents")
        result["engine_dialog_visible"] = evaluate("!!document.querySelector('#engineInstallMask')")
        result["remaining_demo_controls"] = observe("Array.from(document.querySelectorAll('.demo')).filter(n=>!n.closest('.hidden')).map(n=>({id:n.id,text:n.textContent.trim(),message:n.dataset.msg,handler:n.onclick?String(n.onclick):null}))")
        result["mute_solo_handlers"] = observe("Array.from(document.querySelectorAll('.track-btn')).map(n=>({text:n.textContent,handler:String(n.onclick)}))")
        result["tts_search_handler"] = evaluate("String(document.querySelector('#ttsVoiceSearch').oninput)")
        result["dereverb_toggle_matches"] = evaluate("document.querySelectorAll('#dereverbToggle').length")
        # Refreshing the global snapshot must preserve the selected TTS voice.
        for name in ("audit-voice-one", "audit-voice-two"):
            store.save_profile(window.bridge.project, VoiceProfile(name=name, consent_confirmed=True))
        evaluate("__vsBridge.invoke('app.refresh',{})")
        QTest.qWait(250)
        evaluate("document.querySelectorAll('#ttsVoiceList .voice-row')[1].click()")
        before = evaluate("document.querySelector('#ttsVoiceList .active').dataset.voiceId")
        evaluate("__vsBridge.invoke('app.refresh',{})")
        QTest.qWait(250)
        result["tts_selection_after_refresh"] = {"before": before, "after": evaluate("document.querySelector('#ttsVoiceList .active').dataset.voiceId")}
        # This fixture represents the normal review_required service response.
        evaluate("VS_PAGES.training.state={draft:{id:'audit-draft',confirmed_seconds:0,segments:[{id:'audit-segment',seconds:1,text:'needs review',flags:['noise'],included:true,eligible:false}],abnormal:[{id:'audit-segment'}]}};VS_PAGES.training.confirmDraft()")
        QTest.qWait(100)
        result["draft_review"] = observe("({dialog:!!document.querySelector('#trainDraftPanel'),rows:document.querySelectorAll('#trainDraftPanel .sample-row').length,editors:document.querySelectorAll('#trainDraftPanel input,#trainDraftPanel textarea').length})")
        result["bridge_error"] = evaluate("__vsBridge.error||''")
    finally:
        QFileDialog.getOpenFileName = original_picker
        window.close()
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(encoded + "\n", encoding="utf-8")
    print(encoded, flush=True)
    temporary.cleanup()
    # QtWebEngine child processes may otherwise keep the test process alive.
    os._exit(0)


if __name__ == "__main__":
    main()
