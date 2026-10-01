"""Exercise the production workstation with isolated files and QtWebChannel.

The worker transport is a recorder; waveform decode and preview routing are
real. No user workspace, model installation or GPU inference is touched.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QEventLoop, QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_voice_studio.application.jobs import JobStage, ProductJob, ProductJobStatus
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.services.cover import CoverService
from local_voice_studio.ui.web.shell import WebStudioWindow
from cover_flow_probe import FakeClient, _wav


def main():
    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(prefix="vs-workstation-probe-")
    root = Path(temporary.name)
    data = root / "data"
    paths = AppPaths(data, root / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "studio.sqlite3", data / "cache")
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("workstation")
    client = FakeClient()
    service = CoverService(paths, store, project, client)
    service.import_song(_wav(root / "真实测试甲.wav", 6))
    service.import_song(_wav(root / "真实测试乙.wav", 6))
    window = WebStudioWindow(paths, store, client=client)
    window.resize(1440, 900)
    window.show()
    checks = {}

    def js(source):
        box = {}
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        window.view.page().runJavaScript(source, lambda value: (box.update(value=value), loop.quit()))
        timer.start(6000)
        loop.exec()
        timer.stop()
        if "value" not in box:
            raise AssertionError("JavaScript callback timed out: " + source)
        return box["value"]

    def wait(expression):
        for _ in range(100):
            QTest.qWait(50)
            if js(expression):
                return
        raise AssertionError("Not ready: " + expression + "\n" + str(js("JSON.stringify({error:__vsBridge.error,toast:document.querySelector('#toast').textContent,selection:VS_WORKSTATION.state.selection})")))

    wait("!!(window.__vsBridge && __vsBridge.pagesReady && window.VS_WORKSTATION)")
    js("window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>probeErrors.push(String(e.reason)))")
    wait("VS_MEDIA.state.tracks.length === 1")
    checks["five_destinations"] = js("Array.from(document.querySelectorAll('.nav-item')).map(n=>n.dataset.page).join(',')==='cover,tts,voices,separator,exports'")
    checks["no_global_import"] = js("!document.querySelector('#quickImportBtn')")
    checks["no_roadmap_or_duplicate_workbench"] = js("!document.querySelector('.production-bar') && !document.querySelector('[data-page-view=assessment]')")
    checks["player_hidden_until_audio"] = js("getComputedStyle(document.querySelector('.transport')).display === 'none'")
    js("document.querySelector('[data-page=voices]').click();document.querySelector('[data-page-view=voices] .page-actions .btn').click()")
    wait("!document.querySelector('[data-page-view=train]').classList.contains('hidden')")
    checks["nested_training"] = js("document.querySelector('.nav-item.active').dataset.page === 'voices' && !!document.querySelector('#backToVoices') && document.querySelectorAll('[data-page-view=train] .step-item').length===6")
    js("document.querySelector('#backToVoices').click()")
    wait("!document.querySelector('[data-page-view=voices]').classList.contains('hidden')")
    checks["training_return"] = True
    js("document.querySelector('[data-page=cover]').click()")
    QTest.qWait(100)

    first = js("__vsBridge.song.id")
    other = js("__vsBridge.songs.find(s=>s.id!==__vsBridge.song.id).id")
    initial = js("document.querySelector('#coverPitch').value")
    js("const pitch=document.querySelector('#coverPitch');pitch.value=5;pitch.dispatchEvent(new Event('input',{bubbles:true}))")
    wait("document.querySelector('#autoSaveState').textContent==='参数已自动保存' && !document.querySelector('#undoEdit').disabled")
    js("document.querySelector('#undoEdit').click()")
    checks["undo"] = js("document.querySelector('#coverPitch').value") == initial
    js("document.querySelector('#redoEdit').click()")
    checks["redo"] = js("document.querySelector('#coverPitch').value === '5'")
    checks["keyboard_undo"] = js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}));document.querySelector('#coverPitch').value") == initial
    js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'y',ctrlKey:true,bubbles:true}))")
    checks["keyboard_redo"] = js("document.querySelector('#coverPitch').value === '5'")
    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(other) + "))")
    wait("VS_PAGES.cover.songId === " + json.dumps(other))
    js("(()=>{const pitch=document.querySelector('#coverPitch');pitch.value=-2;pitch.dispatchEvent(new Event('input',{bubbles:true}))})()")
    QTest.qWait(400)
    checks["second_song_draft"] = js("document.querySelector('#coverPitch').value === '-2'")
    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(first) + "))")
    wait("VS_PAGES.cover.songId === " + json.dumps(first))
    checks["song_draft_isolation"] = js("document.querySelector('#coverPitch').value === '5'")
    js("document.querySelector('#zoomIn').click()")
    checks["timeline_zoom"] = js("VS_WORKSTATION.state.zoom===1.5 && document.querySelector('#tracks').scrollWidth>document.querySelector('#tracks').clientWidth")
    js("document.querySelector('#zoomReset').click()")
    # Dispatch native pointer events through the rendered QtWebEngine widget.
    rect = json.loads(js("JSON.stringify((()=>{const r=document.querySelector('#tracks .wave-wrap').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})())"))
    target = window.view.focusProxy() or window.view
    start = QPoint(round(rect["x"] + rect["w"] * .1), round(rect["y"] + rect["h"] / 2))
    end = QPoint(round(rect["x"] + rect["w"] * .4), start.y())
    QTest.mousePress(target, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(target, end, 40)
    QTest.mouseRelease(target, Qt.LeftButton, Qt.NoModifier, end)
    wait("VS_WORKSTATION.state.selection[1]>VS_WORKSTATION.state.selection[0]")
    checks["native_waveform_selection"] = js("VS_WORKSTATION.state.selection[0]>400 && VS_WORKSTATION.state.selection[1]<2800")
    js("document.querySelector('#loopRegion').click()")
    checks["loop_toggle"] = js("VS_WORKSTATION.state.loop && document.querySelector('#loopRegion').classList.contains('active')")
    js("document.querySelector('#previewRangeBtn').click()")
    wait("VS_MEDIA.state.playing && document.querySelector('.app').classList.contains('has-audio')")
    checks["real_preview_playback"] = bool(window.bridge.media.playing and window.bridge.media.plan.active_tracks)
    js("document.querySelector('#collapsePlayer').click()")
    checks["player_collapses"] = js("document.querySelector('.app').classList.contains('player-collapsed') && getComputedStyle(document.querySelector('#playerHandle')).display!=='none'")
    js("document.querySelector('#playerHandle').click()")
    checks["player_expands"] = js("!document.querySelector('.app').classList.contains('player-collapsed')")
    js("document.querySelector('#settingsBtn').click()")
    wait("!document.querySelector('.app').classList.contains('has-audio')")
    checks["player_context_switch"] = not window.bridge.media.playing
    js("document.querySelector('[data-page=cover]').click();document.querySelector('#localGenerate').click()")
    checks["local_generation_explicitly_unwired"] = js("document.querySelector('#toast').textContent==='该功能尚未接入新界面'")
    js("document.querySelector('[data-page=tts]').click();__vsBridge.playFile(__vsBridge.song.source_path,'真实本地语音试听')")
    wait("!!(__vsBridge.audio && !__vsBridge.audio.paused) && document.querySelector('.app').classList.contains('has-audio')")
    wait("document.querySelector('#nowTitle').textContent==='真实本地语音试听'")
    checks["tts_context_player"] = js("VS_WORKSTATION.state.playerKind==='tts' && document.querySelector('#playerMode').textContent==='文字生成' && document.querySelector('#nowTitle').textContent==='真实本地语音试听'")
    if not checks["tts_context_player"]:
        print("TTS_PLAYER_DEBUG " + str(js("JSON.stringify({kind:VS_WORKSTATION.state.playerKind,mode:document.querySelector('#playerMode').textContent,title:document.querySelector('#nowTitle').textContent,errors:probeErrors})")), flush=True)
    js("document.querySelector('[data-page=voices]').click()")
    wait("!document.querySelector('.app').classList.contains('has-audio')")
    checks["file_player_stops_on_other_context"] = js("__vsBridge.audio.paused")
    js("document.querySelector('[data-page=cover]').click()")

    # Put two distinct, valid files into the persisted cover, with recorded
    # voice provenance. The sidebar must use those real take IDs and paths.
    voices = [VoiceProfile(name=name, consent_confirmed=True) for name in ("测试歌声甲", "测试歌声乙")]
    for voice in voices:
        store.save_profile(project, voice)
    cover = CoverProject.load(project, first)
    for index, voice in enumerate(voices):
        output = _wav(cover.root / "takes" / (str(index) + ".wav"), 6)
        cover.add_asset(CoverAsset("probe-mix-" + str(index), "final_mix", str(output.relative_to(cover.root)),
                                   hashlib.sha256(output.read_bytes()).hexdigest(), "ai_generated", "isolated-test",
                                   metadata={"profile_id": voice.id, "pitch_shift": index, "settings": {}}))
    js("__vsBridge.invoke('app.refresh')")
    wait("document.querySelector('#compareA').options.length===2 && !document.querySelector('#comparePair').disabled")
    checks["inline_version_compare"] = js("!!document.querySelector('.timeline #versionComparePanel') && !!document.querySelector('.timeline .card-head #versionCompareToggle') && !document.querySelector('#versionCompareModal') && document.querySelector('#compareA').textContent.includes('测试歌声甲') && document.querySelector('#compareB').textContent.includes('测试歌声乙')")
    js("document.querySelector('#versionCompareToggle').click()")
    js("document.querySelector('#comparePair').click()")
    wait("__vsBridge.audio && !__vsBridge.audio.paused && VS_PAGES.cover.compareOn")
    first_take_path = js("__vsBridge.audio.dataset.source")
    js("__vsBridge.audio.currentTime=2;document.querySelector('#abSeg [data-ab=B]').click()")
    wait("__vsBridge.audio.dataset.source!==" + json.dumps(first_take_path))
    QTest.qWait(150)
    checks["take_compare_uses_real_paths"] = first_take_path.endswith("0.wav") and js("__vsBridge.audio.dataset.source.endsWith('1.wav')")
    checks["take_compare_keeps_position"] = js("__vsBridge.audio.currentTime>=1.9 && __vsBridge.audio.currentTime<2.8")
    checks["take_compare_preserves_active_export"] = not CoverProject.load(project, first).active_take_id
    js("document.querySelector('#comparePair').click()")
    js("document.querySelector('[data-library-view=voice]').click()")
    wait("__vsBridge.libraryView==='voice'")
    checks["voice_grouped_real_versions"] = js("Array.from(document.querySelectorAll('.library-voice-heading b')).some(n=>n.textContent==='测试歌声甲') && Array.from(document.querySelectorAll('.library-voice-heading b')).some(n=>n.textContent==='测试歌声乙') && document.querySelectorAll('[data-library-version]').length===2")
    js("document.querySelector('[data-library-main=probe-mix-0]').click()")
    wait("VS_PAGES.cover.state.active_take_id==='probe-mix-0'")
    wait("document.querySelector('[data-library-main=probe-mix-0]').textContent==='★' && document.querySelector('#activeRendition').textContent.includes('测试歌声甲')")
    checks["take_selection_persisted"] = CoverProject.load(project, first).active_take_id == "probe-mix-0"
    checks["voice_group_and_default_survive_refresh"] = js("__vsBridge.libraryView==='voice' && document.querySelectorAll('.library-voice-heading').length===3")
    screenshot = os.environ.get("VS_PROBE_SCREENSHOT")
    if screenshot:
        QTest.qWait(150)
        Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
        assert window.view.grab().save(screenshot)

    task = window.bridge.cover.start_task("separate_song", {}, kind="separate", stage="separation", title="队列取消验证", cover_id=first)
    js("__vsBridge.invoke('app.refresh');document.querySelector('#gpuBtn').click()")
    wait("!!document.querySelector('[data-task-cancel]')")
    checks["single_real_task_center"] = js("document.querySelector('#taskDrawer').classList.contains('show') && document.querySelector('#taskDrawer h3').textContent==='GPU / 后台任务中心' && !document.querySelector('[data-queue-act]')")
    js("document.querySelector('[data-task-cancel]').click()")
    QTest.qWait(150)
    checks["real_task_cancel_route"] = client.sent[-1][1:] == ("cancel", {"target_request_id": task["request_id"]})
    window.bridge.cover._tasks.clear()
    # A persisted incomplete task must reach the real recovery handler. Invalid
    # checkpoint metadata is rejected there instead of being shown as success.
    recovery = ProductJob("ai_cover", {"project_path": str(project)}, [JobStage("separation")])
    recovery.status = ProductJobStatus.RECOVERABLE
    store.save_product_job(recovery)
    resumed = []
    native_resume = window.bridge._handlers["task.resume"]
    def resume(payload):
        resumed.append(dict(payload))
        return native_resume(payload)
    window.bridge._handlers["task.resume"] = resume
    js("__vsBridge.invoke('app.refresh')")
    wait("!!document.querySelector('[data-task-resume]')")
    js("document.querySelector('[data-task-resume]').click()")
    QTest.qWait(150)
    checks["real_task_resume_route"] = resumed == [{"id": recovery.id}]
    checks["invalid_checkpoint_not_fake_success"] = store.load_product_job(recovery.id).status == ProductJobStatus.RECOVERABLE
    other_project = store.create_project("workstation-other")
    CoverService(paths, store, other_project, client).import_song(_wav(root / "第三首.wav", 6))
    js("__vsBridge.invoke('project.activate',{path:" + json.dumps(str(other_project)) + "})")
    wait("__vsBridge.data.project.path===" + json.dumps(str(other_project)))
    js("(()=>{const pitch=document.querySelector('#coverPitch');pitch.value=-4;pitch.dispatchEvent(new Event('input',{bubbles:true}))})()")
    QTest.qWait(400)
    js("__vsBridge.invoke('project.activate',{path:" + json.dumps(str(project)) + "})")
    wait("__vsBridge.data.project.path===" + json.dumps(str(project)))
    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(first) + "))")
    wait("VS_PAGES.cover.songId === " + json.dumps(first))
    checks["project_draft_isolation"] = js("document.querySelector('#coverPitch').value==='5'")
    checks["no_javascript_error"] = not js("__vsBridge.error||''") and js("probeErrors.length===0")
    if not checks["no_javascript_error"]:
        print("PROBE_ERRORS " + str(js("JSON.stringify(probeErrors)")), flush=True)
    window.close()
    print("VS_WORKSTATION_PROBE " + json.dumps(checks, ensure_ascii=False), flush=True)
    assert all(checks.values()), checks
    temporary.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    os._exit(0)
