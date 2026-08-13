from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


APP_DIR_NAME = "LocalVoiceStudio"
UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
LEGACY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def _documents() -> Path:
    configured = os.environ.get("LOCAL_VOICE_STUDIO_PROJECTS")
    return Path(configured) if configured else Path.home() / "Documents" / APP_DIR_NAME


@dataclass(frozen=True)
class AppPaths:
    data_root: Path
    projects_root: Path
    runtime_root: Path
    engine_root: Path
    models_root: Path
    logs_root: Path
    database: Path

    @classmethod
    def default(cls) -> "AppPaths":
        data_root = Path(os.environ.get("LOCAL_VOICE_STUDIO_HOME", _local_app_data() / APP_DIR_NAME))
        return cls(
            data_root=data_root,
            projects_root=_documents(),
            runtime_root=data_root / "runtime",
            engine_root=data_root / "engines" / "GPT-SoVITS",
            models_root=data_root / "models",
            logs_root=data_root / "logs",
            database=data_root / "studio.sqlite3",
        )

    def ensure(self) -> None:
        for path in (
            self.data_root,
            self.projects_root,
            self.runtime_root,
            self.engine_root.parent,
            self.models_root,
            self.logs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def private_python(self) -> Path:
        windows = self.runtime_root / "env" / "python.exe"
        posix = self.runtime_root / "env" / "bin" / "python"
        return windows if sys.platform == "win32" else posix


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve a path and reject traversal outside *root*."""
    root = root.resolve()
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"路径超出允许目录: {candidate}")
    return candidate


def validate_id(value: str, *, legacy: bool = False, field: str = "id") -> str:
    value = str(value)
    pattern = LEGACY_ID_RE if legacy else UUID_HEX_RE
    if not pattern.fullmatch(value):
        expected = "安全的字母数字、点、下划线或连字符" if legacy else "32 位小写 UUID hex"
        raise ValueError(f"{field} 必须是{expected}")
    return value


def validate_sha256(value: str, *, field: str = "sha256", allow_empty: bool = False) -> str:
    value = str(value)
    if allow_empty and not value:
        return value
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256")
    return value


def safe_child(root: Path, *parts: str, legacy_ids: bool = False) -> Path:
    """Build an owned path from validated identifiers only."""
    checked = [validate_id(part, legacy=legacy_ids, field="path id") for part in parts]
    return ensure_within(root, root.joinpath(*checked))
