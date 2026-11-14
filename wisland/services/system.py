# -*- coding: utf-8 -*-
"""系统服务：无线电（Wi-Fi / 蓝牙）、窗口列表与激活、性能监控（CPU/内存/GPU/网络/磁盘）。"""
from __future__ import annotations

import asyncio
import ctypes
import os
import re
import threading
import time
import traceback
from collections import deque
from ctypes import wintypes as W

from PySide6 import QtCore, QtGui

from wisland.core.bg import launch, ui


# ======================================================================
# 无线电
# ======================================================================
try:
    from winrt.windows.devices.radios import Radio as _Radio, RadioAccessStatus as _RAS, \
        RadioKind as _RK, RadioState as _RS
except Exception:  # noqa: BLE001
    traceback.print_exc()
    _Radio = None
try:
    from winrt.windows.devices.bluetooth import BluetoothAdapter as _BtAdapter
except Exception:  # noqa: BLE001
    _BtAdapter = None


class RadioService(QtCore.QObject):
    """changed(dict): {wifi_on, bt_on, wifi_available, bt_available}

    只读无线电的开 / 关状态；不读取 Wi-Fi 名称或信号强度。
    Windows 11 24H2 起读取 SSID 会被算作访问位置，任务栏位置图标会反复闪烁。
    """

    changed = QtCore.Signal(dict)
    # 只读开关状态（Radio API，开销很小）；控制页打开时会立即刷新一次
    POLL_S = 30.0

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self.state: dict = {"wifi_on": False, "bt_on": False,
                            "wifi_available": False, "bt_available": False}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake: asyncio.Event | None = None
        self._access = None
        self._last_emit = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wisland-radio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.refresh()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def refresh(self):
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self._loop = None
            loop.close()

    async def _main(self):
        self._wake = asyncio.Event()
        while not self._stop.is_set():
            try:
                await self._poll()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _radios(self):
        if _Radio is None:
            return []
        if self._access is None:
            try:
                self._access = await _Radio.request_access_async()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._access = _RAS.UNSPECIFIED
        radios = list(await _Radio.get_radios_async() or [])
        # 部分电脑上 Radio.GetRadiosAsync 不返回蓝牙（驱动 / 权限原因），改从默认蓝牙适配器取
        if _BtAdapter is not None and not any(int(r.kind) == int(_RK.BLUETOOTH) for r in radios):
            try:
                ad = await _BtAdapter.get_default_async()
                r = await ad.get_radio_async() if ad is not None else None
                if r is not None:
                    radios.append(r)
            except Exception:  # noqa: BLE001
                pass
        return radios

    async def _poll(self):
        st = dict(self.state)
        st.update(wifi_on=False, bt_on=False, wifi_available=False, bt_available=False)
        try:
            for r in await self._radios():
                kind = int(r.kind)
                on = int(r.state) == int(_RS.ON)
                if kind == int(_RK.WI_FI):
                    st["wifi_available"] = True
                    st["wifi_on"] = st["wifi_on"] or on
                elif kind == int(_RK.BLUETOOTH):
                    st["bt_available"] = True
                    st["bt_on"] = st["bt_on"] or on
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self.state = st
        if st != self._last_emit:
            self._last_emit = dict(st)
            self.changed.emit(dict(st))

    def _set(self, kind: int, on: bool):
        loop = self._loop
        if loop is None:
            return

        async def go():
            try:
                for r in await self._radios():
                    if int(r.kind) == kind:
                        await r.set_state_async(_RS.ON if on else _RS.OFF)
                await asyncio.sleep(0.5)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            if self._wake:
                self._wake.set()
        try:
            asyncio.run_coroutine_threadsafe(go(), loop)
        except RuntimeError:
            pass

    def set_wifi(self, on: bool):
        if _Radio is not None:
            self._set(int(_RK.WI_FI), bool(on))

    def set_bluetooth(self, on: bool):
        if _Radio is not None:
            self._set(int(_RK.BLUETOOTH), bool(on))


# ======================================================================
# 窗口
# ======================================================================
_u32 = ctypes.WinDLL("user32")
_k32 = ctypes.windll.kernel32
_dwm = ctypes.windll.dwmapi
_shell32 = ctypes.windll.shell32

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_VISIBLE = 0x10000000
WS_MAXIMIZE, WS_CAPTION = 0x01000000, 0x00C00000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
GW_OWNER = 4
DWMWA_CLOAKED = 14
WM_GETICON, WM_CLOSE = 0x007F, 0x0010
ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2
GCLP_HICON, GCLP_HICONSM = -14, -34
SMTO_ABORTIFHUNG = 0x0002
SW_RESTORE, SW_SHOW = 9, 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
_VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}


def send_hotkey(spec: str) -> bool:
    """模拟按下组合键，如 "Ctrl+Alt+W"。"""
    keys = []
    for part in (spec or "").split("+"):
        k = part.strip().lower()
        if k in _VK:
            keys.append(_VK[k])
        elif len(k) == 1 and k.isalnum():
            keys.append(ord(k.upper()))
        elif k.startswith("f") and k[1:].isdigit():
            keys.append(0x70 + int(k[1:]) - 1)
    if not keys:
        return False
    try:
        for vk in keys:
            _u32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(keys):
            _u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:  # noqa: BLE001
        return False
MONITOR_DEFAULTTONEAREST = 2

_WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, W.LPARAM]
_u32.EnumChildWindows.argtypes = [W.HWND, _WNDENUMPROC, W.LPARAM]
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [W.HWND, ctypes.c_int]
_u32.GetClassLongPtrW.restype = ctypes.c_size_t
_u32.GetClassLongPtrW.argtypes = [W.HWND, ctypes.c_int]
_u32.GetWindow.restype = W.HWND
_u32.GetWindow.argtypes = [W.HWND, W.UINT]
_u32.GetWindowTextLengthW.argtypes = [W.HWND]
_u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
_u32.GetWindowThreadProcessId.restype = W.DWORD
_u32.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                     ctypes.POINTER(ctypes.c_size_t)]
_u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_u32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
_u32.IsWindowVisible.argtypes = [W.HWND]
_u32.IsWindow.argtypes = [W.HWND]
_u32.IsIconic.argtypes = [W.HWND]
_u32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [W.HWND]
_u32.BringWindowToTop.argtypes = [W.HWND]
_u32.GetForegroundWindow.restype = W.HWND
_u32.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
_u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
_u32.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
_u32.MonitorFromWindow.restype = W.HMONITOR
_u32.GetShellWindow.restype = W.HWND
_u32.GetAncestor.argtypes = [W.HWND, W.UINT]
_u32.GetAncestor.restype = W.HWND
_u32.keybd_event.argtypes = [W.BYTE, W.BYTE, W.DWORD, ctypes.c_size_t]
_u32.DestroyIcon.argtypes = [W.HICON]
_dwm.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]
_k32.OpenProcess.restype = W.HANDLE
_k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
_k32.CloseHandle.argtypes = [W.HANDLE]
_shell32.ExtractIconExW.argtypes = [W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.HICON),
                                    ctypes.POINTER(W.HICON), W.UINT]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


_u32.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.POINTER(_MONITORINFO)]

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _title(hwnd) -> str:
    n = _u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = W.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_path(pid: int) -> str:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = W.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(h)


def _cloaked(hwnd) -> bool:
    v = W.DWORD()
    if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0:
        return v.value != 0
    return False


def _hicon_to_image(hicon) -> QtGui.QImage | None:
    if not hicon:
        return None
    try:
        img = QtGui.QImage.fromHICON(int(hicon))
        return None if img.isNull() else img
    except Exception:  # noqa: BLE001
        return None


class WindowService(QtCore.QObject):
    notice = QtCore.Signal(str)
    """Alt-Tab 风格窗口列表、激活、关闭、全屏检测。全部是同步的快速 Win32 调用（主线程可用）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._own_pid = os.getpid()
        self._icons: dict[int, QtGui.QImage | None] = {}
        self._exe_icons: dict[str, QtGui.QImage | None] = {}
        self._exe_cache: dict[int, str] = {}

    def start(self):
        pass

    def stop(self):
        pass

    # ---------------- 列表 ----------------
    def _is_alt_tab(self, hwnd) -> bool:
        if not _u32.IsWindowVisible(hwnd):
            return False
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
            return False
        if ex & WS_EX_NOACTIVATE and not ex & WS_EX_APPWINDOW:
            return False
        if _u32.GetWindow(hwnd, GW_OWNER) and not ex & WS_EX_APPWINDOW:
            return False
        if _cloaked(hwnd):
            return False
        return True

    def _real_pid(self, hwnd, pid: int, exe: str) -> int:
        """UWP 窗口属于 ApplicationFrameHost.exe，找其子 CoreWindow 的真实进程。"""
        if exe.lower() != "applicationframehost.exe":
            return pid
        found = []

        def cb(h, lp):
            p = _pid(h)
            if p != pid:
                found.append(p)
                return False
            return True
        _u32.EnumChildWindows(hwnd, _WNDENUMPROC(cb), 0)
        return found[0] if found else pid

    def _exe_for(self, pid: int) -> str:
        p = self._exe_cache.get(pid)
        if p is None:
            p = _exe_path(pid)
            if len(self._exe_cache) > 512:
                self._exe_cache.clear()
            self._exe_cache[pid] = p
        return p

    def list_windows(self) -> list[dict]:
        hwnds: list[int] = []

        def cb(h, lp):
            hwnds.append(h)
            return True
        try:
            _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return []
        fg = _u32.GetForegroundWindow() or 0
        fg_root = _u32.GetAncestor(fg, 3) if fg else 0   # GA_ROOTOWNER
        out = []
        alive = set()
        for h in hwnds:
            try:
                if not self._is_alt_tab(h):
                    continue
                title = _title(h)
                if not title.strip():
                    continue
                pid = _pid(h)
                if pid == self._own_pid:
                    continue
                path = self._exe_for(pid)
                exe = os.path.basename(path)
                rpid = self._real_pid(h, pid, exe)
                if rpid != pid:
                    pid = rpid
                    path = self._exe_for(pid) or path
                    exe = os.path.basename(path)
                alive.add(h)
                out.append({"hwnd": int(h), "title": title, "exe": exe, "pid": pid,
                            "icon": self._icon(h, path),
                            "active": h == fg or h == fg_root})
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        for h in list(self._icons):
            if h not in alive:
                del self._icons[h]
        return out

    def _icon(self, hwnd, path: str):
        if hwnd in self._icons and self._icons[hwnd] is not None:
            return self._icons[hwnd]
        img = None
        for wp in (ICON_BIG, ICON_SMALL2, ICON_SMALL):
            res = ctypes.c_size_t(0)
            if _u32.SendMessageTimeoutW(hwnd, WM_GETICON, wp, 0, SMTO_ABORTIFHUNG, 50,
                                        ctypes.byref(res)) and res.value:
                img = _hicon_to_image(res.value)
                if img is not None:
                    break
        if img is None:
            for idx in (GCLP_HICON, GCLP_HICONSM):
                hi = _u32.GetClassLongPtrW(hwnd, idx)
                if hi:
                    img = _hicon_to_image(hi)
                    if img is not None:
                        break
        if img is None and path:
            img = self._exe_icon(path)
        self._icons[hwnd] = img
        return img

    def _exe_icon(self, path: str):
        key = path.lower()
        if key in self._exe_icons:
            return self._exe_icons[key]
        img = None
        large, small = W.HICON(), W.HICON()
        try:
            if _shell32.ExtractIconExW(path, 0, ctypes.byref(large), ctypes.byref(small), 1) > 0:
                img = _hicon_to_image(large.value) or _hicon_to_image(small.value)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for h in (large, small):
                if h.value:
                    _u32.DestroyIcon(h)
        self._exe_icons[key] = img
        return img

    # ---------------- 操作 ----------------
    def activate(self, hwnd: int) -> bool:
        try:
            hwnd = int(hwnd)
            if not _u32.IsWindow(hwnd):
                return False
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, SW_RESTORE)
            # 不再模拟按 Alt（会让 QQ / 微信 进入菜单模式）。用户刚点击过灵动岛，本进程通常
            # 有权设置前台窗口；不行时再临时挂接前台线程的输入队列。
            ok = bool(_u32.SetForegroundWindow(hwnd))
            if not ok or _u32.GetForegroundWindow() != hwnd:
                fg = _u32.GetForegroundWindow()
                cur_tid = _k32.GetCurrentThreadId()
                fg_tid = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
                tgt_tid = _u32.GetWindowThreadProcessId(hwnd, None)
                attached = []
                for t in {fg_tid, tgt_tid}:
                    if t and t != cur_tid and _u32.AttachThreadInput(cur_tid, t, True):
                        attached.append(t)
                try:
                    _u32.BringWindowToTop(hwnd)
                    ok = bool(_u32.SetForegroundWindow(hwnd))
                finally:
                    for t in attached:
                        _u32.AttachThreadInput(cur_tid, t, False)
            _u32.BringWindowToTop(hwnd)
            return ok or _u32.GetForegroundWindow() == hwnd
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    def find_app_window(self, exe: str) -> int | None:
        """找到某个进程（按 exe 文件名）的顶层窗口，按 Z 序返回最上面的那个。"""
        exe = os.path.basename(exe or "").lower()
        if not exe:
            return None
        hwnds: list[int] = []

        def cb(h, lp):
            hwnds.append(h)
            return True
        try:
            _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        except Exception:  # noqa: BLE001
            return None
        for h in hwnds:
            try:
                if not self._is_alt_tab(h) or not _title(h).strip():
                    continue
                pid = _pid(h)
                path = self._exe_for(pid)
                name = os.path.basename(path)
                rpid = self._real_pid(h, pid, name)
                if rpid != pid:
                    name = os.path.basename(self._exe_for(rpid) or path)
                if name.lower() == exe:
                    return int(h)
            except Exception:  # noqa: BLE001
                continue
        return None

    def find_hidden_main_window(self, exe: str, titles: list[str]) -> int | None:
        """托盘里的应用：主窗口只是被隐藏了。按标题在该进程的顶层窗口里找（包括不可见的）。"""
        exe = os.path.basename(exe or "").lower()
        want = [t.lower() for t in titles or []]
        if not exe or not want:
            return None
        hwnds: list[int] = []

        def cb(h, lp):
            hwnds.append(h)
            return True
        try:
            _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        except Exception:  # noqa: BLE001
            return None
        best = None
        for h in hwnds:
            try:
                if _u32.GetWindow(h, 4):              # 有 owner 的不是主窗口
                    continue
                title = _title(h).strip().lower()
                if title not in want:
                    continue
                name = os.path.basename(self._exe_for(_pid(h))).lower()
                if name != exe:
                    continue
                r = W.RECT()
                _u32.GetWindowRect(h, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if area < 200 * 200:
                    continue
                if best is None or area > best[1]:
                    best = (int(h), area)
            except Exception:  # noqa: BLE001
                continue
        return best[0] if best else None

    def _process_running(self, exe: str) -> bool:
        import psutil
        exe = exe.lower()
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == exe:
                return True
        return False

    def _focus_later(self, exe: str, hk: str = ""):
        h = self.find_app_window(exe)
        if h:
            self.activate(h)
        else:
            self.notice.emit(f"没能唤出窗口：请检查该应用的「显示窗口」快捷键是否为 {hk}")

    def show_hidden(self, hwnd: int) -> bool:
        try:
            _u32.ShowWindow(hwnd, SW_SHOW)
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, SW_RESTORE)
            return self.activate(hwnd)
        except Exception:  # noqa: BLE001
            return False

    def open_or_focus(self, path: str, exe: str | None = None, args=None) -> str:
        """应用已打开 -> 切到它的窗口；否则启动。返回 "focused" / "launched" / "failed"。"""
        exe = exe or (os.path.basename(path) if str(path).lower().endswith(".exe") else "")
        if exe:
            h = self.find_app_window(exe)
            if h and self.activate(h):
                return "focused"
            # 最小化到托盘（窗口被隐藏）：用应用自己的"显示窗口"热键让它自己恢复，
            # 不强行 ShowWindow（会和应用内部状态不一致），也不重新启动 exe（微信会开新实例）
            from wisland.core.config import cfg
            hk = (cfg.get("apps.restore_hotkey") or {}).get(exe.lower())
            if hk and self._process_running(exe):
                if send_hotkey(hk):
                    QtCore.QTimer.singleShot(600, lambda e=exe, k=hk: self._focus_later(e, k))
                    return "focused"
        if args and isinstance(args, str):
            args = args.split()
        return "launched" if launch(path, list(args or [])) else "failed"

    def close(self, hwnd: int) -> bool:
        try:
            return bool(_u32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0))
        except Exception:  # noqa: BLE001
            return False

    def foreground_is_fullscreen(self, own_hwnd=None) -> bool:
        try:
            fg = _u32.GetForegroundWindow()
            if not fg or fg == _u32.GetShellWindow():
                return False
            if own_hwnd and int(fg) == int(own_hwnd):
                return False
            if _pid(fg) == self._own_pid:
                return False
            if _class(fg) in _SHELL_CLASSES:
                return False
            style = _u32.GetWindowLongPtrW(fg, GWL_STYLE)
            # 普通的最大化窗口（带标题栏）不算全屏；任务栏自动隐藏时它也会铺满整个显示器
            if style & WS_MAXIMIZE and (style & WS_CAPTION) == WS_CAPTION:
                return False
            r = W.RECT()
            if not _u32.GetWindowRect(fg, ctypes.byref(r)):
                return False
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            hm = _u32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
            if not hm or not _u32.GetMonitorInfoW(hm, ctypes.byref(mi)):
                return False
            m = mi.rcMonitor
            return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
        except Exception:  # noqa: BLE001
            return False


# ======================================================================
# 性能
# ======================================================================
_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_VALID_DATA, _PDH_CSTATUS_NEW_DATA = 0, 1


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", W.DWORD), ("doubleValue", ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", W.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE)]


class _Pdh:
    """GPU 计数器（PDH 通配实例）。只在性能线程里使用。"""

    PATHS = {
        "gpu": r"\GPU Engine(*engtype_3D)\Utilization Percentage",
        "gpu_mem": r"\GPU Adapter Memory(*)\Dedicated Usage",
    }
    READD_S = 30.0

    def __init__(self):
        try:
            self.pdh = ctypes.windll.pdh
        except OSError:
            self.pdh = None
        self.query = None
        self.counters: dict[str, ctypes.c_void_p] = {}
        self.added_at = 0.0
        self.primed = False

    def _open(self):
        p = self.pdh
        self.close()
        q = ctypes.c_void_p()
        if p.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
            return False
        self.query = q
        for k, path in self.PATHS.items():
            c = ctypes.c_void_p()
            if p.PdhAddEnglishCounterW(q, ctypes.c_wchar_p(path), None, ctypes.byref(c)) == 0:
                self.counters[k] = c
        self.added_at = time.monotonic()
        self.primed = False
        p.PdhCollectQueryData(q)
        return bool(self.counters)

    def close(self):
        if self.query is not None and self.pdh is not None:
            try:
                self.pdh.PdhCloseQuery(self.query)
            except Exception:  # noqa: BLE001
                pass
        self.query = None
        self.counters = {}

    def _array(self, c) -> list[tuple[str, float]]:
        p = self.pdh
        size, count = W.DWORD(0), W.DWORD(0)
        r = p.PdhGetFormattedCounterArrayW(c, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if (r & 0xFFFFFFFF) != _PDH_MORE_DATA or size.value == 0:
            return []
        buf = (ctypes.c_byte * size.value)()
        r = p.PdhGetFormattedCounterArrayW(c, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf)
        if r != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
        out = []
        for i in range(count.value):
            it = items[i]
            if it.FmtValue.CStatus in (_PDH_CSTATUS_VALID_DATA, _PDH_CSTATUS_NEW_DATA):
                out.append((it.szName or "", it.FmtValue.doubleValue))
        return out

    def sample(self) -> tuple[float | None, float | None]:
        """-> (gpu_3d_percent, gpu_dedicated_bytes)"""
        if self.pdh is None:
            return None, None
        try:
            if self.query is None or time.monotonic() - self.added_at > self.READD_S:
                if not self._open():
                    return None, None
                return None, None   # 刚添加的速率计数器需要两次采集
            if self.pdh.PdhCollectQueryData(self.query) != 0:
                return None, None
            gpu = mem = None
            c = self.counters.get("gpu")
            if c is not None:
                per_adapter: dict[str, float] = {}
                for name, v in self._array(c):
                    m = re.search(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", name)
                    key = m.group(1) if m else ""
                    per_adapter[key] = per_adapter.get(key, 0.0) + v
                gpu = max(0.0, min(100.0, max(per_adapter.values()))) if per_adapter else 0.0
            c = self.counters.get("gpu_mem")
            if c is not None:
                vals = self._array(c)
                mem = sum(v for _, v in vals) if vals else None
            return gpu, mem
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.close()
            return None, None


class PerfService(QtCore.QObject):
    """updated(dict): {cpu, cpu_per_core:[...], mem_pct, mem_used, mem_total, gpu(None 可),
    gpu_mem(bytes|None), net_up, net_down, disk_r, disk_w (bytes/s),
    history: {cpu, mem, gpu, net_up, net_down}（各最近 60 个）}"""

    updated = QtCore.Signal(dict)
    INTERVAL = 1.0
    HISTORY = 60

    def __init__(self, parent=None, per_core: bool = True):
        super().__init__(parent)
        ui(lambda: None)   # 确保 bg 的 _Invoker 在主线程创建（run_bg 回调依赖它）
        self.per_core = per_core
        self.state: dict = {}
        self.history = {k: deque(maxlen=self.HISTORY) for k in ("cpu", "mem", "gpu", "net_up", "net_down")}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._procs: dict[int, object] = {}
        self._proc_lock = threading.Lock()
        self._last_gpu = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wisland-perf", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):
        import psutil
        pdh = _Pdh()
        psutil.cpu_percent(None)
        if self.per_core:
            psutil.cpu_percent(None, percpu=True)
        last_t = time.monotonic()
        net0 = psutil.net_io_counters()
        try:
            disk0 = psutil.disk_io_counters()
        except Exception:  # noqa: BLE001
            disk0 = None
        pdh.sample()
        while not self._stop.wait(self.INTERVAL):
            try:
                now = time.monotonic()
                dt = max(1e-3, now - last_t)
                last_t = now
                cpu = psutil.cpu_percent(None)
                cores = psutil.cpu_percent(None, percpu=True) if self.per_core else []
                vm = psutil.virtual_memory()
                net = psutil.net_io_counters()
                up = max(0.0, (net.bytes_sent - net0.bytes_sent) / dt)
                down = max(0.0, (net.bytes_recv - net0.bytes_recv) / dt)
                net0 = net
                dr = dw = 0.0
                try:
                    disk = psutil.disk_io_counters()
                    if disk and disk0:
                        dr = max(0.0, (disk.read_bytes - disk0.read_bytes) / dt)
                        dw = max(0.0, (disk.write_bytes - disk0.write_bytes) / dt)
                    disk0 = disk
                except Exception:  # noqa: BLE001
                    pass
                gpu, gpu_mem = pdh.sample()
                if gpu is None:
                    gpu = self._last_gpu   # 重新添加计数器的那一拍沿用上次
                else:
                    self._last_gpu = gpu
                h = self.history
                h["cpu"].append(cpu)
                h["mem"].append(vm.percent)
                h["gpu"].append(gpu if gpu is not None else 0.0)
                h["net_up"].append(up)
                h["net_down"].append(down)
                st = {"cpu": cpu, "cpu_per_core": cores, "mem_pct": vm.percent,
                      "mem_used": vm.total - vm.available, "mem_total": vm.total,
                      "gpu": gpu, "gpu_mem": gpu_mem, "net_up": up, "net_down": down,
                      "disk_r": dr, "disk_w": dw,
                      "history": {k: list(v) for k, v in h.items()}}
                self.state = st
                self.updated.emit(st)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        pdh.close()

    def top_processes(self, n: int = 5, by: str = "cpu") -> list[dict]:
        """[{name, pid, cpu(占总 CPU 的 %), mem(bytes)}]；后台线程调用。
        首次调用时 CPU 值为 0（需要两次采样），之后为两次调用间的平均值。"""
        import psutil
        ncpu = psutil.cpu_count() or 1
        rows = []
        with self._proc_lock:
            seen = set()
            for p in psutil.process_iter():
                pid = p.pid
                if pid == 0:
                    continue
                seen.add(pid)
                proc = self._procs.get(pid)
                if proc is None:
                    proc = p
                    self._procs[pid] = proc
                    try:
                        proc.cpu_percent(None)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                try:
                    with proc.oneshot():
                        c = proc.cpu_percent(None) / ncpu
                        mem = proc.memory_info().rss
                        name = proc.name()
                    rows.append({"name": name, "pid": pid, "cpu": c, "mem": mem})
                except Exception:  # noqa: BLE001
                    continue
            for pid in list(self._procs):
                if pid not in seen:
                    del self._procs[pid]
        key = "mem" if by == "mem" else "cpu"
        rows.sort(key=lambda r: r[key], reverse=True)
        return rows[:n]
