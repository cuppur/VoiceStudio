"""Offline MelBand RoFormer ONNX bridge for the private worker runtime."""
from __future__ import annotations

import argparse
from pathlib import Path


SAMPLE_RATE = 44100
CHUNK_SAMPLES = 352800
OVERLAP_SAMPLES = 44100


def _load_audio(path: Path):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] != 2:
        raise RuntimeError("RoFormer 仅支持单声道或双声道输入")
    if rate != SAMPLE_RATE:
        from math import gcd
        divisor = gcd(rate, SAMPLE_RATE)
        audio = resample_poly(audio, SAMPLE_RATE // divisor, rate // divisor, axis=0).astype(np.float32)
    return audio.T


def _separate(audio, model: Path):
    import numpy as np
    import onnxruntime as ort

    available = ort.get_available_providers()
    providers = [name for name in ("CUDAExecutionProvider", "CPUExecutionProvider") if name in available]
    if not providers:
        raise RuntimeError("ONNX Runtime 没有可用执行后端")
    session = ort.InferenceSession(str(model), providers=providers)
    input_name = session.get_inputs()[0].name
    length = audio.shape[1]
    stride = CHUNK_SAMPLES - OVERLAP_SAMPLES
    accumulated = np.zeros((2, 2, length), dtype=np.float32)
    weights = np.zeros(length, dtype=np.float32)
    for start in range(0, length, stride):
        end = min(start + CHUNK_SAMPLES, length)
        block = np.zeros((1, 2, CHUNK_SAMPLES), dtype=np.float32)
        block[0, :, :end - start] = audio[:, start:end]
        prediction = np.asarray(session.run(None, {input_name: block})[0], dtype=np.float32)
        if prediction.shape != (1, 2, 2, CHUNK_SAMPLES):
            raise RuntimeError(f"RoFormer 输出形状无效: {prediction.shape}")
        window = np.ones(end - start, dtype=np.float32)
        fade = min(OVERLAP_SAMPLES, len(window))
        if start > 0:
            window[:fade] = np.linspace(0.0, 1.0, fade, endpoint=False, dtype=np.float32)
        if end < length:
            window[-fade:] = np.minimum(window[-fade:], np.linspace(1.0, 0.0, fade, endpoint=False, dtype=np.float32))
        accumulated[:, :, start:end] += prediction[0, :, :, :end - start] * window
        weights[start:end] += window
    return accumulated / np.maximum(weights, 1e-8)[None, None, :]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    source, model, output = Path(args.input).resolve(), Path(args.model).resolve(), Path(args.output).resolve()
    if not source.is_file() or not model.is_file():
        raise RuntimeError("RoFormer 输入或固定模型不存在")
    separated = _separate(_load_audio(source), model)
    import numpy as np
    import soundfile as sf
    vocal_dir, instrumental_dir = output / "vocal", output / "instrumental"
    vocal_dir.mkdir(parents=True, exist_ok=True); instrumental_dir.mkdir(parents=True, exist_ok=True)
    sf.write(vocal_dir / "vocals.wav", np.clip(separated[0].T, -1.0, 1.0), SAMPLE_RATE, subtype="PCM_16")
    sf.write(instrumental_dir / "instrumental.wav", np.clip(separated[1].T, -1.0, 1.0), SAMPLE_RATE, subtype="PCM_16")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
