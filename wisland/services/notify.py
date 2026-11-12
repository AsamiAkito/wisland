# -*- coding: utf-8 -*-
"""消息提醒：QQ / 微信 / 邮件 / 飞书 / 钉钉。

四个来源：
  A. 轮询 Windows 通知数据库 wpndatabase.db（只读），新出现的 toast 按 PrimaryId 前缀映射。
  B. UI 收到 HSHELL_FLASH 时调用 on_flash(hwnd)，按进程名映射（8 秒内同类去重）。
  C. 邮件兜底：每 20 秒检查 Thunderbird 各账户 INBOX mbox 文件是否变大。
  D. 飞书 / 钉钉的自绘通知浮窗：用 WinEvent 监听右下角新出现的无边框弹窗（这两个应用
     默认不走 Windows 系统通知，也不闪任务栏，只能从浮窗识别）。

隐私：邮件不带发件人；QQ / 微信 / 飞书 / 钉钉 可带 "sender"（toast 第一行文字）；
notify.body_preview 里的模块额外读取 toast 正文做预览（只在内存里，不落盘）。
"""
from __future__ import annotations

import configparser
import ctypes
import os
import re
import sqlite3
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PySide6 import QtCore

from wisland.core.bg import launch, run_bg, ui
from wisland.core.config import cfg, expand

WPN_DB = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")
POLL_MS = 1500
MAIL_FILE_MS = 20_000
FLASH_DEDUPE_S = 8.0
POPUP_FALLBACK_MS = 2000
POPUP_DEDUPE_S = 1.5
POPUP_SEEN_S = 10.0
POPUP_TEXT_DELAY = 0.4
POPUP_TEXT_TRIES = 3
# 只有这些 Electron 应用会把新消息画成右下角浮窗；QQ / 微信仍走 toast / 任务栏闪烁。
POPUP_PROCESSES = frozenset({"feishu.exe", "lark.exe", "dingtalk.exe"})
MAIL_TOAST_GRACE_S = 60.0
HISTORY_MAX = 50

_FILETIME_EPOCH_DIFF = 116444736000000000  # 1601 -> 1970，单位 100ns


def filetime_to_epoch(ft) -> float:
    try:
        ft = int(ft)
    except (TypeError, ValueError):
        return time.time()
    if ft <= 0:
        return time.time()
    return (ft - _FILETIME_EPOCH_DIFF) / 1e7


_TEXT_RE = re.compile(r"<text\b[^>]*>(.*?)</text>", re.S | re.I)
DEFAULT_BODY_PREVIEW = ("feishu", "dingtalk")


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def toast_texts(payload) -> list[str]:
    """toast XML 里的全部 <text>（按出现顺序）：通常是 [发送者/标题, 正文, …]。"""
    try:
        s = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
        out = []
        for m in _TEXT_RE.finditer(s):
            t = _unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if t:
                out.append(t[:200])
        return out[:4]
    except Exception:  # noqa: BLE001
        return []


def toast_first_text(payload) -> str:
    """toast XML 的第一个 <text>（通常是发送者 / 群名）。解析失败返回空串。"""
    texts = toast_texts(payload)
    return texts[0][:40] if texts else ""


def body_preview_kinds() -> set:
    """开启正文预览的模块（config.json → notify.body_preview）。"""
    v = cfg.get("notify.body_preview")
    if isinstance(v, list):
        return {k for k in v if isinstance(k, str)}
    return set(DEFAULT_BODY_PREVIEW)


# ---------------- hwnd -> exe ----------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
MONITOR_DEFAULTTONEAREST = 2

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL

WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
_user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE, WINEVENTPROC,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.UnhookWinEvent.restype = wintypes.BOOL


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]


def exe_of_hwnd(hwnd: int) -> str:
    """窗口句柄 -> 小写 exe 文件名（失败返回空串）。"""
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        if not pid.value:
            return ""
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                _kernel32.CloseHandle(h)
        try:
            import psutil
            return psutil.Process(pid.value).name().lower()
        except Exception:  # noqa: BLE001
            return ""
    except Exception:  # noqa: BLE001
        return ""


def looks_like_notify_popup(hwnd: int) -> bool:
    """飞书 / 钉钉的新消息浮窗：无边框弹出层、置顶、工具窗口，贴在某个屏幕工作区的右下角。

    这两个应用默认不用 Windows 系统通知、也不闪任务栏，消息到达时只会创建这样一个小窗口
    （飞书约 360x80）。按窗口特征识别，避免把主窗口 / 设置对话框当成消息。
    """
    try:
        style = _user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        ex = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if not (style & WS_POPUP) or not (ex & WS_EX_TOOLWINDOW) or not (ex & WS_EX_TOPMOST):
            return False
        r = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        w, h = r.right - r.left, r.bottom - r.top
        if not (160 <= w <= 900 and 40 <= h <= 600):
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        hmon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon or not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        work = mi.rcWork
        return (work.right - 300 <= r.right <= work.right + 8
                and work.bottom - 300 <= r.bottom <= work.bottom + 8)
    except Exception:  # noqa: BLE001
        return False


def feishu_notify_window(hwnd: int) -> bool:
    """飞书消息浮窗的额外特征：Electron 顶层窗口类、窗口标题为空。

    飞书的其它弹窗（更新 / 设置等）通常有标题，靠这两条把误报压下去。
    """
    try:
        cls = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, cls, 128)
        if not cls.value.lower().startswith("chrome_widgetwin"):
            return False
        title = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 256)
        return not title.value.strip()
    except Exception:  # noqa: BLE001
        return False


def popup_windows(apps: dict) -> dict:
    """当前可见的、属于 apps（exe -> 模块）的通知浮窗：{hwnd: exe}。"""
    found: dict[int, str] = {}
    if not apps:
        return found

    def visit(hwnd, _lp):
        try:
            if _user32.IsWindowVisible(hwnd) and looks_like_notify_popup(int(hwnd)):
                exe = exe_of_hwnd(int(hwnd))
                if exe in apps and exe in POPUP_PROCESSES:
                    if exe in ("feishu.exe", "lark.exe") and not feishu_notify_window(int(hwnd)):
                        return True
                    found[int(hwnd)] = exe
        except Exception:  # noqa: BLE001
            pass
        return True

    _user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


def popup_text(hwnd: int) -> tuple[str, str]:
    """读飞书 / 钉钉通知浮窗里的文字：返回 (发送者, 正文)；读不到返回 ("", "")。

    浮窗是 Chromium 窗口，没有 WM_GETTEXT 文本；通过 UI Automation 读它的 Label。
    正文有时比发送者晚一两点帧才画出来，所以最多重试几次；失败不影响提醒本身。
    """
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen import UIAutomationClient as UIA

        comtypes.CoInitialize()
        try:
            uia = comtypes.client.CreateObject(UIA.CUIAutomation)
            walker = uia.RawViewWalker
            best: list[str] = []
            for attempt in range(POPUP_TEXT_TRIES):
                time.sleep(POPUP_TEXT_DELAY)
                try:
                    root = uia.ElementFromHandle(int(hwnd))
                except Exception:  # noqa: BLE001
                    break           # 浮窗已经关掉
                texts: list[str] = []

                def walk(el, depth: int):
                    if depth > 6 or len(texts) >= 3:
                        return
                    try:
                        name = str(el.CurrentName or "").strip()
                        ctype = int(el.CurrentControlType)
                        rect = el.CurrentBoundingRectangle
                    except Exception:  # noqa: BLE001
                        return
                    if name and ctype == 50020 and (rect.right - rect.left) > 0:   # UIA_TextControlTypeId
                        texts.append(name)
                    try:
                        child = walker.GetFirstChildElement(el)
                    except Exception:  # noqa: BLE001
                        child = None
                    while child is not None and len(texts) < 3:
                        walk(child, depth + 1)
                        try:
                            child = walker.GetNextSiblingElement(child)
                        except Exception:  # noqa: BLE001
                            break

                walk(root, 0)
                texts = [t for t in texts if t and t not in ("[Generating...]", "Generating...")]
                if len(texts) > len(best):
                    best = texts
                if len(best) >= 2:
                    break           # 发送者 + 正文都拿到了
            if not best:
                return "", ""
            sender = best[0][:40]
            body = best[1][:120] if len(best) > 1 else ""
            return sender, body
        finally:
            comtypes.CoUninitialize()
    except Exception:  # noqa: BLE001
        return "", ""


# ---------------- Thunderbird ----------------

def thunderbird_profile() -> Path | None:
    root = Path(os.path.expandvars(r"%APPDATA%\Thunderbird"))
    ini = root / "profiles.ini"
    if not ini.exists():
        return None
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(ini, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    cand: list[tuple[str, bool]] = []
    for sec in cp.sections():
        if sec.lower().startswith("install") and cp[sec].get("default"):
            cand.append((cp[sec]["default"], True))
    for sec in cp.sections():
        s = cp[sec]
        if sec.lower().startswith("profile") and s.get("path") and s.get("default") == "1":
            cand.append((s["path"], s.get("isrelative", "1") == "1"))
    for sec in cp.sections():
        s = cp[sec]
        if sec.lower().startswith("profile") and s.get("path"):
            cand.append((s["path"], s.get("isrelative", "1") == "1"))
    for path, rel in cand:
        p = (root / path) if rel else Path(path)
        if (p / "ImapMail").is_dir() or (p / "Mail").is_dir():
            return p
    return None


def inbox_files(profile: Path) -> list[Path]:
    out = []
    for sub in ("ImapMail", "Mail"):
        base = profile / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f in ("INBOX", "Inbox"):
                    out.append(Path(dirpath) / f)
    return out


# ---------------- service ----------------

class NotifyService(QtCore.QObject):
    notified = QtCore.Signal(dict)        # {kind, name, source:'toast'|'flash'|'file', time, [sender]}
    counts_changed = QtCore.Signal(dict)  # {kind: n}

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 让 bg 的 invoker 在主线程创建（否则 on_done 回调发不回来）
        self.counts: dict[str, int] = {}
        self.history: list[dict] = []
        self._last_by_kind: dict[str, float] = {}
        self._last_mail_toast = 0.0
        self._max_id: int | None = None
        self._busy = False
        self._mail_busy = False
        self._lock = threading.Lock()
        self._inbox_state: dict[str, tuple[int, float]] | None = None
        self._profile: Path | None = None
        self._running = False

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._mail_timer = QtCore.QTimer(self)
        self._mail_timer.setInterval(MAIL_FILE_MS)
        self._mail_timer.timeout.connect(self._mail_tick)
        self._popup_seen: dict[int, float] = {}
        self._popup_visible: set[int] = set()
        self._popup_last: dict[str, float] = {}
        self._win_hook = None
        self._win_hook_cb = None
        self._fallback_timer = QtCore.QTimer(self)
        self._fallback_timer.setInterval(POPUP_FALLBACK_MS)
        self._fallback_timer.timeout.connect(self._poll_popups)

    # ---------- lifecycle ----------
    def start(self):
        self._running = True
        self._timer.start()
        self._mail_timer.start()
        self._install_popup_hook()
        self._tick()
        self._mail_tick()

    def stop(self):
        self._running = False
        self._timer.stop()
        self._mail_timer.stop()
        self._fallback_timer.stop()
        self._remove_popup_hook()

    # ---------- public ----------
    def publish(self, d: dict):
        """供其它事件源（DeepSeek 桌面版会话日志等）投递一条消息。"""
        self._emit(d)

    def clear(self, kind: str):
        if self.counts.get(kind):
            self.counts[kind] = 0
            self.counts_changed.emit(dict(self.counts))

    def clear_all(self):
        """清空全部消息：未读计数 + 历史列表（不删除消息模块）。"""
        self.clear_messages()

    def clear_messages(self, kind: str | None = None):
        """清空全部消息，或只清空某个模块的消息（未读计数 + 历史）。"""
        if kind is None:
            self.counts.clear()
            self.history.clear()
        else:
            self.counts.pop(kind, None)
            self.history[:] = [h for h in self.history if h.get("kind") != kind]
        self.counts_changed.emit(dict(self.counts))

    def open(self, kind: str):
        target = str(cfg.get(f"notify.open.{kind}") or "").strip()
        if not target:
            latest = next((h for h in self.history
                           if h.get("kind") == kind and (h.get("exe") or h.get("open"))), None)
            if latest:
                target = str(latest.get("exe") or latest.get("open") or "").strip()
        if not target:
            target = next((exe for exe, m in (cfg.get("notify.flash_processes") or {}).items()
                           if m.get("kind") == kind), "")
        opener = getattr(self, "opener", None)     # app.py 注入：已打开则切换窗口
        res = ""
        if target:
            if opener is not None:
                res = opener(expand(target))
            else:
                res = "launched" if launch(expand(target)) else "failed"
        if kind == "mail" and res != "focused":
            self._open_mail(target)
        self.clear(kind)

    @staticmethod
    def _open_mail(target: str = "") -> bool:
        """邮件客户端兜底：先走已知安装路径，再试 Outlook 协议。

        新版 Outlook 的 olk.exe 常常只有 WindowsApps 别名，直接 Popen("olk.exe")
        不一定能找到；经典 Outlook 也不一定在 PATH 里，所以这里补常见路径。
        """
        target = os.path.basename(target).lower()
        order = ([target] if target else []) + [e for e in
                 ("olk.exe", "outlook.exe", "hxoutlook.exe", "thunderbird.exe")
                 if e != target]
        known = {
            "olk.exe": [
                os.path.expandvars(r"%LocalAppData%\Microsoft\WindowsApps\olk.exe"),
                os.path.expandvars(r"%ProgramFiles%\WindowsApps\olk.exe"),
            ],
            "outlook.exe": [
                os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
                os.path.expandvars(r"%LocalAppData%\Microsoft\WindowsApps\outlook.exe"),
            ],
            "thunderbird.exe": [
                os.path.expandvars(r"%ProgramFiles%\Mozilla Thunderbird\thunderbird.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Thunderbird\thunderbird.exe"),
            ],
            "hxoutlook.exe": [
                os.path.expandvars(r"%LocalAppData%\Microsoft\WindowsApps\hxoutlook.exe"),
            ],
        }
        for exe in order:
            for p in known.get(exe, []):
                if not os.path.exists(p):
                    continue
                if launch(p):
                    return True
                try:
                    os.startfile(p)  # noqa: S606  WindowsApps 的别名用 startfile 更稳
                    return True
                except Exception:  # noqa: BLE001
                    pass
        # 最后才走协议；outlook: 由 Outlook 注册，mailto: 作为兜底
        for proto in ("outlook:", "mailto:"):
            if launch(proto):
                return True
        return False


    def on_flash(self, hwnd: int):
        try:
            exe = exe_of_hwnd(int(hwnd))
            if not exe:
                return
            m = (cfg.get("notify.flash_processes") or {}).get(exe)
            if not m:
                return
            kind = m.get("kind", exe)
            now = time.time()
            if now - self._last_by_kind.get(kind, 0) < FLASH_DEDUPE_S:
                return
            self._emit({"kind": kind, "name": m.get("name", kind), "source": "flash",
                        "exe": exe, "time": now})
        except Exception:  # noqa: BLE001
            pass

    # ---------- internal ----------
    @staticmethod
    def _enabled_modules() -> set:
        """消息页启用的模块；未启用的应用通知直接忽略（相当于“未添加该模块”）。"""
        v = cfg.get("notify.modules")
        if isinstance(v, list):
            return {k for k in v if isinstance(k, str)}
        return {"qq", "wechat", "mail"}

    @staticmethod
    def _popup_apps() -> dict:
        """浮窗识别用的进程表：沿用 notify.flash_processes（exe -> {name, kind}）。"""
        v = cfg.get("notify.flash_processes")
        return v if isinstance(v, dict) else {}

    # ---------- 飞书 / 钉钉通知浮窗 ----------
    def _install_popup_hook(self):
        """用 WinEvent 监听新窗口显示；空闲时没有轮询开销。"""
        if self._win_hook is not None:
            return
        if not self._popup_apps():
            return
        try:
            cb = WINEVENTPROC(self._on_win_event)
            hook = _user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, cb, 0, 0,
                                           WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        except Exception:  # noqa: BLE001
            hook, cb = None, None
        if hook:
            self._win_hook, self._win_hook_cb = hook, cb
            print("[notify] 浮窗监听：WinEvent 钩子")
            return
        # 极少见：钩子不可用时退回低频轮询（2 秒一次）
        self._popup_visible = set(popup_windows(self._popup_apps()))
        self._fallback_timer.start()
        print("[notify] 浮窗监听：2s 轮询兜底")

    def _remove_popup_hook(self):
        if self._win_hook is None:
            return
        try:
            _user32.UnhookWinEvent(self._win_hook)
        except Exception:  # noqa: BLE001
            pass
        self._win_hook = None
        self._win_hook_cb = None

    def _on_win_event(self, _hook, event, hwnd, id_object, id_child, _thread, _time):
        """WinEvent 回调（主线程）：只关心顶层窗口的 SHOW，其余立即返回。"""
        if not self._running or event != EVENT_OBJECT_SHOW or id_object != 0 or not hwnd:
            return
        try:
            if not _user32.IsWindowVisible(hwnd) or not looks_like_notify_popup(int(hwnd)):
                return
            apps = self._popup_apps()
            exe = exe_of_hwnd(int(hwnd))
            if exe not in apps or exe not in POPUP_PROCESSES:
                return
            if exe in ("feishu.exe", "lark.exe") and not feishu_notify_window(int(hwnd)):
                return
            self._handle_popup(int(hwnd), exe)
        except Exception:  # noqa: BLE001
            pass

    def _handle_popup(self, hwnd: int, exe: str):
        now = time.time()
        if now - self._popup_seen.get(hwnd, 0) < POPUP_SEEN_S:
            return
        self._popup_seen[hwnd] = now
        if len(self._popup_seen) > 64:
            self._popup_seen = {h: t for h, t in self._popup_seen.items() if now - t < 60}
        self._emit_popup_for(exe, hwnd)

    def _emit_popup_for(self, exe: str, hwnd: int):
        apps = self._popup_apps()
        m = apps.get(exe) or {}
        kind = m.get("kind", exe)
        now = time.time()
        if now - self._popup_last.get(kind, 0) < POPUP_DEDUPE_S:
            return            # 同一个浮窗换句柄重建时只算一条消息
        self._popup_last[kind] = now
        d = {"kind": kind, "name": m.get("name", kind), "source": "popup", "exe": exe, "time": now}
        if kind in body_preview_kinds():
            # 浮窗里的发送者 / 正文用 UI Automation 读；读不到就只提醒「新消息」
            run_bg(popup_text, hwnd,
                   on_done=lambda res, d=d: self._emit_popup(d, res),
                   on_error=lambda _e, d=d: self._emit_popup(d, None))
        else:
            self._emit(d)

    def _poll_popups(self):
        """钩子不可用时的兜底：每 2 秒枚举一次可见浮窗。"""
        if not self._running or self._win_hook is not None:
            return
        apps = self._popup_apps()
        if not apps:
            return
        try:
            cur = popup_windows(apps)
        except Exception:  # noqa: BLE001
            return
        fresh = [h for h in cur if h not in self._popup_visible]
        self._popup_visible = set(cur)
        for hwnd in fresh:
            self._emit_popup_for(cur[hwnd], hwnd)

    def _emit_popup(self, d: dict, res):
        if not self._running:
            return
        if res:
            sender, body = res
            if sender:
                d["sender"] = sender
            if body:
                d["body"] = body
        print(f"[notify] popup {d.get('kind')} sender={bool(d.get('sender'))} body={bool(d.get('body'))}")
        self._emit(d)

    def _emit(self, d: dict):
        kind = d["kind"]
        if kind not in self._enabled_modules():
            return
        self._last_by_kind[kind] = time.time()
        if kind == "mail":
            d.pop("sender", None)
            if d.get("source") == "toast":
                self._last_mail_toast = time.time()
        self.history.insert(0, d)
        del self.history[HISTORY_MAX:]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.notified.emit(d)
        self.counts_changed.emit(dict(self.counts))

    @staticmethod
    def _map_handler(primary: str, apps: dict):
        # 前缀后必须是结尾或非字母数字（'ThunderbirdToast-990…' 命中，'QQExtension_…' 不命中）
        # 不区分大小写：UWP 应用的 AUMID 在通知库里的大小写并不统一
        p = (primary or "").lower()
        for prefix, m in apps.items():
            pref = str(prefix).lower()
            if p.startswith(pref):
                rest = p[len(pref):]
                if not rest or not rest[0].isalnum():
                    return m
        return None

    def _tick(self):
        if self._busy or not self._running:
            return
        self._busy = True
        run_bg(self._poll_db, on_done=self._on_rows, on_error=lambda e: setattr(self, "_busy", False))

    def _query(self, sql: str, args=()):
        uri = "file:" + WPN_DB.replace("\\", "/") + "?mode=ro"
        last = None
        for _ in range(4):
            try:
                con = sqlite3.connect(uri, uri=True, timeout=1.0)
                try:
                    return con.execute(sql, args).fetchall()
                finally:
                    con.close()
            except sqlite3.OperationalError as e:
                last = e
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(0.15)
                    continue
                raise
        raise last  # type: ignore[misc]

    def _poll_db(self):
        if not os.path.exists(WPN_DB):
            return []
        if self._max_id is None:
            r = self._query("SELECT MAX(Id) FROM Notification")
            self._max_id = int(r[0][0] or 0) if r else 0
            return []
        rows = self._query(
            "SELECT n.Id, h.PrimaryId, n.ArrivalTime, n.Type, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId WHERE n.Id > ? ORDER BY n.Id",
            (self._max_id,))
        if rows:
            self._max_id = max(int(r[0]) for r in rows)
        return rows

    def _on_rows(self, rows):
        self._busy = False
        if not rows:
            return
        apps = cfg.get("notify.toast_apps") or {}
        body_kinds = body_preview_kinds()
        for _id, primary, arrival, ntype, payload in rows:
            try:
                if str(ntype or "").lower() not in ("toast", ""):
                    continue
                m = self._map_handler(str(primary or ""), apps)
                if not m:
                    continue
                kind = m.get("kind", "other")
                d = {"kind": kind, "name": m.get("name", kind), "source": "toast",
                     "open": m.get("open", ""), "time": filetime_to_epoch(arrival)}
                texts = toast_texts(payload)
                if texts and kind in ("qq", "wechat", "feishu", "dingtalk"):
                    if len(texts) > 1 or kind not in body_kinds:
                        d["sender"] = texts[0][:40]
                if kind in body_kinds:
                    if len(texts) > 1:
                        d["body"] = texts[1][:120]
                    elif texts:
                        # 只有一个 <text> 的通知：把它当作正文，至少能预览一句
                        d["body"] = texts[0][:120]
                self._emit(d)
            except Exception:  # noqa: BLE001
                continue

    # ---------- recent (debug / test) ----------
    def recent_mapped(self, n: int = 3) -> list[dict]:
        """数据库里最近 n 条已映射通知（阻塞，供测试；不含正文）。"""
        apps = cfg.get("notify.toast_apps") or {}
        rows = self._query(
            "SELECT n.Id, h.PrimaryId, n.ArrivalTime, n.Type, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId ORDER BY n.Id DESC LIMIT 500")
        out = []
        for _id, primary, arrival, ntype, payload in rows:
            m = self._map_handler(str(primary or ""), apps)
            if m and str(ntype or "").lower() == "toast":
                d = {"id": _id, "kind": m.get("kind"), "time": filetime_to_epoch(arrival),
                     "sender_len": len(toast_first_text(payload))}
                out.append(d)
                if len(out) >= n:
                    break
        return out

    # ---------- mail file fallback ----------
    def _mail_tick(self):
        if self._mail_busy or not self._running:
            return
        self._mail_busy = True
        run_bg(self._scan_inbox, on_done=self._on_inbox, on_error=lambda e: setattr(self, "_mail_busy", False))

    def _scan_inbox(self):
        if self._profile is None:
            self._profile = thunderbird_profile()
        if self._profile is None:
            return None
        st = {}
        for p in inbox_files(self._profile):
            try:
                s = p.stat()
                st[str(p)] = (s.st_size, s.st_mtime)
            except OSError:
                continue
        return st

    def _on_inbox(self, st):
        self._mail_busy = False
        if st is None:
            return
        prev = self._inbox_state
        self._inbox_state = st
        if prev is None:
            return
        grew = any(path in prev and size > prev[path][0] for path, (size, _m) in st.items())
        if grew and time.time() - self._last_mail_toast > MAIL_TOAST_GRACE_S:
            m = (cfg.get("notify.toast_apps") or {}).get("ThunderbirdToast") or {}
            self._emit({"kind": "mail", "name": m.get("name", "邮件"), "source": "file",
                        "open": "thunderbird.exe", "time": time.time()})
