# -*- coding: utf-8 -*-
"""歌词：多源聚合获取 LRC 歌词（含翻译），按播放进度给出当前行。

默认顺序：网易云 -> LRCLIB -> 自定义 API。
- 网易云（读本地日志得到歌曲 id）：直接按 id 取歌词；其它播放器按歌名搜索
- LRCLIB：免费公开接口，无需 Key，按歌名 / 歌手 / 时长挑选最接近的一条
- 自定义 API：设置里填 URL 模板即可作为最后兜底

信号 ready(key)：某首歌的歌词取到了（或确认没有），界面收到后重新查询 line()。
"""
from __future__ import annotations

import bisect
import difflib
import json
import re
import threading
from urllib.parse import quote

import httpx
from PySide6 import QtCore

from .. import __version__ as APP_VERSION

from wisland.core.bg import run_bg, ui
from wisland.core.config import cfg

API = "https://music.163.com/api"
LRCLIB = "https://lrclib.net/api"
LRCLIB_UA = f"wisland/{APP_VERSION} (desktop lyrics client)"
HEADERS = {"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
LRCLIB_HEADERS = {"User-Agent": LRCLIB_UA, "Accept": "application/json"}
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 开头的制作信息（作词 / 作曲 / 编曲 …）不当作歌词显示
_CREDIT = re.compile(r"^\s*[^\s:：]{1,12}\s*[:：]")
_BRACKET = re.compile(r"\s*[\(（\[【].*?[\)）\]】]\s*")
_FEAT = re.compile(r"\s*(?:\bfeat\.?|\bft\.?|\bfeaturing)\b.*$", re.I)
_SUFFIX = re.compile(
    r"\s*[-–—]\s*(?:from|live|remaster(?:ed)?|radio edit|acoustic|version|edit|ost|"
    r"soundtrack|instrumental|karaoke)\b.*$",
    re.I,
)
_SOURCE_ALIAS = {
    "netease": "netease", "163": "netease", "网易云": "netease", "网易云音乐": "netease",
    "lrclib": "lrclib", "lrclib.net": "lrclib",
    "custom": "custom", "自定义": "custom", "自建": "custom",
}
_JSON_LYRIC_KEYS = ("syncedLyrics", "synced_lyrics", "lyrics", "lyric", "lrc", "text")


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


def _clean_title(title: str) -> str:
    raw = (title or "").strip()
    t = _BRACKET.sub(" ", raw)
    t = _FEAT.sub(" ", t)
    t = _SUFFIX.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—")
    return t or raw


def _clean_artist(artist: str) -> str:
    return re.sub(r"\s+", " ", (artist or "").strip())


def _norm_text(value: str) -> str:
    value = (value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _title_score(want: str, got: str) -> int:
    a = _norm_text(_clean_title(want))
    b = _norm_text(_clean_title(got))
    if not a or not b:
        return 9
    if a == b:
        return 0
    if a in b or b in a:
        return 1
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.92:
        return 0
    if ratio >= 0.72:
        return 1
    return 9


def _artist_parts(artist: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*(?:,|&|/|、|;|\bfeat\.?|\bft\.?)\s*", artist or "",
                                        flags=re.I) if p.strip()]


def _artist_score(artist: str, names: list[str]) -> int:
    parts = _artist_parts(artist)
    if not parts:
        return 0
    norm = [_norm_text(n) for n in names if n]
    for want in parts:
        w = _norm_text(want)
        if not w:
            continue
        if any(w == n or (len(w) >= 2 and (w in n or n in w)) for n in norm):
            return 0
    return 1


def _duration_of(m: dict) -> float:
    try:
        dur = float(m.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0
    return dur if 30 <= dur <= 15 * 60 else 0.0


def _pick(cands: list[dict], title: str, artist: str, duration: float) -> dict | None:
    """按歌名、歌手、时长从候选里选最可信的一条；没有足够把握返回 None。"""
    scored = []
    for c in cands:
        ts = _title_score(title, c.get("title") or "")
        if ts >= 9:
            continue
        cdur = float(c.get("duration") or 0)
        if duration:
            diff = abs(cdur - duration) if cdur > 0 else 99.0
            if diff > 8.0:
                continue
        else:
            # 不知道时长时只接受精确歌名 + 歌手能对上，避免随机匹配到翻唱 / Live
            diff = 0.0
            if ts != 0:
                continue
            if artist and _artist_score(artist, c.get("artists") or []) != 0:
                continue
        ascore = _artist_score(artist, c.get("artists") or [])
        scored.append((ts, ascore, diff, c))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return scored[0][3]


def _lyric_from_lrclib(obj: dict) -> "Lyrics | None":
    if not isinstance(obj, dict) or obj.get("instrumental"):
        return None
    lines = parse_lrc(str(obj.get("syncedLyrics") or ""))
    return Lyrics(lines, []) if lines else None


def _find_json_lyric(obj) -> str:
    if isinstance(obj, str):
        return obj if _TS.search(obj) else ""
    if isinstance(obj, dict):
        for key in _JSON_LYRIC_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and _TS.search(value):
                return value
        for value in obj.values():
            found = _find_json_lyric(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_json_lyric(value)
            if found:
                return found
    return ""


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
        return "q:" + (m.get("title") or "") + "|" + (m.get("artist") or "")

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

    def clear(self):
        """歌词源配置变化后清掉缓存，让当前歌曲按新顺序重新取一次。"""
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _sources() -> list[str]:
        raw = str(cfg.get("media.lyrics_sources", "netease,lrclib,custom") or "")
        out = []
        for token in re.split(r"[,，、;\s]+", raw):
            key = _SOURCE_ALIAS.get(token.strip().casefold(), token.strip().casefold())
            if key in ("netease", "lrclib", "custom") and key not in out:
                out.append(key)
        return out or ["netease", "lrclib", "custom"]

    # ---------------- 网络 ----------------
    def _fetch(self, m: dict) -> Lyrics | None:
        for source in self._sources():
            try:
                if source == "netease":
                    lyr = self._fetch_netease(m)
                elif source == "lrclib":
                    lyr = self._fetch_lrclib(m)
                else:
                    lyr = self._fetch_custom(m)
            except Exception as e:  # noqa: BLE001  单个源失败不影响后续回退
                print(f"[lyrics] {source} 获取失败：{e}")
                lyr = None
            if lyr:
                return lyr
        return None

    # ---------------- 网易云 ----------------
    def _fetch_netease(self, m: dict) -> Lyrics | None:
        sid = str(m.get("song_id") or "").strip()
        if sid:
            lyr = self._netease_lyric(sid)
            if lyr:
                return lyr
        title = _clean_title(m.get("title") or "")
        if not title:
            return None
        artist = _clean_artist(m.get("artist") or "")
        duration = _duration_of(m)
        queries = [title]
        if artist:
            queries.append(f"{title} {artist}")
        tried: set[str] = set()
        for query in queries:
            songs = self._netease_search(query)
            cands = []
            for song in songs:
                artists = song.get("artists") or song.get("ar") or []
                if not isinstance(artists, list):
                    artists = []
                cands.append({
                    "title": str(song.get("name") or ""),
                    "artists": [str(a.get("name") or "") for a in artists if isinstance(a, dict)],
                    "duration": float(song.get("duration") or 0) / 1000.0,
                    "raw": song,
                })
            best = _pick(cands, title, artist, duration)
            if not best:
                continue
            song_id = str((best.get("raw") or {}).get("id") or "")
            if not song_id or song_id in tried:
                continue
            tried.add(song_id)
            lyr = self._netease_lyric(song_id)
            if lyr:
                return lyr
        return None

    def _netease_search(self, query: str) -> list[dict]:
        r = self._client.get(API + "/search/get/web", params={"s": query, "type": 1, "limit": 8})
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("songs")) or []

    def _netease_lyric(self, sid: str) -> Lyrics | None:
        r = self._client.get(API + "/song/lyric", params={"id": sid, "lv": 1, "tv": -1})
        r.raise_for_status()
        j = r.json()
        if j.get("nolyric") or j.get("uncollected"):
            return None
        lines = parse_lrc((j.get("lrc") or {}).get("lyric", ""))
        if not lines:
            return None
        trans = parse_lrc((j.get("tlyric") or {}).get("lyric", ""))
        return Lyrics(lines, trans)

    # ---------------- LRCLIB ----------------
    def _fetch_lrclib(self, m: dict) -> Lyrics | None:
        title = _clean_title(m.get("title") or "")
        if not title:
            return None
        artist = _clean_artist(m.get("artist") or "")
        album = _clean_artist(m.get("album") or "")
        duration = _duration_of(m)

        # 1) 精确查询；LRCLIB 的 /get 需要歌名 + 歌手 + 时长完全对得上
        params = {"track_name": title}
        if artist:
            params["artist_name"] = artist
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = str(int(round(duration)))
        try:
            r = self._client.get(LRCLIB + "/get", params=params, headers=LRCLIB_HEADERS)
            if r.status_code == 200:
                lyr = _lyric_from_lrclib(r.json())
                if lyr:
                    return lyr
        except (httpx.HTTPError, ValueError):
            pass

        # 2) 搜索；先带歌手，不行再只按歌名。LRCLIB 上很多中文歌的歌手是中文名，
        #    Spotify 的英文歌手名对不上时，只按歌名 + 时长仍能选到正确版本。
        searches = []
        if artist:
            searches.append({"track_name": title, "artist_name": artist})
        searches.append({"track_name": title})
        seen: set[tuple] = set()
        for params in searches:
            key = tuple(sorted(params.items()))
            if key in seen:
                continue
            seen.add(key)
            r = self._client.get(LRCLIB + "/search", params=params, headers=LRCLIB_HEADERS)
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            cands = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get("instrumental") or not str(item.get("syncedLyrics") or "").strip():
                    continue
                cands.append({
                    "title": str(item.get("trackName") or ""),
                    "artists": [str(item.get("artistName") or "")],
                    "duration": float(item.get("duration") or 0),
                    "raw": item,
                })
            best = _pick(cands, title, artist, duration)
            if best:
                lyr = _lyric_from_lrclib(best.get("raw") or {})
                if lyr:
                    return lyr
        return None

    # ---------------- 自定义 API ----------------
    def _fetch_custom(self, m: dict) -> Lyrics | None:
        template = str(cfg.get("media.lyrics_custom_url", "") or "").strip()
        if not template or not template.lower().startswith(("http://", "https://")):
            return None
        duration = _duration_of(m)
        values = {
            "title": m.get("title") or "",
            "artist": m.get("artist") or "",
            "album": m.get("album") or "",
            "duration": str(int(round(duration))) if duration else "",
            "song_id": m.get("song_id") or "",
        }
        try:
            url = template.format(**{k: quote(str(v), safe="") for k, v in values.items()})
        except (KeyError, ValueError):
            return None
        r = self._client.get(url, headers=LRCLIB_HEADERS)
        r.raise_for_status()
        text = ""
        try:
            text = _find_json_lyric(r.json())
        except (ValueError, TypeError):
            text = r.text
        if not text:
            text = r.text
        lines = parse_lrc(text)
        return Lyrics(lines, []) if lines else None
