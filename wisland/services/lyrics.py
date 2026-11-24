# -*- coding: utf-8 -*-
"""歌词：优先使用网易云音乐（含翻译），缺少时间轴歌词时回退到 LRCLIB。

- 网易云（读本地日志得到歌曲 id）：直接按 id 取歌词
- 其它播放器（QQ 音乐、Spotify、浏览器等）：按「歌名 + 歌手」搜索，取时长最接近的一首

信号 ready(key)：某首歌的歌词取到了（或确认没有），界面收到后重新查询 line()。
"""
from __future__ import annotations

import bisect
import re
import threading
import unicodedata

import httpx
from PySide6 import QtCore

from wisland.core.bg import run_bg, ui
from wisland.core.config import cfg

API = "https://music.163.com/api"
HEADERS = {"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 开头的制作信息（作词 / 作曲 / 编曲 …）不当作歌词显示
_CREDIT = re.compile(r"^\s*[^\s:：]{1,12}\s*[:：]")


def _name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    for raw in (text or "").splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():].strip()
        for m in stamps:
            frac = m.group(3) or "0"
            t = int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / (10 ** len(frac))
            out.append((t, body))
    out.sort(key=lambda x: x[0])
    # 去掉开头连续的制作信息行
    i = 0
    while i < len(out) and (not out[i][1] or (out[i][0] < 40 and _CREDIT.match(out[i][1]))):
        i += 1
    return out[i:]


class Lyrics:
    def __init__(self, lines: list[tuple[float, str]], trans: list[tuple[float, str]]):
        self.times = [t for t, _ in lines]
        self.lines = [s for _, s in lines]
        tmap = {round(t, 1): s for t, s in trans if s}
        self.trans = [tmap.get(round(t, 1), "") for t in self.times]

    def __bool__(self):
        return bool(self.lines)

    def at(self, pos: float) -> tuple[int, str, str]:
        """-> (行号, 歌词, 翻译)；还没到第一行时行号为 -1。"""
        i = bisect.bisect_right(self.times, pos + 0.15) - 1
        if i < 0:
            return -1, "", ""
        return i, self.lines[i], self.trans[i]


class LyricsService(QtCore.QObject):
    ready = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self._client = httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), headers=HEADERS)
        self._cache: dict[str, Lyrics | None] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def key(m: dict) -> str:
        if m.get("song_id"):
            return "id:" + str(m["song_id"])
        return "q:" + repr((m.get("title"), m.get("artist"), m.get("album"), round(m.get("duration") or 0)))

    def get(self, m: dict) -> Lyrics | None:
        """返回已缓存的歌词；没有时在后台获取，取到后发 ready(key)。"""
        if not cfg.get("media.lyrics", True) or not m.get("title"):
            return None
        k = self.key(m)
        with self._lock:
            if k in self._cache:
                return self._cache[k]
            if k in self._pending:
                return None
            self._pending.add(k)
        run_bg(self._fetch, dict(m), on_done=lambda res, k=k: self._done(k, res),
               on_error=lambda e, k=k: self._done(k, None))
        return None

    def _done(self, k: str, res):
        with self._lock:
            self._pending.discard(k)
            self._cache[k] = res
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        self.ready.emit(k)

    # ---------------- 网络 ----------------
    def _fetch(self, m: dict) -> Lyrics | None:
        try:
            lyrics = self._netease(m)
        except (httpx.HTTPError, ValueError):
            lyrics = None
        return lyrics or self._lrclib(m)

    def _netease(self, m: dict) -> Lyrics | None:
        sid = str(m.get("song_id") or "") or self._search(m)
        if not sid:
            return None
        r = self._client.get(API + "/song/lyric", params={"id": sid, "lv": 1, "tv": -1})
        r.raise_for_status()
        j = r.json()
        if j.get("nolyric") or j.get("uncollected"):
            return None
        lines = parse_lrc((j.get("lrc") or {}).get("lyric", ""))
        trans = parse_lrc((j.get("tlyric") or {}).get("lyric", ""))
        lyr = Lyrics(lines, trans)
        return lyr if lyr else None

    def _search(self, m: dict) -> str:
        dur = float(m.get("duration") or 0)
        if not (30 <= dur <= 15 * 60):
            return ""  # 避免把没有时间轴或很长的视频误配为歌曲
        title = re.sub(r"\s*[\(（\[【].*?[\)）\]】]\s*", " ", m.get("title") or "").strip() or m.get("title", "")
        q = f"{title} {m.get('artist') or ''}".strip()
        r = self._client.get(API + "/search/get/web", params={"s": q, "type": 1, "limit": 8})
        r.raise_for_status()
        songs = ((r.json().get("result") or {}).get("songs")) or []
        names = {_name(title), _name(m.get("title"))}
        matches = []
        for song in songs:
            delta = abs((song.get("duration") or 0) / 1000 - dur)
            if song.get("id") and _name(song.get("name")) in names and delta <= 8:
                matches.append((delta, str(song["id"])))
        return min(matches)[1] if matches else ""

    def _lrclib(self, m: dict) -> Lyrics | None:
        dur = float(m.get("duration") or 0)
        artist = m.get("artist") or ""
        if not artist.strip() or not (30 <= dur <= 15 * 60):
            return None
        params = {"track_name": m["title"], "artist_name": artist}
        response = self._client.get("https://lrclib.net/api/search", params=params,
                                    headers={"User-Agent": "Wisland (https://github.com/AsamiAkito/wisland)",
                                             "Referer": "https://lrclib.net/"})
        response.raise_for_status()
        matches = []
        for record in response.json():
            if (_name(record.get("trackName")) != _name(m["title"])
                    or _name(record.get("artistName")) != _name(artist)):
                continue
            delta = abs((record.get("duration") or 0) - dur)
            if delta <= 8:
                same_album = _name(record.get("albumName")) == _name(m.get("album"))
                matches.append((not same_album, delta, record))
        for _, _, record in sorted(matches, key=lambda item: item[:2]):
            lines = parse_lrc(record.get("syncedLyrics"))
            if lines:
                return Lyrics(lines, [])
        return None
