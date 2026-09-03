from __future__ import annotations

import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.cleanup import VocalCleanupService, VocalCleanupSettings
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db.sqlite")


class _Backend:
    id = "test-cleanup"
    version = "test-v1"
    model_sha256 = ""

    def __init__(self):
        self.calls = 0

    def cleanup(self, source: Path, output: Path, settings: VocalCleanupSettings, cancel=None) -> None:
        self.calls += 1
        with wave.open(str(source), "rb") as reader:
            params = reader.getparams(); frames = reader.readframes(reader.getnframes())
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as writer:
            writer.setparams(params); writer.writeframes(frames)


def _cover(tmp_path: Path):
    paths = _paths(tmp_path); paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "project"; project.mkdir()
    cover = CoverProject.create(project, cover_id="c" * 32)
    cover.attest_rights()
    vocal = cover.root / "stems" / "vocals.wav"
    with wave.open(str(vocal), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b"\x01\x00" * 16000)
    cover.set_stem("vocal", vocal)
    return paths, project, cover


def test_cleanup_creates_separated_lineage_and_reuses_cache(tmp_path):
    paths, project, cover = _cover(tmp_path); backend = _Backend()
    service = VocalCleanupService(project, paths=paths, backend=backend)
    settings = VocalCleanupSettings(denoise=True)
    first = service.cleanup(cover.id, settings)
    second = service.cleanup(cover.id, settings)
    restored = CoverProject.load(project, cover.id)
    asset = restored.get_asset(first["asset_id"])
    assert first["content_origin"] == "separated" and first["cache_hit"] is False
    assert second["cache_hit"] is True and backend.calls == 1
    assert asset and asset.source_asset_ids == ["vocal"] and asset.metadata["cleanup"]["denoise"] is True


def test_cleanup_rejects_disabled_settings(tmp_path):
    paths, project, cover = _cover(tmp_path)
    with pytest.raises(ValueError, match="未启用"):
        VocalCleanupService(project, paths=paths, backend=_Backend()).cleanup(cover.id, VocalCleanupSettings())
