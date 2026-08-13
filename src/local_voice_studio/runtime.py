from __future__ import annotations

import os
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import AppPaths


class EngineRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerLaunch:
    program: Path
    arguments: list[str]
    source_root: Path


@dataclass(frozen=True)
class RuntimeIntegrity:
    valid: bool
    errors: tuple[str, ...]
    manifest: dict | None = None


class EngineRuntimeResolver:
    """Resolve the real private interpreter. A frozen GUI executable is never Python."""

    def __init__(self, paths: AppPaths, frozen: bool | None = None, executable: str | None = None, bundle_root: Path | None = None):
        self.paths = paths
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.executable = Path(executable or sys.executable)
        self.bundle_root = bundle_root or Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))

    def candidates(self) -> list[Path]:
        return [
            self.paths.runtime_root / "env" / "python.exe",
            self.paths.runtime_root / "python.exe",
            self.paths.data_root / "engine" / "venv" / "Scripts" / "python.exe",
        ]

    def resolve_private_python(self) -> Path:
        for candidate in self.candidates():
            if candidate.is_file():
                return candidate.resolve()
        if not self.frozen and self.executable.is_file():
            return self.executable.resolve()
        raise EngineRuntimeError("本地引擎尚未安装完整。请进入“设置”并点击“安装/修复本地引擎”。")

    def worker_launch(self) -> WorkerLaunch:
        python = self.resolve_private_python()
        if self.frozen and python == self.executable.resolve():
            raise EngineRuntimeError("打包程序不能作为 GPU 工作进程解释器，请修复本地引擎。")
        source = self.bundle_root / "worker_source" if self.frozen else Path(__file__).resolve().parents[1]
        return WorkerLaunch(python, ["-X", "utf8", "-u", "-m", "local_voice_studio.worker"], source)

    def resolve_private_tool(self, name: str) -> Path | None:
        """Resolve only application-owned media tools; never consult PATH."""
        executable = name if name.lower().endswith(".exe") else f"{name}.exe"
        candidates = (
            self.paths.data_root / "tools" / executable,
            self.paths.runtime_root / "env" / "Library" / "bin" / executable,
            self.paths.engine_root / executable,
            self.paths.engine_root / "tools" / executable,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def verify_install_manifest(self) -> RuntimeIntegrity:
        path = self.paths.runtime_root / "install-manifest.json"
        if not path.is_file():
            return RuntimeIntegrity(False, ("安装清单不存在",))
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return RuntimeIntegrity(False, (f"安装清单无法读取: {exc}",))
        if manifest.get("schema_version") != 2:
            return RuntimeIntegrity(False, ("安装清单版本过旧，需要一键修复",), manifest)
        errors: list[str] = []
        for field in ("asset_manifest_version", "engine_commit", "pretrained_revision"):
            if not manifest.get(field):
                errors.append(f"安装清单缺少字段: {field}")
        manifest_digest = str(manifest.get("asset_manifest_sha256", "")).lower()
        if len(manifest_digest) != 64 or any(ch not in "0123456789abcdef" for ch in manifest_digest):
            errors.append("资产清单摘要无效")
        lockfiles = manifest.get("lockfiles")
        if not isinstance(lockfiles, dict) or any(
            len(str((lockfiles.get(name) or {}).get("sha256", ""))) != 64 for name in ("conda", "pip")
        ):
            errors.append("依赖锁摘要缺失或无效")
        verified_files = manifest.get("verified_files")
        if not isinstance(verified_files, list) or not verified_files:
            errors.append("安装清单没有已验证文件")
            verified_files = []
        for item in verified_files:
            relative = Path(str(item.get("path", "")))
            expected = str(item.get("sha256", "")).lower()
            size = item.get("size")
            if relative.is_absolute() or ".." in relative.parts or len(expected) != 64:
                errors.append("安装清单含不安全或不完整的文件记录")
                continue
            target = (self.paths.data_root / relative).resolve()
            if self.paths.data_root.resolve() not in target.parents:
                errors.append(f"文件记录越界: {relative}")
            elif not target.is_file():
                errors.append(f"文件缺失: {relative}")
            elif size is not None and target.stat().st_size != int(size):
                errors.append(f"文件大小异常: {relative}")
            elif _sha256(target) != expected:
                errors.append(f"文件摘要异常: {relative}")
        return RuntimeIntegrity(not errors, tuple(errors), manifest)


def utf8_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
