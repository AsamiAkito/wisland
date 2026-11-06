# -*- coding: utf-8 -*-
"""音量（pycaw，主线程 COM）与亮度（Twinkle Tray CLI / DDC-CI / WMI，后台单线程）。"""
from __future__ import annotations

import ctypes
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W

from PySide6 import QtCore, QtGui

from wisland.core.bg import run_hidden, ui
from wisland.core.config import cfg, expand


# ======================================================================
# 音量
# ======================================================================
class AudioService(QtCore.QObject):
    """默认播放设备的主音量。所有 COM 调用只在主线程。"""

    volume_changed = QtCore.Signal(float, bool)   # (0..1, muted)

    POLL_MS = 250
    DEVICE_CHECK_S = 3.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enum = None
        self._dev_id = None
        self._vol = None      # IAudioEndpointVolume
        self._meter = None    # IAudioMeterInformation
        self._last = None     # (volume, muted)
        self._last_dev_check = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ---------------- 设备 ----------------
    def _acquire(self) -> bool:
        try:
            import comtypes
            from pycaw.pycaw import (IAudioEndpointVolume, IAudioMeterInformation,
                                     IMMDeviceEnumerator)
            try:
                from pycaw.constants import CLSID_MMDeviceEnumerator
            except Exception:  # noqa: BLE001
                from pycaw.pycaw import CLSID_MMDeviceEnumerator  # type: ignore
            if self._enum is None:
                self._enum = comtypes.CoCreateInstance(
                    CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
            dev = self._enum.GetDefaultAudioEndpoint(0, 1)   # eRender, eMultimedia
            self._dev_id = dev.GetId()
            self._vol = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None) \
                .QueryInterface(IAudioEndpointVolume)
            try:
                self._meter = dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None) \
                    .QueryInterface(IAudioMeterInformation)
            except Exception:  # noqa: BLE001
                self._meter = None
            self._last_dev_check = time.monotonic()
            return True
        except Exception:  # noqa: BLE001
            # 没有播放设备时会走到这里
            self._vol = self._meter = None
            self._dev_id = None
            self._last_dev_check = time.monotonic()
            return False

    def _check_device(self):
        """便宜地检查默认设备是否变了（只取 ID 字符串）。"""
        now = time.monotonic()
        if now - self._last_dev_check < self.DEVICE_CHECK_S and self._vol is not None:
            return
        self._last_dev_check = now
        try:
            if self._enum is None:
                self._acquire()
                return
            dev_id = self._enum.GetDefaultAudioEndpoint(0, 1).GetId()
            if dev_id != self._dev_id or self._vol is None:
                self._acquire()
        except Exception:  # noqa: BLE001
            self._vol = self._meter = None
            self._dev_id = None

    def _ev(self):
        if self._vol is None:
            self._acquire()
        return self._vol

    def _call(self, fn, default=None):
        """调用 COM，出错则重新获取设备再试一次。"""
        for attempt in range(2):
            ev = self._ev()
            if ev is None:
                return default
            try:
                return fn(ev)
            except Exception:  # noqa: BLE001
                self._vol = self._meter = None
                if attempt == 1:
                    return default
        return default

    # ---------------- 生命周期 ----------------
    def start(self):
        self._acquire()
        self._poll()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        try:
            self._check_device()
            cur = (round(self.volume(), 4), self.muted())
            if cur != self._last:
                self._last = cur
                self.volume_changed.emit(cur[0], cur[1])
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _notify_soon(self):
        QtCore.QTimer.singleShot(0, self._poll)

    # ---------------- API ----------------
    def available(self) -> bool:
        return self._ev() is not None

    def volume(self) -> float:
        return float(self._call(lambda ev: ev.GetMasterVolumeLevelScalar(), 0.0) or 0.0)

    def set_volume(self, v: float):
        v = max(0.0, min(1.0, float(v)))
        self._call(lambda ev: ev.SetMasterVolumeLevelScalar(v, None))
        self._notify_soon()

    def muted(self) -> bool:
        return bool(self._call(lambda ev: ev.GetMute(), False))

    def set_muted(self, b: bool):
        self._call(lambda ev: ev.SetMute(1 if b else 0, None))
        self._notify_soon()

    def toggle_mute(self):
        self.set_muted(not self.muted())

    def step(self, delta: float):
        """delta 为 0..1 刻度，例如 +0.02。调大音量时自动取消静音。"""
        self.set_volume(self.volume() + delta)
        if delta > 0 and self.muted():
            self.set_muted(False)

    def peak(self) -> float:
        m = self._meter
        if m is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(m.GetPeakValue())))
        except Exception:  # noqa: BLE001
            self._meter = None
            self._vol = None
            return 0.0


# ======================================================================
# 亮度
# ======================================================================
_user32 = ctypes.WinDLL("user32")
_dxva2 = ctypes.windll.dxva2


class _PHYS(ctypes.Structure):
    _fields_ = [("h", W.HANDLE), ("desc", W.WCHAR * 128)]


class _MI(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT),
                ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]


_MONENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [W.HMONITOR, ctypes.POINTER(W.DWORD)]
_dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [W.HMONITOR, W.DWORD, ctypes.POINTER(_PHYS)]
_dxva2.DestroyPhysicalMonitors.argtypes = [W.DWORD, ctypes.POINTER(_PHYS)]
_dxva2.GetMonitorBrightness.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD),
                                        ctypes.POINTER(W.DWORD)]
_dxva2.SetMonitorBrightness.argtypes = [W.HANDLE, W.DWORD]
_dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [W.HANDLE, W.BYTE, ctypes.POINTER(W.DWORD),
                                                   ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD)]
_dxva2.SetVCPFeature.argtypes = [W.HANDLE, W.BYTE, W.DWORD]
_user32.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.POINTER(_MI)]


def _enum_physical():
    """[(device_name, index_on_device, handle, description)]；调用方负责 _destroy。"""
    hmons = []

    def cb(h, dc, r, lp):
        hmons.append(h)
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(cb), 0)
    out, arrays = [], []
    for hm in hmons:
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        _user32.GetMonitorInfoW(hm, ctypes.byref(mi))
        n = W.DWORD()
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hm, ctypes.byref(n)) or n.value == 0:
            out.append((mi.szDevice, 0, None, ""))
            continue
        arr = (_PHYS * n.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(hm, n.value, arr):
            out.append((mi.szDevice, 0, None, ""))
            continue
        arrays.append((n.value, arr))
        for i, p in enumerate(arr):
            out.append((mi.szDevice, i, p.h, p.desc))
    return out, arrays


def _destroy(arrays):
    for n, arr in arrays:
        try:
            _dxva2.DestroyPhysicalMonitors(n, arr)
        except Exception:  # noqa: BLE001
            pass


def _ddc_get(h) -> int | None:
    if not h:
        return None
    mn, cur, mx = W.DWORD(), W.DWORD(), W.DWORD()
    if _dxva2.GetMonitorBrightness(h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)) and mx.value > mn.value:
        return round((cur.value - mn.value) * 100 / (mx.value - mn.value))
    ct = W.DWORD()
    if _dxva2.GetVCPFeatureAndVCPFeatureReply(h, 0x10, ctypes.byref(ct), ctypes.byref(cur), ctypes.byref(mx)) \
            and mx.value > 0:
        return round(cur.value * 100 / mx.value)
    return None


def _ddc_set(h, value: int) -> bool:
    if not h:
        return False
    mn, cur, mx = W.DWORD(), W.DWORD(), W.DWORD()
    if _dxva2.GetMonitorBrightness(h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)) and mx.value > mn.value:
        raw = mn.value + round(value * (mx.value - mn.value) / 100)
        if _dxva2.SetMonitorBrightness(h, raw):
            return True
    return bool(_dxva2.SetVCPFeature(h, 0x10, int(value)))


class _Wmi:
    """root\\wmi 亮度（笔记本内屏）。只在亮度工作线程里使用（MTA）。"""

    def __init__(self):
        self.svc = None
        self.failed = False

    def _conn(self):
        if self.svc is None and not self.failed:
            try:
                import comtypes.client
                self.svc = comtypes.client.CoGetObject(
                    "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\wmi")
            except Exception:  # noqa: BLE001
                self.failed = True
        return self.svc

    @staticmethod
    def _p(o, name):
        return o.Properties_.Item(name).Value

    def read(self) -> list[tuple[str, int]]:
        svc = self._conn()
        if svc is None:
            return self._read_ps()
        try:
            return [(str(self._p(o, "InstanceName")), int(self._p(o, "CurrentBrightness")))
                    for o in svc.ExecQuery("SELECT * FROM WmiMonitorBrightness")
                    if self._p(o, "Active")]
        except Exception:  # noqa: BLE001
            # 没有内屏时 WMI 会抛“不支持”，属正常
            self.svc = None
            return []

    def set(self, value: int, instance: str | None = None) -> bool:
        svc = self._conn()
        if svc is None:
            return self._set_ps(value)
        try:
            ok = False
            for m in svc.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods"):
                if instance and str(self._p(m, "InstanceName")) != instance:
                    continue
                ip = m.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
                ip.Properties_.Item("Timeout").Value = 0
                ip.Properties_.Item("Brightness").Value = int(value)
                m.ExecMethod_("WmiSetBrightness", ip)
                ok = True
            return ok
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.svc = None
            return False

    # 最后手段：PowerShell（慢，约 1 秒）
    @staticmethod
    def _read_ps():
        try:
            r = run_hidden(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance -Namespace root/wmi WmiMonitorBrightness | "
                            "ForEach-Object { \"$($_.InstanceName)|$($_.CurrentBrightness)\" }"], timeout=8)
            out = []
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                if "|" in line:
                    a, b = line.rsplit("|", 1)
                    out.append((a.strip(), int(b)))
            return out
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _set_ps(value):
        try:
            r = run_hidden(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance -Namespace root/wmi WmiMonitorBrightnessMethods | "
                            f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=0;Brightness={int(value)}}}"],
                           timeout=8)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False


def _init_worker():
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001
        pass


class BrightnessService(QtCore.QObject):
    """显示器亮度。

    monitors: [{id, name, brightness(int 0..100 或 None), method("ddc"/"wmi"/None), device}]
    """

    changed = QtCore.Signal(list)

    DEBOUNCE_MS = 150
    TWINKLE_CHECK_S = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)   # 确保 bg 的 _Invoker 在主线程创建（它是惰性创建的）
        self.monitors: list[dict] = []
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wisland-bright",
                                        initializer=_init_worker)
        self._wmi = _Wmi()
        self._pending: dict = {}      # id 或 None(全部) -> value
        self._busy = False
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._flush)
        self._twinkle_state = (0.0, False)
        self._twinkle_launched = False
        self._lock = threading.Lock()

    def start(self):
        self.refresh()

    def stop(self):
        self._debounce.stop()
        self._exec.shutdown(wait=False, cancel_futures=True)

    def _submit(self, fn, on_done=None):
        def task():
            try:
                res = fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                return
            if on_done:
                ui(lambda: on_done(res))
        try:
            self._exec.submit(task)
        except RuntimeError:
            pass   # 已 stop

    # ---------------- 读取 ----------------
    def refresh(self):
        self._submit(self._read_all, self._on_read)

    def _read_all(self) -> list[dict]:
        phys, arrays = _enum_physical()
        try:
            mons = []
            for dev, idx, h, desc in phys:
                b = _ddc_get(h)
                mons.append({"id": f"{dev}#{idx}", "device": dev, "desc": desc,
                             "brightness": b, "method": "ddc" if b is not None else None,
                             "wmi": None})
        finally:
            _destroy(arrays)
        wmi = self._wmi.read()
        if wmi:
            # 内屏 DDC 不可用：把 WMI 实例依次分给 DDC 失败的显示器
            free = [m for m in mons if m["method"] is None]
            for (inst, val), m in zip(wmi, free):
                m.update(brightness=val, method="wmi", wmi=inst)
            if not free and not mons:
                mons.append({"id": "wmi#0", "device": "", "desc": "内置显示器",
                             "brightness": wmi[0][1], "method": "wmi", "wmi": wmi[0][0]})
        return mons

    def _on_read(self, mons: list[dict]):
        names = {}
        try:
            for s in QtGui.QGuiApplication.screens():
                names[s.name()] = (s.model() or s.manufacturer() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        generic = ("generic pnp monitor", "通用即插即用监视器", "generic monitor", "")
        for i, m in enumerate(mons):
            desc = (m.get("desc") or "").strip()
            nm = desc if desc.lower() not in generic else (names.get(m["device"]) or f"显示器 {i + 1}")
            m["name"] = nm
            m["num"] = i + 1
        self.monitors = mons
        self._emit()
        if not any(m["method"] for m in mons):
            self._maybe_launch_twinkle()

    def _emit(self):
        self.changed.emit([{"id": m["id"], "name": m["name"], "brightness": m["brightness"]}
                           for m in self.monitors])

    # ---------------- 设置（防抖） ----------------
    def set(self, monitor_id, value):
        value = int(max(0, min(100, round(value))))
        dirty = False
        if monitor_id is None:
            self._pending = {None: value}
        else:
            self._pending[monitor_id] = value
        for m in self.monitors:
            if (monitor_id is None or m["id"] == monitor_id) and m["brightness"] != value:
                m["brightness"] = value
                dirty = True
        if dirty:
            self._emit()
        if not self._debounce.isActive() and not self._busy:
            self._debounce.start()

    def _flush(self):
        if self._busy:
            self._debounce.start()
            return
        if not self._pending:
            return
        jobs, self._pending = self._pending, {}
        mons = [dict(m) for m in self.monitors]
        self._busy = True
        self._submit(lambda: self._apply(jobs, mons), self._on_applied)

    def _on_applied(self, _res):
        self._busy = False
        if self._pending:
            self._debounce.start()

    def _twinkle_running(self) -> bool:
        t, val = self._twinkle_state
        if time.monotonic() - t < self.TWINKLE_CHECK_S:
            return val
        val = False
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                n = (p.info.get("name") or "").lower()
                if "twinkle" in n:
                    val = True
                    break
        except Exception:  # noqa: BLE001
            pass
        self._twinkle_state = (time.monotonic(), val)
        return val

    def _twinkle_exe(self) -> str:
        p = expand(cfg.get("brightness.twinkle") or "")
        return p if p and os.path.isfile(p) else ""

    def _twinkle(self, args: list[str]) -> bool:
        exe = self._twinkle_exe()
        if not exe:
            return False
        try:
            run_hidden([exe, *args], timeout=5, cwd=os.path.dirname(exe))
            return True
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    def _maybe_launch_twinkle(self):
        """直连方式全都不可用时，尝试把 Twinkle Tray 启动到托盘（只尝试一次）。"""
        if self._twinkle_launched or not self._twinkle_exe():
            return
        self._twinkle_launched = True

        def go():
            if not self._twinkle_running():
                from wisland.core.bg import launch
                launch(self._twinkle_exe())
                self._twinkle_state = (0.0, False)
        self._submit(go)

    def _apply(self, jobs: dict, mons: list[dict]):
        use_twinkle = self._twinkle_running() and bool(self._twinkle_exe())
        if None in jobs:
            v = jobs.pop(None)
            if not (use_twinkle and self._twinkle(["--All", f"--Set={v}"])):
                self._apply_direct([(m, v) for m in mons])
        todo = []
        for mid, v in jobs.items():
            m = next((x for x in mons if x["id"] == mid), None)
            if m is None:
                continue
            if use_twinkle and self._twinkle([f"--MonitorNum={m.get('num', 1)}", f"--Set={v}"]):
                continue
            todo.append((m, v))
        if todo:
            self._apply_direct(todo)
        return True

    def _apply_direct(self, items: list[tuple[dict, int]]):
        need_ddc = [(m, v) for m, v in items if m.get("method") == "ddc"]
        for m, v in items:
            if m.get("method") == "wmi":
                self._wmi.set(v, m.get("wmi"))
        if need_ddc:
            phys, arrays = _enum_physical()
            try:
                by_id = {f"{dev}#{idx}": h for dev, idx, h, _ in phys}
                for m, v in need_ddc:
                    h = by_id.get(m["id"])
                    if h:
                        _ddc_set(h, v)
            finally:
                _destroy(arrays)
