"""Metadata-only LRCLIB lookup; selecting/applying lyrics belongs to the UI.

The public API is documented at https://lrclib.net/docs. No audio, local paths,
or project contents are sent. A search produces candidates, never a replacement
for an existing LRC, and plain lyrics are never given invented timestamps.
"""
from __future__ import annotations

import json
import math
import re
import socket
import unicodedata
from collections.abc import Iterable
from urllib import error, parse, request

API_ROOT = "https://lrclib.net/api"
USER_AGENT = "VoiceStudio/1.0 (local desktop lyrics client; https://lrclib.net)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 20
MAX_LYRICS_CHARS = 512 * 1024
_TIME_TAG = re.compile(r"\[(\d{1,5}):(\d{2})(?:[.:](\d{1,3}))?\]")
_TIMED_PREFIX = re.compile(r"^\s*((?:\[\d{1,5}:\d{2}(?:[.:]\d{1,3})?\]\s*)+)")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d{1,9})\s*\]", re.IGNORECASE)
_METADATA = re.compile(r"\[(?:ar|al|ti|by|offset|length|re|ve|au|id):[^\]]*\]", re.IGNORECASE)


class OnlineLyricsError(RuntimeError):
    """A user-readable lookup/response failure, without remote response bodies."""


def parse_synced(text: str) -> list[tuple[float, str]]:
    """Normalize line LRC, multi-time tags and a global millisecond offset.

    A positive LRC offset makes lyrics appear earlier; negative offsets delay
    them. Times before zero are clamped. Exact duplicate time/text pairs are
    removed, while different translations at the same time remain intact.
    """
    if not isinstance(text, str):
        raise ValueError("同步歌词必须是文本")
    if len(text) > MAX_LYRICS_CHARS:
        raise ValueError("歌词文本过大，请使用较小的 LRC 文件")
    text = text.lstrip("\ufeff")
    offsets = _OFFSET.findall(text)
    offset = int(offsets[-1]) / 1000 if offsets else 0.0
    if abs(offset) > 86400:
        raise ValueError("歌词时间偏移超出有效范围")
    found: set[tuple[float, str]] = set()
    lines: list[tuple[float, str]] = []
    for raw in text.splitlines():
        prefix = _TIMED_PREFIX.match(raw)
        if not prefix:
            continue
        body = _METADATA.sub("", raw[prefix.end():]).strip()
        if not body:
            continue
        for match in _TIME_TAG.finditer(prefix.group(1)):
            minutes, seconds = int(match[1]), int(match[2])
            if seconds >= 60:
                continue
            fraction = float("0." + match[3]) if match[3] else 0.0
            time = round(max(0.0, minutes * 60 + seconds + fraction - offset), 3)
            pair = (time, body)
            if pair not in found:
                found.add(pair)
                lines.append(pair)
    return sorted(lines, key=lambda row: row[0])


def format_lrc(lines: Iterable[tuple[float, str]]) -> str:
    """Serialize existing real timestamps as normalized centisecond LRC."""
    valid: list[tuple[float, str]] = []
    for seconds, text in lines:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("歌词时间必须是非负有限数值")
        if not isinstance(text, str):
            raise ValueError("歌词正文必须是文本")
        body = " ".join(text.splitlines()).strip()
        if body:
            valid.append((float(seconds), body))
    result = []
    seen = set()
    for seconds, body in sorted(valid, key=lambda row: row[0]):
        centiseconds = int(math.floor(seconds * 100 + 0.5))
        pair = (centiseconds, body)
        if pair in seen:
            continue
        seen.add(pair)
        minutes, remainder = divmod(centiseconds, 6000)
        result.append(f"[{minutes:02d}:{remainder // 100:02d}.{remainder % 100:02d}]{body}")
    return "\n".join(result) + ("\n" if result else "")


def _record_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError("歌词记录 ID 无效")
    number = int(value)
    if number > 2**63 - 1:
        raise ValueError("歌词记录 ID 无效")
    return number


def _metadata(value: object, label: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label}无效或过长")
    return value.strip()


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if c.isalnum())


class _FixedHostRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = parse.urlsplit(newurl)
            allowed = target.scheme == "https" and target.hostname == "lrclib.net" and target.port in (None, 443) and target.path.startswith("/api/") and not target.username and not target.password
        except ValueError:
            allowed = False
        if not allowed:
            raise OnlineLyricsError("歌词服务返回了不受信任的跳转地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class OnlineLyricsClient:
    """A bounded stdlib client for the fixed public LRCLIB HTTPS service."""

    def __init__(self, *, timeout: float = 12.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("联网歌词超时必须在 0 到 30 秒之间")
        self.timeout = float(timeout)

    def _fetch(self, path: str, params: dict[str, str] | None = None):
        url = API_ROOT + path
        if params:
            url += "?" + parse.urlencode(params)
        req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"})
        try:
            opener = request.build_opener(_FixedHostRedirect())
            with opener.open(req, timeout=self.timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        length = int(length)
                    except (TypeError, ValueError) as exc:
                        raise OnlineLyricsError("歌词服务返回了无效的响应长度") from exc
                    if length < 0 or length > MAX_RESPONSE_BYTES:
                        raise OnlineLyricsError("歌词服务响应过大，请缩小搜索范围")
                chunks, size = [], 0
                while True:
                    chunk = response.read(min(65536, MAX_RESPONSE_BYTES - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise OnlineLyricsError("歌词服务响应过大，请缩小搜索范围")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks).decode("utf-8-sig"))
        except error.HTTPError as exc:
            if exc.code == 429:
                message = "歌词服务请求过于频繁，请稍后重试"
            elif exc.code == 404:
                message = "未找到这条歌词记录，请重新搜索"
            else:
                message = f"歌词服务暂时不可用（HTTP {exc.code}），请稍后重试"
            raise OnlineLyricsError(message) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise OnlineLyricsError("联网歌词请求超时，请检查网络后重试") from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                message = "联网歌词请求超时，请检查网络后重试"
            else:
                message = "无法连接歌词服务，请检查网络后重试，也可以导入 LRC"
            raise OnlineLyricsError(message) from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OnlineLyricsError("歌词服务返回了无法读取的数据，请稍后重试") from exc
        except OSError as exc:
            raise OnlineLyricsError("歌词下载中断，请检查网络后重试") from exc

    @staticmethod
    def _candidate(value: object) -> dict:
        if not isinstance(value, dict):
            raise OnlineLyricsError("歌词服务返回了无效的记录")
        try:
            record_id = _record_id(value.get("id"))
            title = _metadata(value.get("trackName") or value.get("name"), "歌词曲名")
            artist_value, album_value = value.get("artistName"), value.get("albumName")
            artist = _metadata("" if artist_value is None else artist_value, "歌词歌手")
            album = _metadata("" if album_value is None else album_value, "歌词专辑")
            duration = value.get("duration")
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 86400):
                raise ValueError("歌词时长无效")
            instrumental = value.get("instrumental", False)
            if not isinstance(instrumental, bool) or not title:
                raise ValueError("歌词记录无效")
            synced, plain = value.get("syncedLyrics"), value.get("plainLyrics")
            for lyric in (synced, plain):
                if lyric is not None and (not isinstance(lyric, str) or len(lyric) > MAX_LYRICS_CHARS or "\x00" in lyric):
                    raise ValueError("歌词文本无效")
            normalized = format_lrc(parse_synced(synced or ""))
        except (ValueError, TypeError) as exc:
            raise OnlineLyricsError("歌词服务返回了无效的记录，请重新搜索") from exc
        return {
            "id": record_id, "title": title, "artist": artist, "album": album,
            "duration": float(duration) if duration is not None else None, "instrumental": instrumental,
            "synced": normalized, "plain": (plain or "").strip(),
            "has_synced": bool(normalized), "source": "LRCLIB",
            "source_url": f"{API_ROOT}/get/{record_id}",
        }

    def search(self, query: str = "", *, track_name: str = "", artist_name: str = "", duration_seconds: float | None = None) -> list[dict]:
        query = _metadata(query, "搜索关键词")
        track_name = _metadata(track_name, "歌曲名称")
        artist_name = _metadata(artist_name, "歌手名称")
        if not query and not track_name:
            raise ValueError("请输入歌曲名称或搜索关键词")
        if duration_seconds is not None and (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds) or not 0 <= duration_seconds <= 86400):
            raise ValueError("歌曲时长无效")
        params = {"q": query} if query else {"track_name": track_name}
        if not query and artist_name:
            params["artist_name"] = artist_name
        payload = self._fetch("/search", params)
        if not isinstance(payload, list) or len(payload) > 500:
            raise OnlineLyricsError("歌词服务返回了无效的搜索结果")
        candidates, ids = [], set()
        for item in payload:
            candidate = self._candidate(item)
            if candidate["id"] in ids:
                continue
            ids.add(candidate["id"])
            exact_title = bool(track_name) and _key(track_name) == _key(candidate["title"])
            exact_artist = bool(artist_name) and _key(artist_name) == _key(candidate["artist"])
            delta = abs(candidate["duration"] - duration_seconds) if duration_seconds is not None and candidate["duration"] is not None else None
            recommended = bool(exact_title and exact_artist and delta is not None and delta <= 2 and candidate["has_synced"])
            candidate.update(recommended=recommended, duration_delta=round(delta, 2) if delta is not None else None,
                             match_note="曲名、歌手和时长一致，仍请确认版本" if recommended else "请核对原版、混音版及时间轴后选择")
            candidate["_score"] = (recommended, exact_title, exact_artist, candidate["has_synced"], -(delta if delta is not None else math.inf))
            candidates.append(candidate)
        candidates.sort(key=lambda row: row["_score"], reverse=True)
        for candidate in candidates:
            candidate.pop("_score")
        return candidates[:MAX_RESULTS]

    def get(self, record_id: int | str) -> dict:
        record_id = _record_id(record_id)
        candidate = self._candidate(self._fetch(f"/get/{record_id}"))
        if candidate["id"] != record_id:
            raise OnlineLyricsError("歌词服务返回的记录与所选版本不一致")
        return candidate


__all__ = ["OnlineLyricsClient", "OnlineLyricsError", "parse_synced", "format_lrc"]
