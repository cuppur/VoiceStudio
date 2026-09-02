"""Controlled, server-side dataset construction for singing training.

This module deliberately accepts only a project root and source-asset IDs.  It
does not trust caller supplied paths or hashes; all records are resolved from
the owning project manifest and copied into an immutable, project-owned view.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from ..audio import probe_audio, sha256_file
from ..models import is_hard_quality_flag
from ..paths import ensure_within, validate_id


MINIMUM_SECONDS = 180.0
WARNING_SECONDS = 600.0
BUILDER_VERSION = "singing-dataset-v1"


@dataclass(frozen=True)
class DatasetBuildResult:
    dataset_id: str
    profile_id: str
    status: str
    total_seconds: float
    dataset_sha256: str
    manifest_path: str
    source_asset_ids: tuple[str, ...]
    lineage: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "profile_id": self.profile_id,
            "status": self.status,
            "total_seconds": self.total_seconds,
            "dataset_sha256": self.dataset_sha256,
            "manifest_path": self.manifest_path,
            "source_asset_ids": list(self.source_asset_ids),
            "lineage": list(self.lineage),
        }


def canonical_dataset_sha256(manifest: dict[str, Any]) -> str:
    """Hash canonical dataset metadata and source digests, never local paths."""
    value = {key: manifest[key] for key in ("schema_version", "builder_version", "profile_id", "total_seconds", "gate")}
    value["source_assets"] = [
        {key: item[key] for key in ("id", "sha256", "duration_seconds", "sample_rate", "channels", "codec", "source_kind")}
        for item in manifest["source_assets"]
    ]
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SourceAssetDatasetBuilder:
    """Build a validated immutable dataset from registered SourceAssets."""

    def __init__(self, projects_root: Path):
        self.projects_root = Path(projects_root).resolve()

    def build(self, project: Path, profile_id: str, source_asset_ids: Iterable[str], cancel: Any = None) -> DatasetBuildResult:
        project = ensure_within(self.projects_root, Path(project))
        profile_id = validate_id(profile_id, legacy=True, field="profile_id")
        requested = [validate_id(item, legacy=True, field="source_asset_id") for item in source_asset_ids]
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("训练素材 ID 必须非空且不能重复")
        project_manifest_path = ensure_within(project, project / "project.json")
        if not project_manifest_path.is_file():
            raise ValueError("项目清单不存在")
        project_manifest = json.loads(project_manifest_path.read_text(encoding="utf-8"))
        profiles = [item for item in project_manifest.get("voice_profiles", []) if str(item.get("id")) == profile_id]
        if len(profiles) != 1:
            raise ValueError("目标声音配置不存在")
        registered_profiles = {str(item.get("id")) for item in project_manifest.get("voice_profiles", [])}
        assets = {str(item.get("id")): item for item in project_manifest.get("source_assets", []) if isinstance(item, dict)}
        selected: list[dict[str, Any]] = []
        selected_hashes: set[str] = set()
        for asset_id in requested:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise RuntimeError("任务已取消")
            asset = assets.get(asset_id)
            if asset is None or str(asset.get("profile_id")) != profile_id or str(asset.get("profile_id")) not in registered_profiles:
                raise ValueError("训练素材必须属于当前声音配置且已登记")
            if not asset.get("enabled", True) or asset.get("duplicate_of"):
                raise ValueError("训练素材未通过启用/重复校验")
            source = ensure_within(project / "raw", Path(str(asset.get("project_path", ""))))
            if not source.is_file():
                raise ValueError(f"训练素材不存在：{source.name}")
            declared_hash = str(asset.get("sha256", ""))
            actual_hash = sha256_file(source)
            if not declared_hash or actual_hash != declared_hash:
                raise ValueError(f"训练素材哈希不一致：{source.name}")
            if actual_hash in selected_hashes:
                raise ValueError("训练素材内容重复")
            selected_hashes.add(actual_hash)
            probe = probe_audio(source)
            if probe.duration_seconds <= 0 or any(is_hard_quality_flag(flag) or str(flag) == "clipping_risk" for flag in probe.quality_flags):
                raise ValueError(f"训练素材音频质量不合格：{source.name}")
            # Metadata is part of the trust boundary: stale manifest metadata is
            # rejected rather than silently copied into training lineage.
            for key in ("sample_rate", "channels"):
                if int(asset.get(key, 0)) != int(getattr(probe, key, 0)):
                    raise ValueError(f"训练素材音频元数据不一致：{source.name}")
            declared_codec = str(asset.get("codec", "unknown")).lower()
            actual_codec = str(probe.codec).lower()
            pcm_codecs = {"pcm", "pcm_s16le", "pcm_s24le", "pcm_s32le"}
            codec_match = declared_codec == actual_codec or (declared_codec in pcm_codecs and actual_codec in pcm_codecs)
            if not codec_match:
                raise ValueError(f"训练素材音频元数据不一致：{source.name}")
            if abs(float(asset.get("duration_seconds", 0)) - probe.duration_seconds) > 0.05:
                raise ValueError(f"训练素材时长元数据不一致：{source.name}")
            selected.append({"id": asset_id, "sha256": actual_hash, "duration_seconds": probe.duration_seconds, "sample_rate": probe.sample_rate, "channels": probe.channels, "codec": probe.codec})
        total = sum(float(item["duration_seconds"]) for item in selected)
        if total < MINIMUM_SECONDS:
            raise ValueError(f"训练素材不足 {MINIMUM_SECONDS:.0f} 秒")
        status = "sufficient" if total >= WARNING_SECONDS else "warning"
        dataset_id = uuid4().hex
        root = ensure_within(project / "datasets", project / "datasets" / "singing" / profile_id / dataset_id)
        audio_root = ensure_within(root, root / "audio")
        audio_root.mkdir(parents=True, exist_ok=False)
        lineage: list[dict[str, Any]] = []
        try:
            for item in selected:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    raise RuntimeError("任务已取消")
                source = ensure_within(project / "raw", Path(str(assets[item["id"]]["project_path"])))
                copied = audio_root / f"{item['sha256'][:16]}{source.suffix.lower() or '.wav'}"
                shutil.copy2(source, copied)
                if sha256_file(copied) != item["sha256"]:
                    raise ValueError("训练素材复制后哈希不一致")
                lineage.append({**item, "project_relative_path": copied.relative_to(project).as_posix(), "source_kind": str(assets[item["id"]].get("source_kind", "import"))})
            manifest = {"schema_version": 1, "builder_version": BUILDER_VERSION, "dataset_id": dataset_id, "profile_id": profile_id, "source_asset_ids": [item["id"] for item in lineage], "source_hashes": [item["sha256"] for item in lineage], "source_assets": lineage, "total_seconds": total, "gate": status, "created_at": datetime.now(timezone.utc).isoformat()}
            manifest["dataset_sha256"] = canonical_dataset_sha256(manifest)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return DatasetBuildResult(dataset_id, profile_id, status, total, manifest["dataset_sha256"], str(manifest_path), tuple(item["id"] for item in lineage), tuple(lineage))
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise


def build_dataset(project: Path, profile_id: str, source_asset_ids: Iterable[str], *, projects_root: Path) -> DatasetBuildResult:
    return SourceAssetDatasetBuilder(projects_root).build(project, profile_id, source_asset_ids)
