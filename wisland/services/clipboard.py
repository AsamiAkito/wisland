# -*- coding: utf-8 -*-
"""剪贴板历史。

条目 item: {id, kind:'text'|'image', text, image_path, thumb(QImage|None，仅内存), time, pinned}
持久化：DATA_DIR/clipboard.json（全部条目，图片只存路径）；图片 PNG 存在 DATA_DIR/clips/<hash>.png。
跳过密码管理器等标记了 ExcludeClipboardContentFromMonitorProcessing / CanIncludeInClipboardHistory=0 的内容。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from ctypes import wintypes

from PySide6 import QtCore, QtGui

from wisland.core.bg import run_bg, ui
from wisland.core.config import DATA_DIR, cfg

CLIPS_DIR = DATA_DIR / "clips"
STORE_PATH = DATA_DIR / "clipboard.json"
TEXT_MAX = 20_000
THUMB_W, THUMB_H = 240, 160
DEBOUNCE_MS = 120
IGNORE_WINDOW_S = 0.8

# ---------------- SendInput Ctrl+V ----------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x11, 0x56, 0x10, 0x12, 0x5B, 0x5C
ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short


def _key(vk: int, up: bool) -> _INPUT:
    i = _INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return i


def send_ctrl_v():
    seq = []
    # 先松开用户可能还按着的修饰键（如热键 Ctrl+Alt+V 触发时），避免变成 Ctrl+Alt+V / Shift+Ctrl+V
    for vk in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
        if _user32.GetAsyncKeyState(vk) & 0x8000:
            seq.append(_key(vk, True))
    seq += [_key(VK_CONTROL, False), _key(VK_V, False), _key(VK_V, True), _key(VK_CONTROL, True)]
    arr = (_INPUT * len(seq))(*seq)
    _user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))


# ---------------- helpers ----------------

def _excluded(mime: QtCore.QMimeData) -> bool:
    try:
        for f in mime.formats():
            fl = f.lower()
            if "excludeclipboardcontentfrommonitorprocessing" in fl or "clipboard viewer ignore" in fl:
                return True
            if "canincludeinclipboardhistory" in fl:
                data = bytes(mime.data(f))
                if data[:4] == b"\x00\x00\x00\x00":
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _make_thumb(img: QtGui.QImage) -> QtGui.QImage:
    if img.isNull():
        return img
    if img.width() <= THUMB_W and img.height() <= THUMB_H:
        return img.copy()
    return img.scaled(THUMB_W, THUMB_H, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                      QtCore.Qt.TransformationMode.SmoothTransformation)


def _save_image(img: QtGui.QImage) -> tuple[str, QtGui.QImage]:
    """后台线程：按像素内容求哈希，保存 PNG，返回 (路径, 缩略图)。"""
    img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32)
    h = hashlib.sha1()
    h.update(f"{img.width()}x{img.height()}".encode())
    h.update(bytes(img.constBits())[: img.sizeInBytes()])
    path = CLIPS_DIR / f"{h.hexdigest()[:20]}.png"
    if not path.exists():
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp.png"
        if not img.save(tmp, "PNG"):
            raise OSError("保存图片失败")
        os.replace(tmp, path)
    return str(path), _make_thumb(img)


class ClipboardService(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 确保 bg 回调 invoker 在主线程创建
        self._items: list[dict] = []
        self._next_id = 1
        self._ignore_until = 0.0
        self._running = False
        self._cb = QtGui.QGuiApplication.clipboard()
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._capture)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save)
        self._load()

    # ---------- lifecycle ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._cb.dataChanged.connect(self._on_changed)
        n = len(self._items)
        self._trim()                     # 启动时按当前上限裁剪（例如从 60 改成 10）
        if len(self._items) != n:
            self._changed()

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._cb.dataChanged.disconnect(self._on_changed)
        except (RuntimeError, TypeError):
            pass
        self._debounce.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
        self._save()

    # ---------- public ----------
    def items(self) -> list[dict]:
        """置顶在前，其余按时间倒序。返回列表副本（条目为同一 dict，勿修改）。"""
        return [i for i in self._items if i["pinned"]] + [i for i in self._items if not i["pinned"]]

    def get(self, item_id: int) -> dict | None:
        return next((i for i in self._items if i["id"] == item_id), None)

    def pin(self, item_id: int, pinned: bool = True):
        it = self.get(item_id)
        if it and it["pinned"] != bool(pinned):
            it["pinned"] = bool(pinned)
            self._trim()
            self._changed()

    def remove(self, item_id: int):
        it = self.get(item_id)
        if it:
            self._items.remove(it)
            self._delete_file_if_unused(it)
            self._changed()

    def clear(self):
        """清除未置顶条目。"""
        drop = [i for i in self._items if not i["pinned"]]
        self._items = [i for i in self._items if i["pinned"]]
        for it in drop:
            self._delete_file_if_unused(it)
        self._changed()

    def copy(self, item_id: int) -> bool:
        it = self.get(item_id)
        if not it:
            return False
        self._ignore_until = time.monotonic() + IGNORE_WINDOW_S
        if it["kind"] == "text":
            self._cb.setText(it["text"])
        else:
            img = QtGui.QImage(it["image_path"])
            if img.isNull():
                return False
            self._cb.setImage(img)
        # 用过的条目移到最前
        self._items.remove(it)
        self._items.insert(0, it)
        it["time"] = time.time()
        self._changed()
        return True

    def paste(self, item_id: int):
        if self.copy(item_id):
            QtCore.QTimer.singleShot(60, send_ctrl_v)

    # ---------- capture ----------
    def _on_changed(self):
        if time.monotonic() < self._ignore_until:
            return
        self._debounce.start()

    def _capture(self):
        if time.monotonic() < self._ignore_until:
            return
        try:
            mime = self._cb.mimeData()
            if mime is None or _excluded(mime):
                return
            text = mime.text() if mime.hasText() else ""
            if text and text.strip():
                self._add_text(text)
            elif mime.hasImage():
                img = self._cb.image()
                if not img.isNull():
                    run_bg(_save_image, img, on_done=self._add_image)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    def _add_text(self, text: str):
        text = text[:TEXT_MAX]
        old = next((i for i in self._items if i["kind"] == "text" and i["text"] == text), None)
        if old:
            self._items.remove(old)
            old["time"] = time.time()
            self._items.insert(0, old)
        else:
            self._items.insert(0, self._new("text", text=text))
        self._trim()
        self._changed()

    def _add_image(self, res):
        path, thumb = res
        old = next((i for i in self._items if i["kind"] == "image" and i["image_path"] == path), None)
        if old:
            self._items.remove(old)
            old["time"] = time.time()
            old["thumb"] = old.get("thumb") or thumb
            self._items.insert(0, old)
        else:
            self._items.insert(0, self._new("image", image_path=path, thumb=thumb))
        self._trim()
        self._changed()

    def _new(self, kind, text="", image_path="", thumb=None) -> dict:
        it = {"id": self._next_id, "kind": kind, "text": text, "image_path": image_path,
              "thumb": thumb, "time": time.time(), "pinned": False}
        self._next_id += 1
        return it

    def _trim(self):
        try:
            mx = max(1, int(cfg.get("clipboard.max") or 10))
        except (TypeError, ValueError):
            mx = 10
        unpinned = [i for i in self._items if not i["pinned"]]
        for it in unpinned[mx:]:
            self._items.remove(it)
            self._delete_file_if_unused(it)

    def _delete_file_if_unused(self, it: dict):
        p = it.get("image_path")
        if it.get("kind") != "image" or not p:
            return
        if any(i.get("image_path") == p for i in self._items):
            return
        try:
            os.remove(p)
        except OSError:
            pass

    def _changed(self):
        self._save_timer.start()
        self.changed.emit()

    # ---------- persistence ----------
    def _load(self):
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        items = []
        for d in data.get("items", []):
            kind = d.get("kind")
            if kind == "text" and d.get("text"):
                items.append({"id": 0, "kind": "text", "text": str(d["text"])[:TEXT_MAX], "image_path": "",
                              "thumb": None, "time": float(d.get("time") or 0), "pinned": bool(d.get("pinned"))})
            elif kind == "image" and d.get("image_path") and os.path.exists(d["image_path"]):
                img = QtGui.QImage(d["image_path"])
                items.append({"id": 0, "kind": "image", "text": "", "image_path": d["image_path"],
                              "thumb": _make_thumb(img) if not img.isNull() else None,
                              "time": float(d.get("time") or 0), "pinned": bool(d.get("pinned"))})
        for it in items:
            it["id"] = self._next_id
            self._next_id += 1
        self._items = items
        # 清理不再引用的孤立图片
        try:
            used = {os.path.normcase(os.path.abspath(i["image_path"])) for i in items if i["image_path"]}
            if CLIPS_DIR.is_dir():
                for f in CLIPS_DIR.iterdir():
                    if f.suffix.lower() == ".png" and os.path.normcase(str(f.resolve())) not in used:
                        f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _save(self):
        data = {"items": [{"kind": i["kind"], "text": i["text"], "image_path": i["image_path"],
                           "time": i["time"], "pinned": i["pinned"]} for i in self._items]}
        try:
            tmp = STORE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, STORE_PATH)
        except Exception:  # noqa: BLE001
            pass
