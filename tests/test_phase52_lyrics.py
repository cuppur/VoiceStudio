from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.lyrics import (
    CoverLyricsService,
    LyricSegment,
    serialize_lrc_lines,
    _load_segments,
)
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths
from local_voice_studio.protocol import COMMANDS, PAYLOAD_FIELDS, validate_payload
from local_voice_studio.ui import cover_session


def _paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "data"
    return AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "db.sqlite")


def _cover(tmp_path: Path) -> tuple[AppPaths, Path, CoverProject]:
    paths = _paths(tmp_path)
    paths.projects_root.mkdir(parents=True)
    project = paths.projects_root / "project"
    project.mkdir()
    cover = CoverProject.create(project, cover_id="c" * 32)
    cover.attest_rights()
    vocal = cover.root / "stems" / "vocals.wav"
    with wave.open(str(vocal), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b"\x01\x00" * 16000)
    cover.set_stem("vocal", vocal)
    return paths, project, cover


class _FakePython:
    """Stand-in for the private engine interpreter that writes segments JSONL."""

    def __init__(self, segments: list[dict]):
        self.segments = segments

    def __fspath__(self) -> str:
        return "fake-python.exe"


def test_serialize_lrc_sorts_and_formats():
    lines = [LyricSegment(3.5, 4.0, "第二句"), LyricSegment(1.25, 1.8, "第一句")]
    text = serialize_lrc_lines(lines)
    assert text == "[00:01.25]第一句\n[00:03.50]第二句\n"
    assert serialize_lrc_lines([]) == ""


def test_load_segments_skips_malformed_lines(tmp_path):
    path = tmp_path / "segments.jsonl"
    path.write_text(
        '{"start": 1.0, "end": 2.0, "text": "hi"}\n'
        '{"start": -1, "end": 2, "text": "bad"}\n'
        'not json\n'
        '{"start": 3.0, "text": "no end"}\n',
        encoding="utf-8",
    )
    result = _load_segments(path)
    assert [(item.start_seconds, item.end_seconds, item.text) for item in result] == [(1.0, 2.0, "hi"), (3.0, 3.0, "no end")]


def test_cover_lyrics_service_transcribes_and_marks_auto(tmp_path, monkeypatch):
    paths, project, cover = _cover(tmp_path)
    fake = _FakePython([{"start": 0.0, "end": 1.5, "text": "你好"}, {"start": 2.0, "end": 3.0, "text": "世界"}])

    def _engine_python(self):
        return fake

    monkeypatch.setattr(CoverLyricsService, "_engine_python", _engine_python)

    class _FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = iter(())
        def poll(self):
            return 0
        def wait(self):
            return 0

    calls = {}

    def _popen(command, cwd=None, env=None, **kwargs):
        calls["command"] = command
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(json.dumps(item) for item in fake.segments) + "\n", encoding="utf-8")
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    service = CoverLyricsService(project, paths=paths)
    result = service.transcribe("c" * 32)
    assert result["origin"] == "auto" and result["line_count"] == 2
    restored = CoverProject.load(project, "c" * 32)
    assert restored.lyrics_origin == "auto"
    lrc = restored.root / restored.lyrics_path
    assert lrc.read_text(encoding="utf-8") == "[00:00.00]你好\n[00:02.00]世界\n"


def test_cover_lyrics_service_requires_rights_and_vocal(tmp_path):
    paths, project, _ = _cover(tmp_path)
    service = CoverLyricsService(project, paths=paths)
    # No rights attested -> reject.
    no_rights = CoverProject.create(project, cover_id="d" * 32)
    with pytest.raises(ValueError, match="权利"):
        service.transcribe("d" * 32)
    # Rights attested but no separated vocal asset -> reject.
    no_vocal = CoverProject.create(project, cover_id="e" * 32)
    no_vocal.attest_rights()
    with pytest.raises(ValueError, match="已分离的人声轨"):
        service.transcribe("e" * 32)


def test_protocol_transcribe_lyrics_allowlist():
    assert "transcribe_lyrics" in COMMANDS
    fields = PAYLOAD_FIELDS["transcribe_lyrics"]
    payload = validate_payload("transcribe_lyrics", {"project_path": "x", "cover_id": "c" * 32, "language": "zh"})
    assert set(payload) == {"project_path", "cover_id", "language"}
    with pytest.raises(ValueError, match="未知字段"):
        validate_payload("transcribe_lyrics", {"project_path": "x", "cover_id": "c" * 32, "evil": 1})


def test_cover_project_lyrics_origin_persists(tmp_path):
    paths, project, cover = _cover(tmp_path)
    lrc = cover.root / "lyrics" / "lyrics.lrc"
    lrc.write_text("[00:01.00]测试\n", encoding="utf-8")
    cover.set_lyrics(lrc, origin="auto")
    restored = CoverProject.load(project, "c" * 32)
    assert restored.lyrics_origin == "auto"
    with pytest.raises(ValueError, match="lyrics_origin"):
        cover.set_lyrics(lrc, origin="machine")


def test_cover_session_serialize_and_write_roundtrip(tmp_path):
    lines = [cover_session.LyricLine(1.5, "第一句"), cover_session.LyricLine(3.25, "第二句")]
    text = cover_session.serialize_lrc(lines)
    assert text == "[00:01.50]第一句\n[00:03.25]第二句\n"
    path = tmp_path / "lyrics.lrc"
    cover_session.write_lrc(path, lines)
    assert cover_session.parse_lrc(path.read_text(encoding="utf-8")) == lines
