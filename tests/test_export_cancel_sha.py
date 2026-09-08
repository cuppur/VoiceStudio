from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from local_voice_studio.audio import sha256_file
from local_voice_studio.cover.errors import CoverError
from local_voice_studio.cover.exporting import CoverExporter
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db.sqlite")


def test_sha256_file_cancellation_has_uniform_internal_semantics(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (4 * 1024 * 1024))
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 1

    with pytest.raises(InterruptedError, match="任务已取消"):
        sha256_file(source, chunk_size=1024, cancel=cancel)


def test_export_sha_cancel_stops_before_ffprobe_or_staging(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    cover = CoverProject.create(paths.projects_root / "project", cover_id="c" * 32)
    source = cover.root / "generated" / "mix.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * (4 * 1024 * 1024))
    cover.add_asset(CoverAsset("mix", "final_mix", "generated/mix.wav", hashlib.sha256(source.read_bytes()).hexdigest(),
                              "ai_generated", "voicestudio_mixer"))
    cover.attest_rights(True)
    calls: list[str] = []

    def probe(path, *, cancel=None):
        calls.append(str(path))
        raise AssertionError("ffprobe must not start after SHA cancellation")

    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 1

    with pytest.raises(CoverError) as raised:
        CoverExporter(paths, backend=None, probe=probe).export(
            cover.root.parent.parent, cover.id, format="wav", destination=tmp_path / "exports",
            file_name="result", final_asset_id="mix", existing="replace", publication_rights_ack=True,
            cancel=cancel,
        )
    assert raised.value.code == "cover.export_cancelled"
    assert calls == []
    assert not list((tmp_path / "exports").glob("*.staging"))

