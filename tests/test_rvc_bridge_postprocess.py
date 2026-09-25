import numpy as np

from local_voice_studio.singing.rvc_bridge import _normalized_audio


def test_rvc_upstream_int16_pcm_is_normalized_before_dsp():
    pcm = np.array([-32768, -26214, 0, 26214, 32767], dtype=np.int16)

    result = _normalized_audio(pcm)

    assert result.dtype == np.float32
    assert np.allclose(result, np.array(pcm, dtype=np.float32) / 32768, atol=1e-7)


def test_normalized_float_input_is_not_scaled_a_second_time():
    pcm = np.array([-0.5, 0.0, 0.5], dtype=np.float32)

    result = _normalized_audio(pcm)

    assert result.dtype == np.float32
    assert np.array_equal(result, pcm)
