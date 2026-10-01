"""Download lyrics off-thread; persist confirmed selections on the Qt thread."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot

from ....cover.online_lyrics import OnlineLyricsClient, format_lrc, parse_synced
from ....cover.project import CoverProject
from ....models import utc_now
from .base import WebService


class _Replies(QObject):
    ready = Signal(object)


class _Fetch(QRunnable):
    def __init__(self, client, ticket, replies):
        super().__init__()
        self.client, self.ticket, self.replies = client, ticket, replies

    def run(self):
        result = {"request_id": self.ticket["request_id"]}
        try:
            if self.ticket["kind"] == "search":
                data = self.ticket["parameters"]
                result["value"] = self.client.search(**data)
            else:
                result["value"] = self.ticket.get("cached_record") or self.client.get(self.ticket["record_id"])
        except Exception as exc:
            result["error"] = str(exc) or "联网歌词请求失败"
        try:
            self.replies.ready.emit(result)
        except RuntimeError:
            pass  # The window may have been destroyed while HTTP was pending.


def _revision(cover):
    path = cover.root / cover.lyrics_path if cover.lyrics_path else None
    stat = path.stat() if path and path.is_file() else None
    return (cover.lyrics_path, cover.lyrics_origin, dict(cover.lyrics_source),
            cover.lyrics_offset_ms, (stat.st_mtime_ns, stat.st_size) if stat else None)


class OnlineLyricsService(WebService):
    _CACHE_TTL_SECONDS = 600
    _CACHE_MAX_RECORDS = 40
    _CACHE_MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = OnlineLyricsClient()
        self._tickets: dict[str, dict[str, Any]] = {}
        self._candidate_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any], int]] = {}
        self._replies = _Replies(self)
        self._replies.ready.connect(self._finished, Qt.ConnectionType.QueuedConnection)

    def set_project(self, project):
        if Path(project).resolve() != self.project.resolve():
            self._tickets.clear()
            self._candidate_cache.clear()
        super().set_project(project)

    def close(self):
        self._tickets.clear()
        self._candidate_cache.clear()
        super().close()

    def _start(self, cover, kind, **fields):
        if self._closing:
            raise ValueError("窗口正在关闭")
        # A newer request supersedes the older one for this song and operation.
        for key, value in list(self._tickets.items()):
            if value["cover_id"] == cover.id and value["kind"] == kind:
                self._tickets.pop(key)
        if len(self._tickets) >= 8:
            raise ValueError("联网歌词请求较多，请稍后重试")
        identifier = uuid4().hex
        ticket = {"request_id": identifier, "cover_id": cover.id,
                  "project": str(self.project.resolve()), "kind": kind, **fields}
        self._tickets[identifier] = ticket
        QThreadPool.globalInstance().start(_Fetch(self.client, ticket, self._replies))
        return {"request_id": identifier, "cover_id": cover.id}

    def search(self, data):
        cover = CoverProject.load(self.project, str(data.get("cover_id", "")))
        parameters = {name: str(data.get(name, "")).strip() for name in ("query", "track_name", "artist_name")}
        if not parameters["query"] and not parameters["track_name"]:
            raise ValueError("请输入歌名或搜索关键词")
        if any(len(value) > 300 for value in parameters.values()):
            raise ValueError("搜索关键词过长")
        parameters["duration_seconds"] = cover.duration_ms / 1000 if cover.duration_ms else None
        return self._start(cover, "search", parameters=parameters)

    def download(self, data):
        cover = CoverProject.load(self.project, str(data.get("cover_id", "")))
        raw = data.get("record_id")
        if isinstance(raw, bool) or not str(raw).isdigit() or not 0 < int(raw) < 10**15:
            raise ValueError("请选择有效的歌词候选")
        if cover.lyrics_path and data.get("overwrite") is not True:
            raise ValueError("已有歌词，请确认是否替换")
        return self._start(cover, "download", record_id=int(raw), revision=_revision(cover),
                           cached_record=self._cached_candidate(cover, int(raw)))

    def set_offset(self, data):
        cover = CoverProject.load(self.project, str(data.get("cover_id", "")))
        value = data.get("offset_ms", 0)
        if isinstance(value, bool):
            raise ValueError("请输入有效的歌词偏移")
        try:
            numeric = float(value)
            offset = int(numeric)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("请输入有效的歌词偏移") from None
        if not math.isfinite(numeric) or offset != numeric or abs(offset) > 600000:
            raise ValueError("歌词偏移范围为 ±600 秒")
        path = cover.root / cover.lyrics_path if cover.lyrics_path else None
        if (not path or not path.is_file() or path.suffix.lower() != '.lrc'
                or path.stat().st_size > 2 * 1024 * 1024):
            raise ValueError("只有带时间戳的歌词可调整同步时间")
        try:
            parsed = parse_synced(path.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, ValueError):
            parsed = []
        if not parsed:
            raise ValueError("只有带时间戳的歌词可调整同步时间")
        cover.lyrics_offset_ms = offset
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return {"cover_id": cover.id, "offset_ms": offset}

    @staticmethod
    def _candidate(record, duration):
        synced = bool(parse_synced(str(record.get("synced") or "")))
        length = record.get("duration")
        delta = round(float(length) - duration, 2) if isinstance(length, (int, float)) and math.isfinite(length) and duration else None
        return {"record_id": record.get("id"), "title": record.get("title", ""),
                "artist": record.get("artist", ""), "album": record.get("album", ""),
                "duration_seconds": length, "duration_diff_seconds": delta,
                "synced": synced, "instrumental": bool(record.get("instrumental")),
                "source": "LRCLIB", "source_url": record.get("source_url", "")}

    @Slot(object)
    def _finished(self, reply):
        ticket = self._tickets.pop(reply.get("request_id"), None)
        if not ticket or self._closing or ticket["project"] != str(self.project.resolve()):
            return
        payload = {key: ticket[key] for key in ("request_id", "cover_id", "project")}
        topic = "lyrics.online." + ticket["kind"]
        try:
            cover = CoverProject.load(self.project, ticket["cover_id"])
            if reply.get("error"):
                raise ValueError(reply["error"])
            if ticket["kind"] == "search":
                self._remember_candidates(cover, reply["value"])
                payload["results"] = [self._candidate(record, cover.duration_ms / 1000) for record in reply["value"][:20]]
            else:
                if _revision(cover) != ticket["revision"]:
                    raise ValueError("等待下载期间歌词已改变，请重新确认替换")
                payload.update(self._save(cover, reply["value"], ticket["record_id"]))
            payload["ok"] = True
        except Exception as exc:
            payload.update(ok=False, error=str(exc) or "联网歌词操作失败")
        self.notify(topic, payload)

    def _remember_candidates(self, cover, records):
        """Cache bounded search results so choosing one needs no second HTTP request."""
        now = time.monotonic()
        project = str(self.project.resolve())
        self._candidate_cache = {
            key: value for key, value in self._candidate_cache.items()
            if value[0] > now and key[0] == project and key[1] != cover.id
        }
        used = sum(value[2] for value in self._candidate_cache.values())
        for record in records[:20]:
            identifier = record.get("id") if isinstance(record, dict) else None
            if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
                continue
            size = len(str(record.get("synced") or "").encode("utf-8")) + len(str(record.get("plain") or "").encode("utf-8"))
            if size > self._CACHE_MAX_BYTES:
                continue
            key = (project, cover.id, identifier)
            if key in self._candidate_cache:
                used -= self._candidate_cache.pop(key)[2]
            while self._candidate_cache and (len(self._candidate_cache) >= self._CACHE_MAX_RECORDS or used + size > self._CACHE_MAX_BYTES):
                oldest = min(self._candidate_cache, key=lambda item: self._candidate_cache[item][0])
                used -= self._candidate_cache.pop(oldest)[2]
            if used + size > self._CACHE_MAX_BYTES:
                continue
            self._candidate_cache[key] = (now + self._CACHE_TTL_SECONDS, record, size)
            used += size

    def _cached_candidate(self, cover, identifier):
        key = (str(self.project.resolve()), cover.id, identifier)
        cached = self._candidate_cache.get(key)
        if not cached:
            return None
        if cached[0] <= time.monotonic():
            self._candidate_cache.pop(key, None)
            return None
        return cached[1]

    def _save(self, cover, record, identifier):
        if not isinstance(record, dict) or record.get("id") != identifier:
            raise ValueError("歌词服务器返回了不匹配的歌曲，请重新搜索")
        lines = parse_synced(str(record.get("synced") or ""))
        plain = str(record.get("plain") or "").strip()
        if not lines and not plain:
            raise ValueError("此记录没有可用歌词，请选择其他候选")
        content = format_lrc(lines) if lines else plain + "\n"
        if len(content.encode('utf-8')) > 2 * 1024 * 1024:
            raise ValueError("歌词数据过大，下载已停止")
        source = {"kind": "online", "provider": "LRCLIB", "record_id": identifier,
                  "title": str(record.get("title", "")), "artist": str(record.get("artist", "")),
                  "source_url": f"https://lrclib.net/api/get/{identifier}",
                  "duration_seconds": record.get("duration"), "synced": bool(lines), "fetched_at": utc_now()}
        target = cover.root / 'lyrics' / (f'online-{identifier}' + ('.lrc' if lines else '.txt'))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.tmp')
        temporary.write_text(content, encoding='utf-8', newline='')
        temporary.replace(target)
        cover.lyrics_path = target.relative_to(cover.root).as_posix()
        cover.lyrics_origin = 'manual'  # User-selected data, compatible with old project loaders.
        cover.lyrics_source, cover.lyrics_offset_ms = source, 0
        cover.save()
        self.notify('songs.changed', {'cover_id': cover.id})
        return {"source": source, "title": source["title"], "artist": source["artist"],
                "synced": bool(lines), "line_count": len(lines) if lines else len(plain.splitlines())}
