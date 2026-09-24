"""Run the real local training-material pipeline on one isolated short clip."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

from local_voice_studio.audio import sha256_file
from local_voice_studio.paths import AppPaths
from local_voice_studio.training import TrainingPipeline


def find_approved_reference(paths: AppPaths):
    for manifest_path in paths.projects_root.glob("*/project.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for profile in manifest.get("voice_profiles", []) or []:
            if not profile.get("consent_confirmed"):
                continue
            for reference in profile.get("reference_assets", []) or []:
                path = Path(str(reference.get("path", "")))
                if reference.get("approved") and path.is_file():
                    return path, str(reference.get("language", "zh"))
    raise RuntimeError("没有可用于本地隔离测试的已授权音频参考")


def main() -> None:
    installed = AppPaths.default()
    source, language = find_approved_reference(installed)
    with tempfile.TemporaryDirectory(prefix="voicestudio-training-prepare-") as root_text:
        root = Path(root_text)
        projects = root / "projects"
        project = projects / "training-smoke"
        raw = project / "raw"
        raw.mkdir(parents=True)
        copied = raw / ("authorized-reference" + source.suffix.lower())
        shutil.copy2(source, copied)
        paths = AppPaths(installed.data_root, projects, installed.runtime_root, installed.engine_root,
                         installed.models_root, installed.logs_root, installed.database, installed.cache_directory)
        asset = {"id": "authorized-reference", "profile_id": "training-smoke", "original_path": str(copied),
                 "project_path": str(copied), "sha256": sha256_file(copied), "enabled": True, "duplicate_of": ""}
        progress_events = []
        output = TrainingPipeline(paths).prepare({
            "action": "pipeline", "profile_id": "training-smoke", "project_path": str(project),
            "preparation_id": "round3-prepare", "source_asset_ids": [asset["id"]],
            "source_assets": [asset], "processing_options": {"language": language},
        }, lambda amount, _message: progress_events.append(float(amount)), threading.Event())
        manifest = json.loads(output.read_text(encoding="utf-8"))
        transcript = Path(manifest["asr_list"])
        lines = [line for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert manifest["status"] == "completed" and lines
        segments = list(Path(manifest["segments_dir"]).glob("*.wav"))
        assert segments
        print(json.dumps({"ok": True, "status": manifest["status"], "segment_count": len(segments),
                          "transcribed_segment_count": len(lines), "progress_event_count": len(progress_events)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
