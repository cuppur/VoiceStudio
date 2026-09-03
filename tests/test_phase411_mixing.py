"""Phase 4.1.1 mixer architecture contracts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_voice_studio.cover.errors import AssetValidationError, MixAlignmentError
from local_voice_studio.cover.models import ContentOrigin, CoverAssetRole
from local_voice_studio.cover.mixing import CoverMixSettings, FFmpegMixBackend, MixInput
from local_voice_studio.cover.mixing.validation import (
    AudioInfo,
    MixAlignmentValidator,
    MixValidator,
    ResolvedAudioAsset,
)
from local_voice_studio.cover.project import CoverAsset, CoverProject


def _wav(path: Path, data: bytes = b"RIFF" + b"0" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _cover(tmp_path: Path) -> CoverProject:
    cover = CoverProject.create(tmp_path / "项目", cover_id="b" * 32)
    source = _wav(tmp_path / "source.wav")
    cover.copy_source(source)
    instrumental = _wav(cover.root / "stems" / "instrumental.wav", b"inst")
    ai = _wav(cover.root / "generated" / "ai.wav", b"ai")
    cover.set_stem("instrumental", instrumental)
    cover.add_asset(CoverAsset(
        "ai", CoverAssetRole.AI_VOCAL, "generated/ai.wav",
        hashlib.sha256(ai.read_bytes()).hexdigest(), ContentOrigin.AI_GENERATED, "test",
    )
    )
    cover.attest_rights(True)
    return cover


def test_mix_validator_resolves_and_probes_each_input_once(tmp_path: Path):
    cover = _cover(tmp_path)
    calls: list[Path] = []

    def probe(path: Path, *, cancel=None):
        calls.append(path)
        return SimpleNamespace(duration_seconds=3.0, sample_rate=48000, channels=2)

    resolved = MixValidator(cover).resolve_inputs(CoverMixSettings(), probe=probe)
    assert len(calls) == 2
    assert tuple(item.role for item in resolved.inputs) == (
        CoverAssetRole.INSTRUMENTAL, CoverAssetRole.AI_VOCAL,
    )
    assert resolved.duration_seconds == 3.0
    assert resolved.probes[CoverAssetRole.AI_VOCAL] == AudioInfo(3.0, 48000, 2)


def test_mix_validator_rejects_hash_and_origin(tmp_path: Path):
    cover = _cover(tmp_path)
    cover.get_asset(role=CoverAssetRole.AI_VOCAL).content_origin = ContentOrigin.SEPARATED.value
    with pytest.raises(AssetValidationError):
        MixValidator(cover).resolve_inputs(CoverMixSettings(), probe=lambda path, **kwargs: SimpleNamespace(duration_seconds=2))


def test_mix_alignment_reports_tolerance_and_rejects_hard_limit():
    inputs = (
        ResolvedAudioAsset(
            CoverAssetRole.INSTRUMENTAL, "inst", Path("inst.wav"), "a" * 64, 10.0, 48000, 2, 0.0
        ),
        ResolvedAudioAsset(
            CoverAssetRole.AI_VOCAL, "ai", Path("ai.wav"), "b" * 64, 9.8, 48000, 2, 0.0
        ),
    )
    report = MixAlignmentValidator.validate(inputs, tolerance_ms=250, hard_limit_ms=1000)
    assert report.warning is False
    with pytest.raises(MixAlignmentError):
        MixAlignmentValidator.validate((inputs[0], inputs[1].__class__(
            inputs[1].role, inputs[1].asset_id, inputs[1].path, inputs[1].sha256,
            8.9, inputs[1].sample_rate, inputs[1].channels, inputs[1].gain_db,
        )))


def test_backend_filter_has_fades_and_rejects_fade_out_longer_than_duration():
    inputs = [MixInput(CoverAssetRole.INSTRUMENTAL, "inst", Path("inst.wav"), "a" * 64, 0.0)]
    settings = CoverMixSettings(fade_in_ms=100, fade_out_ms=200)
    value = FFmpegMixBackend.build_filter(inputs, settings, duration_seconds=2.0)
    assert "afade=t=in" in value and "afade=t=out" in value and "st=1.8" in value
    with pytest.raises(ValueError, match="fade-out"):
        FFmpegMixBackend.build_filter(inputs, CoverMixSettings(fade_out_ms=2_001), duration_seconds=2.0)


def test_service_has_no_media_command_or_process_fallback():
    source = Path("src/local_voice_studio/cover/mixing/service.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "filter_complex" not in source
    assert "self.backend.render" in source
