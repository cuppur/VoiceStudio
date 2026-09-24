"""Launch the shipped one-click EXE and verify its packaged page via CDP."""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtWebSockets import QWebSocket


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    launcher = ROOT / "VoiceStudio-一键启动.exe"
    packaged_app = ROOT / "dist" / "LocalVoiceStudio" / "LocalVoiceStudio.exe"
    if not launcher.is_file() or not packaged_app.is_file():
        raise RuntimeError("一键启动器或打包主程序不存在")
    if subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                       "if (Get-Process -Name LocalVoiceStudio -ErrorAction SilentlyContinue) { exit 9 }"],
                      capture_output=True).returncode:
        raise RuntimeError("已有 VoiceStudio 进程，不能安全地隔离本次启动测试")

    app = QCoreApplication([])
    output_root = ROOT / "build" / "round3-ui-audit"
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voicestudio-launcher-audit-") as temp_text:
        temp = Path(temp_text)
        projects = temp / "projects"; projects.mkdir()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0)); port = port_socket.getsockname()[1]
        environment = dict(os.environ)
        environment.update({
            "LOCAL_VOICE_STUDIO_HOME": str(temp / "data"),
            "LOCAL_VOICE_STUDIO_PROJECTS": str(projects),
            "LOCAL_VOICE_STUDIO_MODELS": str(temp / "models"),
            "LOCAL_VOICE_STUDIO_CACHE": str(temp / "cache"),
            "QTWEBENGINE_REMOTE_DEBUGGING": f"127.0.0.1:{port}",
            "VOICE_STUDIO_AUDIT_TARGET": str(packaged_app),
        })
        launcher_process = subprocess.Popen([str(launcher)], cwd=ROOT, env=environment,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
        ws = QWebSocket(); replies = {}
        ws.textMessageReceived.connect(lambda raw: replies.update({json.loads(raw).get("id"): json.loads(raw)}))

        def until(predicate, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                if predicate(): return
                time.sleep(0.02)
            raise TimeoutError("一键启动程序未在时限内启动并连接到界面")

        def command(method, params=None):
            key = len(replies) + 1
            ws.sendTextMessage(json.dumps({"id": key, "method": method, "params": params or {}}))
            until(lambda: key in replies)
            reply = replies.pop(key)
            if "error" in reply: raise RuntimeError(reply["error"])
            return reply["result"]

        try:
            launcher_code = launcher_process.wait(timeout=10)
            if launcher_code != 0: raise RuntimeError(f"一键启动器返回 {launcher_code}")
            targets = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not targets:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        targets = json.load(response)
                except OSError:
                    app.processEvents(); time.sleep(0.1)
            if not targets: raise RuntimeError("一键启动器未能启动带调试接口的打包页面")
            ws.open(QUrl(targets[0]["webSocketDebuggerUrl"]))
            until(lambda: ws.isValid())
            until(lambda: command("Runtime.evaluate", {"expression": "!!window.__vsBridge", "returnByValue": True})["result"].get("value"))
            state = command("Runtime.evaluate", {"expression": "({bridge:!!__vsBridge.bridge, page:location.href, pageReady:!!window.VS_PAGES})", "returnByValue": True})["result"]["value"]
            screenshot = command("Page.captureScreenshot", {"format": "png"})
            screenshot_path = output_root / "quick-launcher.png"
            screenshot_path.write_bytes(base64.b64decode(screenshot["data"]))
            evidence = {"launcher_exit_code": launcher_code, "bridge": state["bridge"],
                        "page_ready": state["pageReady"], "page_url": state["page"],
                        "screenshot": str(screenshot_path)}
            (output_root / "quick-launcher-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not evidence["bridge"] or not evidence["page_ready"]: raise RuntimeError("打包页面未完成桥接")
            print(json.dumps(evidence, ensure_ascii=False), flush=True)
        finally:
            ws.close()
            if launcher_process.poll() is None:
                launcher_process.terminate()
            target_pids = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "$p = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $env:VOICE_STUDIO_AUDIT_TARGET }; $p.ProcessId -join ','"],
                env=environment, capture_output=True, text=True, check=False,
            ).stdout.strip()
            for pid in filter(str.isdigit, target_pids.split(",")):
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, check=False)


if __name__ == "__main__":
    main()
