"""Probe the launcher's packaged target in isolated directories via local CDP.

Uses Qt's bundled WebSocket client; no additional dependency or real media.
Only the subprocess started here (and its children) is stopped on completion.
"""
from __future__ import annotations

import base64
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import shutil
import time
import urllib.request
import wave
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtWebSockets import QWebSocket

ROOT = Path(__file__).resolve().parents[1]


def training_fixture(root):
    """Create an isolated long material list and a resumable old review draft."""
    sys.path.insert(0, str(ROOT / 'src'))
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    from local_voice_studio.models import VoiceProfile, SourceAsset, TrainingWorkflow, WorkflowStage, DatasetDraft, DatasetDraftSegment
    data = root / 'data'
    paths = AppPaths(data, root/'projects', data/'runtime', data/'engine', root/'models', data/'logs', data/'studio.sqlite3', root/'cache')
    store = StudioStore(paths)
    project = store.create_project('操作验证')
    store.set_setting('ui.last_project', str(project))
    profile = VoiceProfile('测试声音', True, consent_record='隔离测试', consent_confirmed_at='2026-09-09')
    store.save_profile(project, profile)
    from local_voice_studio.audio import sha256_file
    assets = []
    for index in range(29):
        path = project / 'raw' / f'素材 {index + 1:02}.wav'
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 8000 * 24)
        assets.append(SourceAsset(profile.id,str(path),str(path),sha256_file(path),duration_seconds=24,sample_rate=8000,channels=1,codec='pcm_s16le'))
    store.save_source_assets(project, assets)
    workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.REVIEW_REQUIRED,
                                message='素材已准备，可自动继续或选择人工校对')
    draft = DatasetDraft(workflow.id,profile.id,'test-preparation',[
        DatasetDraftSegment('raw/素材 01.wav',0,6,text='这是用于界面验证的文字。') for _ in range(12)])
    workflow.draft_id = draft.id
    store.save_draft(project,draft); store.save_workflow(project,workflow)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--verify-fixed', action='store_true')
    parser.add_argument('--verify-round2', action='store_true')
    parser.add_argument('--verify-online', action='store_true', help='exercise real LRCLIB search/download through the packaged UI')
    parser.add_argument('--executable', type=Path, default=ROOT / 'dist' / 'LocalVoiceStudio' / 'LocalVoiceStudio.exe')
    options = parser.parse_args()
    app = QCoreApplication([])
    output = options.output
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voicestudio-packaged-audit-") as temporary:
        root = Path(temporary)
        projects = root / "projects"
        projects.mkdir()
        source = projects / "audit.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 8000)
        verify = options.verify_fixed
        round2 = options.verify_round2
        formats = []
        installed_tools = Path(os.environ.get('LOCALAPPDATA', '')) / 'LocalVoiceStudio' / 'tools'
        if verify and (installed_tools / 'ffmpeg.exe').is_file():
            # Isolate application data; reuse existing immutable tool bytes.
            shutil.copytree(installed_tools, root / 'data' / 'tools', copy_function=shutil.copy2)
            for extension in ('mp3', 'flac'):
                target = projects / ('中文 空格.' + extension)
                subprocess.run([str(root / 'data' / 'tools' / 'ffmpeg.exe'), '-v', 'error', '-i', str(source), str(target)], check=True, timeout=30)
                formats.append(target)
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        environment = dict(os.environ)
        environment.update(LOCAL_VOICE_STUDIO_HOME=str(root / "data"),
                           LOCAL_VOICE_STUDIO_PROJECTS=str(projects),
                           LOCAL_VOICE_STUDIO_MODELS=str(root / "models"),
                           LOCAL_VOICE_STUDIO_CACHE=str(root / "cache"),
                           QTWEBENGINE_REMOTE_DEBUGGING=f"127.0.0.1:{port}")
        if round2:
            training_fixture(root)
        executable = options.executable.resolve()
        process = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        ws = QWebSocket()
        replies = {}
        ws.textMessageReceived.connect(lambda raw: replies.update({json.loads(raw).get("id"): json.loads(raw)}))

        def until(predicate, seconds=10):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                if predicate():
                    return
                time.sleep(0.01)
            raise TimeoutError("Packaged application probe timed out")

        identifier = 0
        def command(method, params=None):
            nonlocal identifier
            identifier += 1
            key = identifier
            ws.sendTextMessage(json.dumps({"id": key, "method": method, "params": params or {}}))
            until(lambda: key in replies)
            reply = replies.pop(key)
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]

        def evaluate(expression):
            reply = command("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
            if "exceptionDetails" in reply:
                raise RuntimeError(reply["exceptionDetails"])
            return reply["result"].get("value")

        def verify_online():
            """Use the frozen UI and real HTTP; report metadata, never lyric text."""
            evidence = {'ok':False, 'track_name':'Doar Cu Tine', 'artist_name':'Activ',
                        'search_attempts':[], 'download_attempts':[]}
            try:
                evaluate("document.querySelector('#findOnlineLyrics').click()")
                until(lambda: evaluate("!!document.querySelector('#onlineLyricsMask')"))
                candidate = None
                for attempt in range(2):
                    evaluate("document.querySelector('#lyricsTrackName').value='Doar Cu Tine';document.querySelector('#lyricsArtistName').value='Activ';document.querySelector('#lyricsQuery').value='';document.querySelector('#lyricsSearchButton').click()")
                    started = time.perf_counter()
                    # Exercise another page while the HTTP request is pending.
                    # The modal intentionally stays open; this measures the
                    # actual event loop/bridge response, not modal dismissal.
                    evaluate("document.querySelector('.nav-item[data-page=\"tts\"]').click()")
                    until(lambda: evaluate("!document.querySelector('[data-page-view=\"tts\"]').classList.contains('hidden')"))
                    evidence['navigation_response_ms'] = round((time.perf_counter()-started)*1000,2)
                    evaluate("document.querySelector('.nav-item[data-page=\"cover\"]').click()")
                    until(lambda: evaluate("!document.querySelector('#lyricsSearchButton').disabled"), seconds=45)
                    candidate = evaluate("""(() => {
                        const buttons=Array.from(document.querySelectorAll('[data-lyrics-record]'));
                        const synced=buttons.filter(b=>!b.disabled&&b.textContent==='使用 LRC');
                        const chosen=synced.find(b=>b.dataset.lyricsRecord==='6789539')||synced[0];
                        return chosen ? {record_id:chosen.dataset.lyricsRecord, candidate_count:buttons.length,
                            synced_candidate_count:synced.length} : null;
                    })()""")
                    evidence['search_attempts'].append({'attempt':attempt+1,
                        'elapsed_seconds':round(time.perf_counter()-started,2),
                        'status':evaluate("document.querySelector('#onlineLyricsStatus').textContent"),
                        'synced_candidate_found':bool(candidate)})
                    if candidate:
                        break
                if not candidate:
                    raise RuntimeError('真实 LRCLIB 搜索未返回可用同步歌词（已尝试两次）')
                evidence.update(candidate)
                until(lambda: evaluate("!document.querySelector('[data-page-view=\"cover\"]').classList.contains('hidden')"))
                for attempt in range(2):
                    started = time.perf_counter()
                    evaluate("document.querySelector('[data-lyrics-record=\"" + candidate['record_id'] + "\"]').click()")
                    # Existing fixture lyrics require an explicit UI choice.
                    until(lambda: evaluate("!!Array.from(document.querySelectorAll('.modal-mask.show h3')).find(n=>n.textContent==='替换当前歌词')||/正在下载|已保存|下载失败/.test(document.querySelector('#onlineLyricsStatus').textContent)"))
                    confirmed = evaluate("""(() => {
                        const title=Array.from(document.querySelectorAll('.modal-mask.show h3')).find(n=>n.textContent==='替换当前歌词');
                        if(!title)return false;
                        const button=Array.from(title.closest('.modal-mask').querySelectorAll('button')).find(b=>b.textContent==='替换歌词');
                        button.click();return true;
                    })()""")
                    evidence['overwrite_confirmed'] = evidence.get('overwrite_confirmed',False) or confirmed
                    until(lambda: evaluate("(__vsBridge.song.lyrics_source&&__vsBridge.song.lyrics_source.provider==='LRCLIB'&&String(__vsBridge.song.lyrics_source.record_id)==='" + candidate['record_id'] + "'&&__vsBridge.song.lyrics_synced&&__vsBridge.song.lyrics.length>0)||document.querySelector('#onlineLyricsStatus').classList.contains('error')"), seconds=45)
                    downloaded = evaluate("!!(__vsBridge.song.lyrics_source&&__vsBridge.song.lyrics_source.provider==='LRCLIB'&&String(__vsBridge.song.lyrics_source.record_id)==='" + candidate['record_id'] + "'&&__vsBridge.song.lyrics_synced&&__vsBridge.song.lyrics.length>0)")
                    evidence['download_attempts'].append({'attempt':attempt+1,
                        'elapsed_seconds':round(time.perf_counter()-started,2), 'ok':downloaded,
                        'status':evaluate("document.querySelector('#onlineLyricsStatus').textContent")})
                    if downloaded:
                        break
                if not downloaded:
                    raise RuntimeError('真实 LRCLIB 歌词下载未能保存（已尝试两次）')
                evidence['saved'] = evaluate("({line_count:__vsBridge.song.lyrics.length,synced:__vsBridge.song.lyrics_synced,provider:__vsBridge.song.lyrics_source.provider,record_id:__vsBridge.song.lyrics_source.record_id,source_duration_seconds:__vsBridge.song.lyrics_source.duration_seconds,fixture_duration_ms:__vsBridge.song.duration_ms})")
                evaluate("document.querySelector('#closeOnlineLyrics').click()")
                until(lambda: evaluate("document.querySelectorAll('#lyrics .lyric[data-sync=\"true\"]').length>0"))
                evidence['seek_and_highlight'] = evaluate("""(() => {
                    const row=document.querySelector('#lyrics .lyric[data-sync="true"]');
                    const original=VS_MEDIA.seek,calls=[];
                    VS_MEDIA.seek=function(ms){calls.push(ms);return original.call(this,ms);};
                    try { row.click(); return {requested_ms:Number(row.dataset.time)*1000,
                        native_seek_calls_ms:calls, highlighted_after_click:row.classList.contains('active'),
                        timed_rows:document.querySelectorAll('#lyrics .lyric[data-time]').length}; }
                    finally { VS_MEDIA.seek=original; }
                })()""")
                assert evidence['seek_and_highlight']['native_seek_calls_ms'] == [evidence['seek_and_highlight']['requested_ms']], evidence
                assert evidence['seek_and_highlight']['highlighted_after_click'], evidence
                # The fixture has one second of synthetic audio. Seeking its
                # downloaded song timestamps proves routing, not song accuracy.
                evidence['fixture_limitation'] = 'one-second synthetic source; validates network persistence, timestamp layout and seek routing, not real-song alignment'
                evidence['ok'] = True
            except Exception as exc:
                evidence['error'] = str(exc)
            finally:
                screenshot = command('Page.captureScreenshot', {'format':'png'})
                (output / 'online-lyrics.png').write_bytes(base64.b64decode(screenshot['data']))
                (output / 'online-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                evaluate("document.querySelector('#closeOnlineLyrics')?.click();Array.from(document.querySelectorAll('.modal-mask.show')).filter(n=>n.id!=='onlineLyricsMask').forEach(n=>n.remove())")
            return evidence

        try:
            tabs = []
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        tabs = json.load(response)
                    if tabs:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            if not tabs:
                raise RuntimeError("No packaged page was exposed")
            ws.open(QUrl(tabs[0]["webSocketDebuggerUrl"]))
            until(lambda: ws.isValid())
            until(lambda: evaluate("!!(window.__vsBridge && __vsBridge.data && __vsBridge.pagesReady && window.VS_PAGES)"))
            result = {"executable": str(executable), "page_url": tabs[0]["url"]}
            result['ui_contract'] = evaluate("""(() => {
                const q = selector => document.querySelector(selector), panel = q('#versionComparePanel');
                return {
                    fiveNavigation: document.querySelectorAll('.nav-item').length === 5,
                    noTrainingNavigation: !q('.nav-item[data-page="train"]'),
                    noGlobalImport: !q('#quickImportBtn'),
                    timelineComparison: !!(panel && panel.closest('.timeline') && q('.timeline .card-head #versionCompareToggle') && q('#compareA') && q('#compareB') && q('#comparePair') && q('#abSeg')),
                    comparisonInitiallyCollapsed: !!(panel && panel.classList.contains('hidden')),
                    onlineLyricsEntry: !!q('#findOnlineLyrics'),
                    demoGuard: Array.from(document.scripts).some(s=>!s.src && s.textContent.includes('if (!window.qt || window.__VS_STATIC__)')),
                    noDemoProduction: !q('#productionModal'),
                    bridgeHealthy: !__vsBridge.error,
                };
            })()""")
            assert all(result['ui_contract'].values()), result['ui_contract']
            evaluate("document.querySelector('#versionCompareToggle').click()")
            result['ui_contract']['comparisonExpands'] = evaluate("!document.querySelector('#versionComparePanel').classList.contains('hidden')")
            evaluate("document.querySelector('#versionCompareToggle').click()")
            result['ui_contract']['comparisonCollapses'] = evaluate("document.querySelector('#versionComparePanel').classList.contains('hidden')")
            assert all(result['ui_contract'].values()), result['ui_contract']
            result["before_import"] = evaluate("({bridgeConnected:!!__vsBridge.bridge,bridgeError:__vsBridge.error||'',songs:__vsBridge.songs.length})")
            result["import_reply"] = evaluate("new Promise(resolve=>__vsBridge.invoke('song.import'," + json.dumps({"path": str(source)}) + ",r=>resolve({ok:r.ok,message:r.message})))")
            result["after_import"] = evaluate("({songs:__vsBridge.songs.length,selectedId:__vsBridge.song&&__vsBridge.song.id,operationId:VS_PAGES.cover.songId})")
            if verify:
                until(lambda: evaluate('VS_PAGES.cover.songId === __vsBridge.song.id && VS_MEDIA.state.tracks.length === 1'))
                result['waveform'] = evaluate('({duration:VS_MEDIA.state.duration,rate:VS_MEDIA.state.tracks[0].metadata.sample_rate,silent:VS_MEDIA.state.tracks[0].peaks.every(p=>p[0]===0&&p[1]===0)})')
                assert result['after_import']['selectedId'] == result['after_import']['operationId'], result
                assert result['waveform'] == {'duration': 1000, 'rate': 8000, 'silent': True}, result
                # Seed plain lyrics only in this disposable project. The real
                # packaged snapshot/parser, rather than fake DOM, renders it.
                sys.path.insert(0, str(ROOT / 'src'))
                from local_voice_studio.cover.project import CoverProject
                project_path = Path(evaluate('__vsBridge.data.project.path')).resolve()
                project_path.relative_to(projects.resolve())
                lyric_cover = CoverProject.load(project_path, evaluate('__vsBridge.song.id'))
                lyric_file = lyric_cover.root / 'lyrics' / 'plain.txt'
                lyric_file.parent.mkdir(parents=True, exist_ok=True)
                lyric_file.write_text('这是第一行纯文本歌词\n这是第二行纯文本歌词\n', encoding='utf-8')
                lyric_cover.lyrics_path = lyric_file.relative_to(lyric_cover.root).as_posix()
                lyric_cover.lyrics_origin = 'manual'
                lyric_cover.save()
                evaluate("new Promise(resolve=>__vsBridge.invoke('app.refresh',{},resolve))")
                until(lambda: evaluate("document.querySelectorAll('#lyrics .lyric[data-sync=\"false\"]').length===2"))
                result['plain_lyrics'] = evaluate("""(() => {
                    const rows=Array.from(document.querySelectorAll('#lyrics .lyric'));
                    return {count:rows.length, noTiming:rows.every(n=>!n.hasAttribute('data-time')),
                        noSeek:rows.every(n=>n.onclick===null&&n.getAttribute('aria-disabled')==='true'),
                        textLabel:rows.every(n=>n.querySelector('time').textContent==='文本'),
                        textPreserved:rows[0].textContent.includes('这是第一行纯文本歌词')};
                })()""")
                assert result['plain_lyrics']['count'] == 2 and all(result['plain_lyrics'].values()), result
                screenshot = command('Page.captureScreenshot', {'format':'png'})
                (output / 'plain-lyrics.png').write_bytes(base64.b64decode(screenshot['data']))
            if options.verify_online:
                result['online_lyrics'] = verify_online()
            evaluate("document.querySelector('#coverRender').click()")
            result["render_toast"] = evaluate("document.querySelector('#toast').textContent")
            if verify:
                until(lambda: evaluate("!!document.querySelector('.modal-mask.show')"))
                result['next_step'] = evaluate("document.querySelector('.modal-mask.show h3').textContent")
                assert result['next_step'] == '歌曲权利确认', result
                evaluate("document.querySelector('.modal-mask.show .modal-actions button').click()")
                result['formats'] = []
                for target in formats:
                    response = evaluate("new Promise(resolve=>__vsBridge.invoke('song.import'," + json.dumps({'path': str(target)}) + ",r=>resolve({ok:r.ok,message:r.message})))")
                    assert response['ok'], response
                    until(lambda: evaluate("VS_MEDIA.state.coverId===__vsBridge.song.id && VS_MEDIA.state.tracks.length===1"))
                    result['formats'].append({'format': target.suffix, 'ok': response['ok'], 'duration': evaluate('__vsBridge.song.duration_ms')})
                # Verify the real export dialog's choices, then cancel without
                # creating media or sending a worker command.
                evaluate("window.auditSavedCoverState=VS_PAGES.cover.state;VS_PAGES.cover.state={...VS_PAGES.cover.state,has_final_mix:true};VS_PAGES.cover.exportFinal();true")
                until(lambda: evaluate("document.querySelectorAll('.modal-mask.show [data-format]').length===5"))
                result['export_format_choices'] = evaluate("Array.from(document.querySelectorAll('.modal-mask.show [data-format]')).map(n=>n.dataset.format)")
                assert {'wav','mp3','flac','m4a','both'} == set(result['export_format_choices']), result
                evaluate("document.querySelector('.modal-mask.show .modal-actions button').click();VS_PAGES.cover.state=auditSavedCoverState;delete window.auditSavedCoverState")
                # Delete a disposable second import through the actual bridge.
                # The first song remains available for subsequent state checks.
                response = evaluate("new Promise(resolve=>__vsBridge.invoke('song.import'," + json.dumps({'path':str(source)}) + ",resolve))")
                assert response['ok'], response
                until(lambda: evaluate("VS_PAGES.cover.songId===__vsBridge.song.id"))
                delete_id = evaluate('__vsBridge.song.id')
                before = evaluate('__vsBridge.songs.length')
                response = evaluate("new Promise(resolve=>__vsBridge.invoke('song.delete'," + json.dumps({'cover_id':delete_id}) + ",resolve))")
                assert response['ok'], response
                until(lambda: evaluate('__vsBridge.songs.length===' + str(before-1)))
                result['delete_song'] = {'ok':True, 'external_source_preserved':source.is_file(),
                                         'remaining_songs':evaluate('__vsBridge.songs.length')}
                assert result['delete_song']['external_source_preserved'], result
            screenshot = command("Page.captureScreenshot", {"format": "png"})
            (output / "packaged-import.png").write_bytes(base64.b64decode(screenshot["data"]))
            if round2:
                evaluate("document.querySelector('.nav-item[data-page=\"voices\"]').click()")
                evaluate("Array.from(document.querySelectorAll('[data-page-view=\"voices\"] .page-actions button')).find(b=>b.textContent.trim()==='训练新声音').click()")
                until(lambda: evaluate("!document.querySelector('[data-page-view=\"train\"]').classList.contains('hidden')"))
                result['ui_contract']['nestedTraining'] = True
                until(lambda: evaluate("document.querySelectorAll('#sampleList .sample-row').length===29"))
                result['training_viewports'] = []
                for width,height in ((1440,900),(1280,720)):
                    command('Emulation.setDeviceMetricsOverride', {'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
                    state = evaluate("""(() => { const b=document.querySelector('#trainStart').getBoundingClientRect(); const r=document.querySelector('#reviewTraining').getBoundingClientRect();return {width:innerWidth,height:innerHeight,primary:document.querySelector('#trainStart').textContent,visible:b.top>=0&&b.bottom<=innerHeight&&r.top>=0&&r.bottom<=innerHeight,manualDefault:document.querySelector('#manualTrainingReview').checked};})()""")
                    assert state['visible'] and not state['manualDefault'],state
                    result['training_viewports'].append(state)
                    screenshot=command('Page.captureScreenshot',{'format':'png'})
                    (output/f'training-{width}.png').write_bytes(base64.b64decode(screenshot['data']))
                evaluate("document.querySelector('#reviewTraining').click()")
                until(lambda:evaluate("document.querySelectorAll('#trainDraftPanel textarea').length===12"))
                result['review_accessible'] = True
                screenshot=command('Page.captureScreenshot',{'format':'png'})
                (output/'training-review.png').write_bytes(base64.b64decode(screenshot['data']))
                evaluate("document.querySelector('.modal-mask.show button').click()")
                started=time.perf_counter()
                for _ in range(20):
                    evaluate("new Promise(resolve=>__vsBridge.invoke('cover.state',{cover_id:__vsBridge.song.id},resolve))")
                result['cover_state_roundtrip_mean_ms'] = round((time.perf_counter()-started)*1000/20,2)
            (output / "packaged-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if options.verify_online and not result['online_lyrics']['ok']:
                raise RuntimeError('真实联网歌词验收失败；参见 online-evidence.json')
        finally:
            ws.close()
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
