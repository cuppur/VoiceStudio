import json
from pathlib import Path

import pytest

from local_voice_studio.cover.exporting.audio_probe import ExportAudioProbe
from local_voice_studio.cover.exporting.validation import ExportOutputValidator


class FakeProcess:
    payload = b""
    code = 0
    error = ""
    seen = None

    def __init__(self, command, *, cancel=None, capture_stdout=False):
        type(self).seen = list(command)
        self.stdout = type(self).payload
        self.stderr_tail = type(self).error

    def run(self):
        return type(self).code

    def stop(self):
        pass


def good_payload(**stream):
    return json.dumps({"streams": [{"codec_name": "mp3", "sample_rate": "48000", "channels": 2, **stream}],
                       "format": {"duration": "12.5", "bit_rate": "320000"}}).encode()


def test_export_probe_parses_strict_audio_fields(monkeypatch, tmp_path):
    FakeProcess.payload = good_payload(bit_rate="320000")
    monkeypatch.setattr("local_voice_studio.cover.exporting.audio_probe.ManagedProcess", FakeProcess)
    info = ExportAudioProbe(tmp_path / "ffprobe").probe(tmp_path / "x.mp3")
    assert info.duration_seconds == 12.5
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.codec_name == "mp3"
    assert info.bit_rate == 320000
    assert "-show_entries" in FakeProcess.seen


@pytest.mark.parametrize("payload", [b"not json", b'{"streams": []}', b'{"streams":[{}],"format":{}}'])
def test_export_probe_rejects_non_json_or_incomplete(monkeypatch, tmp_path, payload):
    FakeProcess.payload = payload
    monkeypatch.setattr("local_voice_studio.cover.exporting.audio_probe.ManagedProcess", FakeProcess)
    with pytest.raises(ValueError):
        ExportAudioProbe(tmp_path / "ffprobe").probe(tmp_path / "x.wav")


def test_export_probe_nonzero(monkeypatch, tmp_path):
    FakeProcess.payload = b"{}"
    FakeProcess.code = 1
    FakeProcess.error = "bad input"
    monkeypatch.setattr("local_voice_studio.cover.exporting.audio_probe.ManagedProcess", FakeProcess)
    with pytest.raises(RuntimeError, match="ffprobe"):
        ExportAudioProbe(tmp_path / "ffprobe").probe(tmp_path / "x.wav")
    FakeProcess.code = 0


def test_export_probe_cancel_passthrough(monkeypatch, tmp_path):
    class Cancelled(FakeProcess):
        def run(self):
            raise InterruptedError("cancelled")
    monkeypatch.setattr("local_voice_studio.cover.exporting.audio_probe.ManagedProcess", Cancelled)
    with pytest.raises(InterruptedError):
        ExportAudioProbe(tmp_path / "ffprobe").probe(tmp_path / "x.wav")


def test_export_validator_cancel_forwards_to_managed_probe():
    class Probe:
        cancelled = False
        def cancel(self):
            self.cancelled = True
    probe = Probe()
    validator = ExportOutputValidator(probe)
    validator.cancel()
    assert probe.cancelled
