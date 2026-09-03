"""Phase 4.1.1 export transaction and semantic Preview contracts."""
from __future__ import annotations

import hashlib
import json
import math
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.cancellation import CancellationToken
from local_voice_studio.cover.errors import AssetValidationError, CoverError, ExportConflictError
from local_voice_studio.cover.exporting import CoverExporter
from local_voice_studio.cover.models import CoverAssetRole
from local_voice_studio.cover.preview import PlaybackMode, PreviewMixPlanner, PreviewTrack, TrackRole
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.cover.mixing import CoverMixSettings
from local_voice_studio.paths import AppPaths


def _wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\0\0" * max(1, int(seconds * 48_000)) * 2)
    return path


def _valid_project(tmp_path: Path) -> tuple[AppPaths, CoverProject]:
    projects = tmp_path / "projects"
    project = CoverProject.create(projects / "demo", title="演示翻唱", cover_id="c" * 32)
    mix = _wav(project.root / "generated" / "mix" / "final.wav")
    digest = hashlib.sha256(mix.read_bytes()).hexdigest()
    project.add_asset(CoverAsset(
        "final", CoverAssetRole.FINAL_MIX, "generated/mix/final.wav", digest,
        "ai_generated", "voicestudio_mixer", model_id="model",
        source_asset_ids=["ai", "instrumental"],
        metadata={"profile_id": "profile", "settings": {"ai_gain_db": 6.0}},
    ))
    project.attest_rights()
    paths = AppPaths(tmp_path / "data", projects, tmp_path / "runtime", tmp_path / "engine",
                     tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3")
    return paths, project


class FakeExportBackend:
    def __init__(self, *, fail_format: str = "", cancel_format: str = "") -> None:
        self.fail_format = fail_format
        self.cancel_format = cancel_format
        self.calls: list[str] = []
        self.cancel_calls = 0

    def encode(self, source: Path, target: Path, *, format: str, cancel=None) -> Path:
        self.calls.append(format)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((b"RIFF" if format == "wav" else b"ID3") + format.encode())
        if format == self.cancel_format:
            raise InterruptedError("cancelled by fake backend")
        if format == self.fail_format:
            raise RuntimeError(f"fake {format} failure")
        return target

    def cancel(self) -> None:
        self.cancel_calls += 1


def _export(exporter: CoverExporter, project: CoverProject, destination: Path, **kwargs):
    return exporter.export(project.project_root, project.id, format="both", destination=destination,
                           file_name="result", final_asset_id="final", existing="replace",
                           publication_rights_ack=True, **kwargs)


def _transaction_files(destination: Path) -> list[Path]:
    return [*destination.glob("*.staging"), *destination.glob("*.voicestudio-backup")]


def test_export_cancel_uses_valid_project_and_cleans_transaction(tmp_path: Path) -> None:
    paths, project = _valid_project(tmp_path)
    destination = tmp_path / "exports"
    old = {name: (destination / name) for name in ("result.wav", "result.mp3", "result.voicestudio.json")}
    destination.mkdir()
    for path in old.values():
        path.write_bytes(b"old-" + path.name.encode())
    backend = FakeExportBackend(cancel_format="wav")
    exporter = CoverExporter(paths, backend=backend)

    with pytest.raises(CoverError) as raised:
        _export(exporter, project, destination)

    assert raised.value.code == "cover.export_cancelled"
    assert raised.value.recoverable is True
    assert backend.calls == ["wav"]
    assert all(path.read_bytes() == b"old-" + path.name.encode() for path in old.values())
    assert _transaction_files(destination) == []


def test_export_second_format_failure_rolls_back_without_partial_outputs(tmp_path: Path) -> None:
    paths, project = _valid_project(tmp_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    old = {name: (destination / name) for name in ("result.wav", "result.mp3", "result.voicestudio.json")}
    for path in old.values():
        path.write_bytes(b"old-" + path.name.encode())
    backend = FakeExportBackend(fail_format="mp3")
    exporter = CoverExporter(paths, backend=backend)

    with pytest.raises(RuntimeError, match="fake mp3 failure"):
        _export(exporter, project, destination)

    assert backend.calls == ["wav", "mp3"]
    assert all(path.read_bytes() == b"old-" + path.name.encode() for path in old.values())
    assert _transaction_files(destination) == []


def test_export_sidecar_failure_rolls_back_after_both_formats_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, project = _valid_project(tmp_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    old = {name: (destination / name) for name in ("result.wav", "result.mp3", "result.voicestudio.json")}
    for path in old.values():
        path.write_bytes(b"old-" + path.name.encode())
    backend = FakeExportBackend()
    exporter = CoverExporter(paths, backend=backend)

    original_write_text = Path.write_text

    def fail_sidecar(self: Path, data: str, *args, **kwargs):
        if self.name.endswith(".voicestudio.json.staging"):
            raise OSError("sidecar disk failure")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_sidecar)
    with pytest.raises(OSError, match="sidecar disk failure"):
        _export(exporter, project, destination)

    assert all(path.read_bytes() == b"old-" + path.name.encode() for path in old.values())
    assert _transaction_files(destination) == []


def test_export_rejects_existing_outputs_with_structured_conflict(tmp_path: Path) -> None:
    paths, project = _valid_project(tmp_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    (destination / "result.wav").write_bytes(b"old")
    with pytest.raises(ExportConflictError) as raised:
        CoverExporter(paths, backend=FakeExportBackend()).export(
            project.project_root, project.id, format="wav", destination=destination,
            file_name="result", final_asset_id="final", existing="reject",
            publication_rights_ack=True,
        )
    assert raised.value.code == "cover.export_conflict"


def test_export_asset_hash_mismatch_is_structured(tmp_path: Path) -> None:
    paths, project = _valid_project(tmp_path)
    project.assets[-1].sha256 = "0" * 64
    project.save()
    with pytest.raises(AssetValidationError) as raised:
        CoverExporter(paths, backend=FakeExportBackend()).export(
            project.project_root, project.id, format="wav", destination=tmp_path / "exports",
            file_name="result", final_asset_id="final", existing="replace",
            publication_rights_ack=True,
        )
    assert raised.value.code == "cover.asset_invalid"


def _preview_cover(tmp_path: Path):
    return type("Cover", (), {
        "root": tmp_path,
        "duration_ms": 10_000,
        "assets": [
            CoverAsset("inst", CoverAssetRole.INSTRUMENTAL, "inst.wav", "a" * 64, "separated", "uvr5"),
            CoverAsset("ai", CoverAssetRole.AI_VOCAL, "ai.wav", "b" * 64, "ai_generated", "rvc"),
            CoverAsset("vocal", CoverAssetRole.VOCAL, "vocal.wav", "c" * 64, "separated", "uvr5"),
        ],
    })()


def test_preview_positive_db_normalization_preserves_relative_ratio(tmp_path: Path) -> None:
    cover = _preview_cover(tmp_path)
    settings = CoverMixSettings(ai_gain_db=6.0, instrumental_gain_db=0.0).canonical()
    plan = PreviewMixPlanner().build(cover, settings=settings)
    assert plan.tracks[TrackRole.AI_VOCAL].gain == pytest.approx(1.0)
    assert plan.tracks[TrackRole.INSTRUMENTAL].gain == pytest.approx(10 ** (-6 / 20))
    assert plan.master_gain == pytest.approx(1.0)

    settings = CoverMixSettings(ai_gain_db=3.0, instrumental_gain_db=-6.0, original_vocal_gain_db="-inf").canonical()
    plan = PreviewMixPlanner().build(cover, settings=settings)
    assert plan.tracks[TrackRole.AI_VOCAL].gain / plan.tracks[TrackRole.INSTRUMENTAL].gain == pytest.approx(10 ** (9 / 20))


def test_preview_master_gain_keeps_track_ratio_and_mode_semantics(tmp_path: Path) -> None:
    cover = _preview_cover(tmp_path)
    settings = CoverMixSettings(ai_gain_db=0.0, instrumental_gain_db=0.0, master_gain_db=6.0).canonical()
    plan = PreviewMixPlanner().build(cover, settings=settings, selected_role=TrackRole.VOCAL)
    assert plan.tracks[TrackRole.AI_VOCAL].gain == pytest.approx(1.0)
    assert plan.tracks[TrackRole.INSTRUMENTAL].gain == pytest.approx(1.0)
    assert plan.mode is PlaybackMode.MIX_PREVIEW
    assert plan.selected_role is TrackRole.VOCAL
    assert {track.role for track in plan.active_tracks} == {TrackRole.AI_VOCAL, TrackRole.INSTRUMENTAL}

    selected = PreviewMixPlanner({TrackRole.AI_VOCAL: PreviewTrack(TrackRole.AI_VOCAL, "/ai.wav"),
                                  TrackRole.INSTRUMENTAL: PreviewTrack(TrackRole.INSTRUMENTAL, "/inst.wav")})
    selected_plan = selected.plan(TrackRole.AI_VOCAL, PlaybackMode.SOLO_TRACK)
    assert [track.role for track in selected_plan.active_tracks] == [TrackRole.AI_VOCAL]


def test_preview_mute_solo_and_legacy_role_parse() -> None:
    assert TrackRole is CoverAssetRole
    assert TrackRole.parse("ai-vocal") is CoverAssetRole.AI_VOCAL
    tracks = {
        TrackRole.AI_VOCAL: PreviewTrack(TrackRole.AI_VOCAL, "/ai.wav", muted=True),
        TrackRole.INSTRUMENTAL: PreviewTrack(TrackRole.INSTRUMENTAL, "/inst.wav", solo=True),
        TrackRole.VOCAL: PreviewTrack(TrackRole.VOCAL, "/vocal.wav"),
    }
    plan = PreviewMixPlanner(tracks).plan(TrackRole.VOCAL, PlaybackMode.MIX_PREVIEW)
    assert [track.role for track in plan.active_tracks] == [TrackRole.INSTRUMENTAL]


def test_cancellation_token_is_shared_export_contract() -> None:
    token = CancellationToken()
    assert not token.is_cancelled()
