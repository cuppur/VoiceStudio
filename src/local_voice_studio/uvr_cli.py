from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    engine = Path(args.engine).resolve(); input_dir = Path(args.input).resolve(); output_dir = Path(args.output).resolve()
    weights = engine / "tools" / "uvr5" / "uvr5_weights"
    if not weights.exists():
        raise RuntimeError("尚未安装 UVR5 权重；请在安装脚本中启用 -DownloadUVR5")
    candidates = sorted(weights.glob("*HP2*.pth")) or sorted(weights.glob("*.pth"))
    if not candidates:
        raise RuntimeError("找不到可用的 UVR5 人声分离模型")
    model = candidates[0]
    os.chdir(engine)
    for path in (engine, engine / "tools", engine / "tools" / "uvr5"):
        sys.path.insert(0, str(path))
    from vr import AudioPre
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pre = AudioPre(agg=10, model_path=str(model), device=device, is_half=device == "cuda")
    vocal = output_dir / "vocal"; instrumental = output_dir / "instrumental"; vocal.mkdir(parents=True, exist_ok=True); instrumental.mkdir(parents=True, exist_ok=True)
    supported = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}
    try:
        for source in sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in supported):
            prepared = source
            with tempfile.TemporaryDirectory(prefix="voice-studio-uvr-") as tmp:
                converted = Path(tmp) / (source.stem + ".wav")
                subprocess.run(["ffmpeg", "-y", "-i", str(source), "-vn", "-acodec", "pcm_s16le", "-ac", "2", "-ar", "44100", str(converted)], capture_output=True, check=True)
                prepared = converted
                pre._path_audio_(str(prepared), str(instrumental), str(vocal), "wav", False)
            print(f"{source.name}: 完成", flush=True)
    finally:
        del pre
        if torch.cuda.is_available(): torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
