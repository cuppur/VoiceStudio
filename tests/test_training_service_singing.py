from types import SimpleNamespace

from local_voice_studio.ui.web.services.training import _usable_singing_assets


def test_singing_training_automatically_skips_quality_blocked_assets():
    clean = SimpleNamespace(id="clean", quality_flags=["stereo_review_required"])
    clipped = SimpleNamespace(id="clipped", quality_flags=["clipping_risk"])
    invalid = SimpleNamespace(id="invalid", quality_flags=["decode_error"])

    assert _usable_singing_assets([clean, clipped, invalid]) == [clean]
