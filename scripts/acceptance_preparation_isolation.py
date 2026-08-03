from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore


def send(process: subprocess.Popen[str], request_id: str, command: str, payload: dict) -> None:
    assert process.stdin
    process.stdin.write(json.dumps({"id": request_id, "type": command, "payload": payload}, ensure_ascii=False) + "\n")
    process.stdin.flush()


def wait_for(process: subprocess.Popen[str], request_id: str) -> dict:
    assert process.stdout
    for line in process.stdout:
        print(line.rstrip(), flush=True)
        event = json.loads(line)
        if event.get("id") == request_id and event.get("type") in {"result", "error"}:
            if event["type"] == "error": raise RuntimeError(event["payload"].get("message", "worker failed"))
            return event["payload"]
    raise RuntimeError("worker exited before returning a result")


def main() -> int:
    paths = AppPaths.default(); store = StudioStore(paths); project = Path(store.list_projects()[0]["path"])
    profile = next(item for item in store.list_profiles(project) if len(store.list_source_assets(project, item.id)) >= 3)
    assets = [item for item in store.list_source_assets(project, profile.id) if item.enabled and not item.duplicate_of][:3]
    if len(assets) < 3: raise RuntimeError("实机项目中没有三个可用素材")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    stderr_path = paths.logs_root / "acceptance-preparation.stderr.log"
    with stderr_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen([str(paths.private_python), "-X", "utf8", "-u", "-m", "local_voice_studio.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env=env)
        assert process.stdout; print(process.stdout.readline().rstrip(), flush=True)
        manifests = []
        for selected in (assets, assets[:1]):
            preparation_id = uuid4().hex; request_id = uuid4().hex
            payload = {"action": "pipeline", "preparation_id": preparation_id, "profile_id": profile.id, "source_asset_ids": [item.id for item in selected], "source_assets": [item.to_dict() for item in assets], "project_path": str(project), "processing_options": {"language": "zh", "separate_vocals": False, "denoise": False}}
            send(process, request_id, "prepare_dataset", payload); result = wait_for(process, request_id); manifest = Path(result["outputs"][0]); data = json.loads(manifest.read_text(encoding="utf-8")); manifests.append((manifest, data))
            normalized_ids = {item.stem for item in Path(data["normalized_dir"]).glob("*.wav")}
            if normalized_ids != {item.id for item in selected}: raise RuntimeError(f"准备隔离失败：{normalized_ids}")
        if manifests[0][0].parent == manifests[1][0].parent: raise RuntimeError("两次准备复用了同一运行目录")
        print(json.dumps({"run_abc": str(manifests[0][0]), "run_a": str(manifests[1][0]), "run_a_source_asset_ids": manifests[1][1]["source_asset_ids"]}, ensure_ascii=False), flush=True)
        send(process, "stop", "shutdown", {}); wait_for(process, "stop"); process.wait(timeout=30)
    return 0


if __name__ == "__main__": raise SystemExit(main())
