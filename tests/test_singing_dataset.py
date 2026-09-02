from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest

from local_voice_studio.audio import sha256_file
from local_voice_studio.singing.dataset import SourceAssetDatasetBuilder, canonical_dataset_sha256


def wav(path: Path, seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(100)
        stream.writeframes(b"\x01\x00" * (100 * seconds))


def fixture(tmp_path: Path, seconds: int = 180):
    projects = tmp_path / "projects"; project = projects / "p"; raw = project / "raw"; raw.mkdir(parents=True)
    source = raw / "voice.wav"; wav(source, seconds); digest = sha256_file(source)
    profile = "profile-1"; asset_id = "asset-1"
    manifest = {"schema_version": 4, "project_uid": "project", "voice_profiles": [{"id": profile}], "source_assets": [{"id": asset_id, "profile_id": profile, "project_path": str(source), "original_path": str(source), "sha256": digest, "duration_seconds": float(seconds), "sample_rate": 100, "channels": 1, "codec": "pcm", "enabled": True, "duplicate_of": ""}]}
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    return projects, project, profile, asset_id


def test_builder_copies_registered_assets_and_records_canonical_lineage(tmp_path: Path):
    projects, project, profile, asset = fixture(tmp_path)
    result = SourceAssetDatasetBuilder(projects).build(project, profile, [asset])
    assert result.status == "warning"
    assert result.total_seconds == 180
    saved = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert saved["dataset_sha256"] == canonical_dataset_sha256(saved)
    assert saved["source_assets"][0]["id"] == asset
    copied = project / saved["source_assets"][0]["project_relative_path"]
    assert copied.is_file() and sha256_file(copied) == saved["source_assets"][0]["sha256"]
    again = SourceAssetDatasetBuilder(projects).build(project, profile, [asset])
    assert again.dataset_id != result.dataset_id
    assert again.dataset_sha256 == result.dataset_sha256


def test_builder_rejects_distinct_asset_ids_with_duplicate_content(tmp_path: Path):
    projects, project, profile, asset = fixture(tmp_path)
    value = json.loads((project / "project.json").read_text(encoding="utf-8"))
    duplicate = dict(value["source_assets"][0]); duplicate["id"] = "asset-2"
    value["source_assets"].append(duplicate)
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="内容重复"):
        SourceAssetDatasetBuilder(projects).build(project, profile, [asset, "asset-2"])


def test_builder_gate_statuses(tmp_path: Path):
    projects_short, project_short, profile_short, asset_short = fixture(tmp_path / "short", 179)
    with pytest.raises(ValueError, match="180"):
        SourceAssetDatasetBuilder(projects_short).build(project_short, profile_short, [asset_short])
    projects_180, project_180, profile_180, asset_180 = fixture(tmp_path / "edge", 180)
    assert SourceAssetDatasetBuilder(projects_180).build(project_180, profile_180, [asset_180]).status == "warning"
    projects, project, profile, asset = fixture(tmp_path, 600)
    result = SourceAssetDatasetBuilder(projects).build(project, profile, [asset])
    assert result.status == "sufficient"


def test_builder_rejects_outside_path_and_wrong_hash(tmp_path: Path):
    projects, project, profile, asset = fixture(tmp_path)
    outside = tmp_path / "outside.wav"; wav(outside, 180)
    value = json.loads((project / "project.json").read_text(encoding="utf-8"))
    value["source_assets"][0]["project_path"] = str(outside)
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError): SourceAssetDatasetBuilder(projects).build(project, profile, [asset])

    value["source_assets"][0]["project_path"] = str(project / "raw" / "voice.wav")
    value["source_assets"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        SourceAssetDatasetBuilder(projects).build(project, profile, [asset])


def test_builder_rejects_duplicate_disabled_and_stale_metadata(tmp_path: Path):
    projects, project, profile, asset = fixture(tmp_path)
    value = json.loads((project / "project.json").read_text(encoding="utf-8"))
    value["source_assets"][0]["enabled"] = False
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError): SourceAssetDatasetBuilder(projects).build(project, profile, [asset])
    value["source_assets"][0]["enabled"] = True
    value["source_assets"][0]["sample_rate"] = 44100
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="元数据"):
        SourceAssetDatasetBuilder(projects).build(project, profile, [asset])


def test_builder_rejects_cross_profile_and_non_source_asset_ids(tmp_path: Path):
    projects, project, profile, asset = fixture(tmp_path)
    value = json.loads((project / "project.json").read_text(encoding="utf-8"))
    value["voice_profiles"].append({"id": "profile-2"})
    (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="属于"):
        SourceAssetDatasetBuilder(projects).build(project, "profile-2", [asset])
    with pytest.raises(ValueError, match="属于"):
        SourceAssetDatasetBuilder(projects).build(project, profile, ["cover-vocal"])
