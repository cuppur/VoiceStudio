"""Exercise real UVR5 -> RVC -> mix on isolated synthetic-song project data."""
from __future__ import annotations

import json
import array
import math
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from local_voice_studio.audio import sha256_file
from local_voice_studio.cover.mixing import CoverMixer, CoverMixSettings, FFmpegMixBackend
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.cover.separation import SongSeparationPipeline
from local_voice_studio.paths import AppPaths
from local_voice_studio.runtime import EngineRuntimeResolver
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.singing.pipeline import SingingPipeline
from local_voice_studio.worker import WorkerService
from local_voice_studio.infrastructure.process_options import hidden_process_options


def _candidate(paths: AppPaths):
    model_candidate = None
    reference_candidate = None
    for manifest_path in paths.projects_root.glob("*/project.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for profile in manifest.get("voice_profiles", []) or []:
            if profile.get("consent_confirmed") and reference_candidate is None:
                references = [item for item in profile.get("reference_assets", []) or []
                              if item.get("approved") and item.get("transcript") and Path(str(item.get("path", ""))).is_file()]
                if references:
                    reference_candidate = Path(references[0]["path"])
            if not profile.get("consent_confirmed") or not profile.get("active_singing_model_id"):
                continue
            model_data = next((item for item in profile.get("singing_models", [])
                               if str(item.get("id")) == str(profile["active_singing_model_id"])), None)
            if not model_data:
                continue
            model = SingingModelVersion.from_dict(model_data)
            if model.trust_status != "verified" or not model.files_available(manifest_path.parent) or not model.hashes_match(manifest_path.parent):
                continue
            model_candidate = model_candidate or (manifest_path.parent, profile, model)
    if model_candidate and reference_candidate:
        return *model_candidate, reference_candidate
    raise RuntimeError("本地缺少已验证歌唱模型，或没有可用的已授权参考素材")


def _link_into_project(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.hardlink_to(source)
    except OSError:
        shutil.copy2(source, destination)


def main() -> None:
    paths = AppPaths.default()
    source_project, source_profile, source_model, reference_audio = _candidate(paths)
    ffmpeg = EngineRuntimeResolver(paths).resolve_private_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("本地受信任 FFmpeg 不可用")

    worker = WorkerService(paths)
    if not worker.singing_engine.readiness().ready:
        raise RuntimeError("本地 RVC 运行时未就绪")

    with tempfile.TemporaryDirectory(prefix="voicestudio-full-cover-probe-") as root_text:
        root = Path(root_text)
        projects = root / "projects"
        test_paths = AppPaths(paths.data_root, projects, paths.runtime_root, paths.engine_root,
                              paths.models_root, paths.logs_root, paths.database, paths.cache_directory)
        project = projects / "integration-check"
        project.mkdir(parents=True)
        profile = json.loads(json.dumps(source_profile))
        model = SingingModelVersion.from_dict(next(item for item in profile["singing_models"]
                                                    if str(item.get("id")) == profile["active_singing_model_id"]))
        for relative in (model.checkpoint_relative_path, model.index_relative_path):
            if relative:
                _link_into_project(source_project / relative, project / relative)
        (project / "project.json").write_text(json.dumps({"voice_profiles": [profile]}, ensure_ascii=False), encoding="utf-8")

        voice = root / "voice.wav"
        subprocess.run([str(ffmpeg), "-v", "error", "-i", str(reference_audio), "-t", "5",
                        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", "-y", str(voice)],
                       check=True, capture_output=True, timeout=60, **hidden_process_options())
        song = root / "synthetic-song.wav"
        with wave.open(str(voice), "rb") as input_audio:
            if input_audio.getsampwidth() != 2 or input_audio.getframerate() != 44100:
                raise RuntimeError("隔离测试语音未规范为 44.1 kHz PCM16")
            samples = array.array("h")
            samples.frombytes(input_audio.readframes(input_audio.getnframes()))
        if len(samples) < 44100 * 3:
            raise RuntimeError("可用于转换的授权参考语音不足 3 秒")
        with wave.open(str(song), "wb") as output_audio:
            output_audio.setnchannels(2); output_audio.setsampwidth(2); output_audio.setframerate(44100)
            frames = bytearray(len(samples) * 4)
            for index, sample in enumerate(samples):
                seconds = index / 44100
                backing = 32767 * (0.18 * math.sin(2 * math.pi * 220 * seconds) + 0.09 * math.sin(2 * math.pi * 440 * seconds))
                combined = max(-32768, min(32767, round(sample * 0.8 + backing)))
                struct.pack_into("<hh", frames, index * 4, combined, combined)
            output_audio.writeframes(frames)

        cover = CoverProject.create(project, title="本地完整翻唱链路隔离测试")
        cover.copy_source(song)
        cover.attest_rights(True)
        separation_started = time.monotonic()
        separated = SongSeparationPipeline(project, paths=test_paths).separate(
            cover.id, cover.source_relative_path, cover.source_sha256, engine_id="uvr5")
        separation_seconds = round(time.monotonic() - separation_started, 2)

        conversion_started = time.monotonic()
        converted = SingingPipeline(worker.singing_engine, projects_root=projects, paths=test_paths).convert({
            "project_path": str(project), "profile_id": str(profile["id"]), "cover_id": cover.id,
            "singing_model_id": model.id, "index_rate": 0.75, "protect": 0.33,
        })
        conversion_seconds = round(time.monotonic() - conversion_started, 2)
        mix_started = time.monotonic()
        mixed = CoverMixer(test_paths, backend=FFmpegMixBackend(ffmpeg)).mix(
            project, cover.id, CoverMixSettings(), profile_id=str(profile["id"]), model_id=model.id)
        mix_seconds = round(time.monotonic() - mix_started, 2)

        final = CoverProject.load(project, cover.id).get_asset(role="final_mix")
        assert final and (cover.root / final.relative_path).is_file()
        with wave.open(mixed["output_path"], "rb") as rendered:
            assert rendered.getnframes() > 0 and rendered.getframerate() == 48000 and rendered.getnchannels() == 2
            duration = rendered.getnframes() / rendered.getframerate()
        assert sha256_file(Path(mixed["output_path"])) == final.sha256
        print(json.dumps({"ok": True, "separator": separated["separator"],
                          "separation_seconds": separation_seconds, "conversion_seconds": conversion_seconds,
                          "mix_seconds": mix_seconds, "duration_seconds": round(duration, 2),
                          "voice_output_bytes": Path(converted["output_path"]).stat().st_size,
                          "final_mix_bytes": Path(mixed["output_path"]).stat().st_size,
                          "final_mix_sha256_valid": True, "cache_hit": mixed["cache_hit"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
