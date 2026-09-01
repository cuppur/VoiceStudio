from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import os
from pathlib import Path

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.singing.pipeline import SingingPipeline
from local_voice_studio.singing.rvc import RVCConfig, RVCEngine


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    os.environ["LOCAL_VOICE_STUDIO_RVC_PYTHONPATH"] = str(Path(os.environ["LOCALAPPDATA"]) / "LocalVoiceStudio" / "runtime" / "env" / "Lib" / "site-packages")
    root = Path(tempfile.mkdtemp(prefix="voicestudio-phase3-project-"))
    project = root / "project"; project.mkdir()
    source = Path(r"C:\Temp\vs-rvc-dataset\0247f8ea475a_vocal_603719dbfca044f2814a4840b77fcf0a.wav_10.wav_0000554880_0000703680.wav")
    model_source = Path(r"C:\Users\cruelworld\AppData\Local\LocalVoiceStudio\engines\RVC\assets\weights\phase3-mini.pth")
    cover = CoverProject.create(project, title="Phase 3 real smoke")
    cover.copy_source(source)
    vocal = cover.root / "stems" / "vocals.wav"; vocal.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, vocal); cover.set_stem("vocal", vocal); cover.attest_rights(True)
    model = project / "models" / "singing" / "profile" / "run" / "model.pth"; model.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(model_source, model)
    manifest = {"voice_profiles": [{"id": "profile", "name": "Authorized", "consent_confirmed": True, "consent_confirmed_at": "now", "consent_record": "local authorized test", "singing_models": [SingingModelVersion(profile_id="profile", engine="rvc", engine_version="2.3", checkpoint_relative_path="models/singing/profile/run/model.pth", checkpoint_sha256=sha(model), trust_status="verified").to_dict()], "active_singing_model_id": ""}]}
    manifest["voice_profiles"][0]["active_singing_model_id"] = manifest["voice_profiles"][0]["singing_models"][0]["id"]
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    paths = AppPaths.default(); engine = RVCEngine(RVCConfig(paths.data_root / "engines" / "RVC", paths.runtime_root / "rvc-env" / "Scripts" / "python.exe", paths.runtime_root / "rvc-env", commit=(paths.data_root / "engines" / "RVC" / ".pinned-commit").read_text().strip(), hubert_sha256="cc8c20f4b90a520757260197a3ff2505705a7adbd20ad9eeaa4e1a9b38442ef5", rmvpe_sha256="6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193", pretrained_sha256=("b5d51f589cc3632d4eae36a315b4179397695042edc01d15312e1bddc2b764a4", "2269b73c7a4cf34da09aea99274dabf99b2ddb8a42cbfb065fb3c0aa9a2fc748")))
    pipeline = SingingPipeline(engine)
    payload = {"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "pitch_shift": 0}
    started = time.perf_counter(); first = pipeline.convert(payload); first_ms = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter(); second = pipeline.convert(payload); second_ms = round((time.perf_counter() - started) * 1000)
    restored = CoverProject.load(project, cover.id)
    result = {"project": str(project), "cover_id": cover.id, "first": first, "first_ms": first_ms, "second": second, "second_ms": second_ms, "cache_hit": second.get("cache_hit"), "persisted_ai_asset": bool(restored.get_asset(role="ai_vocal"))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["cache_hit"] and result["persisted_ai_asset"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
