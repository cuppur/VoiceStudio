"""End-to-end interaction test for the cover page inside QtWebEngine.

The probe runs out of process (QtWebEngine cannot be torn down inside pytest)
with a stand-in worker, so it asserts the real page → bridge → service wiring
without touching the GPU.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).parent / "helpers" / "cover_flow_probe.py"


def _run_probe() -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=environment, check=False,
    )
    line = next((item for item in result.stdout.splitlines() if item.startswith("VS_FLOW ")), "")
    assert line, f"probe produced no result\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-2000:]}"
    return json.loads(line[len("VS_FLOW "):])


def test_cover_page_flow_reaches_the_real_service():
    flow = _run_probe()
    # rights confirmation is mandatory before any separation
    assert flow["rights"] == {"shown": 1, "title": "歌曲权利确认"}
    # the main action requests a target voice when none has been selected
    assert flow["voiceChoice"]["shown"] == 1
    assert flow["voiceChoice"]["title"] == "选择目标声音"
    assert flow["voiceChoice"]["count"] == 1
    # the page sends a trusted command with project-owned paths only
    assert len(flow["sent"]) == 1
    _identifier, command, payload = flow["sent"][0]
    assert command == "separate_song"
    assert payload["mode"] == "uvr5"
    assert payload["cover_id"]
    assert payload["source_relative_path"].startswith("source/")
    assert len(payload["source_sha256"]) == 64
    # progress popup appears with the real task label
    assert flow["taskPopup"] == {"shown": True, "title": "分离人声与伴奏"}
    # prototype controls without backend support are hidden
    hidden = flow["hidden"]
    assert hidden["presets"] == 0 and hidden["advanced"] == 0 and hidden["takes"] == 0
    assert hidden["ab"] == 1 and hidden["preview"] == 0  # comparison lives in sidebar; obsolete hero button is hidden
    assert hidden["strength"] == 2 and hidden["toggles"] == 3
    # the primary button reflects the real next step
    assert flow["renderLabel"].endswith("一键翻唱")
    one_click = flow["oneClick"]
    assert one_click["command"] == "separate_song"
    assert one_click["cover_id"] and one_click["profile_id"]
    assert one_click["parent_job_id"]
