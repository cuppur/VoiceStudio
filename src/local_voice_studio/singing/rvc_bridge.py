"""Small subprocess bridge for the pinned RVC source tree.

The product worker starts this module in the isolated RVC environment.  Keeping
the import here prevents PyTorch/RVC from entering the desktop process.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="")
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--verify-model", action="store_true")
    args = parser.parse_args()
    model = Path(args.model).resolve()
    os.environ["weight_root"] = str(model.parent)
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
    if not args.input or not args.output:
        parser.error("--input and --output are required for conversion")
    from configs.config import Config
    from infer.vc.modules import VC
    import soundfile as sf

    vc = VC(Config())
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
