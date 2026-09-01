"""Safe product orchestration for singing-model training and vocal conversion.

The ML implementation is injected as an engine.  This module owns project
manifests, consent, hashes and atomic publication; it never imports torch.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cover.project import CoverProject, CoverAsset, content_origin
from ..paths import ensure_within, validate_id, validate_sha256
from .models import SingingModelVersion


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as stream:
            if stream.getnframes() <= 0 or stream.getframerate() <= 0 or stream.getnchannels() <= 0:
                raise ValueError
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise RuntimeError("转换未生成有效 WAV 音频") from exc


class SingingPipeline:
    def __init__(self, engine: Any, *, progress: Callable[[float, str], None] | None = None):
        self.engine = engine
        self.progress = progress or (lambda _value, _message: None)

    def _project(self, payload: Mapping[str, Any]) -> Path:
        project = Path(str(payload.get("project_path", ""))).resolve()
        if not project.is_dir():
            raise ValueError("项目目录不存在")
        return project

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
        dataset_hash_value = str(payload.get("training_dataset_sha256", ""))
        # UI callers may provide a frozen source-asset selection instead of a
        # precomputed hash; derive the snapshot digest from immutable asset
        # hashes.  Never accept an arbitrary path list as a dataset snapshot.
        supplied_assets = payload.get("source_assets")
        if supplied_assets is not None:
            if not isinstance(supplied_assets, list) or not supplied_assets:
                raise ValueError("歌唱模型训练至少需要一个声音素材")
            manifest_assets = {str(a.get("id")): a for a in manifest.get("source_assets", []) if isinstance(a, dict)}
            for item in supplied_assets:
                if not isinstance(item, dict): raise ValueError("训练素材记录无效")
                asset = manifest_assets.get(str(item.get("id")))
                if asset is None or str(asset.get("profile_id")) != profile["id"]:
                    raise ValueError("训练素材必须属于当前声音配置")
                if not asset.get("enabled", True) or asset.get("duplicate_of"):
                    raise ValueError("训练素材未通过启用/重复校验")
            if not dataset_hash_value:
                digest = hashlib.sha256("".join(sorted(str(a.get("sha256", "")) for a in supplied_assets)).encode()).hexdigest()
                dataset_hash_value = digest
        dataset_hash = validate_sha256(dataset_hash_value, field="training_dataset_sha256")
        run_id = validate_id(str(payload.get("training_run_id", "")), legacy=True, field="training_run_id")
        staging = ensure_within(project, project / "models" / "singing" / profile["id"] / (run_id + ".staging"))
        final = ensure_within(project, project / "models" / "singing" / profile["id"] / run_id)
        if final.exists():
            raise ValueError("训练运行目录已存在")
        staging.mkdir(parents=True, exist_ok=False)
        try:
            self.progress(0.05, "准备歌唱模型训练")
            outputs = self.engine.train({**dict(payload), "experiment_dir": str(staging), "dataset_dir": str(payload.get("dataset_dir", ""))}, cancel=cancel)
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
            checkpoint = checkpoints[0]
            index = next((p for p in paths if p.is_file() and p.suffix.lower() == ".index"), None)
            verifier = getattr(self.engine, "verify_model", None)
            if callable(verifier) and not verifier(checkpoint, index):
                raise RuntimeError("训练模型未通过安全加载验证")
            checkpoint_rel = checkpoint.relative_to(staging).as_posix()
            model = SingingModelVersion(
                profile_id=profile["id"], engine=str(payload.get("engine", "rvc")),
                engine_version=str(payload.get("engine_version", "")),
                checkpoint_relative_path=(Path("models/singing") / profile["id"] / run_id / checkpoint_rel).as_posix(),
                checkpoint_sha256=_sha256(checkpoint),
                index_relative_path=((Path("models/singing") / profile["id"] / run_id / index.relative_to(staging)).as_posix() if index else ""),
                index_sha256=_sha256(index) if index else "", training_dataset_sha256=dataset_hash,
                training_source_asset_ids=[str(x) for x in payload.get("training_source_asset_ids", [])],
            )
            shutil.move(str(staging), str(final))
            profile.setdefault("singing_models", []).append(model.to_dict())
            profile["active_singing_model_id"] = model.id
            profile["training_state"] = "trained_singing_model"
            self._save_manifest(manifest, manifest_path)
            self.progress(1.0, "歌唱模型训练完成")
            return model.to_dict()
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def convert(self, payload: Mapping[str, Any], cancel: Any = None) -> dict[str, Any]:
        project = self._project(payload)
        manifest, profile, _ = self._profile(project, payload)
        cover = CoverProject.load(project, str(payload.get("cover_id", "")))
        if not cover.rights_confirmed:
            raise ValueError("开始 AI 翻唱前必须确认歌曲处理权利")
        source = cover.get_asset(role="vocal")
        if source is None:
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
        pitch = int(payload.get("pitch_shift", payload.get("transpose", 0)))
        if pitch < -12 or pitch > 12: raise ValueError("变调必须在 -12 到 +12 半音之间")
        cache_key = hashlib.sha256((source.sha256 + model.checkpoint_sha256 + model.index_sha256 + model.engine_version + str(pitch) + str(payload.get("inference_settings", "rmvpe"))).encode()).hexdigest()
        cached = next((a for a in reversed(cover.assets) if a.role == "ai_vocal" and a.model_id == model.id and a.model_sha256 == model.checkpoint_sha256 and a.producer_version == cache_key), None)
        if cached:
            cached_path = ensure_within(cover.root, cover.root / cached.relative_path)
            if cached_path.is_file() and _sha256(cached_path) == cached.sha256:
                return {"output_path": str(cached_path), "output_sha256": cached.sha256, "content_origin": "ai_generated", "asset_id": cached.id, "cache_hit": True}
        output_id = validate_id(str(payload.get("output_id", "ai-vocal-" + cache_key[:16])), legacy=True, field="output_id")
        output = ensure_within(cover.root, cover.root / "generated" / "ai-vocal" / (output_id + ".wav"))
        staging = ensure_within(cover.root, cover.root / "generated" / "ai-vocal" / (output_id + ".staging.wav"))
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ValueError("输出资产已存在")
        self.progress(0.1, "准备 AI 人声转换")
        try:
            produced = ensure_within(cover.root, Path(self.engine.convert({**dict(payload), "input_path": str(source_path), "model_path": str(ensure_within(project, project / model.checkpoint_relative_path)), "output_path": str(staging)}, cancel=cancel)))
            if cancel is not None and cancel.is_set():
                raise RuntimeError("任务已取消")
            if produced != staging or not staging.is_file() or not staging.stat().st_size:
                raise RuntimeError("转换未生成有效输出")
            _validate_wav(staging)
            staging.replace(output)
            asset = CoverAsset(id=output_id, role="ai_vocal", relative_path=output.relative_to(cover.root).as_posix(), sha256=_sha256(output), content_origin="ai_generated", producer="rvc_v2", producer_version=cache_key, model_id=model.id, model_sha256=model.checkpoint_sha256, source_asset_ids=[source.id])
            cover.add_asset(asset)
            return {"output_path": str(output), "output_sha256": asset.sha256, "content_origin": "ai_generated", "asset_id": asset.id}
        except Exception:
            staging.unlink(missing_ok=True)
            raise
