from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from local_voice_studio.cover.application import CoverApplicationService, PrepareRenderCommand
from local_voice_studio.cover.cancellation import CancellationToken
from local_voice_studio.cover.exporting.manifest import ProvenanceManifestBuilder
from local_voice_studio.cover.mixing import CoverMixSettings, GainScale
from local_voice_studio.cover.mixing.cache import MixCacheKey
from local_voice_studio.cover.preview import PlaybackMode, PreviewMixPlanner, TrackRole
from local_voice_studio.cover.project import CoverAsset


def test_cover_domain_import_does_not_load_qt():
    code = "import sys; import local_voice_studio.cover.project, local_voice_studio.cover.mixing.models, local_voice_studio.cover.preview; assert not any(name.startswith('PySide6') for name in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_gain_scale_has_one_ui_to_db_boundary():
    assert GainScale.slider_to_db(0) == "-inf"
    assert GainScale.slider_to_db(80) == 0.0
    assert GainScale.db_to_slider("-inf") == 0
    assert GainScale.db_to_linear(0) == 1.0


def test_mix_cache_key_changes_with_gain_and_engine_version(tmp_path: Path):
    path = tmp_path / "ai.wav"; path.write_bytes(b"ai")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    item = SimpleNamespace(role="ai_vocal", asset_id="ai", path=path, sha256=digest, gain_db=0.0)
    settings = CoverMixSettings()
    first = MixCacheKey.build([item], settings, "v1")
    item.gain_db = 1.0
    second = MixCacheKey.build([item], settings, "v1")
    assert first != second
    assert first != MixCacheKey.build([item], settings, "v2")


def test_preview_mix_plan_uses_semantic_roles():
    cover = SimpleNamespace(root=Path("C:/project/covers/c1"), duration_ms=30_000, assets=[
        CoverAsset("inst", "instrumental", "stems/inst.wav", "a" * 64, "separated", "uvr5"),
        CoverAsset("ai", "ai_vocal", "generated/ai.wav", "b" * 64, "ai_generated", "rvc"),
    ])
    plan = PreviewMixPlanner().build(cover, mode="mix")
    assert plan.mode is PlaybackMode.MIX_PREVIEW
    assert plan.roles == {TrackRole.AI_VOCAL, TrackRole.INSTRUMENTAL}
    assert plan.tracks[TrackRole.AI_VOCAL].path.endswith("generated\\ai.wav") or plan.tracks[TrackRole.AI_VOCAL].path.endswith("generated/ai.wav")


def test_preview_mix_plan_converts_domain_db_and_accepts_model_aliases():
    cover = SimpleNamespace(root=Path("C:/project/covers/c1"), duration_ms=30_000, assets=[
        CoverAsset("inst", "instrumental", "stems/inst.wav", "a" * 64, "separated", "uvr5"),
        CoverAsset("ai", "ai_vocal", "generated/ai.wav", "b" * 64, "ai_generated", "rvc"),
        CoverAsset("vocal", "vocal", "stems/vocal.wav", "c" * 64, "separated", "uvr5"),
    ])
    settings = CoverMixSettings(ai_vocal_gain_db=0.0).canonical()
    plan = PreviewMixPlanner().build(cover, mode="mix", settings=settings)
    assert plan.roles == {TrackRole.AI_VOCAL, TrackRole.INSTRUMENTAL}
    assert plan.tracks[TrackRole.AI_VOCAL].gain == 1.0
    assert plan.tracks[TrackRole.VOCAL].gain == 0.0


def test_provenance_builder_has_utc_created_at_and_required_fields():
    manifest = ProvenanceManifestBuilder.build(
        cover_id="c1", asset_id="mix", content_origin="ai_generated", ai_generated=True,
        voice_profile_id="profile", singing_model_id="model", rights_confirmed=True,
        rights_attestation_text_hash="a" * 64, publication_rights_ack=True,
        input_asset_ids=["inst", "ai"], mix_settings={}, outputs=[]
    )
    assert manifest["created_at"].endswith("Z")
    assert manifest["ai_generated"] is True
    assert manifest["input_asset_ids"] == ["inst", "ai"]


def test_cancellation_token_adapts_event():
    import threading
    event = threading.Event(); token = CancellationToken(event)
    assert not token.is_cancelled(); event.set(); assert token.cancelled() and token.is_set()
