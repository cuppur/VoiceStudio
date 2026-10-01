"""End-to-end interaction test for the training page inside QtWebEngine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.fixture(scope="module")
def flow() -> dict:
    return _run_probe()


def test_training_page_lists_real_material_and_starts_the_workflow(flow):
    page = flow["page"]
    assert page["rows"] == 2
    assert page["firstRow"] == "material.wav"
    assert page["button"] == "一键训练"
    assert page["hiddenQuality"] == 0
    assert page["steps"] == 6
    after = flow["afterClick"]
    assert after["toast"] == "已开始自动处理素材"
    assert after["sent"], "no worker command was sent"
    command, profile_id = after["sent"][0]
    assert command == "prepare_dataset"
    assert profile_id


def test_training_page_removes_material_from_the_selected_rows(flow):
    tools = flow["page"]["sampleTools"]
    assert tools["all"] is True
    assert tools["remove"] == "移除所选"
    assert tools["clear"] == "清空全部"
    assert tools["checks"] == 2
    assert tools["deletes"] == 2
    assert tools["times"] == 2
    assert flow["afterPick"]["picked"] == "已选 1 项"
    assert flow["afterRemove"]["rows"] == 1
    assert flow["afterRemove"]["picked"] == "已选 0 项"


def test_training_page_collapses_completed_voices_in_the_left_list(flow):
    voices = flow["page"]["voiceList"]
    assert voices["newButton"].strip() == "＋ 训练新声音"
    assert voices["activeCount"] == "1"
    assert voices["doneCount"] == "1"
    assert voices["activeRows"] == 1
    assert voices["activeFirst"] == "训练声音"
    assert voices["doneFirst"] == "已训练声音"
    assert voices["doneCollapsed"] is True
    assert voices["doneHidden"] is True
    expanded = flow["afterExpand"]
    assert expanded["doneCollapsed"] is False
    assert expanded["doneHidden"] is False
    assert expanded["doneRows"] == 1
    assert expanded["doneFirst"] == "已训练声音"


def test_training_page_new_voice_button_opens_the_name_dialog(flow):
    after = flow["afterNew"]
    assert after["mask"] == 1
    assert "训练新声音" in after["title"]
    assert after["suggested"]
