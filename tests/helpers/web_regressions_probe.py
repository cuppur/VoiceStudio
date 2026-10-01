"""Exercise the repaired product controls against real bridge/services in isolation."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from local_voice_studio.ui.web.shell import WebStudioWindow
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import VoiceProfile, DatasetDraft, DatasetDraftSegment, TrainingWorkflow, WorkflowStage
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtTest import QTest
from cover_flow_probe import FakeClient, _wav


def main():
    app = QApplication([])
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    data = root / 'data'
    paths = AppPaths(data, root / 'projects', data / 'runtime', data / 'engine', data / 'models', data / 'logs', data / 'studio.sqlite3', data / 'cache')
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project('regression')
    client = FakeClient()
    window = WebStudioWindow(paths, store, client=client)
    window.show()
    results = {}

    def js(source):
        box = {}
        loop = QEventLoop()
        timer = QTimer(); timer.setSingleShot(True); timer.timeout.connect(loop.quit)
        window.view.page().runJavaScript(source, lambda value: (box.update(value=value), loop.quit()))
        timer.start(5000); loop.exec(); timer.stop()
        assert 'value' in box, source
        return box['value']

    def wait(source):
        for _ in range(100):
            QTest.qWait(50)
            if js(source): return
        raise AssertionError('Not ready: ' + source + '\n' + str(js('JSON.stringify({id:__vsBridge.song&&__vsBridge.song.id,tracks:VS_MEDIA.state.tracks.map(t=>t.role),error:__vsBridge.error,toast:document.querySelector("#toast").textContent})')))

    wait('!!(window.__vsBridge && __vsBridge.pagesReady)')
    source = _wav(root / '中文 空格' / '同名.wav', 1)
    picked = [str(source)]
    QFileDialog.getOpenFileName = lambda *a, **k: (picked[0], '')
    js("document.querySelector('#coverImport').click()")
    wait('__vsBridge.songs.length === 1 && VS_PAGES.cover.songId === __vsBridge.song.id')
    wait('VS_MEDIA.state.tracks.length === 1')
    first = js('__vsBridge.song.id')
    results['first_import'] = js("!document.querySelector('#importModal') && __vsBridge.song.duration_ms === 1000 && VS_MEDIA.state.tracks[0].peaks.every(p=>p[0]===0&&p[1]===0)")
    js("document.querySelector('#coverRender').click()")
    wait("!!document.querySelector('.modal-mask.show')")
    results['rights_dialog'] = js("document.querySelector('.modal-mask.show h3').textContent === '歌曲权利确认'")
    js("document.querySelector('.modal-mask.show .modal-actions button').click()")
    picked[0] = ''
    js("document.querySelector('#coverImport').click()")
    QTest.qWait(100)
    results['cancel_import'] = js('__vsBridge.songs.length === 1')
    # Native drop creates an opaque, one-use selection. External paths never
    # become arbitrary filesystem privileges on the JavaScript bridge.
    window.bridge.offer_files([str(source)])
    wait('__vsBridge.songs.length === 2')
    second = js('__vsBridge.song.id')
    js('document.querySelector(' + json.dumps('[data-cover-id="' + first + '"]') + ').click()')
    wait('VS_PAGES.cover.songId === __vsBridge.song.id')
    results['duplicate_titles'] = first != second and js('__vsBridge.song.id') == first and js('VS_PAGES.cover.songId') == first
    lrc = root / '中文歌词.lrc'; lrc.write_text('[00:00.00]真实歌词\n[00:00.50]第二句\n', encoding='utf-8')
    picked[0] = str(lrc)
    js("document.querySelector('.lyrics .mini').click()")
    wait('__vsBridge.song.lyrics.length === 2')
    results['lrc_import'] = js("__vsBridge.song.lyrics[0].text === '真实歌词'")
    js('window.received=[];const oldEvent=VS_PAGES.onEvent;VS_PAGES.onEvent=(n,d)=>{received.push(n);oldEvent(n,d)}')
    window.bridge.notify('engine.install.started')
    window.bridge.notify('engine.install.log', {'line': 'real-event'})
    window.bridge.notify('training.error', {'message': 'test-error'})
    window.bridge.notify('voices.changed')
    wait("received.includes('voices.changed') && !!document.querySelector('#engineLog div')")
    results['events'] = js("received.includes('training.error') && document.querySelector('#engineLog').textContent === 'real-event'")
    js("document.querySelector('#engineClose').click()")
    profiles = [VoiceProfile(name=name, consent_confirmed=True) for name in ('唯一甲', '唯一乙')]
    for profile in profiles: store.save_profile(project, profile)
    js("__vsBridge.invoke('app.refresh')")
    wait("document.querySelectorAll('#ttsVoiceList .voice-row').length === 2")
    js("document.querySelectorAll('#ttsVoiceList .voice-row')[1].click()")
    selected = js('__vsBridge.ttsVoiceId')
    js("__vsBridge.invoke('app.refresh')")
    QTest.qWait(200)
    results['tts_preserved'] = js("document.querySelector('#ttsVoiceList .active').dataset.voiceId") == selected
    js("const search=document.querySelector('#ttsVoiceSearch');search.value='唯一甲';search.dispatchEvent(new Event('input'))")
    results['tts_search'] = js("document.querySelectorAll('#ttsVoiceList .voice-row').length === 1 && document.querySelector('#ttsVoiceList').textContent.includes('唯一甲')")
    workflow = TrainingWorkflow(profiles[0].id, profiles[0].name, stage=WorkflowStage.REVIEW_REQUIRED)
    segment = DatasetDraftSegment('audio/sample.wav', 0, 1, text='旧文字')
    draft = DatasetDraft(workflow.id, profiles[0].id, 'preparation', [segment])
    workflow.draft_id = draft.id
    store.save_workflow(project, workflow); store.save_draft(project, draft)
    js('VS_PAGES.training.refresh(' + json.dumps(profiles[0].id) + ')')
    wait('!!(VS_PAGES.training.state.draft && VS_PAGES.training.state.draft.id)')
    js('VS_PAGES.training.confirmDraft()')
    wait("!!document.querySelector('#trainDraftPanel textarea')")
    js("const edit=document.querySelector('#trainDraftPanel textarea');edit.value='校对后的文字';edit.dispatchEvent(new Event('input',{bubbles:true}));Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='保存校对').click()")
    wait("!document.querySelector('#trainDraftPanel')")
    results['draft_saved'] = store.load_draft(project, draft.id).segments[0].text == '校对后的文字'
    # Inspect the conversion payload without running an untrained model.
    js("window.savedInvoke=__vsBridge.invoke;window.convertPayload=null;__vsBridge.invoke=(action,payload)=>{if(action==='cover.convert')convertPayload=payload};VS_PAGES.cover.voiceId='test-voice';document.querySelector('#dereverbToggle').classList.add('on');VS_PAGES.cover.convert()")
    results['dereverb_enabled'] = js("convertPayload.cleanup.dereverb === 'light'")
    js("document.querySelector('#dereverbToggle').classList.remove('on');VS_PAGES.cover.convert()")
    results['dereverb_disabled'] = js("convertPayload.cleanup === null")
    js("__vsBridge.invoke=savedInvoke")
    # Real native player routing, output gains, mute/solo and seeking (no GPU).
    cover = CoverProject.load(project, first)
    for role in ('vocal', 'instrumental'):
        target = cover.root / 'stems' / (role + '.wav'); target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target); cover.set_stem(role, target)
    cover.save()
    js("__vsBridge.invoke('app.refresh')")
    wait('VS_MEDIA.state.tracks.length === 3')
    js("document.querySelector('#tracks .track-btn').click()")
    QTest.qWait(200)
    results['mute'] = not window.bridge.media.plan.active_tracks and window.bridge.media.outputs['original'].volume() == 0
    js("document.querySelector('#tracks .track-btn').click();document.querySelectorAll('#tracks .track-btn')[1].click()")
    QTest.qWait(200)
    results['solo'] = [t.role.value for t in window.bridge.media.plan.active_tracks] == ['original']
    js("const volume=document.querySelector('.transport-right input');volume.value=25;volume.dispatchEvent(new Event('input'))")
    QTest.qWait(200)
    results['volume'] = abs(window.bridge.media.outputs['original'].volume() - .25) < .001
    window.bridge.media.control({'operation': 'seek', 'value': 500})
    results['seek'] = window.bridge.media.position == 500
    task = window.bridge.cover.start_task('separate_song', {}, kind='separate', stage='separation', title='cancel-test', cover_id=first)
    js("__vsBridge.invoke('app.refresh')")
    wait("!!document.querySelector('[data-task-cancel]')")
    js("document.querySelector('[data-task-cancel]').click()")
    QTest.qWait(200)
    results['cancel_task'] = client.sent[-1][1:] == ('cancel', {'target_request_id': task['request_id']})
    window.bridge.cover._tasks.clear()
    # Logs must not trigger full snapshots or rebuild the training material list.
    QTest.qWait(400)
    js("window.refreshCount=0;window.nativeInvoke=__vsBridge.bridge.invoke;__vsBridge.bridge.invoke=function(action,payload,done){if(action==='app.refresh')refreshCount++;return nativeInvoke(action,payload,done)}")
    for index in range(200):
        window.bridge.notify('worker.event', {'event':'log','payload':{'message':str(index)}})
    QTest.qWait(300)
    results['log_refresh_storm_fixed'] = js('refreshCount===0')
    js('__vsBridge.bridge.invoke=nativeInvoke')
    # Parameter controls send actual RVC settings; automatic training is the default.
    results['training_default_auto'] = js("!document.querySelector('#manualTrainingReview').checked && document.querySelector('#trainingQuality').value==='standard'")
    results['supported_controls'] = js("!!document.querySelector('#rvcIndex') && !document.querySelector('#advancedBtn').classList.contains('hidden') && document.querySelector('#previewRangeBtn').textContent.includes('选区') && typeof VS_MEDIA.playRange==='function'")
    js("document.querySelector('[data-page=\"cover\"]').click()")
    js("document.querySelector('#songList .song-overflow').click()")
    wait("!!document.querySelector('.modal-mask.show')")
    js("Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='删除歌曲').click()")
    wait("!!Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='删除')")
    delete_id = js("document.querySelector('#songList .song-row').dataset.coverId")
    js("Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='删除').click()")
    wait('!__vsBridge.songs.some(s=>s.id===' + json.dumps(delete_id) + ')')
    results['song_deleted'] = (project / '.trash' / 'songs' / delete_id / 'manifest.json').is_file()
    other = store.create_project('other')
    reply = json.loads(window.bridge.invoke('project.activate', json.dumps({'path': str(other)})))
    wait("!VS_PAGES.cover.songId")
    results['project_switch'] = reply['ok'] and window.session.current == other and store.get_setting('ui.last_project') == str(other)
    results['no_js_error'] = not js('__vsBridge.error || ""')
    window.close()
    print('VS_REGRESSIONS ' + json.dumps(results), flush=True)
    assert all(results.values()), results
    temp.cleanup()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc(); sys.stderr.flush(); os._exit(1)
    os._exit(0)
