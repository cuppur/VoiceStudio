from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


class AssetManifestError(ValueError):
    """Raised when a runtime asset is not completely pinned."""


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    destination: Path
    sha256: str
    size: int
    mirrors: tuple[str, ...] = ()
    asset_id: str = ""

    def __post_init__(self) -> None:
        digest = self.sha256.lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AssetManifestError(f"asset {self.asset_id or self.destination.name!r} has no valid SHA-256")
        if self.size <= 0:
            raise AssetManifestError(f"asset {self.asset_id or self.destination.name!r} has no valid size")
        allowed = (self.url, *self.mirrors)
        if not allowed or any(urllib.parse.urlsplit(item).scheme != "https" for item in allowed):
            raise AssetManifestError("runtime assets must use HTTPS URLs")

    def urls(self) -> tuple[str, ...]:
        return (self.url, *self.mirrors)


def load_asset_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("assets"), list):
        raise AssetManifestError("unsupported runtime asset manifest")
    seen: set[str] = set()
    for item in data["assets"]:
        required = {"id", "size", "sha256", "destination", "urls"}
        if not required.issubset(item) or item["id"] in seen or not item["urls"]:
            raise AssetManifestError("runtime asset entry is incomplete or duplicated")
        seen.add(item["id"])
        destination = PurePosixPath(str(item["destination"]).replace("\\", "/"))
        if destination.is_absolute() or ".." in destination.parts:
            raise AssetManifestError(f"unsafe asset destination: {item['destination']}")
        DownloadSpec(
            url=item["urls"][0],
            mirrors=tuple(item["urls"][1:]),
            destination=Path(*destination.parts),
            sha256=item["sha256"],
            size=int(item["size"]),
            asset_id=item["id"],
        )
    return data


def asset_spec(manifest: dict, asset_id: str, root: Path) -> DownloadSpec:
    try:
        item = next(entry for entry in manifest["assets"] if entry["id"] == asset_id)
    except StopIteration as exc:
        raise AssetManifestError(f"unregistered runtime asset: {asset_id}") from exc
    relative = PurePosixPath(item["destination"].replace("\\", "/"))
    return DownloadSpec(
        url=item["urls"][0],
        mirrors=tuple(item["urls"][1:]),
        destination=root.joinpath(*relative.parts),
        sha256=item["sha256"],
        size=int(item["size"]),
        asset_id=asset_id,
    )


def verify_file(path: Path, sha256: str, size: int) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == sha256.lower()


def download_resumable(
    spec: DownloadSpec,
    progress: Callable[[int, int], None] | None = None,
    timeout: int = 60,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> Path:
    """Download a pinned asset, revalidating cache and atomically publishing it."""
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    if spec.destination.exists():
        if verify_file(spec.destination, spec.sha256, spec.size):
            return spec.destination
        spec.destination.unlink()

    partial = spec.destination.with_suffix(spec.destination.suffix + ".part")
    errors: list[str] = []
    for url in spec.urls():
        try:
            _download_one(url, partial, spec, progress, timeout, opener)
            if not verify_file(partial, spec.sha256, spec.size):
                raise ValueError("download content does not match the pinned digest and size")
            partial.replace(spec.destination)
            return spec.destination
        except Exception as exc:
            partial.unlink(missing_ok=True)
            errors.append(f"{urllib.parse.urlsplit(url).netloc}: {exc}")
    raise ValueError(f"all mirrors failed for {spec.asset_id or spec.destination.name}: {'; '.join(errors)}")


def _download_one(
    url: str,
    partial: Path,
    spec: DownloadSpec,
    progress: Callable[[int, int], None] | None,
    timeout: int,
    opener: Callable[..., object],
) -> None:
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "LocalVoiceStudio/0.2"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    response = opener(request, timeout=timeout)
    with response:
        final_url = getattr(response, "geturl", lambda: url)()
        if urllib.parse.urlsplit(final_url).scheme != "https":
            raise ValueError("download redirected to a non-HTTPS URL")
        status = getattr(response, "status", 200)
        if offset and status != 206:
            offset = 0
            partial.unlink(missing_ok=True)
        content_length = int(response.headers.get("Content-Length") or 0)
        total = offset + content_length if content_length else spec.size
        if total and total > spec.size:
            raise ValueError("server announced an asset larger than the pinned size")
        mode = "ab" if offset else "wb"
        with partial.open(mode) as stream:
            downloaded = offset
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > spec.size:
                    raise ValueError("download exceeded the pinned size")
                stream.write(chunk)
                if progress:
                    progress(downloaded, spec.size)
            stream.flush()
            os.fsync(stream.fileno())


def validate_zip_members(path: Path, *, max_members: int = 100_000, max_uncompressed: int = 20 * 1024**3) -> None:
    """Reject traversal, links, absolute paths and decompression bombs."""
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if not members or len(members) > max_members:
            raise ValueError("ZIP member count is invalid")
        for member in members:
            name = member.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            mode = member.external_attr >> 16
            if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
                raise ValueError(f"unsafe ZIP path: {name}")
            if (mode & 0o170000) == 0o120000:
                raise ValueError(f"ZIP symbolic link is forbidden: {name}")
            total += member.file_size
            if total > max_uncompressed:
                raise ValueError("ZIP uncompressed content exceeds safety limit")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
