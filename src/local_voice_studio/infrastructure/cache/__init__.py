"""Content-addressed cache helpers shared by product pipelines."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ...audio import sha256_file
from ...product_models import CacheArtifact


def build_cache_key(*, source_sha256: str, operation: str, engine_version: str, model_version: str = "", model_sha256: str = "", parameters: dict[str, Any] | None = None, output_format: str = "") -> str:
    payload = {
        "source_sha256": source_sha256,
        "operation": operation,
        "engine_version": engine_version,
        "model_version": model_version,
        "model_sha256": model_sha256,
        "parameters": parameters or {},
        "output_format": output_format,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CacheStore:
    """Atomic, verifiable file cache; callers publish only completed files."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, operation: str, cache_key: str, suffix: str) -> Path:
        safe_operation = "".join(ch for ch in str(operation) if ch.isalnum() or ch in "-_" ) or "artifact"
        safe_suffix = suffix if suffix.startswith(".") else "." + suffix
        return self.root / safe_operation / (cache_key + safe_suffix)

    def is_valid(self, path: Path, expected_sha256: str = "") -> bool:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        return not expected_sha256 or sha256_file(path).lower() == expected_sha256.lower()

    def publish(self, source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + ".staging")
        staging.write_bytes(Path(source).read_bytes())
        digest = sha256_file(staging)
        staging.replace(destination)
        return digest

    def artifact(self, *, operation: str, cache_key: str, asset_id: str, source_sha256: str, engine_version: str, model_version: str = "", model_sha256: str = "", parameters_hash: str = "") -> CacheArtifact:
        return CacheArtifact(operation=operation, cache_key=cache_key, asset_id=asset_id, source_sha256=source_sha256, engine_version=engine_version, model_version=model_version, model_sha256=model_sha256, parameters_hash=parameters_hash)


