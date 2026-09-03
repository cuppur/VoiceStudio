from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from local_voice_studio.cover.models import CoverAssetRole
from local_voice_studio.cover.mixing import CoverMixSettings
from local_voice_studio.cover.preview import PlaybackMode, PreviewMixPlanner, PreviewTrack, TrackRole
from local_voice_studio.cover.project import CoverAsset


def _cover() -> SimpleNamespace:
    root = Path("C:/project/covers/c1")
    return SimpleNamespace(
        root=root,
        duration_ms=30_000,
        assets=[
            CoverAsset("inst", "instrumental", "stems/inst.wav", "a" * 64, "separated", "uvr5"),
            CoverAsset("ai", "ai_vocal", "generated/ai.wav", "b" * 64, "ai_generated", "rvc"),
            CoverAsset("vocal", "vocal", "stems/vocal.wav", "c" * 64, "separated", "uvr5"),
        ],
    )


def test_track_role_is_domain_role_and_legacy_parse_works() -> None:
    assert TrackRole is CoverAssetRole
    assert TrackRole.parse("ai-vocal") is CoverAssetRole.AI_VOCAL


def test_preview_plus_six_db_normalizes_without_changing_domain_settings() -> None:
    settings = CoverMixSettings(ai_gain_db=6.0, instrumental_gain_db=0.0, original_vocal_gain_db="-inf")
    plan = PreviewMixPlanner().build(_cover(), mode=PlaybackMode.MIX_PREVIEW, settings=settings)
    assert round(plan.tracks[TrackRole.AI_VOCAL].gain, 3) == 1.0
    assert round(plan.tracks[TrackRole.INSTRUMENTAL].gain, 3) == round(10 ** (-6 / 20), 3)
    assert settings.ai_gain_db == 6.0
    assert settings.instrumental_gain_db == 0.0


def test_preview_master_gain_preserves_relative_track_ratio() -> None:
    plan = PreviewMixPlanner().build(
        _cover(), mode="mix_preview",
        settings={"ai_gain_db": 3.0, "instrumental_gain_db": -6.0, "original_vocal_gain_db": "-inf", "master_gain_db": 3.0},
    )
    ai = plan.tracks[TrackRole.AI_VOCAL].gain
    inst = plan.tracks[TrackRole.INSTRUMENTAL].gain
    assert round(20 * __import__("math").log10(ai / inst), 3) == 9.0
    assert ai <= 1.0 and inst <= 1.0


def test_preview_mute_solo_and_selected_track_are_semantically_independent() -> None:
    tracks = {
        TrackRole.VOCAL: PreviewTrack(TrackRole.VOCAL, "vocal.wav", gain=1.0, muted=True),
        TrackRole.INSTRUMENTAL: PreviewTrack(TrackRole.INSTRUMENTAL, "inst.wav", gain=1.0),
        TrackRole.AI_VOCAL: PreviewTrack(TrackRole.AI_VOCAL, "ai.wav", gain=1.0, solo=True),
    }
    planner = PreviewMixPlanner(tracks)
    mix = planner.plan(TrackRole.INSTRUMENTAL, PlaybackMode.MIX_PREVIEW)
    assert mix.roles == {TrackRole.AI_VOCAL}
    selected = planner.plan(TrackRole.INSTRUMENTAL, PlaybackMode.SOLO_TRACK)
    assert selected.roles == {TrackRole.INSTRUMENTAL}
