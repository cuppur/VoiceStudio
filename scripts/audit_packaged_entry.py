"""Probe the launcher's packaged target in isolated directories via local CDP.

Uses Qt's bundled WebSocket client; no additional dependency or real media.
Only the subprocess started here (and its children) is stopped on completion.
"""
from __future__ import annotations

import base64
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


def main():
    app = QCoreApplication([])
    output = Path(sys.argv[1])
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
        verify = '--verify-fixed' in sys.argv
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
        executable = ROOT / "dist" / "LocalVoiceStudio" / "LocalVoiceStudio.exe"
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
            until(lambda: evaluate("!!(window.__vsBridge && __vsBridge.data && window.VS_PAGES)"))
            result = {"executable": "dist/LocalVoiceStudio/LocalVoiceStudio.exe", "page_url": tabs[0]["url"]}
            result["before_import"] = evaluate("({bridgeConnected:!!__vsBridge.bridge,bridgeError:__vsBridge.error||'',songs:__vsBridge.songs.length})")
            result["import_reply"] = evaluate("new Promise(resolve=>__vsBridge.invoke('song.import'," + json.dumps({"path": str(source)}) + ",r=>resolve({ok:r.ok,message:r.message})))")
            result["after_import"] = evaluate("({songs:__vsBridge.songs.length,selectedId:__vsBridge.song&&__vsBridge.song.id,operationId:VS_PAGES.cover.songId})")
            if verify:
                until(lambda: evaluate('VS_PAGES.cover.songId === __vsBridge.song.id && VS_MEDIA.state.tracks.length === 1'))
                result['waveform'] = evaluate('({duration:VS_MEDIA.state.duration,rate:VS_MEDIA.state.tracks[0].metadata.sample_rate,silent:VS_MEDIA.state.tracks[0].peaks.every(p=>p[0]===0&&p[1]===0)})')
                assert result['after_import']['selectedId'] == result['after_import']['operationId'], result
                assert result['waveform'] == {'duration': 1000, 'rate': 8000, 'silent': True}, result
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
            screenshot = command("Page.captureScreenshot", {"format": "png"})
            (output / "packaged-import.png").write_bytes(base64.b64decode(screenshot["data"]))
            (output / "packaged-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            ws.close()
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
