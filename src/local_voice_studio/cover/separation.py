from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..audio import sha256_file
from ..paths import AppPaths, ensure_within, validate_sha256
from ..runtime import EngineRuntimeResolver
from .project import CoverProject

MODEL_NAME = "HP2_all_vocals.pth"
SEPARATOR_VERSION = "uvr5-hp2-v1"


@dataclass(frozen=True)
class UVR5RuntimeStatus:
    status: str
    model_path: Path | None = None
    model_sha256: str = ""
    error: str = ""

    @property
    def ready(self) -> bool: return self.status == "ready"

    @classmethod
    def detect(cls, paths: AppPaths | None = None) -> "UVR5RuntimeStatus":
        paths = paths or AppPaths.default()
        model = paths.engine_root / "tools" / "uvr5" / "uvr5_weights" / MODEL_NAME
        if not model.exists(): return cls("missing", model, error="UVR5 未安装")
        try:
            resolver = EngineRuntimeResolver(paths)
            manifest_path = resolver.bundle_root / "manifests" / "runtime-assets-v1.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pin = next((item for item in manifest.get("installed_file_pins", []) if item.get("id") == "uvr5-hp2"), None)
            if not pin:
                return cls("corrupt", model, error="UVR5 资产清单缺少固定 Hash")
            if not model.is_file() or model.stat().st_size != int(pin["size"]):
                return cls("corrupt", model, error="UVR5 文件损坏")
            actual = sha256_file(model)
            if actual != str(pin["sha256"]).lower():
                return cls("hash_mismatch", model, actual, error="UVR5 模型 Hash 不匹配")
            return cls("ready", model, actual)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return cls("corrupt", model, error="UVR5 文件损坏")


def _cancelled(cancel) -> bool:
    return bool(cancel() if callable(cancel) else cancel and cancel.is_set())


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt": subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else: process.terminate()


class SongSeparationPipeline:
    """Run UVR5 out-of-process and publish verified files into one CoverProject."""

    def __init__(self, project_path: Path, *, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.default()
        self.project_path = ensure_within(self.paths.projects_root, Path(project_path))
        self.process: subprocess.Popen | None = None

    def cancel(self) -> None:
        if self.process and self.process.poll() is None: _kill_tree(self.process)

    def separate(self, cover_id: str, source_relative_path: str, source_sha256: str, *, cancel=None,
                 progress: Callable[[float, str, str], None] | None = None) -> dict:
        runtime = UVR5RuntimeStatus.detect(self.paths)
        if not runtime.ready: raise RuntimeError(runtime.error)
        validate_sha256(source_sha256, field="source_sha256")
        cover = CoverProject.load(self.project_path, cover_id)
        if not cover.rights_confirmed:
            raise PermissionError("开始分离前必须确认歌曲处理与使用权利声明")
        source = ensure_within(cover.root, cover.root / source_relative_path)
        if source_relative_path != cover.source_relative_path or not source.is_file():
            raise ValueError("歌曲路径与 CoverProject 不匹配")
        actual_source_sha = sha256_file(source)
        if actual_source_sha != source_sha256 or actual_source_sha != cover.source_sha256:
            raise ValueError("歌曲输入文件 SHA-256 不匹配")
        cache_key = hashlib.sha256(f"{actual_source_sha}:uvr5:{SEPARATOR_VERSION}:{runtime.model_sha256}".encode()).hexdigest()
        if (cover.separation_status == "completed" and cover.separation_cache_key == cache_key
                and cover.separator == "uvr5" and cover.separator_model_sha256 == runtime.model_sha256
                and cover.verify_outputs()):
            return self._result(cover, actual_source_sha, runtime.model_sha256, cache_hit=True)
        if progress: progress(.05, "validating", "正在验证歌曲")
        cover.separation_status = "processing"; cover.save()
        staging = ensure_within(cover.root, cover.root / ".separation-staging")
        shutil.rmtree(staging, ignore_errors=True)
        input_dir, output_dir = staging / "input", staging / "output"
        input_dir.mkdir(parents=True); output_dir.mkdir()
        shutil.copy2(source, input_dir / source.name)
        try:
            if progress: progress(.15, "preparing_model", "正在准备 UVR5 模型")
            launch = EngineRuntimeResolver(self.paths).worker_launch()
            python = launch.program
            command = [str(python), "-m", "local_voice_studio.uvr_cli", "--engine", str(self.paths.engine_root),
                       "--input", str(input_dir), "--output", str(output_dir)]
            env = os.environ.copy(); env["CUDA_VISIBLE_DEVICES"] = "0"
            env["PYTHONPATH"] = str(launch.source_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            # UVR's CLI is not line-oriented while inference is running.  A
            # blocking readline here would make Cancel wait until the whole
            # song had finished, so keep the child quiet and poll its state.
            self.process = subprocess.Popen(command, cwd=str(self.paths.engine_root), env=env,
                                            stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if progress: progress(.25, "separating", "正在分离人声与伴奏")
            while self.process.poll() is None:
                if _cancelled(cancel):
                    self.cancel()
                    self.process.wait()
                    raise InterruptedError("歌曲分离已取消")
                if cancel is not None and hasattr(cancel, "wait"):
                    cancel.wait(.1)
                else:
                    import time
                    time.sleep(.1)
            if self.process.wait() != 0: raise RuntimeError("UVR5 分离进程失败")
            vocal = next((p for p in (output_dir / "vocal").glob("*.wav") if _valid_wav(p)), None)
            instrumental = next((p for p in (output_dir / "instrumental").glob("*.wav") if _valid_wav(p)), None)
            if not vocal or not instrumental: raise RuntimeError("UVR5 未生成完整的人声与伴奏")
            if progress: progress(.82, "generating_waveforms", "正在验证输出音轨")
            stems = cover.root / "stems"; stems.mkdir(exist_ok=True)
            final_vocal, final_instrumental = stems / "vocals.wav", stems / "instrumental.wav"
            for target in (final_vocal, final_instrumental):
                if target.exists(): target.unlink()
            os.replace(vocal, final_vocal); os.replace(instrumental, final_instrumental)
            # Register the stems through the schema-v2 asset API so provenance
            # and hashes are persisted together with the compatibility paths.
            cover.set_stem("vocal", final_vocal)
            cover.set_stem("instrumental", final_instrumental)
            cover.separator = "uvr5"; cover.separator_model_sha256 = runtime.model_sha256
            cover.separation_cache_key = cache_key; cover.separation_status = "completed"
            cover.output_paths = {"vocal": cover.vocal_path, "instrumental": cover.instrumental_path}
            cover.output_hashes = {"vocal": sha256_file(final_vocal), "instrumental": sha256_file(final_instrumental)}
            cover.save()
            if progress: progress(1.0, "saving_project", "分离结果已保存")
            return self._result(cover, actual_source_sha, runtime.model_sha256, cache_hit=False)
        except InterruptedError:
            cover.separation_status = "cancelled"; cover.save(); raise
        except Exception:
            cover.separation_status = "error"; cover.save(); raise
        finally:
            self.process = None; shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _result(cover: CoverProject, source_sha: str, model_sha: str, *, cache_hit: bool) -> dict:
        vocal = ensure_within(cover.root, cover.root / cover.vocal_path)
        instrumental = ensure_within(cover.root, cover.root / cover.instrumental_path)
        return {"vocal_path": str(vocal), "instrumental_path": str(instrumental), "separator": "uvr5",
                "separator_model_sha256": model_sha, "source_sha256": source_sha, "cache_hit": cache_hit,
                "content_origin": "separated",
                "vocal_sha256": cover.output_hashes["vocal"], "instrumental_sha256": cover.output_hashes["instrumental"]}


def _valid_wav(path: Path) -> bool:
    try:
        if path.stat().st_size <= 44:
            return False
        with path.open("rb") as stream:
            header = stream.read(12)
        return len(header) == 12 and header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE"
    except OSError:
        return False
