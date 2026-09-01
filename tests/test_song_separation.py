from __future__ import annotations
import io
from types import SimpleNamespace
from pathlib import Path
import pytest
from local_voice_studio.cover import CoverProject
from local_voice_studio.cover.separation import MODEL_NAME, SongSeparationPipeline, UVR5RuntimeStatus
from local_voice_studio.paths import AppPaths

def setup_case(tmp_path: Path):
    data = tmp_path / "data"
    paths = AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db")
    project = paths.projects_root / "p1"; project.mkdir(parents=True)
    cover = CoverProject.create(project, cover_id="c1")
    source = tmp_path / "song.wav"; source.write_bytes(b"RIFF" + b"x" * 100)
    cover.copy_source(source)
    cover.attest_rights(True)
    model = paths.engine_root / "tools" / "uvr5" / "uvr5_weights" / MODEL_NAME
    model.parent.mkdir(parents=True); model.write_bytes(b"PK" + b"m" * (1024 * 1024))
    return paths, project, cover

def test_runtime_missing_and_corrupt(tmp_path: Path):
    paths, _, _ = setup_case(tmp_path); model = paths.engine_root / "tools" / "uvr5" / "uvr5_weights" / MODEL_NAME
    model.unlink(); assert UVR5RuntimeStatus.detect(paths).status == "missing"
    model.touch(); assert UVR5RuntimeStatus.detect(paths).status == "corrupt"

class FakeProcess:
    def __init__(self, command, **kwargs):
        out = Path(command[command.index("--output") + 1]); (out / "vocal").mkdir(parents=True); (out / "instrumental").mkdir()
        (out / "vocal" / "vocal_song.wav").write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"v" * 100); (out / "instrumental" / "instrument_song.wav").write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"i" * 100)
        self.pid = 1; self.stdout = io.StringIO(""); self.returncode = 0
    def poll(self): return self.returncode
    def wait(self): return self.returncode
    def communicate(self): return "", ""

def test_cache_and_tamper_repair(tmp_path: Path, monkeypatch):
    paths, project, cover = setup_case(tmp_path)
    monkeypatch.setattr("local_voice_studio.cover.separation.EngineRuntimeResolver.worker_launch", lambda self: SimpleNamespace(program=Path("python.exe"), source_root=Path("src")))
    monkeypatch.setattr("local_voice_studio.cover.separation.subprocess.Popen", FakeProcess)
    pipeline = SongSeparationPipeline(project, paths=paths)
    first = pipeline.separate(cover.id, cover.source_relative_path, cover.source_sha256); assert first["cache_hit"] is False
    assert pipeline.separate(cover.id, cover.source_relative_path, cover.source_sha256)["cache_hit"] is True
    (cover.root / "stems" / "vocals.wav").unlink()
    repaired = pipeline.separate(cover.id, cover.source_relative_path, cover.source_sha256)
    assert repaired["cache_hit"] is False
    (cover.root / "stems" / "instrumental.wav").write_bytes(b"tampered")
    repaired = pipeline.separate(cover.id, cover.source_relative_path, cover.source_sha256)
    assert repaired["cache_hit"] is False

def test_hash_and_path_validation(tmp_path: Path):
    paths, project, cover = setup_case(tmp_path); pipeline = SongSeparationPipeline(project, paths=paths)
    with pytest.raises(ValueError): pipeline.separate(cover.id, cover.source_relative_path, "0" * 64)
    with pytest.raises(ValueError): pipeline.separate(cover.id, "../outside.wav", cover.source_sha256)

def test_rights_gate_is_enforced_in_pipeline(tmp_path: Path):
    paths, project, cover = setup_case(tmp_path); cover.attest_rights(False)
    with pytest.raises(PermissionError, match="权利声明"):
        SongSeparationPipeline(project, paths=paths).separate(cover.id, cover.source_relative_path, cover.source_sha256)

def test_mp3_input_uses_same_verified_pipeline(tmp_path: Path, monkeypatch):
    paths, project, _ = setup_case(tmp_path); cover = CoverProject.create(project, cover_id="mp3"); cover.attest_rights(True)
    source = tmp_path / "song.mp3"; source.write_bytes(b"ID3" + b"x" * 100); cover.copy_source(source)
    monkeypatch.setattr("local_voice_studio.cover.separation.EngineRuntimeResolver.worker_launch", lambda self: SimpleNamespace(program=Path("python.exe"), source_root=Path("src")))
    monkeypatch.setattr("local_voice_studio.cover.separation.subprocess.Popen", FakeProcess)
    assert SongSeparationPipeline(project, paths=paths).separate(cover.id, cover.source_relative_path, cover.source_sha256)["content_origin"] == "separated"

def test_cancel_marks_manifest_and_cleans_staging(tmp_path: Path, monkeypatch):
    paths, project, cover = setup_case(tmp_path)
    class Running(FakeProcess):
        def poll(self): return None
    monkeypatch.setattr("local_voice_studio.cover.separation.EngineRuntimeResolver.worker_launch", lambda self: SimpleNamespace(program=Path("python.exe"), source_root=Path("src")))
    monkeypatch.setattr("local_voice_studio.cover.separation.subprocess.Popen", Running)
    monkeypatch.setattr("local_voice_studio.cover.separation._kill_tree", lambda process: setattr(process, "returncode", -1))
    with pytest.raises(InterruptedError):
        SongSeparationPipeline(project, paths=paths).separate(cover.id, cover.source_relative_path, cover.source_sha256, cancel=lambda: True)
    restored = CoverProject.load(project, cover.id)
    assert restored.separation_status == "cancelled"
    assert not (restored.root / ".separation-staging").exists()
