from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from local_voice_studio.cover import CoverAsset, CoverProject, CoverProjectError


def _asset(tmp_path: Path, *, role="vocal", origin="separated", name="vocal.wav", asset_id="a1"):
    root = tmp_path / "project"; cover = CoverProject.create(root, cover_id="b" * 32)
    path = cover.root / "stems" / name; path.parent.mkdir(exist_ok=True); path.write_bytes(b"audio")
    digest = hashlib.sha256(b"audio").hexdigest()
    return cover, CoverAsset(asset_id, role, f"stems/{name}", digest, origin, "test")


@pytest.mark.parametrize("role", ["original", "vocal", "instrumental", "ai_vocal"])
def test_cover_asset_valid_roles(tmp_path: Path, role: str):
    _, asset = _asset(tmp_path, role=role, origin={"original": "original", "vocal": "separated", "instrumental": "separated", "ai_vocal": "ai_generated"}[role])
    assert asset.role == role


def test_cover_asset_rejects_invalid_role_and_origin(tmp_path: Path):
    with pytest.raises(CoverProjectError): _asset(tmp_path, role="mix", asset_id="bad-role")
    with pytest.raises(CoverProjectError): _asset(tmp_path, origin="ai", asset_id="bad-origin")


@pytest.mark.parametrize("relative", ["../outside.wav", "/tmp/outside.wav", "C:/outside.wav"])
def test_cover_asset_rejects_path_escape(tmp_path: Path, relative: str):
    with pytest.raises(CoverProjectError): CoverAsset("escape", "vocal", relative, "a" * 64, "separated", "test")


def test_asset_hash_mismatch_and_repeated_ids(tmp_path: Path):
    cover, asset = _asset(tmp_path)
    asset.sha256 = "b" * 64
    with pytest.raises(CoverProjectError): cover.add_asset(asset)
    _, first = _asset(tmp_path / "other", asset_id="same")
    cover = CoverProject.create(tmp_path / "duplicate", cover_id="c" * 32)
    cover.add_asset(first)
    with pytest.raises(CoverProjectError): cover.add_asset(first)


def test_v1_migration_and_source_path_privacy(tmp_path: Path):
    project = tmp_path / "legacy"; cover_id = "d" * 32; root = project / "covers" / cover_id
    (root / "source").mkdir(parents=True); (root / "stems").mkdir()
    (root / "source" / "song.wav").write_bytes(b"source")
    (root / "stems" / "vocal.wav").write_bytes(b"vocal")
    (root / "stems" / "inst.wav").write_bytes(b"inst")
    payload = {"schema_version": 1, "id": cover_id, "source_path": "C:/Users/private/song.wav",
               "source_relative_path": "source/song.wav", "source_sha256": hashlib.sha256(b"source").hexdigest(),
               "vocal_path": "stems/vocal.wav", "instrumental_path": "stems/inst.wav"}
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    restored = CoverProject.load(project, cover_id)
    assert {asset.role for asset in restored.assets} == {"original", "vocal", "instrumental"}
    assert restored.original_source_name == "song.wav"
    assert "source_path" not in restored.to_dict()
    assert "private" not in json.dumps(restored.to_dict())


def test_get_asset_returns_latest_role_version(tmp_path: Path):
    cover = CoverProject.create(tmp_path / "project", cover_id="e" * 32)
    first = CoverAsset("v1", "ai_vocal", "generated/one.wav", "a" * 64, "ai_generated", "rvc")
    second = CoverAsset("v2", "ai_vocal", "generated/two.wav", "b" * 64, "ai_generated", "rvc")
    cover.add_asset(first, save=False); cover.add_asset(second, save=False)
    assert cover.get_asset("ai_vocal").id == "v2"
    assert cover.get_asset(role="v1").id == "v1"


def test_create_copy_hash_and_atomic_roundtrip(tmp_path: Path):
    original = tmp_path / "outside.wav"
    original.write_bytes("原始音频".encode())
    cover = CoverProject.create(tmp_path / "project", name="星光翻唱")
    copied = cover.copy_source(original)
    assert copied.parent == cover.root / "source"
    assert cover.source_audio_sha256 and cover.source_audio == "source/outside.wav"
    assert CoverProject.load(tmp_path / "project", cover.id).source_audio_sha256 == cover.source_audio_sha256
    assert not cover.manifest_path.with_suffix(".json.tmp").exists()


def test_paths_are_confined_and_output_hash_is_verifiable(tmp_path: Path):
    cover = CoverProject.create(tmp_path / "project")
    output = cover.root / "stems" / "vocals.wav"; output.write_bytes(b"vocal")
    other = cover.root / "stems" / "instrumental.wav"; other.write_bytes(b"instrumental")
    digest = cover.register_output(output, "vocal"); cover.register_output(other, "instrumental")
    assert cover.verify_outputs()
    output.write_bytes(b"tampered")
    assert not cover.verify_outputs()
    with pytest.raises(ValueError):
        cover.set_lyrics(tmp_path / "outside.lrc")


def test_rights_attestation_and_content_origin(tmp_path: Path):
    cover = CoverProject.create(tmp_path / "project")
    cover.content_origin = "separated"
    cover.attest_rights(version=2, confirmed_at="2026-08-31T00:00:00+00:00")
    restored = CoverProject.load(tmp_path / "project", cover.id)
    assert restored.rights_attestation_version == 2
    assert restored.rights_confirmed and restored.rights_confirmed_at.startswith("2026-08-31")
    assert restored.content_origin == "separated"


def test_old_project_without_covers_is_compatible(tmp_path: Path):
    project = tmp_path / "old-project"; project.mkdir()
    assert CoverProject.list(project) == []
    with pytest.raises(FileNotFoundError):
        CoverProject.load(project, "a" * 32)


def test_manifest_rejects_traversal(tmp_path: Path):
    project = tmp_path / "project"; cover_root = project / "covers" / ("a" * 32); cover_root.mkdir(parents=True)
    payload = CoverProject(str(project), id="a" * 32).to_dict(); payload["source_audio"] = "../../outside.wav"
    (cover_root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        CoverProject.load(project, "a" * 32)
