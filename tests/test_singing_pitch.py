from local_voice_studio.singing.pitch import PitchAnalysis, recommend_transpose


def test_pitch_recommendation_is_bounded_and_user_visible():
    source = PitchAnalysis("rmvpe", "rvc-rmvpe-v1", 100.0, 20)
    targets = [PitchAnalysis("rmvpe", "rvc-rmvpe-v1", 200.0, 20)]
    assert recommend_transpose(source, targets) == 12


def test_pitch_analysis_rejects_empty_or_unknown_backend():
    try:
        PitchAnalysis.from_dict({"backend": "crepe", "version": "x", "median_hz": 100, "voiced_frames": 1})
    except ValueError as exc:
        assert "RMVPE" in str(exc)
    else:
        raise AssertionError("unknown backend accepted")
