"""Exercise the real Qt async boundary with controlled HTTP-shaped clients.

No network, personal projects or audio engines are used. A held HTTP response
lets these tests reproduce edits, project changes and competing downloads.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.data import StudioSnapshot


def _record(identifier="101", *, synced="[00:01.50]联网第一句\n[00:03.00]联网第二句\n", plain=""):
    return {"id": int(identifier), "title": "测试歌曲", "artist": "测试歌手", "album": "测试专辑",
            "duration": 6, "instrumental": False, "synced": synced, "plain": plain,
            "source": "LRCLIB", "source_url": f"https://lrclib.net/api/get/{identifier}"}


class ControlledClient:
    def __init__(self):
        self.records = {"101": _record(), "202": _record("202", synced="[00:02.00]第二次下载的歌词\n")}
        self.gates = {identifier: threading.Event() for identifier in self.records}
        self.started = {identifier: threading.Event() for identifier in self.records}
        self.finished = {identifier: threading.Event() for identifier in self.records}
        self.search_gate = threading.Event()
        self.search_gate.set()
        self.search_started = threading.Event()
        self.http_threads = []
        self.error = None

    def search(self, *args, **kwargs):
        self.http_threads.append(threading.get_ident())
        self.search_started.set()
        assert self.search_gate.wait(5), "test HTTP search response was not released"
        return [dict(record) for record in self.records.values()]

    def get(self, identifier, *args, **kwargs):
        identifier = str(identifier)
        self.http_threads.append(threading.get_ident())
        self.started[identifier].set()
        try:
            assert self.gates[identifier].wait(5), "test HTTP download response was not released"
            if self.error is not None:
                raise self.error
            return dict(self.records[identifier])
        finally:
            self.finished[identifier].set()

    def release_all(self):
        self.search_gate.set()
        for gate in self.gates.values():
            gate.set()


def _until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Qt async result did not arrive before timeout")


@pytest.fixture
def setup_service(tmp_path):
    # Imported here so the test module can be prepared before the service lands.
    from local_voice_studio.ui.web.services.lyrics_online import OnlineLyricsService
    app = QApplication.instance() or QApplication([])
    data = tmp_path / "data"
    paths = AppPaths(data, tmp_path / "projects", data / "runtime", data / "engine",
                     data / "models", data / "logs", data / "studio.sqlite3", data / "cache")
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("联网歌词隔离测试")
    cover = CoverProject.create(project, title="测试歌曲")
    service = OnlineLyricsService(paths, store, project, None)
    client = ControlledClient()
    service.client = client
    events = []
    service.event.connect(lambda name, payload: events.append((name, json.loads(payload), threading.get_ident())))
    yield service, client, events, paths, store, project, cover
    service.close()
    client.release_all()
    QThreadPool.globalInstance().waitForDone(6000)
    app.processEvents()


def _request(service, cover, identifier="101", **extra):
    return service.download({"cover_id": cover.id, "record_id": identifier, **extra})["request_id"]


def _event(events, name, request_id):
    return next((payload for event, payload, _thread in events
                 if event == name and payload.get("request_id") == request_id), None)


def _text(cover):
    current = CoverProject.load(Path(cover.project_root), cover.id)
    return (current.root / current.lyrics_path).read_text(encoding="utf-8") if current.lyrics_path else ""


def _manual(cover, text="[00:00.00]人工歌词\n"):
    target = cover.root / "lyrics" / "manual.lrc"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    cover.set_lyrics(target, origin="manual")
    return target


def test_search_returns_promptly_and_delivers_normalized_results_on_main_thread(setup_service):
    service, client, events, _paths, _store, project, cover = setup_service
    main_thread = threading.get_ident()
    client.search_gate.clear()
    started = time.monotonic()
    request = service.search({"cover_id": cover.id, "query": "测试歌曲", "track_name": "测试歌曲", "artist_name": "测试歌手"})
    assert time.monotonic() - started < 0.3
    _until(client.search_started.is_set)
    assert not _event(events, "lyrics.online.search", request["request_id"])
    client.search_gate.set()
    _until(lambda: _event(events, "lyrics.online.search", request["request_id"]))
    payload = _event(events, "lyrics.online.search", request["request_id"])
    assert payload["cover_id"] == cover.id and Path(payload["project"]) == project
    assert len(payload["results"]) == 2 and str(payload["results"][0]["record_id"]) == "101"
    assert client.http_threads and all(thread != main_thread for thread in client.http_threads)
    assert all(thread == main_thread for _event_name, _payload, thread in events)


def test_selected_search_result_persists_received_lyrics_without_second_http_request(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    search_id = service.search({"cover_id": cover.id, "query": "测试歌曲", "track_name": "测试歌曲"})["request_id"]
    _until(lambda: _event(events, "lyrics.online.search", search_id))
    result = _event(events, "lyrics.online.search", search_id)
    assert result["ok"] and result["results"]

    request_id = _request(service, cover, str(result["results"][0]["record_id"]))
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    payload = _event(events, "lyrics.online.download", request_id)
    assert payload["ok"] and payload["synced"]
    assert not client.started["101"].is_set() and not client.started["202"].is_set()
    assert _text(cover) == client.records[str(result["results"][0]["record_id"])]["synced"]


def test_candidate_cache_is_invalidated_when_project_changes(setup_service):
    service, client, events, _paths, store, project, cover = setup_service
    search_id = service.search({"cover_id": cover.id, "query": "测试歌曲", "track_name": "测试歌曲"})["request_id"]
    _until(lambda: _event(events, "lyrics.online.search", search_id))
    other_project = store.create_project("另一个隔离工程")
    service.set_project(other_project)
    assert not service._candidate_cache


def test_expired_candidate_uses_normal_background_get(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    search_id = service.search({"cover_id": cover.id, "query": "测试歌曲", "track_name": "测试歌曲"})["request_id"]
    _until(lambda: _event(events, "lyrics.online.search", search_id))
    for key, (expires, record, size) in list(service._candidate_cache.items()):
        service._candidate_cache[key] = (time.monotonic() - 1, record, size)
    request_id = _request(service, cover)
    _until(client.started["101"].is_set)
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    assert _event(events, "lyrics.online.download", request_id)["ok"]


def test_download_http_is_background_but_owned_file_and_manifest_commit_on_main_thread(setup_service, monkeypatch):
    service, client, events, _paths, _store, project, cover = setup_service
    commits = []
    main_thread = threading.get_ident()
    save = CoverProject.save
    def record_save(self, *args, **kwargs):
        commits.append(threading.get_ident())
        return save(self, *args, **kwargs)
    monkeypatch.setattr(CoverProject, "save", record_save)
    started = time.monotonic()
    request_id = _request(service, cover)
    assert time.monotonic() - started < 0.3
    _until(client.started["101"].is_set)
    assert not commits and CoverProject.load(project, cover.id).lyrics_path == ""
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    payload = _event(events, "lyrics.online.download", request_id)
    assert payload["ok"] and payload["synced"] and payload["line_count"] == 2, payload
    restored = CoverProject.load(project, cover.id)
    target = (restored.root / restored.lyrics_path).resolve()
    assert restored.root.resolve() in target.parents and target.suffix == ".lrc"
    assert target.read_text(encoding="utf-8") == client.records["101"]["synced"]
    assert restored.lyrics_source["provider"] == "LRCLIB"
    assert str(restored.lyrics_source["record_id"]) == "101"
    assert commits and all(thread == main_thread for thread in commits)
    assert all(thread != main_thread for thread in client.http_threads)


@pytest.mark.parametrize("invalidate", ["project", "delete", "close"])
def test_invalidated_download_cannot_write_into_old_or_new_project(setup_service, invalidate, capsys):
    service, client, events, _paths, store, project, cover = setup_service
    request_id = _request(service, cover)
    _until(client.started["101"].is_set)
    other_project = store.create_project("另一个隔离工程")
    if invalidate == "project":
        service.set_project(other_project)
    elif invalidate == "delete":
        trash = project / ".trash" / cover.id
        trash.parent.mkdir(parents=True, exist_ok=True)
        assert project.resolve() in trash.resolve().parents
        assert project.resolve() in cover.root.resolve().parents
        cover.root.rename(trash)
    else:
        service.close()
        # Simulate destruction of the actual window-owned QObject tree while
        # HTTP is still held, including its child reply-signal object.
        service.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    client.gates["101"].set()
    _until(client.finished["101"].is_set)
    QTest.qWait(100)
    if invalidate == "delete":
        assert not cover.root.exists(), "late HTTP completion resurrected a deleted song"
    else:
        assert CoverProject.load(project, cover.id).lyrics_path == ""
    assert CoverProject.list(other_project) == []
    payload = _event(events, "lyrics.online.download", request_id)
    assert payload is None or not payload.get("ok")
    output = capsys.readouterr()
    assert "Traceback" not in output.err and "RuntimeError" not in output.err, output.err


def test_latest_download_wins_when_older_http_response_arrives_last(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    first = _request(service, cover, "101", overwrite=True)
    _until(client.started["101"].is_set)
    second = _request(service, cover, "202", overwrite=True)
    _until(client.started["202"].is_set)
    client.gates["202"].set()
    _until(lambda: _event(events, "lyrics.online.download", second))
    assert _event(events, "lyrics.online.download", second)["ok"], events
    client.gates["101"].set()
    _until(client.finished["101"].is_set)
    QTest.qWait(100)
    assert _text(cover) == client.records["202"]["synced"]
    earlier = _event(events, "lyrics.online.download", first)
    assert earlier is None or not earlier.get("ok")


def test_existing_lyrics_require_explicit_overwrite_permission(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    _manual(cover)
    client.gates["101"].set()
    try:
        request_id = _request(service, cover)
    except ValueError:
        request_id = None
    if request_id:
        _until(lambda: _event(events, "lyrics.online.download", request_id))
        assert not _event(events, "lyrics.online.download", request_id).get("ok")
    assert _text(cover) == "[00:00.00]人工歌词\n"
    request_id = _request(service, cover, overwrite=True)
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    assert _event(events, "lyrics.online.download", request_id)["ok"], events
    assert _text(cover) == client.records["101"]["synced"]


def test_manual_edit_during_http_wait_is_preserved_even_with_overwrite(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    manual = _manual(cover, "[00:00.00]旧版人工歌词\n")
    request_id = _request(service, cover, overwrite=True)
    _until(client.started["101"].is_set)
    # Same path and same number of characters: a path-only revision is unsafe.
    manual.write_text("[00:00.00]新版人工歌词\n", encoding="utf-8")
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    assert not _event(events, "lyrics.online.download", request_id).get("ok")
    assert _text(cover) == "[00:00.00]新版人工歌词\n"


def test_synced_lyrics_offset_persists_and_snapshot_applies_it_once(setup_service):
    service, client, events, paths, store, project, cover = setup_service
    request_id = _request(service, cover)
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    assert _event(events, "lyrics.online.download", request_id)["ok"], events
    offset = service.set_offset({"cover_id": cover.id, "offset_ms": 2500})
    assert offset["offset_ms"] == 2500
    row = StudioSnapshot(paths, store, project).songs()[0]
    assert CoverProject.load(project, cover.id).lyrics_offset_ms == 2500
    assert [line["seconds"] for line in row["lyrics"]] == [4.0, 5.5]
    assert _text(cover).startswith("[00:01.50]"), "offset must not be baked into source timestamps"


def test_plain_download_keeps_text_without_inventing_timestamps(setup_service):
    service, client, events, paths, store, project, cover = setup_service
    client.records["101"] = _record(synced="", plain="只有文本第一行\n只有文本第二行\n")
    request_id = _request(service, cover)
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    payload = _event(events, "lyrics.online.download", request_id)
    assert payload["ok"] and payload["synced"] is False and payload["line_count"] == 2, payload
    restored = CoverProject.load(project, cover.id)
    assert Path(restored.lyrics_path).suffix == ".txt" and restored.lyrics_source["synced"] is False
    row = StudioSnapshot(paths, store, project).songs()[0]
    assert [line["text"] for line in row["lyrics"]] == ["只有文本第一行", "只有文本第二行"]
    assert all(line["seconds"] is None and line["time"] == "文本" for line in row["lyrics"])
    assert "[00:" not in _text(cover)


def test_plain_lyrics_reject_sync_offset_without_inventing_timing(setup_service):
    service, client, events, _paths, _store, project, cover = setup_service
    client.records["101"] = _record(synced="", plain="没有时间戳的歌词\n")
    request_id = _request(service, cover)
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    assert _event(events, "lyrics.online.download", request_id)["ok"], events
    with pytest.raises(ValueError, match="时间戳"):
        service.set_offset({"cover_id": cover.id, "offset_ms": 2500})
    assert CoverProject.load(project, cover.id).lyrics_offset_ms == 0


def test_http_error_is_reported_without_overwriting_existing_lyrics(setup_service):
    service, client, events, _paths, _store, _project, cover = setup_service
    _manual(cover)
    client.error = RuntimeError("HTTP 503: upstream unavailable")
    request_id = _request(service, cover, overwrite=True)
    client.gates["101"].set()
    _until(lambda: _event(events, "lyrics.online.download", request_id))
    payload = _event(events, "lyrics.online.download", request_id)
    assert not payload.get("ok") and "503" in payload["error"]
    assert _text(cover) == "[00:00.00]人工歌词\n"
