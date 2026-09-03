"""Small subprocess bridge for the pinned RVC source tree.

The product worker starts this module in the isolated RVC environment.  Keeping
the import here prevents PyTorch/RVC from entering the desktop process.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="")
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--index-rate", type=float, default=0.75)
    parser.add_argument("--protect", type=float, default=0.33)
    parser.add_argument("--filter-radius", type=int, default=3)
    parser.add_argument("--f0-method", choices=("auto", "rmvpe"), default="rmvpe")
    parser.add_argument("--verify-model", action="store_true")
    parser.add_argument("--prepare-checkpoint", action="store_true")
    parser.add_argument("--sample-rate", default="48k")
    parser.add_argument("--version", default="v2")
    parser.add_argument("--test-input", default="")
    parser.add_argument("--test-output", default="")
    parser.add_argument("--analyze-pitch", action="store_true")
    parser.add_argument("--pitch-report", default="")
    args = parser.parse_args()
    model = Path(args.model).resolve()
    if args.analyze_pitch:
        if not args.input or not args.pitch_report:
            parser.error("--analyze-pitch requires --input and --pitch-report")
        import numpy as np
        import soundfile as sf
        import torch
        from scipy.signal import resample_poly
        from infer.lib.rmvpe import RMVPE

        audio, sample_rate = sf.read(args.input, dtype="float32", always_2d=True)
        samples = np.asarray(audio.mean(axis=1), dtype=np.float32)
        if sample_rate != 16000:
            from math import gcd
            divisor = gcd(int(sample_rate), 16000)
            samples = resample_poly(samples, 16000 // divisor, int(sample_rate) // divisor).astype(np.float32)
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        model_path = Path(os.environ.get("rmvpe_root", Path.cwd() / "assets" / "rmvpe")) / "rmvpe.pt"
        f0 = np.asarray(RMVPE(str(model_path), bool(torch.cuda.is_available()), device).infer_from_audio(samples, thred=0.03), dtype=np.float32)
        voiced = f0[np.isfinite(f0) & (f0 > 0)]
        if voiced.size == 0:
            raise RuntimeError("RMVPE 未检测到有效人声音高")
        report = {"backend": "rmvpe", "version": "rvc-rmvpe-v1", "median_hz": float(np.median(voiced)),
                  "minimum_hz": float(np.min(voiced)), "maximum_hz": float(np.max(voiced)), "voiced_frames": int(voiced.size)}
        report_path = Path(args.pitch_report).resolve(); report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return 0
    safe_index = args.index
    if args.index:
        # The Windows FAISS build cannot reliably open non-ASCII paths.  Keep
        # the authoritative index in the project and expose a process-local,
        # ASCII-only shadow copy for restricted load and inference.
        shadow_root = Path(os.environ.get("LOCAL_VOICE_STUDIO_ASCII_TEMP", tempfile.gettempdir())).resolve()
        shadow_root.mkdir(parents=True, exist_ok=True)
        if not str(shadow_root).isascii():
            raise RuntimeError("FAISS 需要 ASCII 临时目录；请配置 LOCAL_VOICE_STUDIO_ASCII_TEMP")
        index_shadow = tempfile.TemporaryDirectory(prefix="voicestudio-index-", dir=str(shadow_root))
        atexit.register(index_shadow.cleanup)
        shadow_path = Path(index_shadow.name) / "model.index"
        shutil.copy2(Path(args.index).resolve(), shadow_path)
        safe_index = str(shadow_path)
    os.environ["weight_root"] = str(model.parent)
    os.environ.setdefault("rmvpe_root", str(Path.cwd() / "assets" / "rmvpe"))
    if args.verify_model:
        import torch
        # Never execute arbitrary pickle reducers for a model supplied to the
        # product layer.  Local training artifacts are verified through the
        # same restricted loader as any future import path.
        if not args.index:
            parser.error("--verify-model requires --index")
        loaded_model = torch.load(model, map_location="cpu", weights_only=True)
        if not isinstance(loaded_model, dict):
            raise RuntimeError("RVC checkpoint 结构无效")
        import faiss
        faiss.read_index(safe_index)
        if not args.test_input or not args.test_output:
            return 0
        # Reuse the exact conversion path after the restricted load check.
        bridge_argv = sys.argv
        sys.argv = [bridge_argv[0]]
        from configs.config import Config
        from infer.vc.modules import VC
        import soundfile as sf
        config = Config()
        sys.argv = bridge_argv
        vc = VC(config)
        if vc.get_vc(model.name, 0.5, 0.33) is None:
            raise RuntimeError("RVC 模型加载失败")
        _status, audio = vc.vc_single(0, args.test_input, args.pitch, args.f0_method, safe_index, args.index_rate, args.filter_radius, 1.0, args.protect)
        if audio is None:
            raise RuntimeError("RVC 未生成音频")
        sample_rate, samples = audio
        output = Path(args.test_output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(output, samples, sample_rate, subtype="PCM_16")
        return 0
    if args.prepare_checkpoint:
        import torch
        if not args.output:
            parser.error("--output is required for checkpoint preparation")
        prepared_output = Path(args.output).resolve()
        if model.parent != prepared_output.parent or not model.name.startswith("G_") or prepared_output.name != "model.pth":
            raise RuntimeError("RVC checkpoint 转换仅允许受控训练 staging 产物")
        # Sole pickle-capable load: an upstream G_* checkpoint produced in the
        # same server-created staging directory. Product model verification
        # always uses weights_only=True above.
        raw = torch.load(model, map_location="cpu", weights_only=False)
        raw = raw.get("model", raw)
        weights = {k: v.half() for k, v in raw.items() if "enc_q" not in k}
        config = [1025, 32, 192, 192, 768, 2, 6, 3, 0, "1", [3, 7, 11], [[1, 3, 5], [1, 3, 5], [1, 3, 5]], [12, 10, 2, 2], 512, [24, 20, 4, 4], 109, 256, 48000]
        torch.save({"weight": weights, "config": config, "info": "local training", "sr": args.sample_rate, "f0": 1, "version": args.version}, prepared_output)
        return 0
    if not args.input or not args.output:
        parser.error("--input and --output are required for conversion")
    # Upstream Config parses process argv at import time; hide bridge flags.
    bridge_argv = sys.argv
    sys.argv = [bridge_argv[0]]
    from configs.config import Config
    from infer.vc.modules import VC
    import soundfile as sf

    config = Config()
    sys.argv = bridge_argv
    vc = VC(config)
    loaded = vc.get_vc(model.name, 0.5, 0.33)
    if loaded is None:
        raise RuntimeError("RVC 模型加载失败")
    _status, audio = vc.vc_single(
        0, args.input, args.pitch, args.f0_method, safe_index,
        args.index_rate, args.filter_radius, 1.0, args.protect,
    )
    if audio is None:
        raise RuntimeError("RVC 未生成音频")
    sample_rate, samples = audio
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, samples, sample_rate, subtype="PCM_16")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
