"""Small subprocess bridge for the pinned RVC source tree.

The product worker starts this module in the isolated RVC environment.  Keeping
the import here prevents PyTorch/RVC from entering the desktop process.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="")
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--verify-model", action="store_true")
    parser.add_argument("--prepare-checkpoint", action="store_true")
    parser.add_argument("--sample-rate", default="48k")
    parser.add_argument("--version", default="v2")
    args = parser.parse_args()
    model = Path(args.model).resolve()
    os.environ["weight_root"] = str(model.parent)
    os.environ.setdefault("rmvpe_root", str(Path.cwd() / "assets" / "rmvpe"))
    if args.verify_model:
        import torch
        # Never execute arbitrary pickle reducers for a model supplied to the
        # product layer.  Local training artifacts are verified through the
        # same restricted loader as any future import path.
        torch.load(model, map_location="cpu", weights_only=True)
        if args.index:
            import faiss
            faiss.read_index(str(Path(args.index).resolve()))
        return 0
    if args.prepare_checkpoint:
        import torch
        if not args.output:
            parser.error("--output is required for checkpoint preparation")
        raw = torch.load(model, map_location="cpu", weights_only=False)
        raw = raw.get("model", raw)
        weights = {k: v.half() for k, v in raw.items() if "enc_q" not in k}
        config = [1025, 32, 192, 192, 768, 2, 6, 3, 0, "1", [3, 7, 11], [[1, 3, 5], [1, 3, 5], [1, 3, 5]], [12, 10, 2, 2], 512, [24, 20, 4, 4], 109, 256, 48000]
        torch.save({"weight": weights, "config": config, "info": "local training", "sr": args.sample_rate, "f0": 1, "version": args.version}, args.output)
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
        0, args.input, args.pitch, "rmvpe", args.index,
        0.75, 0, 1.0, 0.33,
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
