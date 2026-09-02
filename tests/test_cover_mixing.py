"""Phase 4 mixer contracts.

These tests intentionally use a recording runner: the mixer must make all
media decisions explicit and must not silently trust client supplied paths.
"""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.cover.mixing import CoverMixSettings, CoverMixer
from local_voice_studio.paths import AppPaths


def wav(path: Path, seconds: float = 1.0, rate: int = 48_000, channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(b"\0\0" * int(seconds * rate) * channels)
    return path


class Runner:
    def __init__(self): self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append((list(command), kwargs))
        output = Path(command[-1]); wav(output)
        return 0


def paths_for(tmp_path: Path) -> AppPaths:
    root = tmp_path / "data"
    return AppPaths(root, tmp_path, root / "runtime", root / "engine", root / "models", root / "logs", root / "db.sqlite3")


def project(tmp_path: Path, *, rights: bool = True) -> CoverProject:
    cover = CoverProject.create(tmp_path / "项目", cover_id="a" * 32)
    original = wav(tmp_path / "原曲.wav")
    cover.copy_source(original)
    vocal = wav(cover.root / "stems" / "vocals.wav")
    instrumental = wav(cover.root / "stems" / "instrumental.wav")
    cover.set_stem("vocal", vocal); cover.set_stem("instrumental", instrumental)
    cover.attest_rights(rights)
    return cover


def profile(cover: CoverProject) -> dict:
    checkpoint = cover.root.parent.parent / "models" / "singing" / "profile" / "model.pth"
    index = checkpoint.with_suffix(".index")
    checkpoint.parent.mkdir(parents=True, exist_ok=True); checkpoint.write_bytes(b"model"); index.write_bytes(b"index")
    return {"id": "profile", "consent_confirmed": True, "consent_confirmed_at": "now", "consent_record": "授权",
            "active_singing_model_id": "model", "singing_models": [{"id": "model", "trust_status": "verified",
            "checkpoint_relative_path": checkpoint.relative_to(cover.root.parent.parent).as_posix(),
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "index_relative_path": index.relative_to(cover.root.parent.parent).as_posix(),
            "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()}]}


def test_mix_settings_defaults_are_safe_and_explicit():
    value = CoverMixSettings()
    assert value.original_vocal_gain_db == float("-inf")
    assert value.version == "cover-mix-v1"


def test_mixer_uses_ai_vocal_and_instrumental_and_records_command(tmp_path, monkeypatch):
    cover = project(tmp_path); ai = wav(cover.root / "generated" / "ai.wav")
    cover.add_asset(CoverAsset("ai", "ai_vocal", "generated/ai.wav", hashlib.sha256(ai.read_bytes()).hexdigest(), "ai_generated", "rvc_v2"))
    runner = Runner(); mixer = CoverMixer(paths_for(tmp_path))
    mixer._ffmpeg = lambda: Path("ffmpeg.exe")
    class P:
        returncode = 0
        def __init__(self, args): wav(Path(args[-1]))
        def poll(self): return 0
    import local_voice_studio.cover.mixing as mod
    monkeypatch.setattr(mod, "probe_audio", lambda *a, **k: type("Probe", (), {"duration_seconds": 2.0, "sample_rate": 48000, "channels": 2})())
    old = mod.subprocess.Popen; mod.subprocess.Popen = lambda args, **kwargs: (runner.commands.append((args, kwargs)) or P(args))
    monkeypatch.setattr(mod, "probe_audio", lambda *a, **k: type("Probe", (), {"duration_seconds": 2.0, "sample_rate": 48000, "channels": 2})())
    try: result = mixer.mix(cover.root.parent.parent, cover.id, CoverMixSettings(), profile_manifest=profile(cover))
    finally: mod.subprocess.Popen = old
    command = runner.commands[0][0]
    assert str(ai) in command and str(cover.root / "stems" / "instrumental.wav") in command
    assert result["output_path"].endswith(".wav") and result["cache_hit"] is False
    saved = CoverProject.load(cover.root.parent.parent, cover.id).get_asset(result["asset_id"])
    assert saved and saved.role == "final_mix" and saved.content_origin == "ai_generated"


def test_mixer_requires_rights_and_verified_ai_asset(tmp_path):
    cover = project(tmp_path, rights=False)
    with pytest.raises(PermissionError): CoverMixer(paths_for(tmp_path)).mix(cover.root.parent.parent, cover.id, CoverMixSettings(), profile_manifest=profile(cover))
    cover = project(tmp_path / "bad"); bad = wav(cover.root / "generated" / "bad.wav")
    cover.add_asset(CoverAsset("ai", "ai_vocal", "generated/bad.wav", hashlib.sha256(bad.read_bytes()).hexdigest(), "ai_generated", "rvc_v2"))
    bad.write_bytes(b"tampered")
    with pytest.raises(ValueError): CoverMixer(paths_for(tmp_path / "bad")).mix(cover.root.parent.parent, cover.id, CoverMixSettings(), profile_manifest=profile(cover))


def test_mixer_cache_hit_miss_and_tamper_repair(tmp_path, monkeypatch):
    cover = project(tmp_path); ai = wav(cover.root / "generated" / "ai.wav")
    digest = hashlib.sha256(ai.read_bytes()).hexdigest()
    cover.add_asset(CoverAsset("ai", "ai_vocal", "generated/ai.wav", digest, "ai_generated", "rvc_v2"))
    mixer = CoverMixer(paths_for(tmp_path)); mixer._ffmpeg = lambda: Path("ffmpeg.exe"); settings = CoverMixSettings()
    class P:
        returncode = 0
        def __init__(self, args): wav(Path(args[-1]))
        def poll(self): return 0
    import local_voice_studio.cover.mixing as mod
    monkeypatch.setattr(mod, "probe_audio", lambda *a, **k: type("Probe", (), {"duration_seconds": 2.0, "sample_rate": 48000, "channels": 2})())
    old = mod.subprocess.Popen; mod.subprocess.Popen = lambda *a, **k: P(a[0])
    manifest = profile(cover)
    try: first = mixer.mix(cover.root.parent.parent, cover.id, settings, profile_manifest=manifest); second = mixer.mix(cover.root.parent.parent, cover.id, settings, profile_manifest=manifest)
    finally: mod.subprocess.Popen = old
    assert first["cache_hit"] is False and second["cache_hit"] is True
    Path(second["output_path"]).write_bytes(b"tampered")
    old = mod.subprocess.Popen; mod.subprocess.Popen = lambda *a, **k: P(a[0])
    try: repaired = mixer.mix(cover.root.parent.parent, cover.id, settings, profile_manifest=manifest)
    finally: mod.subprocess.Popen = old
    assert repaired["cache_hit"] is False and Path(repaired["output_path"]).is_file()


def test_mixer_cancel_cleans_staging_and_does_not_publish(tmp_path):
    cover = project(tmp_path); ai = wav(cover.root / "generated" / "ai.wav")
    cover.add_asset(CoverAsset("ai", "ai_vocal", "generated/ai.wav", hashlib.sha256(ai.read_bytes()).hexdigest(), "ai_generated", "rvc_v2"))
    with pytest.raises(RuntimeError, match="取消"):
        CoverMixer(paths_for(tmp_path)).mix(cover.root.parent.parent, cover.id, CoverMixSettings(), profile_manifest=profile(cover), cancel=lambda: True)
    assert not list((cover.root / "outputs").glob("*.staging*"))
