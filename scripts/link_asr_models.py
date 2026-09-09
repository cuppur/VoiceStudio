"""Bridge the locally installed ASR models into the ModelScope cache.

FunASR resolves SenseVoice/FSMN-VAD by ModelScope id (``iic/SenseVoiceSmall``)
and therefore looks in the ModelScope cache, while the installer places the
models under ``models/lyrics``.  Without this bridge FunASR re-downloads the
model every run and can block for minutes on a slow connection.

Run once after installing (or repairing) the local engine:

    python scripts/link_asr_models.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_voice_studio.paths import AppPaths  # noqa: E402

MODELS = (
    ("SenseVoiceSmall", "iic--SenseVoiceSmall"),
    ("FSMN-VAD", "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch"),
)


def link(source: Path, target: Path) -> str:
    if target.is_file() and target.stat().st_size == source.stat().st_size:
        return "already linked"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
        return "hard link"
    except OSError:
        import shutil
        shutil.copy2(source, target)
        return "copied"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    paths = AppPaths.default()
    cache = paths.models_root / "modelscope" / "models"
    lyrics = paths.lyrics_models_root
    linked = 0
    for local_name, cache_name in MODELS:
        source = lyrics / local_name / "model.pt"
        if not source.is_file():
            print(f"skip {local_name}: {source} 不存在（请先安装本地引擎）")
            continue
        target = cache / cache_name / "snapshots" / "master" / "model.pt"
        if options.dry_run:
            print(f"would link {source} -> {target}")
            continue
        print(f"{local_name}: {link(source, target)} -> {target}")
        linked += 1
    print(f"done: {linked} model(s) bridged into {cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
