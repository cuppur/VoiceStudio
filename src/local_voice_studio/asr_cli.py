from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalVoiceStudio local ASR bridge")
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-l", "--language", default="zh")
    args = parser.parse_args()

    # The pinned GPT-SoVITS revision ships both Fun-ASR-Nano and SenseVoice.
    # Fun-ASR-Nano currently needs a newer Transformers/Qwen3 stack than the
    # engine revision pins. SenseVoice is the compatible multilingual backend
    # and keeps all transcription local.
    from tools.asr.funasr_asr import execute_asr

    result = execute_asr(
        input_folder=args.input,
        output_folder=args.output,
        model_size="large",
        language=args.language,
        backend="sensevoice",
    )
    print(result, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
