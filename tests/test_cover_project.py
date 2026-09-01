from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_voice_studio.cover import CoverProject


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
