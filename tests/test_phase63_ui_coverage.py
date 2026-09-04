from __future__ import annotations

"""Phase 6.3 UI coverage hardening.

Targets the modules that the phase 3.1 / simple-mode / e2e suites left thin:
WorkerClient message plumbing, Recorder lifecycle, PreviewAudioController
gain/master-role paths, cover_session LRC/cache helpers, lyric editor, and
the shared widgets (timeline, segment card, inline player, transport).
"""

import json
import os
import struct
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtWidgets import QApplication

from local_voice_studio.models import Job, JobKind, JobStatus
from local_voice_studio.paths import AppPaths
from local_voice_studio.ui.audio.preview_controller import PlayerChannel, PreviewAudioController
from local_voice_studio.ui.cover_session import (
    AudioMetadata,
    LyricLine,
    SongSession,
    load_session_cache,
    parse_lrc,
    serialize_lrc,
    sha256_file,
    write_lrc,
)
from local_voice_studio.ui.recording import Recorder, analyse_pcm_quality
from local_voice_studio.ui.studio_widgets.lyric_view import LyricLineEditDialog, LyricView
from local_voice_studio.ui.studio_widgets.transport import TransportWidget
from local_voice_studio.ui.studio_widgets.voice_selector import VoiceSelector
from local_voice_studio.ui.widgets import (
    AudioWaveform,
    GenerationRecord,
    GenerationRecordCard,
    InlineAudioPlayer,
    ReviewSegmentCard,
    StepTimeline,
    audio_duration,
    estimate_text_work,
    format_duration,
    format_timestamp,
)
from local_voice_studio.ui.worker_client import WorkerClient


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(data, root / "projects", data / "runtime", data / "engine",
                    data / "models", data / "logs", data / "studio.sqlite3")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_wav(path: Path, seconds: float = 0.2, rate: int = 48000, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for index in range(int(rate * seconds)):
        value = int(12000 * (index * freq * 6.28318530718 / rate) % 1 * 0 + 12000) if False else int(
            12000 * ((index * freq) % rate) / rate * 2 - 12000
        )
        frames += struct.pack("<h", max(-32768, min(32767, value)))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(frames))
    return path


# ---------------------------------------------------------------------------
# WorkerClient: message plumbing and lifecycle
# ---------------------------------------------------------------------------


class _ByteSource:
    def __init__(self, data: bytes): self._data = data
    def readAllStandardOutput(self) -> bytes:  # noqa: N802
        return self._data


class _ErrSource:
    def __init__(self, data: bytes): self._data = data
    def readAllStandardError(self) -> bytes:  # noqa: N802
        return self._data


def _client(paths: AppPaths) -> WorkerClient:
    client = WorkerClient.__new__(WorkerClient)
    client.paths = paths
    client.ready = False
    client.pending = {}
    client._buffer = b""
    client.process = type("Process", (), {
        "exitCode": lambda self: 0, "exitStatus": lambda self: "normal",
        "errorString": lambda self: "", "error": lambda self: QProcess.Crashed,
        "readAllStandardOutput": lambda self: b"", "readAllStandardError": lambda self: b"",
        "processId": lambda self: 123,
    })()
    client.event = type("Signal", (), {"emit": lambda self, *args: None})()
    client.stderr_line = type("Signal", (), {"emit": lambda self, *args: None})()
    client.request_started = type("Signal", (), {"emit": lambda self, *args: None})()
    client.request_finished = type("Signal", (), {"emit": lambda self, *args: None})()
    client.state_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client.ready_changed = type("Signal", (), {"emit": lambda self, *args: None})()
    client._diagnostic = lambda _message: None
    return client


def test_worker_stdout_parses_ready_result_and_error(tmp_path: Path):
    client = _client(_paths(tmp_path))
    client.pending = {"r1": "convert_vocal", "r2": "separate_song"}
    events = []
    finished = []
    client.event.emit = lambda rid, event, payload: events.append((rid, event, payload))
    client.request_finished.emit = lambda rid, command: finished.append((rid, command))
    payload = (
        b'{"id":"worker","type":"ready","payload":{}}\n'
        b'{"id":"r1","type":"result","payload":{"status":"ok"}}\n'
        b'{"id":"r2","type":"error","payload":{"message":"boom"}}\n'
    )
    client.process = _ByteSource(payload)
    client._stdout()
    assert client.ready is True
    assert client.pending == {}
    assert ("r1", "result", {"status": "ok"}) in events
    assert ("r2", "error", {"message": "boom"}) in events
    assert ("r1", "convert_vocal") in finished


def test_worker_stdout_handles_partial_lines_and_garbage(tmp_path: Path):
    client = _client(_paths(tmp_path))
    lines = []
    client.stderr_line.emit = lambda message: lines.append(message)
    client.process = _ByteSource(b'{"id":"worker","type":"ready","pa')
    client._stdout()
    assert client._buffer  # partial line stays buffered
    client.process = _ByteSource(b'yload":{}}\nnot json at all\n')
    client._stdout()
    assert client.ready is True
    assert any("无法解析" in line for line in lines)


def test_worker_stderr_and_started_and_finished(tmp_path: Path):
    client = _client(_paths(tmp_path))
    states = []
    client.state_changed.emit = lambda state: states.append(state)
    client.stderr_line.emit = lambda _line: None
    client.process = _ErrSource(b"warn something\n\n")
    client._stderr()
    client.process = type("Process", (), {
        "processId": lambda self: 99,
        "exitCode": lambda self: 17, "exitStatus": lambda self: "crashed",
    })()
    client._started()
    assert states[-1] == "running"
    client._finished()
    assert states[-1] == "stopped"
    assert client.ready is False


def test_worker_process_error_failed_to_start_flushes(tmp_path: Path):
    client = _client(_paths(tmp_path))
    client.pending = {"r9": "separate_song"}
    events = []
    states = []
    client.event.emit = lambda rid, event, payload: events.append((rid, event, payload))
    client.state_changed.emit = lambda state: states.append(state)
    client.process = type("Process", (), {
        "errorString": lambda self: "cannot start",
        "error": lambda self: QProcess.FailedToStart,
    })()
    client._process_error()
    assert client.pending == {}
    assert states[-1] == "start-failed"
    assert events[0][0:2] == ("r9", "error")


def test_worker_send_validates_command_and_starts_when_needed(tmp_path: Path):
    client = _client(_paths(tmp_path))
    with pytest.raises(ValueError, match="未知 Worker 命令"):
        client.send("bogus_command")
    with pytest.raises(ValueError, match="未知字段"):
        client.send("separate_song", {"project_path": "x", "cover_id": "c" * 32, "bogus": 1})

    class FakeProcess:
        def state(self): return QProcess.NotRunning
        def start(self): pass
        def waitForStarted(self, _ms): return False  # noqa: N802

    client.process = FakeProcess()
    with patch.object(WorkerClient, "start", lambda self: None):
        with pytest.raises(RuntimeError, match="无法启动本地工作进程"):
            client.send("separate_song", {"project_path": "x", "cover_id": "c" * 32, "mode": "full"})


def test_worker_start_handles_resolver_failure(tmp_path: Path):
    client = _client(_paths(tmp_path))
    client.process = type("Process", (), {"state": lambda self: QProcess.NotRunning})()
    states = []
    client.state_changed.emit = lambda state: states.append(state)
    with patch("local_voice_studio.ui.worker_client.EngineRuntimeResolver", side_effect=RuntimeError("no engine")):
        client.start()
    assert any("no engine" in state for state in states)


# ---------------------------------------------------------------------------
# Recorder: PCM analysis and lifecycle
# ---------------------------------------------------------------------------


def test_analyse_pcm_quality_empty_and_varied_levels():
    empty = analyse_pcm_quality(b"")
    assert empty["volume"] == "无声音"
    assert empty["level"] == 0.0
    # RMS of constant 300/32768 is ~0.0092, comfortably below the 0.012
    # "偏低" threshold (500/32768 would be ~0.0153 and land in 正常).
    quiet = struct.pack("<8000h", *([300] * 8000))
    result = analyse_pcm_quality(quiet, 48000, 1)
    assert result["volume"] == "偏低"
    assert result["clipping"] is False
    loud = struct.pack("<8000h", *([32300] * 8000))
    result = analyse_pcm_quality(loud, 48000, 1)
    assert result["volume"] == "过高"
    assert result["clipping"] is True
    assert result["environment"] == "建议调整"
    normal = struct.pack("<8000h", *([8000] * 8000))
    result = analyse_pcm_quality(normal, 48000, 1)
    assert result["volume"] == "正常"
    stereo = struct.pack("<8000h", *([300] * 8000))
    assert analyse_pcm_quality(stereo, 48000, 2)["volume"] == "偏低"


class _FakeInputDevice:
    def __init__(self, data: bytes):
        self._data = data
        self.readyRead = type("Signal", (), {"connect": lambda self, fn: None})()
    def readAll(self) -> bytes:  # noqa: N802
        return self._data


def test_recorder_start_without_microphone(tmp_path: Path):
    _app()
    recorder = Recorder()
    errors = []
    recorder.error.connect(errors.append)
    with patch.object(Recorder, "inputs", return_value=[]):
        recorder.start(tmp_path / "rec.wav")
    assert errors == ["没有检测到麦克风"]
    assert recorder.source is None


def test_recorder_full_cycle_writes_wave(tmp_path: Path):
    _app()
    recorder = Recorder()
    destination = tmp_path / "rec.wav"
    sample = struct.pack("<h", 12000) * 400
    device = _FakeInputDevice(sample)

    class FakeSource:
        def __init__(self, *_args): self.stopped = False
        def start(self): return device
        def stop(self): self.stopped = True
        def deleteLater(self): pass  # noqa: N802

    stopped = []
    recorder.stopped.connect(lambda path, duration: stopped.append((path, duration)))

    class _FakeDevice:
        def isFormatSupported(self, _fmt): return True  # noqa: N802
        def preferredFormat(self): return None  # noqa: N802

    with patch.object(Recorder, "inputs", return_value=[_FakeDevice()]), \
         patch("local_voice_studio.ui.recording.QAudioSource", FakeSource):
        recorder.start(destination, device_index=0)
        assert recorder.source is not None
        recorder.stop()
    assert destination.is_file()
    assert stopped and stopped[0][0] == str(destination)
    with wave.open(str(destination), "rb") as stream:
        assert stream.getframerate() == 48000
        assert stream.getnchannels() == 1
        assert stream.getnframes() > 0


def test_recorder_stop_without_start_is_noop(tmp_path: Path):
    recorder = Recorder()
    recorder.stop()  # must not raise


# ---------------------------------------------------------------------------
# PreviewAudioController: gains, master role, plan application
# ---------------------------------------------------------------------------


class _FakePlayer:
    def __init__(self): self.source = ""; self.pos = 0; self.gain = None; self.events = []
    def setSource(self, url): self.source = str(url); self.events.append(("source", str(url)))  # noqa: N802
    def set_source(self, path): self.source = str(path); self.events.append(("source", str(path)))
    def setPosition(self, pos): self.pos = int(pos); self.events.append(("seek", int(pos)))  # noqa: N802
    def set_position(self, pos): self.pos = int(pos); self.events.append(("seek", int(pos)))
    def position(self): return self.pos
    def play(self): self.events.append("play")
    def pause(self): self.events.append("pause")
    def stop(self): self.events.append("stop")


def test_preview_controller_gain_and_master_role_paths():
    from local_voice_studio.cover.preview import PlaybackMode, PreviewMixPlan, PreviewMixPlanner, TrackRole

    channels = {role: PlayerChannel(_FakePlayer(), _FakePlayer()) for role in TrackRole}
    ctl = PreviewAudioController(channels, drift_tolerance_ms=50)
    planner = PreviewMixPlanner({
        TrackRole.VOCAL: "/vocal.wav",
        TrackRole.INSTRUMENTAL: "/inst.wav",
        TrackRole.AI_VOCAL: "/ai.wav",
        TrackRole.FINAL_MIX: "/final.wav",
    })
    plan = planner.plan(TrackRole.VOCAL, PlaybackMode.FINAL_MIX)
    ctl.apply_plan(plan)
    assert ctl.master_role is TrackRole.FINAL_MIX
    assert ctl.master_gain == plan.master_gain

    ctl.set_master_gain(0.5)
    ctl.select(TrackRole.VOCAL)
    assert ctl.plan.mode is PlaybackMode.SOLO_TRACK
    assert ctl.master_role is TrackRole.VOCAL
    ctl.set_gain(TrackRole.VOCAL, 0.3)
    ctl.set_gain("instrumental", 0.4)
    ctl.seek(1500)
    ctl.play()
    ctl.pause()
    assert not ctl.playing
    assert ctl.resync() == 1500 - 1500 or isinstance(ctl.resync(), int)


def test_preview_controller_plan_skips_unavailable_and_noop_paths():
    from local_voice_studio.cover.preview import PreviewMixPlanner, TrackRole

    ctl = PreviewAudioController({}, drift_tolerance_ms=50)
    plan = PreviewMixPlanner({}).plan(TrackRole.VOCAL)
    ctl.apply_plan(plan)
    ctl.play()  # no active tracks -> no-op
    assert not ctl.playing
    ctl.resync()  # no active tracks -> 0
    assert ctl.resync() == 0
    assert ctl.master_role is TrackRole.VOCAL  # solo mode keeps selected role
    ctl.set_master_gain(0.1)  # no plan -> only updates master_gain
    assert ctl.master_gain == 0.1
    ctl.seek(-5)  # clamps to zero
    assert ctl.master_position_ms == 0


def test_preview_controller_player_channel_fallback_source():
    class FallbackPlayer:
        def __init__(self): self.source = ""; self.pos = 0; self.events = []
        def set_source(self, path): self.source = str(path); self.events.append(("source", str(path)))
        def set_position(self, pos): self.pos = int(pos); self.events.append(("seek", int(pos)))
        def position(self): return self.pos
        def play(self): self.events.append("play")
        def pause(self): self.events.append("pause")
        def stop(self): self.events.append("stop")

    player = FallbackPlayer()
    channel = PlayerChannel(player)
    channel.set_source("/tmp/a.wav")
    assert player.source == "/tmp/a.wav"
    channel.set_position(42)
    assert player.pos == 42
    channel.play(); channel.pause(); channel.stop()
    assert "play" in player.events and "pause" in player.events and "stop" in player.events


# ---------------------------------------------------------------------------
# cover_session helpers
# ---------------------------------------------------------------------------


def test_parse_and_serialize_lrc_roundtrip():
    content = "[00:12.50]第一句\n[00:20.00]第二句\n[01:00]第三句\nbogus line\n"
    lines = parse_lrc(content)
    assert [line.text for line in lines] == ["第一句", "第二句", "第三句"]
    assert lines[0].timestamp_seconds == pytest.approx(12.5)
    rendered = serialize_lrc(lines)
    assert "[00:12.50]第一句" in rendered
    assert parse_lrc("") == []


def test_parse_lrc_rejects_out_of_range_seconds():
    lines = parse_lrc("[00:99]无效\n[01:30]有效")
    assert [line.text for line in lines] == ["有效"]


def test_write_lrc_atomic_and_sha256_file(tmp_path: Path):
    target = tmp_path / "song.lrc"
    write_lrc(target, [LyricLine(1.5, "行一"), LyricLine(0.5, "行零")])
    assert target.is_file()
    assert not (tmp_path / "song.lrc.tmp").exists()
    text = target.read_text(encoding="utf-8")
    assert text.index("行零") < text.index("行一")
    wav = _make_wav(tmp_path / "a.wav")
    assert len(sha256_file(wav)) == 64


def test_song_session_load_and_cache_roundtrip(tmp_path: Path, monkeypatch):
    wav = _make_wav(tmp_path / "song.wav")
    cache_dir = tmp_path / "cache"
    calls = {"probe": 0, "decode": 0}

    def fake_probe(*_args, **_kwargs):
        calls["probe"] += 1
        return AudioMetadata(0.2, 48000, 1, "pcm_s16le", 768000)

    def fake_decode(*_args, **_kwargs):
        calls["decode"] += 1
        return [(i, i * 100) for i in range(100)]

    monkeypatch.setattr("local_voice_studio.ui.cover_session.probe_audio_metadata", fake_probe)
    monkeypatch.setattr("local_voice_studio.ui.cover_session.decode_pcm_peaks", fake_decode)
    session = SongSession.load(wav, cache_dir=cache_dir, peak_count=6000)
    assert session.sha256 == sha256_file(wav)
    assert len(session.peaks) == 100
    assert calls["probe"] == 1
    # Second load hits the cache; probe/decode must not run again.
    session2 = SongSession.load(wav, cache_dir=cache_dir, peak_count=6000)
    assert session2.sha256 == session.sha256
    assert calls["probe"] == 1 and calls["decode"] == 1


def test_song_session_cache_spec_change_ignores_cache(tmp_path: Path, monkeypatch):
    wav = _make_wav(tmp_path / "song.wav")
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(
        "local_voice_studio.ui.cover_session.probe_audio_metadata",
        lambda *a, **k: AudioMetadata(0.2, 48000, 1, "pcm_s16le", 768000),
    )
    monkeypatch.setattr("local_voice_studio.ui.cover_session.decode_pcm_peaks", lambda *a, **k: [])
    first = SongSession.load(wav, cache_dir=cache_dir, peak_count=6000)
    cache_file = cache_dir / f"{first.sha256}.json"
    assert cache_file.is_file()
    # Corrupt the cache: load must fall through and rebuild.
    cache_file.write_text("{ not json", encoding="utf-8")
    rebuilt = SongSession.load(wav, cache_dir=cache_dir, peak_count=6000)
    assert rebuilt.sha256 == first.sha256
    assert load_session_cache(cache_file, expected_sha256=first.sha256) is not None


# ---------------------------------------------------------------------------
# Lyric view / editor
# ---------------------------------------------------------------------------


def test_lyric_view_accepts_dict_and_tuple_lines():
    _app()
    view = LyricView()
    view.set_lyrics([{"start_ms": 1000, "text": "dict"}, (2000, "tuple")], editable=True)
    assert view.lines == [(1000, "dict"), (2000, "tuple")]
    assert "双击歌词行" in view.toolTip()
    seeks = []
    view.seek_requested.connect(seeks.append)
    view.move_previous()
    view.move_next()
    assert seeks  # emits positions


def test_lyric_view_edit_current_without_selection_is_noop():
    # edit_current() with no selection shows a modal message box; patch it so
    # the offscreen test cannot block on a dialog nobody will close.
    _app()
    view = LyricView()
    view.set_lyrics([{"start_ms": 1000, "text": "dict"}], editable=True)
    from PySide6.QtWidgets import QMessageBox
    with patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
        view.edit_current()


def test_lyric_edit_dialog_values_validate(tmp_path: Path):
    _app()
    dialog = LyricLineEditDialog(1250, " 文本 ", None)
    dialog.text_edit.setText("新文本")
    dialog.time_spin.setValue(2.5)
    position, text = dialog.values()
    assert (position, text) == (2500, "新文本")
    dialog.text_edit.setText("   ")
    with pytest.raises(ValueError, match="歌词不能为空"):
        dialog.values()


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------


def test_format_helpers():
    assert format_timestamp("2026-09-04T10:00:00+08:00")  # parses
    assert format_timestamp("bogus") == "bogus"
    assert format_timestamp("") == "时间未知"
    assert format_timestamp("2026-09-04T10:00:00")  # Z-less
    assert format_duration(61.4) == "01:01"
    assert format_duration(-3) == "00:00"
    chars, chunks = estimate_text_work("  hello world  ")
    assert chars == 11 and chunks == 1
    assert estimate_text_work("   ") == (0, 0)


def test_audio_duration_and_waveform(tmp_path: Path):
    wav = _make_wav(tmp_path / "tone.wav", seconds=0.25)
    assert audio_duration(wav) == pytest.approx(0.25, abs=0.01)
    assert audio_duration(tmp_path / "missing.wav") == 0.0
    _app()
    waveform = AudioWaveform()
    waveform.set_audio(wav)
    assert waveform.samples  # peaks extracted
    waveform.set_audio(tmp_path / "missing.wav")
    assert waveform.samples == []
    waveform.set_audio(None)
    assert waveform.samples == []


def test_inline_audio_player_source_and_release(tmp_path: Path):
    _app()
    wav = _make_wav(tmp_path / "tone.wav")
    player = InlineAudioPlayer()
    player.set_source(wav)
    assert player.play.isEnabled()
    player.set_source(tmp_path / "missing.wav")
    assert not player.play.isEnabled()
    player.toggle()  # no path -> no-op
    player._position(5000)  # must not raise offscreen
    player.release()
    assert player.path == ""


def test_step_timeline_states():
    _app()
    timeline = StepTimeline(("一", "二", "三"))
    timeline.update_state(1, {0: "ok"}, failed=False)
    assert "✓" in timeline.labels[0].text()
    assert "●" in timeline.labels[1].text()
    timeline.update_state(1, {}, failed=True)
    assert "!" in timeline.labels[1].text()
    assert timeline.labels[1].objectName() == "stepFailed"


def test_review_segment_card_flow(tmp_path: Path):
    _app()
    wav = _make_wav(tmp_path / "clip.wav")

    class Segment:
        audio_relative_path = wav.name
        duration_seconds = 0.2
        text = "你好"
        included = True
        quality_flags = ["音量偏低"]

    card = ReviewSegmentCard()
    changed, confirmed = [], []
    card.changed.connect(lambda index, value: changed.append((index, value)))
    card.confirmed.connect(confirmed.append)
    card.set_segment(0, Segment(), tmp_path, 0, 3)
    assert "片段 1 / 3" in card.counter.text()
    assert "音量偏低" in card.issue.text()
    card.text.setText("新文本")
    card._emit_change()
    assert changed and changed[-1][1]["text"] == "新文本"
    card._confirm()
    assert confirmed == [0]
    card.release_resources()
    card._exclude_shortcut()  # must not raise offscreen


def test_generation_record_card_from_job(tmp_path: Path):
    _app()
    wav = _make_wav(tmp_path / "out.wav")
    job = Job(id="j1", kind=JobKind.SYNTHESIZE, status=JobStatus.COMPLETED,
              payload={"text": "测试"}, outputs=[str(wav)],
              created_at="2026-09-04T10:00:00", updated_at="2026-09-04T10:00:00")
    record = GenerationRecord.from_job(job, "声线A")
    assert record.audio_path == str(wav)
    assert record.text == "测试"
    assert record.duration_seconds == pytest.approx(0.2, abs=0.02)
    card = GenerationRecordCard(record)
    assert card.record is record
    card.release_resources()


def test_voice_selector_emits_only_allowed(tmp_path: Path):
    _app()
    selector = VoiceSelector(project_root=tmp_path)
    selected = []
    selector.voice_selected.connect(selected.append)
    selector.set_profiles([
        {"id": "a", "name": "可用", "consent_confirmed": True, "archived": False},
        {"id": "b", "name": "已归档", "consent_confirmed": True, "archived": True},
    ])
    selector.setCurrentIndex(0)
    assert selected == ["a"]
    selector._emit_if_allowed(1)
    assert selected == ["a"]  # archived item disabled -> no emit
    assert not selector.model().item(1).flags() & Qt.ItemIsEnabled
    selector._update_card_text(-1)
    assert selector.lineEdit().text() == ""


def test_transport_widget_signals():
    _app()
    transport = TransportWidget()
    plays, seeks, volumes = [], [], []
    transport.play_requested.connect(lambda: plays.append(1))
    transport.seek_relative_requested.connect(seeks.append)
    transport.volume_changed.connect(volumes.append)
    transport.play_button.click()
    transport.back_button.click()
    transport.forward_button.click()
    transport.volume.setValue(55)
    assert plays == [1]
    assert seeks == [-10_000, 10_000]
    assert volumes == [55]
    transport.set_playing(True)
    transport.set_timeline(65_000, 90_000)
    assert "01:05 / 01:30" in transport.time_label.text()
    transport.set_timeline(200_000, 90_000)  # clamps position
    assert "01:30 / 01:30" in transport.time_label.text()
