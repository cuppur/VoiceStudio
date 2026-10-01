"""QtWebEngine lyric workflow probe with real files and a controlled network boundary.

Uses the real desktop channel, snapshot parser and native preview seek. Network
requests receive queued fixture events so failure/race behavior is repeatable.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.services.cover import CoverService
from local_voice_studio.ui.web.shell import WebStudioWindow
from cover_flow_probe import FakeClient, _wav


def main():
    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(prefix="vs-online-lyrics-ui-")
    root = Path(temporary.name)
    data = root / "data"
    paths = AppPaths(data, root / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "studio.sqlite3", data / "cache")
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("lyrics-ui")
    client = FakeClient()
    service = CoverService(paths, store, project, client)
    service.import_song(_wav(root / "测试歌曲.wav", 12))
    service.import_song(_wav(root / "只有文本.wav", 12))
    service.import_song(_wav(root / "旧歌词已缺失.wav", 12))
    covers = CoverProject.list(project)
    timed = next(cover for cover in covers if cover.title == "测试歌曲")
    plain = next(cover for cover in covers if cover.title == "只有文本")
    missing = next(cover for cover in covers if cover.title == "旧歌词已缺失")
    lrc = timed.root / "lyrics" / "test.lrc"
    lrc.parent.mkdir(exist_ok=True)
    lines = ["[00:00.50]第一句", "[00:01.50]第二句", "[00:01.50]第二句翻译"]
    lines.extend(f"[00:{2 + index * .4:05.2f}]歌词第 {index + 4} 行" for index in range(20))
    lrc.write_text("\n".join(lines), encoding="utf-8")
    timed.set_lyrics(lrc)
    txt = plain.root / "lyrics" / "text.txt"
    txt.parent.mkdir(exist_ok=True)
    txt.write_text("没有时间轴的歌词\n这是纯文本第二行\n", encoding="utf-8")
    plain.set_lyrics(txt)
    missing_lrc = missing.root / "lyrics" / "missing.lrc"
    missing_lrc.parent.mkdir(exist_ok=True)
    missing_lrc.write_text("[00:00.50]待删除\n", encoding="utf-8")
    missing.set_lyrics(missing_lrc)
    missing_lrc.unlink()
    window = WebStudioWindow(paths, store, client=client)
    window.resize(1440, 900)
    window.show()
    checks = {}
    searches, downloads = [], []
    records = [
        {"record_id": 101, "title": "测试歌曲", "artist": "原唱甲", "album": "专辑", "duration_seconds": 12, "duration_diff_seconds": 0, "synced": True, "instrumental": False, "source": "LRCLIB"},
        {"record_id": 102, "title": "测试歌曲 · 现场", "artist": "原唱乙", "album": "", "duration_seconds": 19, "duration_diff_seconds": 7, "synced": False, "instrumental": False, "source": "LRCLIB"},
        {"record_id": 103, "title": "测试歌曲 · 伴奏", "artist": "原唱甲", "duration_seconds": 12, "duration_diff_seconds": 0, "synced": False, "instrumental": True, "source": "LRCLIB"},
    ]

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
            raise AssertionError("JavaScript timed out: " + source)
        return box["value"]

    def wait(expression):
        for _ in range(100):
            QTest.qWait(50)
            if js(expression):
                return
        raise AssertionError("Not ready: " + expression + "\n" + str(js("JSON.stringify({error:__vsBridge.error,toast:document.querySelector('#toast').textContent,status:document.querySelector('#onlineLyricsStatus')?.textContent,errors:window.probeErrors})")))

    def acknowledge_search(payload):
        request = {**payload, "request_id": "search-" + str(len(searches) + 1)}
        searches.append(request)
        return {"ok": True, "request_id": request["request_id"]}

    def acknowledge_download(payload):
        request = {**payload, "request_id": "download-" + str(len(downloads) + 1)}
        downloads.append(request)
        return {"ok": True, "request_id": request["request_id"]}

    window.bridge._handlers["cover.lyrics.search"] = acknowledge_search
    window.bridge._handlers["cover.lyrics.download"] = acknowledge_download

    def emit_search(request, **extra):
        window.bridge.notify("lyrics.online.search", {"request_id": request["request_id"], "cover_id": request["cover_id"], "project": str(project), **extra})

    def emit_download(request, **extra):
        window.bridge.notify("lyrics.online.download", {"request_id": request["request_id"], "cover_id": request["cover_id"], "project": str(project), **extra})

    def confirm_replace():
        js("Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='替换歌词').click()")

    wait("!!(window.__vsBridge && __vsBridge.pagesReady && window.VS_LYRICS?.ready)")
    js("window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>probeErrors.push(String(e.reason)));__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(timed.id) + "))")
    wait("VS_MEDIA.state.tracks.length===1 && document.querySelectorAll('#lyrics .lyric').length===23")
    checks["compare_in_waveform_header"] = js("(()=>{const b=document.querySelector('#versionCompareToggle'),h=document.querySelector('.timeline-heading'),t=document.querySelector('.preview-toolbar');return b.parentElement===t.parentElement && b.previousElementSibling===h && b.nextElementSibling===t && !document.querySelector('.library #versionComparePanel')})()")
    for width, height in ((1440, 900), (1280, 720)):
        window.resize(width, height)
        QTest.qWait(120)
        checks["compare_header_position_" + str(width)] = js("(()=>{const b=document.querySelector('#versionCompareToggle').getBoundingClientRect(),h=document.querySelector('.timeline-heading').getBoundingClientRect(),t=document.querySelector('.preview-toolbar').getBoundingClientRect();return b.x>=h.right-1 && b.right<=t.x+1 && b.y>=h.y-2})()")
    js("document.querySelector('#versionCompareToggle').click()")
    checks["compact_comparison_expand"] = js("!document.querySelector('#versionComparePanel').classList.contains('hidden') && document.querySelector('#versionComparePanel').getBoundingClientRect().height<140")
    js("document.querySelector('#versionCompareToggle').click()")
    window.resize(1440, 900)
    QTest.qWait(120)

    js("VS_LYRICS.updatePosition(0)")
    checks["no_highlight_before_first_line"] = js("document.querySelectorAll('#lyrics .active').length===0")
    js("VS_LYRICS.updatePosition(1500)")
    checks["same_timestamp_translation_highlight"] = js("document.querySelectorAll('#lyrics .active').length===2")
    js("document.querySelectorAll('#lyrics .lyric')[3].click()")
    QTest.qWait(100)
    checks["real_lyric_seek"] = window.bridge.media.position == 2000
    js("VS_LYRICS.updatePosition(9000)")
    checks["active_line_auto_scroll"] = js("document.querySelector('#lyrics').scrollTop>0")
    js("document.querySelector('#lyrics').scrollTop=0;document.querySelector('#lyrics').dispatchEvent(new WheelEvent('wheel',{bubbles:true,deltaY:-20}));VS_LYRICS.updatePosition(9400)")
    checks["manual_scroll_respected"] = js("document.querySelector('#lyrics').scrollTop===0")
    js("document.querySelector('#lyricsOffsetInput').value=1.2;document.querySelector('#applyLyricsOffset').click()")
    wait("__vsBridge.song.lyrics_offset_ms===1200")
    checks["offset_persisted_once"] = CoverProject.load(project, timed.id).lyrics_offset_ms == 1200 and js("Math.abs(Number(document.querySelector('#lyrics .lyric').dataset.time)-1.7)<.001")
    js("__vsBridge.playFile(__vsBridge.song.source_path,'歌词试听');")
    wait("__vsBridge.audio && __vsBridge.audio.duration>0")
    js("__vsBridge.audio.pause();document.querySelectorAll('#lyrics .lyric')[1].click()")
    QTest.qWait(100)
    checks["html_audio_lyric_seek_and_timeupdate"] = js("Math.abs(__vsBridge.audio.currentTime-2.7)<.01 && document.querySelectorAll('#lyrics .active').length===2")
    js("__vsBridge.audio.currentTime=.1;__vsBridge.audio.dispatchEvent(new Event('timeupdate'))")
    checks["html_audio_before_first_line"] = js("document.querySelectorAll('#lyrics .active').length===0")

    js("document.querySelector('#findOnlineLyrics').click()")
    checks["search_prefill_and_disclosure"] = js("document.querySelector('#lyricsTrackName').value==='测试歌曲' && document.querySelector('#onlineLyricsMask').textContent.includes('不上传音频')")
    js("document.querySelector('#lyricsArtistName').value='原唱';document.querySelector('#lyricsSearchButton').click()")
    QTest.qWait(100)
    checks["search_is_async_and_busy"] = len(searches) == 1 and js("document.querySelector('#lyricsSearchButton').disabled && document.querySelector('#onlineLyricsMask').getAttribute('aria-busy')==='true'")
    emit_search(searches[-1], error="测试超时")
    wait("!document.querySelector('#lyricsSearchButton').disabled")
    checks["search_error_retry_available"] = js("document.querySelector('#onlineLyricsStatus').textContent.includes('失败')")
    checks["offline_error_preserves_existing_lyrics"] = js("document.querySelectorAll('#lyrics .lyric[data-sync=true]').length===23 && document.querySelector('#lyrics').textContent.includes('第一句')")
    js("document.querySelector('#lyricsSearchButton').click()")
    QTest.qWait(100)
    emit_search({**searches[-1], "request_id": "stale-result"}, results=records)
    QTest.qWait(50)
    checks["wrong_request_ignored"] = js("document.querySelector('#lyricsSearchButton').disabled && document.querySelectorAll('[data-lyrics-record]').length===0")
    emit_search(searches[-1], results=records)
    wait("document.querySelectorAll('[data-lyrics-record]').length===3")
    checks["candidate_types_and_duration"] = js("document.querySelector('#onlineLyricsResults').textContent.includes('同步 LRC') && document.querySelector('#onlineLyricsResults').textContent.includes('纯文本') && document.querySelector('#onlineLyricsResults').textContent.includes('长 7.0 秒') && document.querySelector('[data-lyrics-record=\"103\"]').disabled")
    js("document.querySelector('[data-lyrics-record=\"101\"]').click()")
    wait("Array.from(document.querySelectorAll('.modal-mask.show h3')).some(n=>n.textContent==='替换当前歌词')")
    checks["existing_lyrics_require_confirmation"] = not downloads
    js("Array.from(document.querySelectorAll('.modal-mask.show button')).find(b=>b.textContent==='取消').click()")
    checks["cancel_does_not_download"] = not downloads
    js("document.querySelector('[data-lyrics-record=\"101\"]').click()")
    confirm_replace()
    QTest.qWait(100)
    checks["download_uses_explicit_overwrite"] = len(downloads) == 1 and downloads[-1]["overwrite"] is True and js("document.querySelector('#lyricsSearchButton').disabled")
    emit_download(downloads[-1], ok=False, error="测试下载失败")
    wait("!document.querySelector('#lyricsSearchButton').disabled")
    checks["download_failure_retry"] = js("document.querySelector('#onlineLyricsStatus').textContent.includes('下载失败') && !document.querySelector('[data-lyrics-record=\"101\"]').disabled && document.querySelector('[data-lyrics-record=\"103\"]').disabled")
    js("document.querySelector('[data-lyrics-record=\"101\"]').click()")
    confirm_replace()
    QTest.qWait(100)
    cover = CoverProject.load(project, timed.id)
    target = cover.root / "lyrics" / "downloaded.lrc"
    target.write_text("[00:02.50]下载后的第一句\n[00:05.00]下载后的第二句\n", encoding="utf-8")
    cover.set_lyrics(target)
    cover.lyrics_source = {"kind": "online", "provider": "LRCLIB", "record_id": 101, "synced": True}
    cover.save()
    emit_download(downloads[-1], ok=True, synced=True, line_count=2, source="LRCLIB", title="测试歌曲", artist="原唱甲")
    wait("document.querySelector('#lyrics').textContent.includes('下载后的第一句')")
    checks["download_refreshes_real_saved_lyrics"] = js("document.querySelectorAll('#lyrics .lyric[data-sync=true]').length===2 && document.querySelector('#lyricsSourceLabel').textContent.includes('LRCLIB') && !document.querySelector('#applyLyricsOffset').disabled")
    def early_search(payload):
        reply = acknowledge_search(payload)
        emit_search(searches[-1], results=records)
        emit_search({**searches[-1], "request_id": "unrelated-early"}, results=[])
        return reply

    def early_download(payload):
        reply = acknowledge_download(payload)
        emit_download(downloads[-1], ok=False, error="提前返回的失败")
        return reply

    window.bridge._handlers["cover.lyrics.search"] = early_search
    window.bridge._handlers["cover.lyrics.download"] = early_download
    js("document.querySelector('#lyricsSearchButton').click()")
    wait("!document.querySelector('#lyricsSearchButton').disabled && document.querySelectorAll('[data-lyrics-record]').length===3")
    checks["search_result_before_ack_settles_matching_id"] = js("document.querySelector('#onlineLyricsStatus').textContent.includes('找到 3 个')")
    js("document.querySelector('[data-lyrics-record=\"101\"]').click()")
    confirm_replace()
    wait("document.querySelector('#onlineLyricsStatus').textContent.includes('提前返回的失败')")
    checks["download_result_before_ack_settles"] = js("!document.querySelector('#lyricsSearchButton').disabled")
    window.bridge._handlers["cover.lyrics.search"] = acknowledge_search
    window.bridge._handlers["cover.lyrics.download"] = acknowledge_download
    js("document.querySelector('#closeOnlineLyrics').click();__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(plain.id) + "))")
    wait("document.querySelector('#lyrics').textContent.includes('没有时间轴的歌词')")
    checks["plain_text_never_fakes_timestamps"] = js("Array.from(document.querySelectorAll('#lyrics .lyric')).every(n=>n.dataset.sync==='false' && !n.hasAttribute('data-time') && n.getAttribute('aria-disabled')==='true' && !n.onclick) && document.querySelector('#lyricsSourceLabel').textContent.includes('纯文本') && document.querySelector('#applyLyricsOffset').disabled")
    old_position = window.bridge.media.position
    js("document.querySelector('#lyrics .lyric').click();VS_LYRICS.updatePosition(9000)")
    QTest.qWait(80)
    checks["plain_text_no_seek_or_highlight"] = window.bridge.media.position == old_position and js("document.querySelectorAll('#lyrics .active').length===0")

    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(missing.id) + "));document.querySelector('#findOnlineLyrics').click();document.querySelector('#lyricsSearchButton').click()")
    QTest.qWait(100)
    emit_search(searches[-1], results=records)
    wait("document.querySelectorAll('[data-lyrics-record]').length===3")
    before_download = len(downloads)
    js("document.querySelector('[data-lyrics-record=\"101\"]').click()")
    wait("Array.from(document.querySelectorAll('.modal-mask.show h3')).some(n=>n.textContent==='替换当前歌词')")
    checks["missing_old_lyrics_still_require_confirmation"] = len(downloads) == before_download and js("__vsBridge.song.has_lyrics && __vsBridge.song.lyrics.length===0")
    confirm_replace()
    QTest.qWait(100)
    checks["missing_old_lyrics_can_be_replaced"] = len(downloads) == before_download + 1 and downloads[-1]["overwrite"] is True
    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(plain.id) + "))")

    js("document.querySelector('#findOnlineLyrics').click();document.querySelector('#lyricsSearchButton').click()")
    QTest.qWait(100)
    request = searches[-1]
    js("__vsBridge.selectSong(__vsBridge.songs.find(s=>s.id===" + json.dumps(timed.id) + "))")
    emit_search(request, results=records)
    QTest.qWait(100)
    checks["late_song_result_cannot_reopen_or_overwrite"] = js("!document.querySelector('#onlineLyricsMask') && document.querySelector('#lyrics').textContent.includes('下载后的第一句')")
    screenshot = os.environ.get("VS_LYRICS_UI_SCREENSHOT")
    if screenshot:
        js("document.querySelector('#versionCompareToggle').click();VS_LYRICS.updatePosition(2500)")
        window.resize(1442, 902)
        QTest.qWait(300)
        window.resize(1440, 900)
        window.view.update()
        QTest.qWait(700)
        target_image = Path(screenshot)
        target_image.parent.mkdir(parents=True, exist_ok=True)
        window.view.grab().save(str(target_image))
        js("document.querySelector('#versionCompareToggle').click()")
    js("document.querySelector('#findOnlineLyrics').click();document.querySelector('#lyricsSearchButton').click()")
    QTest.qWait(100)
    request = searches[-1]
    other_project = store.create_project("lyrics-other")
    CoverService(paths, store, other_project, client).import_song(_wav(root / "别的工程.wav", 12))
    js("__vsBridge.invoke('project.activate',{path:" + json.dumps(str(other_project)) + "})")
    wait("__vsBridge.data.project.path===" + json.dumps(str(other_project)))
    emit_search(request, results=records)
    QTest.qWait(100)
    checks["late_project_result_ignored"] = js("!document.querySelector('#onlineLyricsMask') && !document.querySelector('#lyrics').textContent.includes('下载后的第一句')")
    checks["no_javascript_error"] = js("probeErrors.length===0 && !__vsBridge.error")
    if not checks["no_javascript_error"]:
        print("LYRICS_PROBE_ERRORS " + str(js("JSON.stringify(probeErrors)")), flush=True)
    window.close()
    print("VS_ONLINE_LYRICS_UI " + json.dumps(checks, ensure_ascii=False), flush=True)
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
