"""Contract checks for metadata-only network lookup and real LRC timestamps."""
from __future__ import annotations

import io
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from local_voice_studio.cover import online_lyrics as lyrics


def record(record_id=1, **changes):
    value = {"id": record_id, "trackName": "Test Song", "artistName": "Artist", "albumName": "Album", "duration": 200,
             "instrumental": False, "syncedLyrics": "[00:08.00]First line\n[00:20.00]Second line", "plainLyrics": "First line\nSecond line"}
    value.update(changes)
    return value


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def network(monkeypatch, payload=None, *, body=None, headers=None, exception=None):
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req, timeout))
            if exception is not None:
                raise exception
            content = body if body is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return Response(content, headers)

    monkeypatch.setattr(lyrics.request, "build_opener", lambda *handlers: Opener())
    return calls


def test_lrc_multitime_offset_sort_metadata_and_duplicates():
    text = "\ufeff[ti:Song]\n[offset:+500]\n[00:10.25][00:04.000]Repeated\n[00:04.000]Repeated\n[00:04.000]Translation\n[00:12.00][ar:Artist]\n[00:65.00]invalid\nplain lyrics"
    assert lyrics.parse_synced(text) == [(3.5, "Repeated"), (3.5, "Translation"), (9.75, "Repeated")]
    assert lyrics.format_lrc(lyrics.parse_synced(text)) == "[00:03.50]Repeated\n[00:03.50]Translation\n[00:09.75]Repeated\n"


def test_plain_text_is_never_assigned_timestamps():
    assert lyrics.parse_synced("Plain lyrics\nAnother line") == []
    assert lyrics.format_lrc([]) == ""
    assert lyrics.parse_synced("[offset:-250]\n[00:01]Delayed") == [(1.25, "Delayed")]
    assert lyrics.parse_synced("[offset:500]\n[00:00.20]At start") == [(0.0, "At start")]


def test_format_rounds_carry_and_removes_duplicates():
    assert lyrics.format_lrc([(59.999, "Carry"), (2.05, "\nEarlier\n"), (2.05, "Earlier")]) == "[00:02.05]Earlier\n[01:00.00]Carry\n"
    with pytest.raises(ValueError):
        lyrics.format_lrc([(float("nan"), "Invalid")])


def test_search_encodes_only_metadata_and_ranks_exact_version(monkeypatch):
    calls = network(monkeypatch, [record(1, trackName="Test Song (Remix)", duration=200), record(2), record(3, duration=207), record(4, syncedLyrics=None)])
    result = lyrics.OnlineLyricsClient(timeout=6).search("", track_name="Test Song", artist_name="Artist", duration_seconds=200)
    req, timeout = calls[0]
    assert urlsplit(req.full_url).hostname == "lrclib.net"
    assert urlsplit(req.full_url).scheme == "https"
    assert parse_qs(urlsplit(req.full_url).query) == {"track_name": ["Test Song"], "artist_name": ["Artist"]}
    assert req.data is None and "VoiceStudio/" in req.headers["User-agent"] and timeout == 6
    assert result[0]["id"] == 2 and result[0]["recommended"] is True
    assert all(not item["recommended"] for item in result if item["id"] != 2)
    assert next(item for item in result if item["id"] == 4)["synced"] == ""
    assert next(item for item in result if item["id"] == 4)["plain"] == "First line\nSecond line"


def test_query_unicode_encoding_and_result_limit(monkeypatch):
    calls = network(monkeypatch, [record(n) for n in range(1, 26)] + [record(1)])
    result = lyrics.OnlineLyricsClient().search("中文歌名 & Artist")
    assert len(result) == 20
    assert parse_qs(urlsplit(calls[0][0].full_url).query) == {"q": ["中文歌名 & Artist"]}
    assert all(not item["recommended"] for item in result)


def test_get_normalizes_preserves_source_and_checks_selected_id(monkeypatch):
    network(monkeypatch, record(7, syncedLyrics="[offset:100]\n[00:08.00]Line"))
    result = lyrics.OnlineLyricsClient().get("7")
    assert result["synced"] == "[00:07.90]Line\n"
    assert result["source"] == "LRCLIB" and result["source_url"] == "https://lrclib.net/api/get/7"
    network(monkeypatch, record(8))
    with pytest.raises(lyrics.OnlineLyricsError, match="不一致"):
        lyrics.OnlineLyricsClient().get(7)


@pytest.mark.parametrize("record_id", [True, 0, -1, "../../x", "01", 1.5, 2**63])
def test_invalid_id_never_sends_a_request(monkeypatch, record_id):
    calls = network(monkeypatch, record())
    with pytest.raises(ValueError, match="ID"):
        lyrics.OnlineLyricsClient().get(record_id)
    assert calls == []


@pytest.mark.parametrize("code,message", [(404, "未找到"), (429, "过于频繁"), (503, "暂时不可用")])
def test_http_errors_are_user_readable(monkeypatch, code, message):
    network(monkeypatch, exception=HTTPError("https://lrclib.net/api/get/1", code, "private body", {}, None))
    with pytest.raises(lyrics.OnlineLyricsError, match=message):
        lyrics.OnlineLyricsClient().get(1)


@pytest.mark.parametrize("failure,message", [(socket.timeout(), "超时"), (URLError(socket.timeout()), "超时"), (URLError("offline"), "无法连接")])
def test_network_errors_are_user_readable(monkeypatch, failure, message):
    network(monkeypatch, exception=failure)
    with pytest.raises(lyrics.OnlineLyricsError, match=message):
        lyrics.OnlineLyricsClient().search("Song")


def test_actual_read_is_bounded_even_without_content_length(monkeypatch):
    monkeypatch.setattr(lyrics, "MAX_RESPONSE_BYTES", 100)
    network(monkeypatch, body=b" " * 101)
    with pytest.raises(lyrics.OnlineLyricsError, match="过大"):
        lyrics.OnlineLyricsClient().search("Song")
    network(monkeypatch, body=b"[]", headers={"Content-Length": "101"})
    with pytest.raises(lyrics.OnlineLyricsError, match="过大"):
        lyrics.OnlineLyricsClient().search("Song")


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b"{}", b"[null]", b"[{\"id\":true}]"])
def test_invalid_encoding_or_payload_is_rejected(monkeypatch, body):
    network(monkeypatch, body=body)
    with pytest.raises(lyrics.OnlineLyricsError):
        lyrics.OnlineLyricsClient().search("Song")


def test_instrumental_and_no_synced_are_explicit(monkeypatch):
    network(monkeypatch, record(instrumental=True, syncedLyrics=None, plainLyrics=None))
    result = lyrics.OnlineLyricsClient().get(1)
    assert result["instrumental"] is True and result["has_synced"] is False
    assert result["synced"] == "" and result["plain"] == ""


def test_documented_nullable_metadata_is_supported(monkeypatch):
    network(monkeypatch, [record(1, artistName=None, albumName=None, duration=None)])
    result = lyrics.OnlineLyricsClient().search("Song", track_name="Test Song", artist_name="Artist", duration_seconds=200)[0]
    assert result["artist"] == "" and result["album"] == "" and result["duration"] is None
    assert result["recommended"] is False and result["duration_delta"] is None


def test_untrusted_redirect_is_rejected_before_forwarding_metadata():
    req = lyrics.request.Request("https://lrclib.net/api/search?q=Song")
    with pytest.raises(lyrics.OnlineLyricsError, match="不受信任"):
        lyrics._FixedHostRedirect().redirect_request(req, None, 302, "Found", {}, "https://other.example/api/search")


@pytest.mark.parametrize("payload", [record(duration=float("nan")), record(syncedLyrics=12), record(instrumental="false"), record(trackName=""), record(artistName=False)])
def test_bad_record_fields_are_rejected(monkeypatch, payload):
    network(monkeypatch, payload)
    with pytest.raises(lyrics.OnlineLyricsError):
        lyrics.OnlineLyricsClient().get(1)
