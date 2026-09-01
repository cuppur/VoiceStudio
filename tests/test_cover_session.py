import json
from pathlib import Path
import pytest

from local_voice_studio.ui import cover_session as cover


def test_parse_lrc_multitag_invalid_and_empty():
    value = "[00:02.50][00:01.5] hello\n[bad]skip\n[01:99.00] bad\n[00:03.00]   \n"
    assert cover.parse_lrc(value) == [cover.LyricLine(1.5, "hello"), cover.LyricLine(2.5, "hello")]
    assert cover.parse_lrc("") == []


def test_probe_metadata_uses_private_ffprobe(monkeypatch, tmp_path):
    ffprobe = tmp_path / "ffprobe.exe"; ffprobe.write_bytes(b"")
    monkeypatch.setattr(cover, "_tool", lambda name, paths=None: ffprobe)
    seen = {}
    def run(command, **kwargs):
        seen["command"] = command
        return json.dumps({"format": {"duration": "12.5", "bit_rate": "128000"}, "streams": [{"sample_rate": "44100", "channels": 2, "codec_name": "mp3"}]})
    monkeypatch.setattr(cover, "_run_cancellable", run)
    result = cover.probe_audio_metadata(tmp_path / "song.mp3")
    assert result.duration_seconds == 12.5 and result.sample_rate == 44100
    assert str(ffprobe) == seen["command"][0]


def test_decode_pcm_peaks_clamps_seek_and_buckets(monkeypatch, tmp_path):
    # 10 signed samples; requesting 4 buckets gives exact min/max pairs.
    raw = b"".join(int(value).to_bytes(2, "little", signed=True) for value in [-4, 2, 8, -1, 3, 7, -8, 0, 5, 1])
    result = cover.peaks_from_pcm_chunks([raw[:7], raw[7:13], raw[13:]], total_samples=10, peak_count=4)
    # Buckets use proportional floor boundaries: [0, 3, 5, 7, 10].
    assert result == [(-4, 8), (-1, 3), (-8, 7), (1, 5)]


def test_streaming_peak_aggregation_is_bounded_and_cancellable():
    chunk = b"\x00\x80\xff\x7f" * 32_768
    peaks = cover.peaks_from_pcm_chunks((chunk for _ in range(30)), total_samples=1_966_080, peak_count=6000)
    assert len(peaks) <= 6000 and peaks[0] == (-32768, 32767)
    with pytest.raises(InterruptedError):
        cover.peaks_from_pcm_chunks([chunk], total_samples=100, cancel=lambda: True)


def test_decode_zero_and_short_audio_return_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(cover, "_tool", lambda name, paths=None: tmp_path / (name + ".exe"))
    assert cover.decode_pcm_peaks(tmp_path / "zero.wav", cover.AudioMetadata(0, 44100, 1)) == []
    assert cover.decode_pcm_peaks(tmp_path / "short.wav", cover.AudioMetadata(1, 44100, 1), start_seconds=2) == []


def test_cache_roundtrip(tmp_path):
    session = cover.SongSession("song.wav", [cover.LyricLine(1, "嗨")], cover.AudioMetadata(2, 16000, 1, "pcm"), "a" * 64, [(-2, 3)])
    path = cover.save_session_cache(session, tmp_path)
    loaded = cover.load_session_cache(path, expected_sha256="a" * 64)
    assert loaded.to_dict() == session.to_dict()
    with pytest.raises(ValueError):
        cover.load_session_cache(path, expected_sha256="b" * 64)
