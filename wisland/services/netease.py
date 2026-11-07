# -*- coding: utf-8 -*-
"""网易云音乐播放状态：读取本地日志 cloudmusic.elog（无需插件、无需开启 SMTC）。

网易云 PC 版 3.x 默认不向 Windows 系统媒体控件（SMTC）报告播放信息；即使在设置里打开「开启SMTC」，
也不提供播放进度。但它会把播放事件写进 %LOCALAPPDATA%\\NetEase\\CloudMusic\\cloudmusic.elog：
  - "checkPlayPrivilege" / "playOneTrackInPlayingList"  当前歌曲（id / 歌名 / 歌手 / 专辑 / 时长 / 封面）
  - "native播放资源load完成"                              真正开始出声
  - "setPlayingPosition", 秒                             拖动进度条后的位置
  - "native播放state", 1|2                               1 = 播放，2 = 暂停
日志是逐字节替换编码的（解码公式来自社区项目 netease-cloudmusic-detector，MIT）。

只解析歌名 / 歌手 / 专辑 / 时长 / 歌曲 id / 封面 / 进度；日志里带签名的播放地址不会被读取或保存。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time

import psutil

ELOG = os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\cloudmusic.elog")
PRELOAD_BYTES = 4 * 1024 * 1024     # 启动时只回看日志末尾这么多字节
MAX_CARRY = 1024 * 1024

# 解码表：密文字节 -> 明文字节
_TABLE = bytes(((((b >> 4) ^ (((b & 15) + 8) % 16)) % 16) * 16 + (b >> 6) * 4 + ((~(b >> 4)) & 3)) & 0xFF
               for b in range(256))
_INV = bytearray(256)
for _b in range(256):
    _INV[_TABLE[_b]] = _b
NEWLINE = _INV[0x0A]                 # 日志里的换行也是编码过的

_HEADER = re.compile(r"^\[(\d+):(\d+):(\d{4}/\d{6}):(\d+):([A-Z]+):")
_SEEK = re.compile(r'"setPlayingPosition"\s*,\s*(-?\d+(?:\.\d+)?)')
_STATE = re.compile(r'"native[^"]*state"\s*,\s*(\d+)')
_EXIT = re.compile(r"Process\s+(\d+)\s+exited")

_k32 = ctypes.WinDLL("kernel32")
_k32.GetTickCount64.restype = ctypes.c_ulonglong


def _event_mono(tick_ms: int) -> float:
    """日志头里的时间戳是开机以来的毫秒数（GetTickCount），换算成 time.monotonic()。"""
    return time.monotonic() - max(0.0, (_k32.GetTickCount64() - tick_ms) / 1000.0)


def _json(line: str):
    a, b = line.find("{"), line.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(line[a:b + 1])
    except ValueError:
        return None


def _track(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    t = data.get("track") if isinstance(data.get("track"), dict) else data
    if not t.get("id"):
        return None
    album = t.get("album") if isinstance(t.get("album"), dict) else {}
    artists = t.get("artists") if isinstance(t.get("artists"), list) else []
    return {
        "id": str(t["id"]),
        "title": str(t.get("name") or ""),
        "artist": " / ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")),
        "album": str(album.get("name") or ""),
        "duration": (float(t.get("duration") or 0) / 1000.0),
        "cover": str(album.get("picUrl") or ""),
    }


class NeteaseLog:
    """在媒体线程里定期调用 poll()；snapshot() 返回当前播放状态（没有在播的网易云时为 None）。"""

    def __init__(self, path: str = ELOG):
        self.path = path
        self._lock = threading.Lock()
        self._offset = -1
        self._carry = b""
        self._reset()

    def _reset(self):
        self.track: dict | None = None
        self.playing = False
        self.pos = 0.0              # 暂停时的位置；播放时为 anchor 时刻的位置
        self.anchor = 0.0           # 播放中：位置 = pos + (now - anchor)
        self.anchored = False
        self.pid = 0

    # ---------------- 读取 ----------------
    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self._offset = -1
            self._reset()
            return
        with self._lock:
            if self._offset < 0 or size < self._offset:          # 首次 / 日志被重建
                self._reset()
                start = max(0, size - PRELOAD_BYTES)
                self._carry = b""
                self._read(start, size, skip_partial=start > 0)
            elif size > self._offset:
                self._read(self._offset, size)
            self._offset = size

    def _read(self, start: int, end: int, skip_partial: bool = False):
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                buf = f.read(end - start)
        except OSError:
            return
        if skip_partial:                          # 从中间开始读：丢掉第一段不完整的行
            i = buf.find(bytes([NEWLINE]))
            buf = buf[i + 1:] if i >= 0 else b""
        buf = self._carry + buf
        cut = buf.rfind(bytes([NEWLINE]))
        if cut < 0:
            self._carry = buf[-MAX_CARRY:]
            return
        self._carry = buf[cut + 1:][-MAX_CARRY:]
        text = buf[:cut + 1].translate(_TABLE).decode("utf-8", "replace")
        for line in text.split("\n"):
            self._line(line)

    # ---------------- 状态机 ----------------
    def _now_pos(self, at: float) -> float:
        return self.pos + max(0.0, at - self.anchor) if self.playing else self.pos

    def _line(self, line: str):
        if not line.startswith("[") or "【playing】" not in line and "exit" not in line:
            return
        m = _HEADER.match(line)
        if not m:
            return
        pid, at = int(m.group(1)), _event_mono(int(m.group(4)))
        if "exitApp" in line:
            self._reset()
            return
        ex = _EXIT.search(line)
        if ex:
            if self.pid and int(ex.group(1)) == self.pid:
                self._reset()
            return
        if '"checkPlayPrivilege"' in line or '"playOneTrackInPlayingList"' in line:
            t = _track(_json(line))
            if t and (self.track is None or t["id"] != self.track["id"]):
                self.track, self.pid = t, pid
                self.playing = '"playOneTrackInPlayingList"' in line
                self.pos, self.anchor, self.anchored = 0.0, at, False
            elif t and self.track and not self.track.get("title"):
                self.track.update({k: v for k, v in t.items() if v})
            return
        sk = _SEEK.search(line)
        if sk:
            self.pos, self.anchor, self.anchored = max(0.0, float(sk.group(1))), at, True
            return
        stt = _STATE.search(line)
        if stt:
            cur = self._now_pos(at)
            self.playing = stt.group(1) == "1"
            self.pos, self.anchor, self.anchored = cur, at, True
            return
        if '"native' in line and "load" in line and "完成" in line:
            d = _json(line) or {}
            sid = str(d.get("songId") or "")
            if self.track and sid == self.track["id"] and self.playing and not self.anchored:
                self.pos, self.anchor = 0.0, at          # 缓冲结束、真正出声：校正进度
            elif sid and (self.track is None or sid != self.track["id"]):
                self.track = {"id": sid, "title": "", "artist": "", "album": "", "duration": 0.0, "cover": ""}
                self.playing, self.pos, self.anchor, self.anchored, self.pid = True, 0.0, at, False, pid

    # ---------------- 输出 ----------------
    def snapshot(self) -> dict | None:
        with self._lock:
            t = self.track
            if not t or not t.get("title"):
                return None
            if self.pid and not psutil.pid_exists(self.pid):
                self._reset()
                return None
            now = time.monotonic()
            pos = self._now_pos(now)
            if t["duration"]:
                pos = min(pos, t["duration"])
            return {"song_id": t["id"], "title": t["title"], "artist": t["artist"], "album": t["album"],
                    "duration": t["duration"], "cover": t["cover"], "playing": self.playing,
                    "position": pos, "stamp": now}
