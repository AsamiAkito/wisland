# -*- coding: utf-8 -*-
"""DeepSeek 桌面版完成通知：只读 DSH 的会话日志，替代 dsh-pet 的系统通知。

DSH 把每个会话写成 zstd 压缩的 JSONL（每次追加一个独立 frame）：
    %USERPROFILE%\\.dsh\\sessions\\<工作区>\\session-<uuid>\\session.v4.jsonl.zstd

其中 turn/end 表示一轮结束；正文取这一轮最后一条 assistant/message 的 text 块
（部分版本的 turn/end 自带 data.text，优先用它）。日志只读，不碰 DSH 的网络与数据库。

实现要点：
- 启动时只 stat 文件并记下“已读到的压缩字节”，不重放历史；标题从会话投影缓存补齐。
- 之后每次只读新增的 frame，大日志也不会反复解压。
- 以服务启动时间为基线：只提醒启动之后结束的轮次；子代理会话默认忽略。
- DSH 窗口在前台时不弹（和 dsh-pet 的系统通知一致）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PySide6 import QtCore

from ..core.bg import run_bg
from ..core.config import cfg, expand

POLL_MS = 1200
MAX_FILES = 40
KEEP_TURNS = 12
DEFAULT_BODY_CHARS = 120
START_GRACE_MS = 3000
DSH_PROCESS = "deepseek harness.exe"


# ---------------- zstd frame 级读取 ----------------

def _frame_decompressor():
    """返回一个只处理一帧的解压器；3.14+ 用标准库，否则用 zstandard。"""
    try:
        from compression import zstd as _zstd  # type: ignore[attr-defined]
    except ImportError:
        _zstd = None
    if _zstd is not None:
        return _zstd.ZstdDecompressor()
    import zstandard

    return zstandard.ZstdDecompressor().decompressobj()


def decompress_frames(chunk: bytes) -> tuple[bytes, int]:
    """解压 chunk 里所有完整 frame，返回（输出, 已消费字节数）。

    DSH 每次写入追加一个完整 frame；末尾可能是还没写完整的 frame，此时停在它前面，
    下一轮从同一偏移继续读。这样既不会丢事件，也不用反复解压整个日志。
    """
    out = bytearray()
    pos = 0
    size = len(chunk)
    while pos < size:
        dec = _frame_decompressor()
        try:
            out += dec.decompress(chunk[pos:])
        except Exception:  # noqa: BLE001  frame 不完整 / 校验失败
            break
        if not getattr(dec, "eof", False):
            break
        consumed = size - pos - len(getattr(dec, "unused_data", b""))
        if consumed <= 0:
            break
        pos += consumed
    return bytes(out), pos


def _starts_with_frame(chunk: bytes) -> bool:
    """chunk 开头是不是 zstd frame（含 skippable frame）；不足 4 字节时按“是”处理。"""
    if len(chunk) < 4:
        return True
    magic = int.from_bytes(chunk[:4], "little")
    return magic == 0xFD2FB528 or 0x184D2A50 <= magic <= 0x184D2A5F


# ---------------- 会话解析 ----------------

def sessions_root() -> Path:
    raw = str(cfg.get("dsh.sessions_dir") or "").strip()
    if raw:
        return Path(expand(raw))
    return Path(os.path.expanduser("~")) / ".dsh" / "sessions"


def _clean(text: str, limit: int) -> str:
    s = str(text or "").replace("```", " ")
    s = s.replace("**", "").replace("`", "")
    s = " ".join(s.split())
    s = s.lstrip("#>-* ").strip()
    if limit > 0 and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def _blocks_text(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            t = str(block.get("text") or "").strip()
            if t:
                parts.append(t)
    return "\n".join(parts)


def _short_title(text: str) -> str:
    s = " ".join(str(text or "").split())
    if len(s) > 30:
        s = s[:30] + "…"
    return s


def _cached_title(root: Path, sid: str) -> str:
    """从 DSH 的会话投影缓存里取标题；失败返回空串。"""
    try:
        path = root.parent / "storages" / "session_projcache" / "sessions" / f"{sid}.json"
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = (data.get("record") or {}).get("rows") or {}
        title = (rows.get("title") or {}).get("val")
        return str(title or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _end_notification(state: dict, ev: dict, body_chars: int) -> dict | None:
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
    kind = reason.get("kind")
    turn = data.get("turn")
    title = state.get("title") or state.get("prompt") or "DeepSeek"
    if kind == "completed":
        body = data.get("text") or state["texts"].get(turn) or ""
        return {"title": _short_title(title), "body": _clean(body, body_chars)}
    if kind == "error":
        err = reason.get("error") if isinstance(reason.get("error"), dict) else {}
        return {"title": _short_title(title) + " · 生成失败",
                "body": _clean(str(err.get("message") or "生成失败"), body_chars)}
    if kind == "max-tokens":
        return {"title": _short_title(title) + " · 输出被截断", "body": "已达到输出 token 上限"}
    # aborted / interrupted：用户自己取消的，不提醒
    return None


def _foreground_is_dsh() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        import psutil

        return psutil.Process(pid.value).name().casefold() == DSH_PROCESS
    except Exception:  # noqa: BLE001
        return False


def _files(root: Path) -> list[tuple[float, Path, os.stat_result]]:
    found = []
    try:
        for path in root.rglob("session.v*.jsonl.zstd"):
            try:
                st = path.stat()
            except OSError:
                continue
            found.append((st.st_mtime, path, st))
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _head_info(path: Path) -> tuple[int, str, str]:
    """只解第一帧，拿 header 里的 delegationDepth / 标题 / 首条用户消息。

    baseline 从文件末尾开始读，拿不到文件开头的 session header；子代理会话必须靠它
    区分，否则重启后会把子任务的完成当成主会话提醒。第一帧通常就是 header，很小。
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(262_144)
        if not chunk:
            return 0, "", ""
        dec = _frame_decompressor()
        data = dec.decompress(chunk)
        depth, title, prompt = 0, "", ""
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            typ = ev.get("type")
            if typ == "session":
                depth = int(ev.get("delegationDepth") or 0)
            elif typ == "session/title":
                obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                if isinstance(obj.get("title"), str):
                    title = obj["title"].strip()
            elif typ == "user/message" and not prompt:
                obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                prompt = _short_title(_blocks_text(obj.get("content")))
        return depth, title, prompt
    except Exception:  # noqa: BLE001
        return 0, "", ""


def baseline(root: Path, states: dict) -> None:
    """记录当前文件末尾，不读内容：启动前的历史轮次不会补弹。"""
    for _mtime, path, st in _files(root):
        key = str(path)
        if key in states:
            continue
        sid = path.parent.name
        depth, title, prompt = _head_info(path)
        states[key] = {
            "offset": st.st_size,  # 从文件末尾开始读
            "size": st.st_size,
            "mtime": st.st_mtime_ns,
            "seq": 0,
            "depth": depth,
            "title": title or _cached_title(root, sid),
            "prompt": prompt,
            "texts": {},
            "tail": "",
            "sid": sid,
        }


def scan(root: Path, since_ms: int, states: dict, body_chars: int = DEFAULT_BODY_CHARS,
         include_subagents: bool = False, skip_when_active: bool = True) -> list[dict]:
    """增量读取会话日志，返回启动基线之后结束的轮次。"""
    out: list[dict] = []
    if not root.exists():
        return out
    focused = skip_when_active and _foreground_is_dsh()

    for _mtime, path, st in _files(root)[:MAX_FILES]:
        key = str(path)
        state = states.get(key)
        if state is None:
            # 启动之后才出现的会话：从头读，标题在日志里
            sid = path.parent.name
            state = {"offset": 0, "size": 0, "mtime": 0, "seq": 0, "depth": 0,
                     "title": _cached_title(root, sid), "prompt": "", "texts": {},
                     "tail": "", "sid": sid}
            states[key] = state
        if st.st_size < state["offset"]:
            # 文件被重写 / 压缩：从头再来；seq 保留，避免把已提醒过的轮次再发一遍
            state["offset"] = 0
            state["tail"] = ""
        if st.st_size == state["offset"]:
            state["mtime"] = st.st_mtime_ns
            state["size"] = st.st_size
            continue
        try:
            with open(path, "rb") as f:
                f.seek(state["offset"])
                chunk = f.read()
        except OSError:
            continue
        data, consumed = decompress_frames(chunk)
        if consumed <= 0:
            if not _starts_with_frame(chunk):
                # baseline 的偏移落在半个 frame 里：从文件头重来（历史轮次由时间基线挡住）
                state["offset"] = 0
                state["tail"] = ""
                state["stall"] = 0
                continue
            # 连续几轮都读不动，可能文件被原地重写；从头再试一次
            stall = int(state.get("stall") or 0) + 1
            state["stall"] = stall
            if stall >= 4:
                state["offset"] = 0
                state["tail"] = ""
                state["stall"] = 0
            continue  # 末尾是半个 frame，下一轮再读
        state["stall"] = 0
        state["offset"] += consumed
        state["mtime"] = st.st_mtime_ns
        state["size"] = st.st_size
        text = state.get("tail", "") + data.decode("utf-8", "replace")
        state["tail"] = ""
        lines = text.split("\n")
        old_seq = int(state.get("seq") or 0)
        max_seq = old_seq
        fresh = []
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                if idx == len(lines) - 1:
                    state["tail"] = line[:1_000_000]  # 只可能是半行，下一轮再拼
                continue
            if not isinstance(ev, dict):
                continue
            typ = ev.get("type")
            seq = ev.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
            if typ == "session":
                state["depth"] = int(ev.get("delegationDepth") or 0)
                sid = ev.get("id")
                if isinstance(sid, str) and sid:
                    state["sid"] = sid
                continue
            if typ == "session/title":
                data_obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                title = data_obj.get("title")
                if isinstance(title, str) and title.strip():
                    state["title"] = title.strip()
                continue
            if typ == "user/message":
                if not state.get("prompt"):
                    data_obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                    prompt = _blocks_text(data_obj.get("content"))
                    if prompt:
                        state["prompt"] = _short_title(prompt)
                continue
            if typ == "assistant/message":
                data_obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                turn = data_obj.get("turn")
                msg = data_obj.get("message") if isinstance(data_obj.get("message"), dict) else {}
                answer = _blocks_text(msg.get("content"))
                if turn is not None and answer:
                    state["texts"][turn] = answer
                continue
            if typ != "turn/end":
                continue
            end_ms = ev.get("time") or 0
            if not isinstance(seq, int) or seq <= old_seq or end_ms < since_ms:
                continue
            if int(state.get("depth") or 0) > 0 and not include_subagents:
                continue
            item = _end_notification(state, ev, body_chars)
            if item is not None:
                data_obj = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                item.update({"session": state.get("sid") or path.parent.name,
                             "turn": data_obj.get("turn"),
                             "time": end_ms / 1000.0})
                fresh.append(item)
        state["seq"] = max_seq
        if len(state["texts"]) > KEEP_TURNS * 3:
            for turn in sorted(state["texts"])[:-KEEP_TURNS]:
                state["texts"].pop(turn, None)
        if focused:
            continue
        out.extend(fresh)
    out.sort(key=lambda item: item.get("time") or 0)
    return out


class DshService(QtCore.QObject):
    """轮询 DSH 会话日志，把 turn/end 投递成一条消息。"""

    notified = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._states: dict = {}
        self._busy = False
        self._running = False
        self._started_ms = 0
        self._last_error = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._running = True
        self._started_ms = int(time.time() * 1000) - START_GRACE_MS
        self._states = {}
        try:
            baseline(sessions_root(), self._states)
        except Exception as e:  # noqa: BLE001
            print("[dsh] 初始化会话日志失败：", e)
        self._timer.start()
        self._tick()

    def stop(self):
        self._running = False
        self._timer.stop()
        self._states = {}

    def _tick(self):
        if self._busy or not self._running:
            return
        if not bool(cfg.get("dsh.enabled", True)):
            # 关闭期间也推进基线，重新打开时不补弹关闭期间的轮次
            self._started_ms = int(time.time() * 1000)
            return
        root = sessions_root()
        try:
            body_chars = int(cfg.get("dsh.body_chars", DEFAULT_BODY_CHARS) or DEFAULT_BODY_CHARS)
        except (TypeError, ValueError):
            body_chars = DEFAULT_BODY_CHARS
        body_chars = max(20, min(400, body_chars))
        self._busy = True
        run_bg(scan, root, self._started_ms, self._states, body_chars,
               bool(cfg.get("dsh.include_subagents", False)),
               bool(cfg.get("dsh.skip_when_active", True)),
               on_done=self._on_rows, on_error=self._on_error)

    def _on_rows(self, rows):
        self._busy = False
        for item in rows or []:
            item["kind"] = "dsh"
            item["name"] = "DeepSeek"
            item["source"] = "dsh"
            self.notified.emit(item)

    def _on_error(self, exc):
        self._busy = False
        now = time.monotonic()
        if now - self._last_error > 60:
            self._last_error = now
            print("[dsh] 读取会话日志失败：", exc)
