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
from pathlib import Path


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
    args = parser.parse_args()
    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        print(f"输入文件不存在: {source}", file=sys.stderr)
        return 2

    from funasr import AutoModel  # engine environment only

    model = AutoModel(
        model="iic/SenseVoiceSmall",
        vad_model="fsmn-vad",
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
