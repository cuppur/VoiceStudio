"""SenseVoice lyric transcription bridge for the private worker runtime.

Runs inside the pinned GPT-SoVITS engine environment where funasr is
available.  The input is one separated vocal WAV; the output is JSON Lines:
one ``{"start": seconds, "end": seconds, "text": str}`` object per recognized
segment.  Timestamps come from the engine's fsmn-vad segmentation; when the
model returns no per-sentence timestamps we fall back to one whole-file line,
which the worker still records as auto-recognized (never official lyrics).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
import hashlib
from pathlib import Path


_REQUIRED_FILES = {
    "SenseVoiceSmall": {
        "model.pt": (936291369, "833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea"),
        "config.yaml": (1855, "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19"),
        "configuration.json": (396, "02810a7f8e9e8aee10370a265f7e799728ce25b4c00cdbf4602b303ee395a38e"),
        "tokens.json": (352064, "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97"),
        "chn_jpn_yue_eng_ko_spectok.bpe.model": (377341, "aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8"),
        "am.mvn": (11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5"),
    },
    "FSMN-VAD": {
        "model.pt": (1721366, "b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5"),
        "config.yaml": (1215, "486861ca26ddb79081663b6179cb204c6bfae71c52f04aafc48a9e9d8dde1e93"),
        "configuration.json": (365, "7bce8867e37d55c3dd8f672695ced18077a2be199ea529a5d432d5350fc0acba"),
        "am.mvn": (8040, "6820fef9687708c4fc3fab2530179c8fcea6262daa25514380056cd8f6eb1754"),
    },
}


def _verify_model_directory(root: Path, model_name: str) -> None:
    """Require the complete pinned local model; never repair it from the network."""
    expected = _REQUIRED_FILES[model_name]
    failures = []
    for name, (size, digest) in expected.items():
        path = root / name
        if not path.is_file():
            failures.append(f"missing {name}")
            continue
        if path.stat().st_size != size:
            failures.append(f"size {name}")
            continue
        digest_context = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_context.update(chunk)
        actual = digest_context.hexdigest()
        if actual != digest:
            failures.append(f"sha256 {name}")
    if failures:
        raise RuntimeError(f"本地歌词模型校验失败（{model_name}）：{', '.join(failures)}")


def _duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            frames = stream.getnframes()
            rate = stream.getframerate()
            return (frames / rate) if rate else 0.0
    except (OSError, EOFError, wave.Error):
        return 0.0


def _as_segments(result, duration: float) -> list[dict]:
    segments: list[dict] = []
    items = result if isinstance(result, list) else [result]
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        timestamp = item.get("timestamp")
        if isinstance(timestamp, list) and timestamp:
            for entry in timestamp:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                try:
                    start = float(entry[0]) / 1000.0
                    end = float(entry[1]) / 1000.0
                except (TypeError, ValueError):
                    continue
                segment_text = str(entry[2]).strip() if len(entry) > 2 and entry[2] else text
                if segment_text:
                    segments.append({"start": start, "end": max(end, start), "text": segment_text})
        else:
            segments.append({"start": 0.0, "end": duration, "text": text})
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceStudio SenseVoice lyric bridge")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--vad-dir", required=True)
    args = parser.parse_args()
    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        print(f"输入文件不存在: {source}", file=sys.stderr)
        return 2
    model_dir = Path(args.model_dir).resolve()
    vad_dir = Path(args.vad_dir).resolve()
    try:
        _verify_model_directory(model_dir, "SenseVoiceSmall")
        _verify_model_directory(vad_dir, "FSMN-VAD")
    except RuntimeError as exc:
        print("自动歌词模型尚未安装，请进入设置修复本地引擎", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    from funasr import AutoModel  # engine environment only

    model = AutoModel(
        model=str(model_dir),
        vad_model=str(vad_dir),
        device="cuda" if _cuda_available() else "cpu",
        disable_update=True,
    )
    duration = _duration_seconds(source)
    try:
        result = model.generate(input=str(source), language=args.language,
                                vad_filter=True, return_raw_text=False)
    except TypeError:
        result = model.generate(input=str(source), language=args.language)
    segments = _as_segments(result, duration)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for segment in segments:
            stream.write(json.dumps(segment, ensure_ascii=False) + "\n")
    temporary.replace(output)
    print(f"lyrics segments: {len(segments)}", flush=True)
    return 0


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
