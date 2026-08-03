from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--engine", required=True); parser.add_argument("--tools", required=True); args = parser.parse_args()
    engine = Path(args.engine).resolve(); pretrained = engine / "GPT_SoVITS" / "pretrained_models"; errors: list[str] = []
    result = {"python_executable": sys.executable, "python_version": sys.version.split()[0], "torch_version": "", "cuda_available": False, "cuda_version": "", "gpu_name": "", "compute_capability": "", "tensor_test_passed": False, "gpt_sovits_imported": False, "models_ready": False, "ffmpeg_ready": False, "minimal_model_load_passed": False, "compatible": False, "actionable_errors": errors}
    try:
        import torch
        result.update(torch_version=torch.__version__, cuda_available=torch.cuda.is_available(), cuda_version=torch.version.cuda or "")
        if not torch.cuda.is_available(): raise RuntimeError("CUDA 不可用")
        props = torch.cuda.get_device_properties(0); result.update(gpu_name=props.name, compute_capability=f"sm_{props.major}{props.minor}")
        result["tensor_test_passed"] = (torch.tensor([1.0], device="cuda") * 2).cpu().item() == 2.0
        if props.major >= 12 and "sm_120" not in torch.cuda.get_arch_list(): errors.append("PyTorch wheel 不包含 sm_120")
    except Exception as exc: errors.append(f"PyTorch/CUDA：{exc}")
    required = [engine / "GPT_SoVITS/TTS_infer_pack/TTS.py", pretrained / "s1v3.ckpt", pretrained / "v2Pro/s2Gv2ProPlus.pth", pretrained / "chinese-roberta-wwm-ext-large", pretrained / "chinese-hubert-base"]
    missing = [str(item) for item in required if not item.exists()]; result["models_ready"] = not missing
    if missing: errors.append(f"缺少 {len(missing)} 个引擎或模型文件")
    ffmpeg_candidates = [Path(sys.prefix) / "Library/bin/ffmpeg.exe", Path(args.tools) / "ffmpeg.exe"]
    if shutil.which("ffmpeg"): ffmpeg_candidates.append(Path(shutil.which("ffmpeg")))
    for ffmpeg in ffmpeg_candidates:
        if ffmpeg.is_file() and subprocess.run([str(ffmpeg), "-version"], capture_output=True).returncode == 0:
            result["ffmpeg_ready"] = True; break
    if not result["ffmpeg_ready"]: errors.append("FFmpeg 不可用")
    if result["models_ready"]:
        try:
            os.chdir(engine); sys.path[:0] = [str(engine), str(engine / "GPT_SoVITS")]
            with contextlib.redirect_stdout(sys.stderr):
                from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
                result["gpt_sovits_imported"] = True
                config = {"custom": {"device": "cuda", "is_half": True, "version": "v2ProPlus", "t2s_weights_path": str(pretrained / "s1v3.ckpt"), "vits_weights_path": str(pretrained / "v2Pro/s2Gv2ProPlus.pth"), "bert_base_path": str(pretrained / "chinese-roberta-wwm-ext-large"), "cnhuhbert_base_path": str(pretrained / "chinese-hubert-base")}}
                instance = TTS(TTS_Config(config)); result["minimal_model_load_passed"] = instance is not None
        except Exception as exc: errors.append(f"GPT-SoVITS 模型加载：{exc}")
    result["compatible"] = bool(result["tensor_test_passed"] and result["gpt_sovits_imported"] and result["models_ready"] and result["ffmpeg_ready"] and result["minimal_model_load_passed"] and not errors)
    print(json.dumps(result, ensure_ascii=False)); return 0 if result["compatible"] else 1


if __name__ == "__main__": raise SystemExit(main())
