"""Independent, lightweight checks for a real singing-model inference."""
from __future__ import annotations

import hashlib
import audioop
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VerificationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": list(self.errors), "details": dict(self.details)}


class SingingModelVerifier:
    """Run the complete trust gate without coupling it to product pipeline state."""

    def __init__(self, engine: Any):
        self.engine = engine

    def verify(self, checkpoint: Path, index: Path, test_audio: Path, output_dir: Path, cancel: Any = None) -> VerificationResult:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return VerificationResult(False, ["模型验证已取消"])
        restricted = self.engine.verify_model(checkpoint, index)
        if not restricted:
            errors = list(getattr(restricted, "errors", []) or [])
            return VerificationResult(False, errors or ["模型未通过 restricted load/index 校验"])
        input_result = verify_inference_output(test_audio, test_audio)
        if not input_result.ok:
            return VerificationResult(False, input_result.errors, input_result.details)
        output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / ("verification-" + checkpoint.stem + ".wav")
        try:
            try:
                produced = self.engine.convert({"input_path": str(test_audio), "model_path": str(checkpoint), "index_path": str(index), "output_path": str(output)}, cancel=cancel)
                if Path(produced).resolve() != output.resolve():
                    return VerificationResult(False, ["验证引擎未生成指定输出路径"])
            except Exception as exc:
                return VerificationResult(False, [f"真实推理验证失败: {exc}"])
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return VerificationResult(False, ["模型验证已取消"])
            result = verify_inference_output(test_audio, output, source_sha256=str(input_result.details.get("input_sha256", "")))
            result.details.update({"restricted_load": True, "index_verified": True})
            return result
        finally:
            output.unlink(missing_ok=True)


def _wav_info(path: Path) -> tuple[int, int, int, int, bytes]:
    with wave.open(str(path), "rb") as stream:
        rate, channels, frames, width = stream.getframerate(), stream.getnchannels(), stream.getnframes(), stream.getsampwidth()
        return rate, channels, frames, width, stream.readframes(frames)


def verify_inference_output(test_input: Path, output: Path, *, source_sha256: str = "") -> VerificationResult:
    """Check an authorized 3-8 second snapshot and a real decoded output."""
    errors: list[str] = []
    details: dict[str, object] = {"input": str(test_input), "output": str(output)}
    try:
        in_rate, _in_channels, in_frames, in_width, in_pcm = _wav_info(test_input)
        input_seconds = in_frames / in_rate if in_rate else 0
        details["input_seconds"] = input_seconds
        details["input_sha256"] = _sha256(test_input)
        if not 3.0 <= input_seconds <= 8.0:
            errors.append("验证输入必须为 3-8 秒授权快照")
        input_peak = audioop.max(in_pcm, in_width) if in_pcm else 0; input_rms = audioop.rms(in_pcm, in_width) if in_pcm else 0
        details.update(input_peak=input_peak, input_rms=input_rms)
        if input_peak <= 0 or input_rms <= 0:
            errors.append("验证输入不可为静音")
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        errors.append(f"验证输入 WAV 无法解码: {exc}")
        in_rate = in_frames = 0
    try:
        out_rate, _out_channels, out_frames, out_width, out_pcm = _wav_info(output)
        output_seconds = out_frames / out_rate if out_rate else 0
        details.update(output_seconds=output_seconds, output_sha256=_sha256(output))
        output_peak = audioop.max(out_pcm, out_width) if out_pcm else 0; output_rms = audioop.rms(out_pcm, out_width) if out_pcm else 0
        details.update(output_peak=output_peak, output_rms=output_rms)
        if output_peak <= 0 or output_rms <= 0:
            errors.append("真实推理输出为静音")
        if in_frames and in_rate and out_rate:
            ratio = output_seconds / (in_frames / in_rate)
            details["duration_ratio"] = ratio
            if not 0.9 <= ratio <= 1.1:
                errors.append("真实推理输出时长偏差超过 ±10%")
        if source_sha256 and details.get("output_sha256", "").lower() == source_sha256.lower():
            errors.append("真实推理输出 Hash 与输入相同")
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        errors.append(f"真实推理输出 WAV 无法解码: {exc}")
    return VerificationResult(not errors, errors, details)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
