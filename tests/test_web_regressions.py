import json
import os
from pathlib import Path
import subprocess
import sys


def test_repaired_controls_through_real_webchannel():
    probe = Path(__file__).parent / 'helpers' / 'web_regressions_probe.py'
    result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=90,
                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    line = next((s for s in result.stdout.splitlines() if s.startswith('VS_REGRESSIONS ')), '')
    assert result.returncode == 0 and line, result.stdout + '\n' + result.stderr
    checks = json.loads(line.removeprefix('VS_REGRESSIONS '))
    assert len(checks) >= 15 and all(checks.values()), checks
