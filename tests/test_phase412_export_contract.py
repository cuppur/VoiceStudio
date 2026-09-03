from pathlib import Path
from types import SimpleNamespace

from local_voice_studio.cover.exporting.backend import FFmpegExportBackend
from local_voice_studio.cover.exporting.validation import ExportOutputValidator


def test_ffmpeg_export_commands_lock_product_audio_contract(monkeypatch, tmp_path: Path):
    calls = []
    class FakeProcess:
        stderr_tail = ""
        def __init__(self, args, **kwargs): calls.append(args); self.args = args
        def run(self): Path(self.args[-1]).write_bytes(b"x" * 256); return 0
        def stop(self): pass
    monkeypatch.setattr("local_voice_studio.cover.exporting.backend.process_module.ManagedProcess", FakeProcess)
    source = tmp_path / "in.wav"; source.write_bytes(b"x")
    for fmt in ("wav", "mp3"):
        FFmpegExportBackend(Path("ffmpeg.exe")).encode(source, tmp_path / f"out.{fmt}", format=fmt)
    wav_args, mp3_args = calls
    assert ["-c:a", "pcm_s16le"] == wav_args[wav_args.index("-c:a"):wav_args.index("-c:a") + 2]
    assert ["-ar", "48000", "-ac", "2"] == wav_args[wav_args.index("-ar"):wav_args.index("-ar") + 4]
    assert ["-c:a", "libmp3lame"] == mp3_args[mp3_args.index("-c:a"):mp3_args.index("-c:a") + 2]
    assert ["-b:a", "320k"] == mp3_args[mp3_args.index("-b:a"):mp3_args.index("-b:a") + 2]
    assert ["-ar", "48000", "-ac", "2"] == mp3_args[mp3_args.index("-ar"):mp3_args.index("-ar") + 4]


def test_export_output_validator_rejects_shape_and_duration(tmp_path: Path):
    path = tmp_path / "output.wav"
    path.write_bytes(b"x" * 256)
    try:
        probe = lambda *_a, **_k: SimpleNamespace(duration_seconds=175, sample_rate=44100, channels=1, codec_name="pcm_s16le")
        try:
            ExportOutputValidator(probe).validate(path, expected_format="wav", source_duration_seconds=180)
        except Exception as exc:
            assert getattr(exc, "code", "") == "cover.asset_invalid"
        else:
            raise AssertionError("invalid output was accepted")
    finally:
        path.unlink(missing_ok=True)
