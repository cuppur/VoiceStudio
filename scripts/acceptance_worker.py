from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["prepare"]); parser.add_argument("--seconds", type=float, default=90); args = parser.parse_args()
    paths = AppPaths.default(); store = StudioStore(paths); project = Path(store.list_projects()[0]["path"]); profile = store.list_profiles(project)[0]; assets = [item for item in store.list_source_assets(project, profile.id) if item.enabled and not item.duplicate_of]
    selected = []; total = 0.0
    for asset in assets:
        selected.append(asset); total += asset.duration_seconds
        if total >= args.seconds: break
    if total < 60: raise RuntimeError("迁移素材不足 60 秒")
    payload = {"action": "pipeline", "profile_id": profile.id, "source_asset_ids": [item.id for item in selected], "source_assets": [item.to_dict() for item in assets], "project_path": str(project), "processing_options": {"language": "zh", "separate_vocals": False, "denoise": False}}
    env = os.environ.copy(); source = str(Path(__file__).resolve().parents[1] / "src"); env.update(PYTHONPATH=source, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    stderr_path = paths.logs_root / "acceptance-prepare.stderr.log"
    with stderr_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen([str(paths.private_python), "-X", "utf8", "-u", "-m", "local_voice_studio.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env=env)
        assert process.stdin and process.stdout; print(process.stdout.readline().rstrip(), flush=True)
        process.stdin.write(json.dumps({"id": "acceptance-prepare", "type": "prepare_dataset", "payload": payload}, ensure_ascii=False) + "\n"); process.stdin.flush()
        code = 1
        for line in process.stdout:
            print(line.rstrip(), flush=True); event = json.loads(line)
            if event.get("id") == "acceptance-prepare" and event.get("type") in {"result", "error"}:
                code = 0 if event["type"] == "result" else 1; break
        process.stdin.write('{"id":"stop","type":"shutdown","payload":{}}\n'); process.stdin.flush(); process.wait(timeout=30)
    return code


if __name__ == "__main__": raise SystemExit(main())
