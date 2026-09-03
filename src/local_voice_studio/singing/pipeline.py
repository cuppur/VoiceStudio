"""Safe product orchestration for singing-model training and vocal conversion.

The ML implementation is injected as an engine.  This module owns project
manifests, consent, hashes and atomic publication; it never imports torch.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cover.project import CoverProject, CoverAsset, RIGHTS_ATTESTATION_TEXT_HASH, content_origin
from ..paths import AppPaths, ensure_within, validate_id, validate_sha256
from ..runtime import EngineRuntimeResolver
from .models import RVCInferenceSettings, SingingModelVersion
from .dataset import SourceAssetDatasetBuilder
from .verification import SingingModelVerifier, validate_wav_quality


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_wav(path: Path, *, reference: Path, cancel: Any = None) -> None:
    result = validate_wav_quality(path, reference=reference, cancel=cancel)
    if not result.ok:
        if "音频质量校验已取消" in result.errors:
            raise RuntimeError("任务已取消")
        raise RuntimeError("转换输出质量校验失败：" + "；".join(result.errors))


class SingingPipeline:
    def __init__(self, engine: Any, *, projects_root: Path | None = None, paths: AppPaths | None = None, progress: Callable[[float, str], None] | None = None):
        self.engine = engine
        self.projects_root = Path(projects_root).resolve() if projects_root is not None else None
        self.paths = paths
        self.progress = progress or (lambda _value, _message: None)

    def _project(self, payload: Mapping[str, Any]) -> Path:
        project = Path(str(payload.get("project_path", ""))).resolve()
        if self.projects_root is not None:
            project = ensure_within(self.projects_root, project)
        if not project.is_dir():
            raise ValueError("项目目录不存在")
        return project

    def _verification_clip(self, snapshot: Any, project: Path, cancel: Any = None) -> Path:
        supported = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}
        candidate = next((item for item in snapshot.lineage if 3.0 <= float(item.get("duration_seconds", 0)) and Path(str(item.get("project_relative_path", ""))).suffix.lower() in supported), None)
        if candidate is None:
            raise RuntimeError("授权训练快照中没有可用于模型验证的 3 秒以上音频")
        source = ensure_within(project, project / str(candidate["project_relative_path"]))
        output = ensure_within(Path(snapshot.manifest_path).parent, Path(snapshot.manifest_path).parent / "verification-input.wav")
        if source.suffix.lower() == ".wav":
            with wave.open(str(source), "rb") as reader:
                rate = reader.getframerate(); frames = min(reader.getnframes(), int(rate * 5.0))
                if rate <= 0 or frames < rate * 3:
                    raise RuntimeError("模型验证输入不足 3 秒")
                with wave.open(str(output), "wb") as writer:
                    writer.setparams((reader.getnchannels(), reader.getsampwidth(), rate, 0, reader.getcomptype(), reader.getcompname()))
                    writer.writeframes(reader.readframes(frames))
            return output
        ffmpeg = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg") if self.paths else None
        if ffmpeg is None:
            raise RuntimeError("非 WAV 验证输入需要受信任的 FFmpeg")
        process = subprocess.Popen([str(ffmpeg), "-v", "error", "-i", str(source), "-t", "5", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", "-y", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                output.unlink(missing_ok=True)
                raise RuntimeError("任务已取消")
            time.sleep(.05)
        if process.returncode:
            error = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace")
            output.unlink(missing_ok=True)
            raise RuntimeError("模型验证音频解码失败: " + error[-300:])
        with wave.open(str(output), "rb") as reader:
            if reader.getframerate() <= 0 or reader.getnframes() < reader.getframerate() * 3:
                output.unlink(missing_ok=True)
                raise RuntimeError("模型验证输入不足 3 秒")
        return output

    @staticmethod
    def _manifest(project: Path) -> tuple[dict[str, Any], Path]:
        path = ensure_within(project, project / "project.json")
        if not path.is_file():
            raise ValueError("项目清单不存在")
        return json.loads(path.read_text(encoding="utf-8")), path

    @staticmethod
    def _save_manifest(value: dict[str, Any], path: Path) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _profile(self, project: Path, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path]:
        profile_id = validate_id(str(payload.get("profile_id", "")), legacy=True, field="profile_id")
        manifest, path = self._manifest(project)
        profile = next((item for item in manifest.get("voice_profiles", []) if str(item.get("id")) == profile_id), None)
        if not profile:
            raise ValueError("目标声音配置不存在")
        if profile.get("archived"):
            raise ValueError("归档声音配置不可用于 AI 翻唱")
        if profile.get("consent_confirmed") is not True:
            raise ValueError("目标声音尚未确认本人或授权使用")
        if not profile.get("consent_confirmed_at") or not profile.get("consent_record"):
            raise ValueError("目标声音授权记录不完整")
        return manifest, profile, path

    def train(self, payload: Mapping[str, Any], cancel: Any = None) -> dict[str, Any]:
        project = self._project(payload)
        manifest, profile, manifest_path = self._profile(project, payload)
        forbidden = {"dataset_dir", "training_dataset_sha256", "checkpoint_path", "index_path", "source_assets", "training_source_asset_ids"}
        supplied = sorted(key for key in forbidden if key in payload)
        if supplied:
            raise ValueError("客户端不得提供受控训练字段: " + ", ".join(supplied))
        source_ids = payload.get("source_asset_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError("歌唱模型训练至少需要一个 SourceAsset")
        if self.projects_root is None:
            raise ValueError("SingingPipeline 必须配置 projects_root")
        self.progress(0.03, "验证授权素材")
        snapshot = SourceAssetDatasetBuilder(self.projects_root).build(project, profile["id"], source_ids, cancel=cancel)
        run_id = validate_id(str(payload.get("training_run_id", "")), legacy=True, field="training_run_id")
        staging = ensure_within(project, project / "models" / "singing" / profile["id"] / (run_id + ".staging"))
        final = ensure_within(project, project / "models" / "singing" / profile["id"] / run_id)
        if final.exists():
            raise ValueError("训练运行目录已存在")
        staging.mkdir(parents=True, exist_ok=False)
        final_persisted = False
        try:
            self.progress(0.08, "构建训练数据")
            dataset_dir = Path(snapshot.manifest_path).parent / "audio"
            outputs = self.engine.train({**dict(payload), "experiment_dir": str(staging), "dataset_dir": str(dataset_dir), "training_dataset_sha256": snapshot.dataset_sha256}, cancel=cancel)
            if cancel is not None and cancel.is_set():
                raise RuntimeError("任务已取消")
            paths = [ensure_within(staging, Path(p)) for p in outputs]
            # Engines may return only the experiment directory; discover the
            # immutable outputs beneath staging as a defensive fallback.
            if not paths:
                paths = [p for p in staging.rglob("*") if p.is_file()]
            checkpoints = [p for p in paths if p.is_file() and p.suffix.lower() in {".pth", ".pt", ".ckpt"}]
            if not checkpoints:
                raise RuntimeError("训练未生成检查点")
            checkpoint = next((p for p in checkpoints if p.name.lower() == "model.pth"), checkpoints[0])
            index = next((p for p in paths if p.is_file() and p.suffix.lower() == ".index"), None)
            if index is None:
                raise RuntimeError("训练未生成 RVC Index")
            checkpoint_rel = checkpoint.relative_to(staging).as_posix()
            model = SingingModelVersion(
                profile_id=profile["id"], engine=str(payload.get("engine", "rvc")),
                engine_version=str(payload.get("engine_version", "")),
                checkpoint_relative_path=(Path("models/singing") / profile["id"] / run_id / checkpoint_rel).as_posix(),
                checkpoint_sha256=_sha256(checkpoint),
                index_relative_path=((Path("models/singing") / profile["id"] / run_id / index.relative_to(staging)).as_posix() if index else ""),
                index_sha256=_sha256(index), training_dataset_sha256=snapshot.dataset_sha256,
                training_dataset_id=snapshot.dataset_id,
                training_source_asset_ids=list(snapshot.source_asset_ids), training_lineage=list(snapshot.lineage),
                trust_status="unverified",
            )
            shutil.move(str(staging), str(final))
            checkpoint = ensure_within(final, final / checkpoint_rel)
            index = ensure_within(final, final / index.relative_to(staging))
            verification_input = self._verification_clip(snapshot, project, cancel)
            self.progress(0.9, "验证歌唱模型")
            verification = SingingModelVerifier(self.engine).verify(checkpoint, index, verification_input, final, cancel=cancel)
            if cancel is not None and cancel.is_set():
                shutil.rmtree(final, ignore_errors=True)
                raise RuntimeError("任务已取消")
            model.trust_status = "verified" if verification.ok else "verification_failed"
            profile.setdefault("singing_models", []).append(model.to_dict())
            if verification.ok:
                profile["active_singing_model_id"] = model.id
                profile["training_state"] = "trained_singing_model"
            else:
                if profile.get("active_singing_model_id") == model.id: profile["active_singing_model_id"] = ""
                profile["training_state"] = "singing_verification_failed"
            self._save_manifest(manifest, manifest_path)
            final_persisted = True
            if not verification.ok:
                raise RuntimeError("训练完成，但模型验证失败：" + "; ".join(verification.errors))
            self.progress(1.0, "歌唱模型训练完成并已验证")
            return {**model.to_dict(), "verification": verification.to_dict(), "dataset": snapshot.to_dict()}
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if final.exists() and not final_persisted:
                shutil.rmtree(final, ignore_errors=True)
            raise

    def convert(self, payload: Mapping[str, Any], cancel: Any = None) -> dict[str, Any]:
        project = self._project(payload)
        supplied = sorted(key for key in {"source_path", "model_path", "output_path", "content_origin"} if key in payload)
        if supplied:
            raise ValueError("客户端不得提供受控转换字段: " + ", ".join(supplied))
        manifest, profile, _ = self._profile(project, payload)
        cover = CoverProject.load(project, str(payload.get("cover_id", "")))
        if not cover.rights_confirmed or not cover.rights_confirmed_at or cover.rights_attestation_version != 1 or cover.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH:
            raise ValueError("开始 AI 翻唱前必须确认歌曲处理权利")
        source = cover.get_asset(role="vocal")
        if source is None or source.content_origin != "separated":
            raise ValueError("翻唱输入必须是已分离的人声轨")
        source_path = ensure_within(cover.root, cover.root / source.relative_path)
        if not source_path.is_file() or not source.sha256 or _sha256(source_path) != source.sha256:
            raise ValueError("输入人声资产缺失或 Hash 不匹配")
        model_id = str(payload.get("singing_model_id") or profile.get("active_singing_model_id", ""))
        model_data = next((x for x in profile.get("singing_models", []) if str(x.get("id")) == model_id), None)
        if not model_data:
            raise ValueError("未选择可用歌唱模型")
        model = SingingModelVersion.from_dict(model_data)
        if model.profile_id != profile["id"] or model.trust_status != "verified" or not model.files_available(project) or not model.hashes_match(project):
            raise ValueError("歌唱模型不可用或未通过完整性验证")
        settings = RVCInferenceSettings.from_payload(dict(payload))
        cache_material = json.dumps({
            "inference": settings.canonical(),
            "source_vocal": {
                "sha256": source.sha256,
                "cleanup_engine": source.producer if source.producer != "separation" else "",
                "cleanup_engine_version": source.producer_version if source.producer != "separation" else "",
                "cleanup_model_sha256": source.model_sha256 if source.producer != "separation" else "",
            },
        }, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256((source.sha256 + model.checkpoint_sha256 + model.index_sha256 + model.engine_version + cache_material).encode()).hexdigest()
        cached = next((a for a in reversed(cover.assets) if a.role == "ai_vocal" and a.content_origin == "ai_generated" and a.producer == "rvc_v2" and a.source_asset_ids == [source.id] and a.model_id == model.id and a.model_sha256 == model.checkpoint_sha256 and a.producer_version == cache_key), None)
        if cached:
            cached_path = ensure_within(cover.root, cover.root / cached.relative_path)
            if cached_path.is_file() and _sha256(cached_path) == cached.sha256:
                try: _validate_wav(cached_path, reference=source_path, cancel=cancel)
                except RuntimeError: pass
                else: return {"output_path": str(cached_path), "output_sha256": cached.sha256, "content_origin": "ai_generated", "asset_id": cached.id, "cache_hit": True}
        output_id = validate_id(str(payload.get("output_id", "ai-vocal-" + cache_key[:16])), legacy=True, field="output_id")
        output = ensure_within(cover.root, cover.root / "generated" / "ai-vocal" / (output_id + ".wav"))
        staging = ensure_within(cover.root, cover.root / "generated" / "ai-vocal" / (output_id + ".staging.wav"))
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ValueError("输出资产已存在")
        self.progress(0.1, "准备 AI 人声转换")
        try:
            produced = ensure_within(cover.root, Path(self.engine.convert({**dict(payload), **settings.to_payload(), "input_path": str(source_path), "model_path": str(ensure_within(project, project / model.checkpoint_relative_path)), "index_path": str(ensure_within(project, project / model.index_relative_path)), "output_path": str(staging)}, cancel=cancel)))
            if cancel is not None and cancel.is_set():
                raise RuntimeError("任务已取消")
            if produced != staging or not staging.is_file() or not staging.stat().st_size:
                raise RuntimeError("转换未生成有效输出")
            _validate_wav(staging, reference=source_path, cancel=cancel)
            staging.replace(output)
            asset = CoverAsset(id=output_id, role="ai_vocal", relative_path=output.relative_to(cover.root).as_posix(), sha256=_sha256(output), content_origin="ai_generated", producer="rvc_v2", producer_version=cache_key, model_id=model.id, model_sha256=model.checkpoint_sha256, source_asset_ids=[source.id])
            cover.add_asset(asset)
            return {"output_path": str(output), "output_sha256": asset.sha256, "content_origin": "ai_generated", "asset_id": asset.id, "cache_hit": False}
        except Exception:
            staging.unlink(missing_ok=True)
            raise
