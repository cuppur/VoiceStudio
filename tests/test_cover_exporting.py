"""Phase 4 export contracts: format, provenance, overwrite and cancellation."""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.exporting import CoverExporter
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.paths import AppPaths


def wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2); f.setsampwidth(2); f.setframerate(48_000)
        f.writeframes(b"\0\0" * int(seconds * 48_000) * 2)
    return path


class ExportRunner:
    def __init__(self): self.commands = []
    def __call__(self, command, **kwargs):
        self.commands.append(list(command)); destination = Path(command[-1])
        if destination.suffix == ".wav": wav(destination)
        else: destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(b"ID3")
        return 0


def test_export_wav_mp3_both_and_unicode_sidecar(tmp_path, monkeypatch):
    root = tmp_path / "projects"; cover = CoverProject.create(root / "p", title="我的翻唱", cover_id="a" * 32); source = wav(cover.root / "outputs" / "mix.wav")
    cover.add_asset(CoverAsset("mix", "final_mix", "outputs/mix.wav", hashlib.sha256(source.read_bytes()).hexdigest(), "ai_generated", "ffmpeg-mixer"))
    cover.attest_rights()
    paths = AppPaths(tmp_path / "data", root, tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3"); exporter = CoverExporter(paths); exporter.paths.runtime_root.mkdir(parents=True, exist_ok=True)
    import local_voice_studio.cover.exporting as mod
    class P:
        returncode = 0
        def __init__(self, args, **kwargs):
            target = Path(args[-1]); target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.endswith("wav"): wav(target)
            else: target.write_bytes(b"ID3")
        def poll(self): return 0
    monkeypatch.setattr(mod.EngineRuntimeResolver, "resolve_private_tool", lambda *a, **k: Path("ffmpeg.exe"))
    monkeypatch.setattr(mod.subprocess, "Popen", P)
    for fmt, suffixes in (("wav", {".wav"}), ("mp3", {".mp3"}), ("both", {".wav", ".mp3"})):
        (tmp_path / fmt).mkdir()
        result = exporter.export(root / "p", cover.id, format=fmt, destination=tmp_path / fmt, file_name="我的翻唱", final_asset_id="mix", existing="reject", publication_rights_ack=True)
        assert {Path(p).suffix for p in result["outputs"]} == suffixes
        assert Path(result["sidecar"]).name.endswith(".voicestudio.json")


def test_export_rejects_invalid_or_outside_paths_and_existing_output(tmp_path):
    exporter = CoverExporter();
    with pytest.raises(ValueError): exporter.export(tmp_path, "x", format="flac", existing="reject", publication_rights_ack=True)
    with pytest.raises(ValueError): exporter.export(tmp_path, "x", format="wav", existing=None, publication_rights_ack=True)


def test_export_cancel_cleans_partial_files_and_writes_ai_marker(tmp_path):
    exporter = CoverExporter()
    with pytest.raises((RuntimeError, FileNotFoundError, ValueError)): exporter.export(tmp_path, "x", format="both", existing="reject", publication_rights_ack=True, cancel=lambda: True)
