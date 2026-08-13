from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_AUDIO_PREFIXES = ("tests/fixtures/synthetic/",)
PERSONAL_PREFIXES = ("参考声音/", "voice-assets/", "personal-audio/")
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wma"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    )
    return [item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    violations: list[str] = []
    for raw in tracked_files():
        path = raw.replace("\\", "/")
        lower = path.lower()
        if any(path.startswith(prefix) for prefix in PERSONAL_PREFIXES):
            violations.append(path)
            continue
        if PurePosixPath(lower).suffix in AUDIO_SUFFIXES and not any(
            path.startswith(prefix) for prefix in ALLOWED_AUDIO_PREFIXES
        ):
            violations.append(path)
    if violations:
        print("Personal or non-synthetic audio is tracked:", file=sys.stderr)
        for item in violations:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("PUBLIC_ASSET_POLICY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
