from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import wave
import platform
from pathlib import Path
from typing import Any, Callable

from .paths import AppPaths


class EngineNotReady(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_torch_load(source, *args, **kwargs):
    """Restricted checkpoint loader, including the V2ProPlus two-byte ZIP header."""
    import torch
    loader = kwargs.pop("_loader", torch.load)
    if kwargs.get("weights_only") is False:
        raise ValueError("拒绝 weights_only=False 的不安全模型加载")
    kwargs["weights_only"] = True
    actual_source = source
    if isinstance(source, (str, os.PathLike, Path)):
        data = Path(source).read_bytes()
        if len(data) >= 4 and data[:2] != b"PK" and data[2:4] == b"\x03\x04":
            actual_source = io.BytesIO(b"PK" + data[2:])
    try:
        from utils import HParams
    except (ImportError, AttributeError):
        return loader(actual_source, *args, **kwargs)
    with torch.serialization.safe_globals([HParams]):
        return loader(actual_source, *args, **kwargs)


class GptSovitsEngine:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self._tts = None
        self._loaded_key: tuple[str, str, str] | None = None

    def readiness(self) -> dict[str, Any]:
        engine = self.paths.engine_root
        pretrained = engine / "GPT_SoVITS" / "pretrained_models"
        required = [
            engine / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py",
            pretrained / "s1v3.ckpt",
            pretrained / "v2Pro" / "s2Gv2ProPlus.pth",
            pretrained / "chinese-roberta-wwm-ext-large",
            pretrained / "chinese-hubert-base",
        ]
        missing = [str(item) for item in required if not item.exists()]
        return {"ready": not missing, "engine_root": str(engine), "missing": missing}

    def gpu_health(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "torch_version": "",
            "cuda_available": False,
            "cuda_version": "",
            "gpu_name": "",
            "compute_capability": "",
            "tensor_test_passed": False,
            "gpt_sovits_imported": False,
            "models_ready": False,
            "ffmpeg_ready": bool(self.ffmpeg()),
            "compatible": False,
            "actionable_errors": [],
        }
        try:
            import torch
            result.update(torch_version=torch.__version__, cuda_version=torch.version.cuda or "", cuda_available=torch.cuda.is_available())
            if not torch.cuda.is_available():
                result["actionable_errors"].append("CUDA 不可用；请确认 NVIDIA 驱动正常，并重新安装本地引擎。")
                return result
            device = torch.cuda.get_device_properties(0)
            result.update({
                "gpu_name": device.name,
                "compute_capability": f"sm_{device.major}{device.minor}",
                "memory_bytes": device.total_memory,
                "architectures": torch.cuda.get_arch_list(),
            })
            tensor = torch.tensor([1.0], device="cuda") * 2
            torch.cuda.synchronize()
            result["tensor_test_passed"] = tensor.cpu().item() == 2.0
            readiness = self.readiness()
            result["models_ready"] = readiness["ready"]
            if readiness["ready"]:
                os.chdir(self.paths.engine_root)
                for item in (self.paths.engine_root, self.paths.engine_root / "GPT_SoVITS"):
                    if str(item) not in sys.path: sys.path.insert(0, str(item))
                try:
                    import GPT_SoVITS.TTS_infer_pack.TTS  # noqa: F401
                    result["gpt_sovits_imported"] = True
                except Exception as exc:
                    result["actionable_errors"].append(f"GPT-SoVITS 模块导入失败：{exc}")
            else:
                result["actionable_errors"].append("GPT-SoVITS 模型文件不完整，请点击“安装/修复本地引擎”。")
            arch_ok = "sm_120" in result["architectures"] or device.major < 12
            if not arch_ok:
                result["actionable_errors"].append("当前 PyTorch 不包含 sm_120 内核，需要 PyTorch 2.7.1+cu128。")
            if not result["ffmpeg_ready"]:
                result["actionable_errors"].append("FFmpeg 未安装，无法合并或导出 MP3。")
            result["compatible"] = bool(arch_ok and result["tensor_test_passed"] and result["gpt_sovits_imported"] and result["models_ready"] and result["ffmpeg_ready"])
            return result
        except Exception as exc:
            if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") == "torch":
                result["actionable_errors"].append("本地引擎尚未安装 PyTorch，请进入“设置”并点击“安装/修复本地引擎”。")
            else:
                result["actionable_errors"].append(f"GPU 检测失败：{exc}")
            result["exception"] = type(exc).__name__
            return result

    def load(self, profile: dict[str, Any], force_cpu: bool = False) -> None:
        ready = self.readiness()
        if not ready["ready"]:
            raise EngineNotReady("GPT-SoVITS 尚未安装完整")
        pretrained = self.paths.engine_root / "GPT_SoVITS" / "pretrained_models"
        gpt = profile.get("active_gpt_checkpoint") or str(pretrained / "s1v3.ckpt")
        sovits = profile.get("active_sovits_checkpoint") or str(pretrained / "v2Pro" / "s2Gv2ProPlus.pth")
        if profile.get("active_gpt_checkpoint") or profile.get("active_sovits_checkpoint"):
            expected_gpt = str(profile.get("active_gpt_sha256") or "")
            expected_sovits = str(profile.get("active_sovits_sha256") or "")
            trust = str(profile.get("active_model_trust_status") or "")
            if len(expected_gpt) != 64 or len(expected_sovits) != 64 or trust not in {"verified", "trusted-local"}:
                raise EngineNotReady("当前模型缺少可信哈希登记，已拒绝加载")
            if _file_sha256(Path(gpt)) != expected_gpt or _file_sha256(Path(sovits)) != expected_sovits:
                raise EngineNotReady("当前模型文件已被替换，已拒绝加载")
        device = "cpu" if force_cpu else "cuda"
        key = (gpt, sovits, device)
        if self._tts is not None and self._loaded_key == key:
            return
        health = self.gpu_health() if not force_cpu else {"compatible": True}
        if not health.get("compatible"):
            raise EngineNotReady("；".join(health.get("actionable_errors") or ["GPU 环境不兼容"]))
        os.chdir(self.paths.engine_root)
        for item in (self.paths.engine_root, self.paths.engine_root / "GPT_SoVITS"):
            if str(item) not in sys.path:
                sys.path.insert(0, str(item))
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            original_torch_load = torch.load
            def restricted_load(source, *args, **kwargs):
                if kwargs.get("weights_only") is False:
                    kwargs["weights_only"] = True
                return safe_torch_load(source, *args, _loader=original_torch_load, **kwargs)
            torch.load = restricted_load
            try:
                # Patch before importing upstream so both ``torch.load`` and
                # any ``from torch import load`` bindings are restricted.
                from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
                config = {
                    "custom": {
                        "device": device,
                        "is_half": not force_cpu,
                        "version": "v2ProPlus",
                        "t2s_weights_path": gpt,
                        "vits_weights_path": sovits,
                        "bert_base_path": str(pretrained / "chinese-roberta-wwm-ext-large"),
                        "cnhuhbert_base_path": str(pretrained / "chinese-hubert-base"),
                    }
                }
                self._tts = TTS(TTS_Config(config))
            finally:
                torch.load = original_torch_load
        self._loaded_key = key

    def stop(self) -> None:
        if self._tts is not None:
            self._tts.stop()

    def synthesize_segment(self, payload: dict[str, Any], destination: Path) -> tuple[int, Path, int]:
        if self._tts is None:
            raise EngineNotReady("尚未加载声音配置")
        inputs = {
            "text": payload["text"],
            "text_lang": payload.get("text_lang", "zh"),
            "ref_audio_path": payload["ref_audio_path"],
            "prompt_text": payload.get("prompt_text", ""),
            "prompt_lang": payload.get("prompt_lang", "zh"),
            "top_k": int(payload.get("top_k", 15)),
            "top_p": float(payload.get("top_p", 1.0)),
            "temperature": float(payload.get("temperature", 1.0)),
            "speed_factor": float(payload.get("speed_factor", 1.0)),
            "fragment_interval": float(payload.get("fragment_interval", 0.3)),
            "seed": int(payload.get("seed", -1)),
            "repetition_penalty": float(payload.get("repetition_penalty", 1.35)),
            "text_split_method": "cut0",
            "parallel_infer": True,
            "split_bucket": True,
            "return_fragment": False,
        }
        chunks = []
        sample_rate = 0
        with contextlib.redirect_stdout(sys.stderr):
            for sample_rate, audio in self._tts.run(inputs):
                chunks.append(audio)
        if not chunks:
            raise RuntimeError("模型没有返回音频")
        import numpy as np
        output = np.concatenate(chunks).astype(np.int16, copy=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(output.tobytes())
        return sample_rate, destination, len(output)

    def ffmpeg(self) -> Path | None:
        candidates = [
            self.paths.data_root / "tools" / "ffmpeg.exe",
            self.paths.runtime_root / "env" / "Library" / "bin" / "ffmpeg.exe",
            self.paths.engine_root / "ffmpeg.exe",
        ]
        for item in candidates:
            if not item.is_file(): continue
            try:
                if subprocess.run([str(item), "-version"], capture_output=True, timeout=10).returncode == 0: return item
            except (OSError, subprocess.SubprocessError): continue
        return None

    def merge_and_encode(self, wav_files: list[Path], merged_wav: Path, mp3: Path, pause_seconds: float = 0.3) -> list[Path]:
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            raise EngineNotReady("找不到 FFmpeg，无法合并和导出 MP3")
        self.merge_wavs(wav_files, merged_wav, pause_seconds)
        subprocess.run(
            [str(ffmpeg), "-y", "-i", str(merged_wav), "-codec:a", "libmp3lame", "-b:a", "320k", str(mp3)],
            capture_output=True, check=True,
        )
        return [merged_wav, mp3]

    def merge_wavs(self, wav_files: list[Path], merged_wav: Path, pause_seconds: float = 0.3) -> list[Path]:
        merged_wav.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 0
        channels = 0
        sample_width = 0
        with wave.open(str(merged_wav), "wb") as output:
            for index, item in enumerate(wav_files):
                with wave.open(str(item), "rb") as source:
                    if index == 0:
                        sample_rate = source.getframerate(); channels = source.getnchannels(); sample_width = source.getsampwidth(); output.setnchannels(channels); output.setsampwidth(sample_width); output.setframerate(sample_rate)
                    elif (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (sample_rate, channels, sample_width):
                        raise ValueError("分段音频格式不一致，无法无损合并")
                    output.writeframes(source.readframes(source.getnframes()))
                if index < len(wav_files) - 1:
                    silent_frames = round(sample_rate * max(0.0, pause_seconds))
                    output.writeframes(b"\x00" * silent_frames * channels * sample_width)
        return [merged_wav]
