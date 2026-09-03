import wave
from pathlib import Path

from local_voice_studio.singing.verification import SingingModelVerifier, VerificationResult, validate_wav_quality, verify_inference_output


def _wav(path: Path, seconds: float, value: int = 1000, *, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels); stream.setsampwidth(2); stream.setframerate(rate)
        frame = value.to_bytes(2, "little", signed=True) * channels
        stream.writeframes(frame * int(rate * seconds))


def test_real_verification_accepts_decoded_non_silent_output(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    _wav(source, 4.0, 1000); _wav(output, 4.1, 1200)
    result = verify_inference_output(source, output)
    assert result.ok and result.details["duration_ratio"] == 4.1 / 4.0


def test_real_verification_rejects_short_or_silent_output(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    _wav(source, 2.0, 1000); _wav(output, 2.0, 0)
    result = verify_inference_output(source, output)
    assert not result.ok
    assert any("3-8" in error for error in result.errors)
    assert any("静音" in error for error in result.errors)


def test_quality_gate_accepts_matching_pcm_without_loading_entire_file(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    _wav(source, 4.0, 1000); _wav(output, 4.1, 1200)
    result = validate_wav_quality(output, reference=source)
    assert result.ok
    assert result.details["finite_samples"] is True
    assert result.details["sample_rate"] == 16000


def test_quality_gate_rejects_shape_drift_and_hard_clipping(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    _wav(source, 4.0, 1000)
    _wav(output, 4.0, 32767, rate=8000, channels=2)
    result = validate_wav_quality(output, reference=source)
    assert not result.ok
    assert any("采样率" in error for error in result.errors)
    assert any("声道数" in error for error in result.errors)
    assert any("硬削波" in error for error in result.errors)


class _Engine:
    def __init__(self, verify=True, fail=False): self.verify, self.fail = verify, fail
    def verify_model(self, checkpoint, index): return VerificationResult(self.verify, [] if self.verify else ["bad model"])
    def convert(self, payload, cancel=None):
        if self.fail: raise RuntimeError("conversion failed")
        _wav(Path(payload["output_path"]), 4.0, 1200)
        return Path(payload["output_path"])


def test_model_verifier_rejects_restricted_load_failure(tmp_path):
    source = tmp_path / "source.wav"; _wav(source, 4.0)
    result = SingingModelVerifier(_Engine(False)).verify(tmp_path / "m.pth", tmp_path / "m.index", source, tmp_path / "out")
    assert not result.ok and "bad model" in result.errors


def test_model_verifier_reports_conversion_failure(tmp_path):
    source = tmp_path / "source.wav"; _wav(source, 4.0)
    result = SingingModelVerifier(_Engine(fail=True)).verify(tmp_path / "m.pth", tmp_path / "m.index", source, tmp_path / "out")
    assert not result.ok and "conversion failed" in result.errors[0]


def test_model_verifier_runs_conversion_and_validates_output(tmp_path):
    source = tmp_path / "source.wav"; _wav(source, 4.0)
    result = SingingModelVerifier(_Engine()).verify(tmp_path / "m.pth", tmp_path / "m.index", source, tmp_path / "out")
    assert result.ok and result.details["restricted_load"] and result.details["index_verified"]
    assert not list((tmp_path / "out").glob("verification-*.wav"))
