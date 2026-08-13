from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_voice_studio.audio import scan_audio_files, sha256_file
from local_voice_studio.engine import safe_torch_load
from local_voice_studio.models import (
    DatasetDraftSegment,
    DatasetSegment,
    GenerationRecord,
    ModelVersion,
    ReferenceSelector,
    TrainingWorkflow,
)
from local_voice_studio.paths import AppPaths, ensure_within, validate_id, validate_sha256
from local_voice_studio.storage import StudioStore


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db.sqlite3")


def test_identifier_hash_and_path_boundaries(tmp_path: Path) -> None:
    assert validate_id("a" * 32) == "a" * 32
    assert validate_id("legacy-profile.1", legacy=True) == "legacy-profile.1"
    assert validate_sha256("0" * 64) == "0" * 64
    for invalid in ("../x", "C:relative", "\\server\\share", "a/b", "x\x00y"):
        with pytest.raises(ValueError): validate_id(invalid, legacy=True)
    with pytest.raises(ValueError): ensure_within(tmp_path, tmp_path / ".." / "escape")


def test_schema_v3_migrates_without_touching_snapshots(tmp_path: Path) -> None:
    paths = _paths(tmp_path); store = StudioStore(paths)
    project = paths.projects_root / "old"; (project / "datasets" / "snap").mkdir(parents=True)
    snapshot = project / "datasets" / "snap" / "manifest.json"
    snapshot.write_text('{"schema_version":2,"sentinel":"unchanged"}', encoding="utf-8")
    before = sha256_file(snapshot)
    (project / "project.json").write_text(json.dumps({
        "schema_version": 3, "id": "old", "name": "old", "voice_profiles": [],
        "source_assets": [], "dataset_snapshots": [], "workflows": [],
    }), encoding="utf-8")
    value = store.load_project(project)
    assert value["schema_version"] == 4
    assert len(value["project_uid"]) == 32
    assert value["generation_records"] == []
    assert sha256_file(snapshot) == before


def test_legacy_model_migration_is_two_phase_without_torch(tmp_path: Path) -> None:
    paths = _paths(tmp_path); store = StudioStore(paths)
    project = paths.projects_root / "old-model"; checkpoints = project / "checkpoints"; checkpoints.mkdir(parents=True)
    gpt = checkpoints / "g.ckpt"; sovits = checkpoints / "s.pth"; gpt.write_bytes(b"gpt"); sovits.write_bytes(b"sovits")
    (project / "project.json").write_text(json.dumps({
        "schema_version": 3, "id": "old-model", "name": "old", "source_assets": [], "dataset_snapshots": [], "workflows": [],
        "voice_profiles": [{"id": "voice-1", "name": "voice", "consent_confirmed": True,
            "active_gpt_checkpoint": str(gpt), "active_sovits_checkpoint": str(sovits),
            "active_model_version_id": "version-1", "model_versions": [{"id": "version-1", "gpt_checkpoint": str(gpt), "sovits_checkpoint": str(sovits)}]}],
    }), encoding="utf-8")
    profile = store.load_project(project)["voice_profiles"][0]
    version = profile["model_versions"][0]
    assert version["trust_status"] == "legacy-pending"
    assert version["gpt_sha256"] == sha256_file(gpt)
    assert profile["active_model_trust_status"] == "legacy-pending"


def test_generation_record_and_step_results_are_persistent(tmp_path: Path) -> None:
    paths = _paths(tmp_path); store = StudioStore(paths); project = store.create_project("demo")
    manifest = store.load_project(project)
    workflow = TrainingWorkflow("profile-1", "demo", step_results={"importing": {"status": "completed"}})
    store.save_workflow(project, workflow)
    assert store.load_workflow(project, workflow.id).step_results["importing"]["status"] == "completed"
    record = GenerationRecord(manifest["project_uid"], "profile-1", "hello", status="completed")
    store.save_generation_record(project, record)
    assert store.list_generation_records(project)[0].text == "hello"


def test_quality_override_and_reference_selection() -> None:
    hard = DatasetDraftSegment("a.wav", 0, 7, text="hello", quality_flags=["invalid_audio"], override_reason="听过了")
    warning = DatasetDraftSegment("b.wav", 0, 7, text="hello", quality_flags=["low_loudness"])
    assert not hard.eligible
    assert not warning.eligible
    warning.override_reason = "人工试听可用"
    assert warning.eligible
    segments = [
        DatasetSegment("1" * 64, "a.wav", 0, 5, text="short", id="b"),
        DatasetSegment("2" * 64, "b.wav", 0, 7, text="clear reference sentence", id="a"),
        DatasetSegment("3" * 64, "c.wav", 0, 7, text="music", quality_flags=["BGM"], id="c"),
    ]
    assert ReferenceSelector.select(segments).id == "a"


def test_model_version_hash_and_project_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"; root = project / "checkpoints"; root.mkdir(parents=True)
    gpt = root / "g.ckpt"; sovits = root / "s.pth"; gpt.write_bytes(b"g"); sovits.write_bytes(b"s")
    version = ModelVersion(gpt_checkpoint=str(gpt), sovits_checkpoint=str(sovits), gpt_sha256=sha256_file(gpt), sovits_sha256=sha256_file(sovits), trust_status="verified")
    StudioStore.verify_model_version(project, version)
    sovits.write_bytes(b"changed")
    with pytest.raises(ValueError): StudioStore.verify_model_version(project, version)


def test_safe_torch_load_blocks_code_and_supports_custom_header(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    marker = tmp_path / "owned.txt"

    class Payload:
        def __reduce__(self):
            return (Path.write_text, (marker, "owned"))

    hostile = tmp_path / "hostile.ckpt"
    torch.save(Payload(), hostile)
    with pytest.raises(Exception): safe_torch_load(hostile)
    assert not marker.exists()
    normal = tmp_path / "normal.pth"; torch.save({"weight": torch.tensor([1])}, normal)
    data = normal.read_bytes(); normal.write_bytes(b"VS" + data[2:])
    loaded = safe_torch_load(normal)
    assert loaded["weight"].item() == 1


def test_audio_scan_limits_and_cancel(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"; audio.write_bytes(b"not mp3")
    with pytest.raises(ValueError): scan_audio_files([audio], max_file_bytes=1)
    with pytest.raises(RuntimeError): scan_audio_files([audio], cancel=lambda: True)
