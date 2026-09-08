"""End-to-end interaction test for the TTS and voice pages inside QtWebEngine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).parent / "helpers" / "tts_flow_probe.py"


def _run_probe() -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=environment, check=False,
    )
    line = next((item for item in result.stdout.splitlines() if item.startswith("VS_TTS ")), "")
    assert line, f"probe produced no result\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-2000:]}"
    return json.loads(line[len("VS_TTS "):])


def test_tts_page_generates_through_the_real_service():
    flow = _run_probe()
    hidden = flow["ttsHidden"]
    # prototype-only controls without engine support are hidden
    assert hidden["emotions"] == 1
    assert hidden["pitchRows"] == 1
    assert hidden["outputCard"] == 1
    # the voice list is rendered with a real profile and a stable id
    assert hidden["rows"] == 1
    assert hidden["activeVoice"]
    # clicking generate sends the trusted load_profile command first
    assert flow["ttsSent"], "no worker command was sent"
    _identifier, command, payload = flow["ttsSent"][0]
    assert command == "load_profile"
    assert payload["id"] == hidden["activeVoice"]
    assert payload["project_path"]
    assert flow["toast"] == "已开始生成语音"
    assert flow["taskPopup"]["shown"] is True
    assert flow["taskPopup"]["title"] == "生成语音"
    assert flow["charCount"].startswith("13 字")


def test_voice_page_shows_real_detail_and_versions():
    flow = _run_probe()
    voices = flow["voices"]
    assert voices["cards"] == 1
    assert voices["detail"] == "测试声音"
    assert voices["versions"] >= 1
    assert voices["actions"] == 4
