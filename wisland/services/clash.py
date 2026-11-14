# -*- coding: utf-8 -*-
"""Clash Verge Rev（mihomo 内核）状态与控制。

传输：先试 HTTP external-controller（0.6s 内能连上才用），否则走 Windows 命名管道
（\\\\.\\pipe\\verge-mihomo-*），在管道上手写 HTTP/1.1（Connection: close）。选择结果会缓存，失败时重新探测。

信号 updated(dict):
  {ok, error, running, mode('rule'|'global'|'direct'), tun(bool), up, down (B/s), conns,
   group, node, nodes:[names], delay(ms|None), sysproxy(bool), transport('http'|'pipe'|''),
   sub:{used, total, expire}}   # 当前订阅流量配额（字节；expire 为 Unix 时间戳，0 = 未知）
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import winreg
from typing import Callable
from urllib.parse import quote

import httpx
from PySide6 import QtCore

from wisland.core.bg import launch, run_bg, ui
from wisland.core.config import cfg, expand

PIPE_DIR = "\\\\.\\pipe\\"
INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
SKIP_GROUPS = {"GLOBAL", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
DELAY_URL = "http://www.gstatic.com/generate_204"


class ClashError(Exception):
    pass


# ---------------- 极简 HTTP/1.1 over named pipe ----------------

def _parse_response(raw: bytes) -> tuple[int, dict, bytes]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise ClashError("响应不完整")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ClashError("响应格式错误")
    status = int(parts[1])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    elif "content-length" in headers:
        body = body[: int(headers["content-length"])]
    return status, headers, body


def _dechunk(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        size = int(data[i:j].split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out += data[j + 2: j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


def _complete(buf: bytes) -> bool:
    """已收到完整响应（不必等对端关闭）。"""
    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        return False
    h = head.lower()
    if b"transfer-encoding: chunked" in h:
        return body.endswith(b"0\r\n\r\n")
    for ln in h.split(b"\r\n"):
        if ln.startswith(b"content-length:"):
            try:
                return len(body) >= int(ln.split(b":", 1)[1])
            except ValueError:
                return False
    if head.split(b" ", 2)[1:2] in ([b"204"], [b"304"]):
        return True
    return False


def pipe_request(pipe: str, method: str, path: str, secret: str, body: bytes | None = None) -> tuple[int, bytes]:
    req = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close", "Accept: application/json"]
    if secret:
        req.append(f"Authorization: Bearer {secret}")
    if body is not None:
        req.append("Content-Type: application/json")
        req.append(f"Content-Length: {len(body)}")
    data = ("\r\n".join(req) + "\r\n\r\n").encode("utf-8") + (body or b"")
    f = None
    for attempt in range(6):
        try:
            f = open(pipe, "r+b", buffering=0)
            break
        except OSError as e:
            if getattr(e, "winerror", 0) == 231 or e.errno == 16:  # ERROR_PIPE_BUSY
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    if f is None:
        raise ClashError("管道忙")
    buf = b""
    with f:
        f.write(data)
        while True:
            try:
                chunk = f.read(65536)
            except OSError:  # ERROR_BROKEN_PIPE = 对端关闭
                break
            if not chunk:
                break
            buf += chunk
            if _complete(buf):
                break
    status, _h, payload = _parse_response(buf)
    return status, payload


def find_pipe(configured: str = "") -> str:
    try:
        names = os.listdir(PIPE_DIR)
    except OSError:
        names = []
    if configured:
        base = configured.replace("/", "\\").split("\\")[-1]
        if base in names:
            return PIPE_DIR + base
    for n in names:
        if n.lower().startswith("verge-mihomo") and not n.lower().startswith("clash-verge-service"):
            return PIPE_DIR + n
    return ""


# ---------------- 系统代理 ----------------

def read_sysproxy() -> tuple[bool, str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY) as k:
            en = winreg.QueryValueEx(k, "ProxyEnable")[0]
            try:
                srv = winreg.QueryValueEx(k, "ProxyServer")[0]
            except OSError:
                srv = ""
        return bool(en), str(srv or "")
    except OSError:
        return False, ""


def write_sysproxy(enable: bool, port: int):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{int(port)}")
    wininet = ctypes.WinDLL("wininet")
    wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
    wininet.InternetSetOptionW(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(None, 37, None, 0)  # INTERNET_OPTION_REFRESH


# ---------------- 订阅流量 ----------------

def _sub_int(v) -> int | None:
    """订阅配额里的数值：兼容字符串 / 浮点；None、非数字、负数都视为不合法。"""
    if v is None:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def _sub_quota(item: dict) -> dict:
    """从 profiles.yaml 的一个订阅 item 里取流量配额；不完整时返回 {}。"""
    extra = item.get("extra")
    if not isinstance(extra, dict):
        return {}
    upload, download = _sub_int(extra.get("upload")), _sub_int(extra.get("download"))
    total = _sub_int(extra.get("total"))
    if upload is None or download is None or not total:
        return {}
    expire = _sub_int(extra.get("expire")) or 0
    return {"used": upload + download, "total": total, "expire": expire}


def read_subscription(config_dir: str) -> dict:
    """Clash Verge 当前订阅的流量配额；没有时返回 {}。"""
    try:
        import yaml
        with open(os.path.join(config_dir, "profiles.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        current = str(data.get("current") or "")
        cur = next((it for it in items if current and str(it.get("uid") or "") == current), None)
        if cur is not None:
            return _sub_quota(cur)
        for it in items:                       # current 失效时退回第一个有配额的订阅
            sub = _sub_quota(it)
            if sub:
                return sub
        return {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------- service ----------------

class ClashService(QtCore.QObject):
    updated = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 确保 bg 回调 invoker 在主线程创建
        self._secret = ""
        self._controller = ""
        self._pipe_cfg = ""
        self._transport: str = ""   # 'http' | 'pipe' | ''
        self._pipe = ""
        self._http = httpx.Client(timeout=httpx.Timeout(3.0, connect=0.6), trust_env=False)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_totals: tuple[float, int, int] | None = None
        self._last_proxies_t = 0.0
        self._proxy_info: dict = {"group": "", "node": "", "nodes": [], "delay": None}
        self._sub: dict = {}
        self._sub_t = 0.0
        self.last: dict = {}

    # ---------- lifecycle ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wisland-clash", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    # ---------- config / transport ----------
    def _load_config(self):
        d = expand(cfg.get("clash.config_dir") or "")
        for name in ("config.yaml", "clash-verge.yaml"):
            p = os.path.join(d, name)
            if not os.path.exists(p):
                continue
            try:
                import yaml
                with open(p, encoding="utf-8") as f:
                    c = yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                continue
            self._secret = str(c.get("secret") or "")
            self._controller = str(c.get("external-controller") or "")
            self._pipe_cfg = str(c.get("external-controller-pipe") or "")
            return

    def _subscription(self) -> dict:
        """订阅流量：最多每 5 秒重读一次 profiles.yaml（避免每秒解析 YAML）。"""
        now = time.monotonic()
        if now - self._sub_t < 5.0:
            return self._sub
        self._sub_t = now
        self._sub = read_subscription(expand(cfg.get("clash.config_dir") or ""))
        return self._sub

    def _detect(self):
        self._load_config()
        self._transport = ""
        if self._controller:
            host = self._controller
            if host.startswith(":"):
                host = "127.0.0.1" + host
            host = host.replace("0.0.0.0", "127.0.0.1")
            try:
                r = self._http.get(f"http://{host}/version", headers=self._auth(),
                                   timeout=httpx.Timeout(1.0, connect=0.6))
                if r.status_code < 500:
                    self._controller = host
                    self._transport = "http"
                    return
            except Exception:  # noqa: BLE001
                pass
        self._pipe = find_pipe(self._pipe_cfg)
        if self._pipe:
            self._transport = "pipe"

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._secret}"} if self._secret else {}

    def _request(self, method: str, path: str, payload=None, timeout: float = 3.0):
        """返回解析后的 JSON（或 None）。失败抛异常。"""
        with self._lock:
            if not self._transport:
                self._detect()
            transport = self._transport
        if not transport:
            raise ClashError("Clash 未运行")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            if transport == "http":
                r = self._http.request(method, f"http://{self._controller}{path}", content=body,
                                       headers={**self._auth(), "Content-Type": "application/json"},
                                       timeout=httpx.Timeout(timeout, connect=0.6))
                status, data = r.status_code, r.content
            else:
                status, data = pipe_request(self._pipe, method, path, self._secret, body)
        except (OSError, httpx.TransportError, ClashError):
            with self._lock:
                self._transport = ""  # 下次重新探测
            raise
        if status == 401:
            with self._lock:
                self._transport = ""
            raise ClashError("鉴权失败（secret）")
        if status >= 400:
            msg = ""
            try:
                msg = json.loads(data).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            raise ClashError(f"HTTP {status} {msg}".strip())
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError:
            return None

    # ---------- polling ----------
    def _loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                d = self._poll()
            except Exception as e:  # noqa: BLE001
                en, _ = read_sysproxy()
                self._last_totals = None
                down_err = isinstance(e, (FileNotFoundError, httpx.ConnectError)) or "未运行" in str(e)
                d = {"ok": False, "error": "Clash 未运行" if down_err else f"Clash 连接失败：{str(e)[:60]}",
                     "running": False, "mode": "", "tun": False, "up": 0, "down": 0, "conns": 0,
                     "group": "", "node": "", "nodes": [], "delay": None, "sysproxy": en, "transport": "",
                     "sub": {}}
            self.last = d
            self.updated.emit(d)
            wait = (1.0 if d.get("ok") else 3.0) - (time.monotonic() - t0)
            self._wake.wait(max(0.05, wait))
            self._wake.clear()

    def _poll(self) -> dict:
        conf = self._request("GET", "/configs") or {}
        mode = str(conf.get("mode") or "").lower()
        tun = bool((conf.get("tun") or {}).get("enable"))
        conn = self._request("GET", "/connections") or {}
        now = time.monotonic()
        upt, dnt = int(conn.get("uploadTotal") or 0), int(conn.get("downloadTotal") or 0)
        up = down = 0.0
        if self._last_totals:
            t, u0, d0 = self._last_totals
            dt = max(0.2, now - t)
            if upt >= u0 and dnt >= d0:
                up, down = (upt - u0) / dt, (dnt - d0) / dt
        self._last_totals = (now, upt, dnt)
        if time.time() - self._last_proxies_t > 5 or self._proxy_info.get("_mode") != mode:
            try:
                self._proxy_info = self._proxies(mode)
                self._proxy_info["_mode"] = mode
                self._last_proxies_t = time.time()
            except Exception:  # noqa: BLE001
                pass
        en, _srv = read_sysproxy()
        pi = self._proxy_info
        return {"ok": True, "error": "", "running": True, "mode": mode, "tun": tun,
                "up": up, "down": down, "conns": len(conn.get("connections") or []),
                "group": pi.get("group", ""), "node": pi.get("node", ""), "nodes": list(pi.get("nodes", [])),
                "delay": pi.get("delay"), "sysproxy": en, "transport": self._transport,
                "sub": self._subscription()}

    @staticmethod
    def _last_delay(proxies: dict, name: str, depth: int = 0):
        p = proxies.get(name) or {}
        hist = p.get("history") or []
        if hist:
            dl = hist[-1].get("delay")
            if dl:
                return int(dl)
            if p.get("type") not in GROUP_TYPES:
                return None
        if p.get("now") and depth < 4:
            return ClashService._last_delay(proxies, p["now"], depth + 1)
        return None

    def _proxies(self, mode: str) -> dict:
        data = (self._request("GET", "/proxies") or {}).get("proxies") or {}
        glob = data.get("GLOBAL") or {}
        group = ""
        if mode == "global" and glob:
            group = "GLOBAL"
        else:
            order = list(glob.get("all") or []) or list(data.keys())
            for n in order:
                p = data.get(n) or {}
                if n not in SKIP_GROUPS and p.get("type") == "Selector":
                    group = n
                    break
        if not group:
            return {"group": "", "node": "", "nodes": [], "delay": None}
        g = data.get(group) or {}
        node = g.get("now") or ""
        return {"group": group, "node": node, "nodes": list(g.get("all") or []),
                "delay": self._last_delay(data, node) if node else None}

    def _after_change(self, _res=None):
        self._last_proxies_t = 0
        self._wake.set()

    # ---------- controls ----------
    def set_mode(self, mode: str):
        if mode not in ("rule", "global", "direct"):
            return
        run_bg(self._request, "PATCH", "/configs", {"mode": mode}, on_done=self._after_change)

    def select(self, group: str, name: str):
        run_bg(self._request, "PUT", "/proxies/" + quote(group, safe=""), {"name": name},
               on_done=self._after_change)

    def set_tun(self, enable: bool):
        run_bg(self._request, "PATCH", "/configs", {"tun": {"enable": bool(enable)}}, on_done=self._after_change)

    def test_delay(self, name: str, cb: Callable[[int | None], None]):
        """测速 -> cb(ms 或 None)（主线程回调）。"""
        def work():
            path = (f"/proxies/{quote(name, safe='')}/delay?timeout=3000&url={quote(DELAY_URL, safe='')}")
            try:
                d = self._request("GET", path, timeout=5.0) or {}
            except ClashError:
                return None
            dl = d.get("delay")
            return int(dl) if dl else None
        run_bg(work, on_done=lambda ms: (cb(ms), self._after_change()), on_error=lambda e: cb(None))

    def set_sysproxy(self, enable: bool):
        def work():
            write_sysproxy(enable, int(cfg.get("clash.proxy_port") or 7897))
        run_bg(work, on_done=self._after_change)

    def open_app(self):
        p = expand(cfg.get("clash.app") or "")
        if p:
            launch(p)
