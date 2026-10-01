"""The workstation controls must operate through the production channel."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_workstation_controls_in_real_qtwebengine():
    probe = Path(__file__).parent / "helpers" / "workstation_ui_probe.py"
    result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=100,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    line = next((item for item in result.stdout.splitlines() if item.startswith("VS_WORKSTATION_PROBE ")), "")
    assert result.returncode == 0 and line, result.stdout + "\n" + result.stderr[-6000:]
    checks = json.loads(line.removeprefix("VS_WORKSTATION_PROBE "))
    assert len(checks) >= 20 and all(checks.values()), checks
