from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    destination: Path
    sha256: str = ""
    size: int = 0


def download_resumable(
    spec: DownloadSpec,
    progress: Callable[[int, int], None] | None = None,
    timeout: int = 60,
) -> Path:
    """Download to .part, resume with HTTP Range, then verify and atomically move."""
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    partial = spec.destination.with_suffix(spec.destination.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "LocalVoiceStudio/0.1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(spec.url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if offset and status != 206:
            offset = 0
            partial.unlink(missing_ok=True)
        content_length = int(response.headers.get("Content-Length") or 0)
        total = offset + content_length if content_length else spec.size
        mode = "ab" if offset else "wb"
        with partial.open(mode) as stream:
            downloaded = offset
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
            stream.flush()
            os.fsync(stream.fileno())
    if spec.size and partial.stat().st_size != spec.size:
        raise ValueError(f"下载大小不符: {partial.stat().st_size} != {spec.size}")
    if spec.sha256:
        actual = _sha256(partial)
        if actual.lower() != spec.sha256.lower():
            raise ValueError(f"下载校验失败: {spec.destination.name}")
    partial.replace(spec.destination)
    return spec.destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

