from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from local_voice_studio.cover.errors import CoverError
from local_voice_studio.cover.exporting import CoverExporter
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine",
                    data / "models", data / "logs", data / "db.sqlite3")


class FakeExportBackend:
    def __init__(self, *, fail_format: str | None = None, cancel_after: str | None = None):
        self.fail_format = fail_format
        self.cancel_after = cancel_after

    def encode(self, source: Path, target: Path, *, format: str, cancel=None) -> Path:
        if format == self.fail_format:
            raise RuntimeError("injected encoder failure")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((format.upper() + "-TEST").encode())
        if format == self.cancel_after and cancel is not None:
            source = getattr(cancel, "_source", None)
            if hasattr(source, "set"):
                source.set()
        return target

    def cancel(self) -> None:
        return None


def _project(tmp_path: Path) -> tuple[AppPaths, CoverProject, Path]:
    paths = _paths(tmp_path)
    cover = CoverProject.create(paths.projects_root / "project", title="测试翻唱", cover_id="c" * 32)
    source = cover.root / "generated" / "mix.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"valid-final-mix")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    cover.add_asset(CoverAsset("mix", "final_mix", "generated/mix.wav", digest, "ai_generated", "voicestudio_mixer"))
    cover.attest_rights(True)
    return paths, cover, source


def _exporter(paths: AppPaths, backend: FakeExportBackend) -> CoverExporter:
    return CoverExporter(paths, backend=backend)


def test_export_cancel_cleans_staging_and_preserves_existing_outputs(tmp_path: Path) -> None:
    paths, cover, _ = _project(tmp_path)
    destination = tmp_path / "exports"; destination.mkdir()
    old = {name: (destination / name) for name in ("测试.wav", "测试.mp3", "测试.voicestudio.json")}
    for path in old.values(): path.write_bytes(("old-" + path.name).encode())
    backend = FakeExportBackend(cancel_after="wav")
    with pytest.raises(CoverError) as raised:
        _exporter(paths, backend).export(cover.root.parent.parent, cover.id, format="both", file_name="测试",
                                          destination=destination, existing="replace",
                                          publication_rights_ack=True)
    assert raised.value.code == "cover.export_cancelled"
    assert all(path.read_bytes().startswith(b"old-") for path in old.values())
    assert not list(destination.glob("*.staging"))
    assert not list(destination.glob("*.voicestudio-backup"))


def test_export_second_format_failure_rolls_back_transaction(tmp_path: Path) -> None:
    paths, cover, _ = _project(tmp_path)
    destination = tmp_path / "exports"; destination.mkdir()
    old = {name: (destination / name) for name in ("测试.wav", "测试.mp3", "测试.voicestudio.json")}
    for path in old.values(): path.write_bytes(("old-" + path.name).encode())
    with pytest.raises(RuntimeError, match="injected"):
        _exporter(paths, FakeExportBackend(fail_format="mp3")).export(
            cover.root.parent.parent, cover.id, format="both", file_name="测试", destination=destination,
            existing="replace", publication_rights_ack=True,
        )
    assert all(path.read_bytes().startswith(b"old-") for path in old.values())
    assert not list(destination.glob("*.staging"))
    assert not list(destination.glob("*.voicestudio-backup"))


def test_export_sidecar_failure_rolls_back_before_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, cover, _ = _project(tmp_path)
    destination = tmp_path / "exports"; destination.mkdir()
    old = {name: (destination / name) for name in ("测试.wav", "测试.voicestudio.json")}
    for path in old.values(): path.write_bytes(("old-" + path.name).encode())
    original_write_text = Path.write_text

    def fail_sidecar(path: Path, data: str, *args, **kwargs):
        if path.name.endswith(".voicestudio.json.staging"):
            raise OSError("injected sidecar failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_sidecar)
    with pytest.raises(OSError, match="injected"):
        _exporter(paths, FakeExportBackend()).export(
            cover.root.parent.parent, cover.id, format="wav", file_name="测试", destination=destination,
            existing="replace", publication_rights_ack=True,
        )
    assert all(path.read_bytes().startswith(b"old-") for path in old.values())
    assert not list(destination.glob("*.staging"))
    assert not list(destination.glob("*.voicestudio-backup"))


def test_export_service_contains_no_process_or_encoder_fallback() -> None:
    source = Path("src/local_voice_studio/cover/exporting/service.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "libmp3lame" not in source
    assert "pcm_s16le" not in source
