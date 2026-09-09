"""End-to-end interaction test for the training page inside QtWebEngine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).parent / "helpers" / "training_flow_probe.py"


def _run_probe() -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, str(PROBE)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=environment, check=False,
    )
    line = next((item for item in result.stdout.splitlines() if item.startswith("VS_TRAIN ")), "")
    assert line, f"probe produced no result\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-2000:]}"
    return json.loads(line[len("VS_TRAIN "):])


def test_training_page_lists_real_material_and_starts_the_workflow():
    flow = _run_probe()
    page = flow["page"]
    assert page["rows"] == 1
    assert page["firstRow"] == "material.wav"
    assert page["button"] == "一键训练"
    assert page["hiddenQuality"] == 0
    assert page["steps"] == 5
    after = flow["afterClick"]
    assert after["toast"] == "已开始自动处理素材"
    assert after["sent"], "no worker command was sent"
    command, profile_id = after["sent"][0]
    assert command == "prepare_dataset"
    assert profile_id
