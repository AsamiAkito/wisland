# -*- coding: utf-8 -*-
"""媒体会话：Windows GlobalSystemMediaTransportControls（SMTC）。

所有 WinRT 调用都在一个专用 asyncio 线程中执行；信号从该线程发出（Qt 自动排队到主线程）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import threading
import time
import traceback

from PySide6 import QtCore, QtGui

from wisland.core.config import cfg

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
    )
    from winrt.windows.storage.streams import Buffer as _Buffer, InputStreamOptions as _ISO
except Exception:  # noqa: BLE001
    traceback.print_exc()
    _Manager = None

POLL_S = 0.7
_PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING

_FRIENDLY = [
    ("cloudmusic", "网易云音乐"),
    ("bilibili", "哔哩哔哩"),
    ("chrome", "Chrome"),
    ("zen", "Zen"),
    ("msedge", "Edge"),
    ("firefox", "Firefox"),
    ("spotify", "Spotify"),
    ("qqmusic", "QQ音乐"),
]


def friendly_name(app_id: str) -> str:
    low = (app_id or "").lower()
    for key, name in _FRIENDLY:
        if key in low:
            return name
    s = app_id or ""
    for sep in ("!", "\\", "/"):
        s = s.rsplit(sep, 1)[-1]
    if s.lower().endswith(".exe"):
        s = s[:-4]
    return s or "媒体"


def _secs(v) -> float:
    """TimeSpan -> 秒（pywinrt 映射为 datetime.timedelta；兼容 int ticks）。"""
    if v is None:
        return 0.0
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    try:
        return float(v) / 1e7
    except Exception:  # noqa: BLE001
        return 0.0


def _empty_state() -> dict:
    return {
        "has_session": False, "app_id": "", "app_name": "", "title": "", "artist": "",
        "album": "", "playing": False, "position": 0.0, "duration": 0.0,
        "stamp": time.monotonic(), "can_prev": False, "can_next": False,
    }


class MediaService(QtCore.QObject):
    changed = QtCore.Signal(dict)
    cover_changed = QtCore.Signal(object)   # QtGui.QImage | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state: dict = _empty_state()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mgr = None
        self._session = None
        self._track_key = None
        self._cover_tries = 0
        self._last_emit = None

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if _Manager is None:
            self.changed.emit(dict(self.state))
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wisland-media", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self._loop = None
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _main(self):
        while not self._stop.is_set():
            try:
                if self._mgr is None:
                    self._mgr = await _Manager.request_async()
                await self._poll()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._mgr = None
                self._session = None
            # 分段睡眠以便快速退出
            end = time.monotonic() + POLL_S
            while not self._stop.is_set() and time.monotonic() < end:
                await asyncio.sleep(0.1)

    # ---------------- 轮询 ----------------
    def _choose(self, sessions):
        prio = [str(p).lower() for p in (cfg.get("media.priority") or [])]

        def rank(s):
            aid = (s.source_app_user_model_id or "").lower()
            for i, p in enumerate(prio):
                if p in aid:
                    return i
            return len(prio)

        cands = []
        for s in sessions:
            try:
                info = s.get_playback_info()
                playing = info is not None and int(info.playback_status) == _PLAYING
            except Exception:  # noqa: BLE001
                playing = False
            cands.append((0 if playing else 1, rank(s), s))
        if not cands:
            return None
        cands.sort(key=lambda t: (t[0], t[1]))
        return cands[0][2]

    async def _poll(self):
        sessions = list(self._mgr.get_sessions() or [])
        s = self._choose(sessions)
        self._session = s
        if s is None:
            st = _empty_state()
            if self.state.get("has_session") or self._last_emit is None:
                self._track_key = None
                self.cover_changed.emit(None)
                self._publish(st, force=True)
            return

        app_id = s.source_app_user_model_id or ""
        try:
            props = await s.try_get_media_properties_async()
        except Exception:  # noqa: BLE001
            props = None
        title = (props.title if props else "") or ""
        artist = (props.artist if props else "") or ""
        album = (props.album_title if props else "") or ""

        info = s.get_playback_info()
        playing = bool(info and int(info.playback_status) == _PLAYING)
        can_prev = can_next = False
        if info and info.controls:
            can_prev = bool(info.controls.is_previous_enabled)
            can_next = bool(info.controls.is_next_enabled)

        pos = dur = 0.0
        try:
            tl = s.get_timeline_properties()
            if tl:
                start = _secs(tl.start_time)
                dur = max(0.0, _secs(tl.end_time) - start)
                pos = max(0.0, _secs(tl.position) - start)
                # 按 last_updated_time 外推到“现在”
                if playing:
                    lut = tl.last_updated_time
                    if isinstance(lut, _dt.datetime) and lut.year > 1601:
                        now = _dt.datetime.now(_dt.timezone.utc)
                        if lut.tzinfo is None:
                            lut = lut.replace(tzinfo=_dt.timezone.utc)
                        age = (now - lut).total_seconds()
                        if 0 < age < 6 * 3600:
                            pos += age
                if dur > 0:
                    pos = min(pos, dur)
        except Exception:  # noqa: BLE001
            pass

        st = {
            "has_session": True, "app_id": app_id, "app_name": friendly_name(app_id),
            "title": title, "artist": artist, "album": album, "playing": playing,
            "position": pos, "duration": dur, "stamp": time.monotonic(),
            "can_prev": can_prev, "can_next": can_next,
        }

        key = (app_id, title, artist, album)
        if key != self._track_key:
            self._track_key = key
            self._cover_tries = 0
            img = await self._read_cover(props)
            self.cover_changed.emit(img)
            if img is None:
                self._cover_tries = 1
        elif 0 < self._cover_tries < 6:
            # 浏览器等常常在标题之后才给出缩略图，重试几次
            self._cover_tries += 1
            img = await self._read_cover(props)
            if img is not None:
                self._cover_tries = 0
                self.cover_changed.emit(img)
        self._publish(st)

    def _publish(self, st: dict, force: bool = False):
        cmp = {k: v for k, v in st.items() if k not in ("stamp", "position")}
        prev = self.state
        drift = False
        if prev.get("has_session") and st.get("has_session"):
            # 位置与外推值差太多（拖动进度、暂停时）才视为变化
            expect = prev.get("position", 0.0)
            if prev.get("playing"):
                expect += st["stamp"] - prev.get("stamp", st["stamp"])
            drift = abs(expect - st["position"]) > 1.5
        self.state = st
        if force or drift or cmp != self._last_emit:
            self._last_emit = cmp
            self.changed.emit(dict(st))

    async def _read_cover(self, props):
        try:
            thumb = props.thumbnail if props else None
            if thumb is None:
                return None
            stream = await thumb.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 20 * 1024 * 1024:
                return None
            buf = _Buffer(size)
            out = await stream.read_async(buf, size, _ISO.READ_AHEAD)
            data = bytes(memoryview(out))[: int(out.length)]
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            img = QtGui.QImage.fromData(data)
            return None if img.isNull() else img
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return None

    # ---------------- 控制 ----------------
    def _submit(self, make_coro):
        loop = self._loop
        if loop is None:
            return

        async def wrapper():
            try:
                s = self._session
                if s is not None:
                    await make_coro(s)
                    await asyncio.sleep(0.15)
                    await self._poll()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        try:
            asyncio.run_coroutine_threadsafe(wrapper(), loop)
        except RuntimeError:
            pass

    def play_pause(self):
        self._submit(lambda s: s.try_toggle_play_pause_async())

    def next(self):
        self._submit(lambda s: s.try_skip_next_async())

    def prev(self):
        self._submit(lambda s: s.try_skip_previous_async())

    def seek(self, seconds: float):
        def go(s):
            start = 0.0
            try:
                start = _secs(s.get_timeline_properties().start_time)
            except Exception:  # noqa: BLE001
                pass
            return s.try_change_playback_position_async(int((max(0.0, seconds) + start) * 1e7))
        self._submit(go)
