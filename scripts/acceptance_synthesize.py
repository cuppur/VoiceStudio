from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.pages import _parse_asr_result


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def send(process: subprocess.Popen[str], request: dict) -> None:
    assert process.stdin
    process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    process.stdin.flush()


def wait_for(process: subprocess.Popen[str], request_id: str) -> dict:
    assert process.stdout
    for line in process.stdout:
        print(line.rstrip(), flush=True)
        event = json.loads(line)
        if event.get("id") == request_id and event.get("type") in {"result", "error"}:
            if event["type"] == "error":
                raise RuntimeError(event["payload"].get("message", "worker failed"))
            return event["payload"]
    raise RuntimeError("worker exited before returning a result")


def main() -> int:
    paths = AppPaths.default()
    store = StudioStore(paths)
    project = Path(store.list_projects()[0]["path"])
    profile = store.list_profiles(project)[0]
    preparation = project / "datasets" / "working" / profile.id / "preparation.json"
    data = json.loads(preparation.read_text(encoding="utf-8"))
    lines = Path(data["asr_list"]).read_text(encoding="utf-8").splitlines()
    candidates = []
    for line in lines:
        audio, _speaker, language, raw_text = line.split("|", 3)
        language, text, flags = _parse_asr_result(raw_text, language)
        audio_path = Path(audio)
        seconds = duration(audio_path)
        if 5 <= seconds <= 10 and text.strip() and not flags:
            candidates.append((audio_path, language, text.strip(), seconds))
    if not candidates:
        raise RuntimeError("ASR did not produce a usable 5-10 second reference")
    reference, language, prompt, seconds = candidates[0]
    print(json.dumps({"reference": str(reference), "seconds": seconds, "asr_prompt": prompt}, ensure_ascii=False), flush=True)

    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    stderr_path = paths.logs_root / "acceptance-synthesize.stderr.log"
    with stderr_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(
            [str(paths.private_python), "-X", "utf8", "-u", "-m", "local_voice_studio.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert process.stdout
        print(process.stdout.readline().rstrip(), flush=True)
        send(process, {"id": "health", "type": "health", "payload": {}})
        wait_for(process, "health")
        send(process, {"id": "load", "type": "load_profile", "payload": profile.to_dict()})
        wait_for(process, "load")
        common = {
            "text": "你好，这是一段本地声音工坊的真实试听。Hello from Local Voice Studio.",
            "text_lang": "zh",
            "ref_audio_path": str(reference),
            "prompt_text": prompt,
            "prompt_lang": "zh" if language.lower() in {"zh", "zh-cn"} else language.lower(),
            "seed": 20260803,
        }
        send(process, {"id": "preview-real", "type": "synthesize", "payload": {**common, "preview": True}})
        preview = wait_for(process, "preview-real")
        export_dir = project / "exports" / "acceptance"
        send(process, {"id": "export-real", "type": "synthesize", "payload": {**common, "preview": False, "output_dir": str(export_dir)}})
        formal = wait_for(process, "export-real")
        print(json.dumps({"preview": preview["outputs"], "formal": formal["outputs"]}, ensure_ascii=False), flush=True)
        send(process, {"id": "stop", "type": "shutdown", "payload": {}})
        wait_for(process, "stop")
        process.wait(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
