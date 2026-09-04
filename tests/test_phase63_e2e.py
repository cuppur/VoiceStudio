"""Phase 6.3 end-to-end cover workflow tests.

These tests drive the real WorkerService command dispatch, the real
CoverApplicationService trust boundary, the real CoverProject manifest and
six-state stage machine, and the real CoverMixer/CoverExporter validation
logic.  Only the heavy media backends (UVR5 separation, RVC inference and
FFmpeg) are replaced by deterministic fakes, mirroring how the worker
resolves them in production.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import wave
from pathlib import Path

import pytest

from local_voice_studio.audio import sha256_file
from local_voice_studio.models import SingingModelVersion, VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.protocol import Message
from local_voice_studio.storage import StudioStore
from local_voice_studio.worker import WorkerService
from local_voice_studio.cover.project import CoverProject


def _paths(tmp_path: Path) -> AppPaths:
    root = tmp_path / "data"
    return AppPaths(root, tmp_path / "projects", root / "runtime", root / "engine",
                    root / "models", root / "logs", root / "db")


def _wav(path: Path, seconds: float = 1.0, rate: int = 48000, channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes(b"\x00\x00\x00\x00" * int(rate * seconds))


def _real_project(tmp_path: Path):
    """Create a project with a consented voice profile and a verified singing model."""
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    store = StudioStore(paths)
    project = store.create_project("e2e-cover")
    checkpoint = project / "models" / "singing" / "profile" / "run" / "model.pth"
    checkpoint.parent.mkdir(parents=True); checkpoint.write_bytes(b"checkpoint-bytes")
    index = checkpoint.with_suffix(".index"); index.write_bytes(b"index-bytes")
    profile = VoiceProfile("授权声音", True, id="profile", consent_record="本人授权",
                           consent_confirmed_at="now")
    model = SingingModelVersion(
        profile_id=profile.id, engine="rvc_v2",
        checkpoint_relative_path=checkpoint.relative_to(project).as_posix(),
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        index_relative_path=index.relative_to(project).as_posix(),
        index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
        trust_status="verified",
    )
    profile.singing_models = [model]; profile.active_singing_model_id = model.id
    store.save_profile(project, profile)
    return paths, store, project, profile


def _cover_with_source(project: Path, tmp_path: Path) -> CoverProject:
    original = tmp_path / "song.wav"; _wav(original, seconds=2.0)
    cover = CoverProject.create(project, title="e2e-song")
    cover.copy_source(original)
    cover.attest_rights()
    return cover


class _EventCollector:
    def __init__(self, service: WorkerService):
        self.service = service
        self.events: list[tuple[str, str, dict]] = []

    def __call__(self, request_id: str, event: str, payload: dict) -> None:
        self.events.append((request_id, event, payload))


def _run(service: WorkerService, command: str, payload: dict) -> list[tuple[str, str, dict]]:
    service.emit = _EventCollector(service)
    service.handle(Message(command, payload))
    if service.current_thread is not None:
        service.current_thread.join(10)
    return service.emit.events


def _wav_probe(path: Path, cancel=None):
    with wave.open(str(path), "rb") as stream:
        return {
            "duration_seconds": stream.getnframes() / max(1, stream.getframerate()),
            "sample_rate": stream.getframerate(),
            "channels": stream.getnchannels(),
            "codec": "pcm",
            "bit_rate": stream.getframerate() * stream.getnchannels() * stream.getsampwidth() * 8,
        }


class _ProbeExporter:
    """CoverExporter wrapper that injects the pure-Python WAV probe."""

    def __init__(self, paths, *, backend=None):
        from local_voice_studio.cover.exporting import CoverExporter
        self._real = CoverExporter(paths, backend=backend, probe=_wav_probe)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _make_fake_singing(project: Path, cover: CoverProject):
    """Fake SingingPipeline.convert writing a real ai_vocal WAV asset."""

    class FakeSingingPipeline:
        def __init__(self, engine, *, projects_root=None, paths=None, progress=None):
            self.engine = engine

        def convert(self, payload, cancel=None):
            from local_voice_studio.cover.models import ContentOrigin, CoverAssetRole
            from local_voice_studio.cover.project import CoverAsset
            current = CoverProject.load(project, cover.id)
            vocal = current.get_asset(role="vocal")
            assert vocal is not None, "AI vocal requires a separated vocal asset"
            output = cover.root / "generated" / "ai-vocal" / "ai-vocal-test.wav"
            _wav(output, seconds=2.0)
            cover.add_asset(CoverAsset(
                id="ai-vocal-test", role=CoverAssetRole.AI_VOCAL.value,
                relative_path=output.relative_to(cover.root).as_posix(),
                sha256=sha256_file(output), content_origin=ContentOrigin.AI_GENERATED.value,
                producer="rvc_v2", producer_version="cache-test",
                model_id=str(payload.get("singing_model_id", "")), source_asset_ids=[vocal.id],
            ))
            return {"output_path": str(output), "output_sha256": sha256_file(output),
                    "content_origin": "ai_generated", "asset_id": "ai-vocal-test",
                    "cache_hit": False}

        def cancel(self):
            pass

    return FakeSingingPipeline(object())


def _install_fakes(monkeypatch, tmp_path: Path, project: Path, cover: CoverProject):
    """Swap the heavy backends for deterministic fakes writing real files."""
    import local_voice_studio.worker as worker_module
    from local_voice_studio.cover.mixing.backend import AudioRenderResult

    class FakeSeparation:
        def __init__(self, project_root, *, paths=None):
            pass

        def separate(self, cover_id, source_relative_path, source_sha256, *, engine_id="uvr5",
                     cancel=None, progress=None):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise InterruptedError("歌曲分离已取消")
            vocal = cover.root / "stems" / "vocals.wav"
            instrumental = cover.root / "stems" / "instrumental.wav"
            _wav(vocal, seconds=2.0); _wav(instrumental, seconds=2.0)
            cover.set_stem("vocal", vocal); cover.set_stem("instrumental", instrumental)
            cover.separation_status = "completed"; cover.save()
            return {"vocal_path": str(vocal), "instrumental_path": str(instrumental),
                    "separator": "uvr5", "separator_version": "test",
                    "separator_model_sha256": "a" * 64, "source_sha256": cover.source_sha256,
                    "cache_hit": False, "content_origin": "separated",
                    "vocal_sha256": sha256_file(vocal), "instrumental_sha256": sha256_file(instrumental)}

        def cancel(self):
            pass

    class FakeMixBackend:
        def __init__(self, ffmpeg):
            self.ffmpeg = ffmpeg

        def render(self, inputs, settings, staging_path, *, duration_seconds=None, cancel=None):
            _wav(staging_path, seconds=2.0)
            return AudioRenderResult(staging_path, float(duration_seconds or 2.0))

        def cancel(self):
            pass

    class FakeExportBackend:
        def __init__(self, ffmpeg):
            self.ffmpeg = ffmpeg

        def encode(self, source: Path, target: Path, *, format: str, cancel=None):
            if format == "wav":
                shutil.copy2(source, target)
            else:
                target.write_bytes(b"ID3" + b"\x00" * 16)
            return target

        def cancel(self):
            pass

    monkeypatch.setattr(worker_module, "SongSeparationPipeline", FakeSeparation)
    monkeypatch.setattr(worker_module, "FFmpegMixBackend", FakeMixBackend)
    monkeypatch.setattr(worker_module, "FFmpegExportBackend", FakeExportBackend)
    monkeypatch.setattr(
        worker_module.EngineRuntimeResolver, "resolve_private_tool",
        lambda self, name: tmp_path / "fake-tools" / ("ffmpeg.exe" if name == "ffmpeg" else "ffprobe.exe"),
    )
    monkeypatch.setattr(worker_module, "CoverExporter", _ProbeExporter)


def test_e2e_cover_full_flow_a(tmp_path, monkeypatch):
    """Flow A: import -> separate -> AI vocal -> mix -> export, all stages completed."""
    paths, store, project, profile = _real_project(tmp_path)
    cover = _cover_with_source(project, tmp_path)
    service = WorkerService(paths, singing_engine=object())
    _install_fakes(monkeypatch, tmp_path, project, cover)
    service._singing = lambda request_id: _make_fake_singing(project, cover)

    events = _run(service, "separate_song",
                  {"project_path": str(project), "cover_id": cover.id, "mode": "uvr5"})
    assert any(event == "result" for _, event, _ in events), events
    assert CoverProject.load(project, cover.id).separation_status == "completed"
    assert CoverProject.load(project, cover.id).get_asset(role="vocal") is not None

    events = _run(service, "convert_vocal",
                  {"project_path": str(project), "cover_id": cover.id, "profile_id": profile.id,
                   "singing_model_id": profile.active_singing_model_id, "pitch_shift": 0,
                   "inference_settings": {}})
    assert any(event == "result" for _, event, _ in events), events
    assert CoverProject.load(project, cover.id).ai_vocal_status == "completed"
    assert CoverProject.load(project, cover.id).get_asset(role="ai_vocal") is not None

    events = _run(service, "render_cover",
                  {"project_path": str(project), "cover_id": cover.id, "profile_id": profile.id,
                   "singing_model_id": profile.active_singing_model_id, "mix_settings": {}})
    assert any(event == "result" for _, event, _ in events), events
    assert CoverProject.load(project, cover.id).mix_status == "completed"
    final = CoverProject.load(project, cover.id).get_asset(role="final_mix")
    assert final is not None

    destination = tmp_path / "exports"; destination.mkdir()
    events = _run(service, "export_cover",
                  {"project_path": str(project), "cover_id": cover.id,
                   "final_asset_id": final.id, "format": "wav",
                   "file_name": "我的翻唱_AI_VoiceStudio", "destination": str(destination),
                   "existing_policy": "reject", "publication_rights_acknowledged": True})
    assert any(event == "result" for _, event, _ in events), events
    assert CoverProject.load(project, cover.id).export_status == "completed"
    assert (destination / "我的翻唱_AI_VoiceStudio.wav").is_file()
    assert (destination / "我的翻唱_AI_VoiceStudio.voicestudio.json").is_file()


def test_e2e_cover_cancel_marks_stage_cancelled(tmp_path, monkeypatch):
    """Flow C: cancellation during separation must persist stage=cancelled."""
    paths, store, project, profile = _real_project(tmp_path)
    cover = _cover_with_source(project, tmp_path)
    service = WorkerService(paths, singing_engine=object())
    _install_fakes(monkeypatch, tmp_path, project, cover)

    import local_voice_studio.worker as worker_module

    entered = threading.Event()
    release = threading.Event()

    class SlowSeparation:
        def __init__(self, project_root, *, paths=None):
            pass

        def separate(self, cover_id, source_relative_path, source_sha256, *, engine_id="uvr5",
                     cancel=None, progress=None):
            entered.set()
            while not release.is_set():
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    raise InterruptedError("歌曲分离已取消")
                time.sleep(0.01)
            raise AssertionError("separation should have been cancelled")

        def cancel(self):
            pass

    monkeypatch.setattr(worker_module, "SongSeparationPipeline", SlowSeparation)
    collector = _EventCollector(service)
    service.emit = collector
    service.handle(Message("separate_song",
                           {"project_path": str(project), "cover_id": cover.id, "mode": "uvr5"}))
    request_id = service.current_request_id
    assert entered.wait(5), "separation never entered the fake pipeline"
    service.handle(Message("cancel", {"target_request_id": request_id}))
    release.set()
    if service.current_thread is not None:
        service.current_thread.join(10)
    assert any(event == "error" for _, event, _ in collector.events), collector.events
    assert CoverProject.load(project, cover.id).separation_status == "cancelled"


def test_e2e_cover_stress_five_runs(tmp_path, monkeypatch):
    """Stress: the full happy path must succeed five times in a row."""
    for run in range(5):
        paths, store, project, profile = _real_project(tmp_path / f"run{run}")
        cover = _cover_with_source(project, tmp_path)
        service = WorkerService(paths, singing_engine=object())
        _install_fakes(monkeypatch, tmp_path, project, cover)
        service._singing = lambda request_id: _make_fake_singing(project, cover)

        events = _run(service, "separate_song",
                      {"project_path": str(project), "cover_id": cover.id, "mode": "uvr5"})
        assert any(event == "result" for _, event, _ in events), events
        events = _run(service, "convert_vocal",
                      {"project_path": str(project), "cover_id": cover.id, "profile_id": profile.id,
                       "singing_model_id": profile.active_singing_model_id, "pitch_shift": 0,
                       "inference_settings": {}})
        assert any(event == "result" for _, event, _ in events), events
        events = _run(service, "render_cover",
                      {"project_path": str(project), "cover_id": cover.id, "profile_id": profile.id,
                       "singing_model_id": profile.active_singing_model_id, "mix_settings": {}})
        assert any(event == "result" for _, event, _ in events), events
        final = CoverProject.load(project, cover.id).get_asset(role="final_mix")
        assert final is not None
        destination = tmp_path / f"exports{run}"; destination.mkdir()
        events = _run(service, "export_cover",
                      {"project_path": str(project), "cover_id": cover.id,
                       "final_asset_id": final.id, "format": "wav",
                       "file_name": f"歌曲{run}", "destination": str(destination),
                       "existing_policy": "reject", "publication_rights_acknowledged": True})
        assert any(event == "result" for _, event, _ in events), events
        assert CoverProject.load(project, cover.id).export_status == "completed"
