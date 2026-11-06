# -*- coding: utf-8 -*-
"""自选股行情：轮询本地盯盘后端（Flask，默认 8321 端口）。

信号
  updated(dict)  {markets: {'a': [quote...], 'hk': [...], 'us': [...]}, status: {'a': '交易中', ...},
                  time, ok, error}
                 quote = {code, name, price, pct, chg, prev_close, open, high, low, time}（按自选顺序；
                 行情缺失时 price/pct/chg 为 None）
  alert(dict)    {code, name, price, pct, reason: 'pct'|'above'|'below', text}
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import threading
import time
from typing import Callable

import httpx
from PySide6 import QtCore

from wisland.core.bg import run_bg, ui
from wisland.core.config import DATA_DIR, cfg, expand

CN_TZ = _dt.timezone(_dt.timedelta(hours=8))  # 中国无夏令时，固定 UTC+8
ALERT_PATH = DATA_DIR / "stock_alerts.json"
WATCHLIST_REFRESH_S = 60
IDLE_INTERVAL_S = 60


def _cn_now() -> _dt.datetime:
    return _dt.datetime.now(CN_TZ)


def _is_us_dst(now: _dt.datetime) -> bool:
    """美国夏令时：3 月第 2 个周日 ~ 11 月第 1 个周日（按北京时间日期近似）。"""
    y = now.year

    def nth_sunday(month, n):
        d = _dt.date(y, month, 1)
        first = d + _dt.timedelta(days=(6 - d.weekday()) % 7)
        return first + _dt.timedelta(weeks=n - 1)

    start, end = nth_sunday(3, 2), nth_sunday(11, 1)
    return start < now.date() <= end  # 北京时间比美东早，切换日次日才生效


def market_status(market: str, now: _dt.datetime | None = None) -> str:
    now = now or _cn_now()
    hm = now.hour * 100 + now.minute
    wd = now.weekday()
    if market == "hk":
        if wd >= 5:
            return "休市"
        if 930 <= hm < 1200 or 1300 <= hm < 1600:
            return "交易中"
        if 1200 <= hm < 1300:
            return "午间休市"
        return "已收盘"
    if market == "us":
        start, end = (2130, 400) if _is_us_dst(now) else (2230, 500)
        if hm >= start:
            return "交易中" if wd < 5 else "休市"
        if hm < end:
            return "交易中" if 1 <= wd <= 5 else "休市"
        return "已收盘"
    if wd >= 5:
        return "休市"
    if 915 <= hm < 930:
        return "集合竞价"
    if 930 <= hm < 1130 or 1300 <= hm < 1500:
        return "交易中"
    if 1130 <= hm < 1300:
        return "午间休市"
    return "已收盘"


def trade_day(market: str, now: _dt.datetime | None = None) -> str:
    """提醒按"交易日"去重：美股夜盘跨零点，算到开盘那天。"""
    now = now or _cn_now()
    if market == "us":
        now = now - _dt.timedelta(hours=8)
    return now.strftime("%Y-%m-%d")


def market_of(code: str) -> str:
    c = code.lower()
    if c.startswith("hk"):
        return "hk"
    if c.startswith("us"):
        return "us"
    return "a"


def fmt_pct(p: float) -> str:
    return f"{'+' if p > 0 else ''}{p:.2f}%"


class StockService(QtCore.QObject):
    updated = QtCore.Signal(dict)
    alert = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 确保 bg 回调 invoker 在主线程创建
        self._client = httpx.Client(timeout=httpx.Timeout(6.0, connect=2.0), trust_env=False)
        self._lock = threading.Lock()
        self._watch: dict[str, list[dict]] = {}
        self._watch_t = 0.0
        self._busy = False
        self._running = False
        self._autostarted = False
        self._fail_count = 0
        self.last: dict = {}
        self._alerts = self._load_alerts()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

    # ---------- lifecycle ----------
    def start(self):
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        self._timer.stop()

    def refresh(self):
        """立即刷新（并重新读取自选列表）。"""
        self._watch_t = 0
        if not self._busy:
            self._timer.stop()
            self._tick()

    @property
    def server(self) -> str:
        return str(cfg.get("stocks.server") or "http://127.0.0.1:8321").rstrip("/")

    # ---------- polling ----------
    def _schedule(self):
        if not self._running:
            return
        markets = cfg.get("stocks.markets") or ["a", "hk", "us"]
        active = any(market_status(m) in ("交易中", "集合竞价") for m in markets)
        try:
            iv = float(cfg.get("stocks.interval") or 5)
        except (TypeError, ValueError):
            iv = 5.0
        sec = max(2.0, iv) if active else IDLE_INTERVAL_S
        if not self.last.get("ok"):
            sec = min(sec, 10.0)  # 后端没起来时 10 秒重试
        self._timer.start(int(sec * 1000))

    def _tick(self):
        if self._busy or not self._running:
            return
        self._busy = True
        run_bg(self._fetch, on_done=self._on_data, on_error=self._on_err)

    def _get(self, path: str, **params):
        r = self._client.get(self.server + path, params=params)
        r.raise_for_status()
        return r.json()

    def _fetch(self) -> dict:
        markets = list(cfg.get("stocks.markets") or ["a", "hk", "us"])
        now = time.time()
        if not self._watch or now - self._watch_t > WATCHLIST_REFRESH_S:
            watch = {}
            for m in markets:
                wl = self._get("/api/watchlist", market=m)
                watch[m] = [{"code": s.get("code", ""), "name": s.get("name", "")} for s in wl if s.get("code")]
            self._watch, self._watch_t = watch, now
        codes = [s["code"] for m in markets for s in self._watch.get(m, [])]
        quotes = {}
        # 分批，避免 URL 过长
        for i in range(0, len(codes), 60):
            q = self._get("/api/quote", codes=",".join(codes[i:i + 60])) or {}
            quotes.update(q)
        qmap = {k.lower(): v for k, v in quotes.items()}
        out = {}
        for m in markets:
            rows = []
            for s in self._watch.get(m, []):
                q = qmap.get(s["code"].lower())
                if q:
                    row = {k: q.get(k) for k in ("code", "name", "price", "pct", "chg", "prev_close",
                                                 "open", "high", "low", "time")}
                    row["code"] = s["code"]
                    row["name"] = q.get("name") or s["name"]
                else:
                    row = {"code": s["code"], "name": s["name"], "price": None, "pct": None, "chg": None}
                rows.append(row)
            out[m] = rows
        return {"markets": out, "status": {m: market_status(m) for m in markets},
                "time": time.time(), "ok": True, "error": ""}

    def _on_data(self, data: dict):
        self._busy = False
        self._fail_count = 0
        self.last = data
        try:
            self.updated.emit(data)
            self._check_alerts(data)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        self._schedule()

    def _on_err(self, e: Exception):
        self._busy = False
        self._fail_count += 1
        unreachable = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
        msg = "盯盘后端未启动" if unreachable else f"行情获取失败：{str(e)[:80]}"
        if unreachable and not self._autostarted:
            self._autostarted = True
            vbs = expand(cfg.get("stocks.autostart") or "")
            if vbs and os.path.exists(vbs):
                msg = "正在启动盯盘后端…"
                try:
                    import subprocess
                    subprocess.Popen(["wscript.exe", vbs], cwd=os.path.dirname(vbs),
                                     creationflags=0x08000000)
                except Exception:  # noqa: BLE001
                    pass
        self.last = {"markets": self.last.get("markets", {}), "status": {}, "time": time.time(),
                     "ok": False, "error": msg}
        self.updated.emit(self.last)
        self._schedule()

    # ---------- minute ----------
    def minute(self, code: str, cb: Callable[[list], None]):
        """异步取当日分时价格 -> cb(list[float])（主线程回调；失败给空列表）。"""
        def work():
            j = self._get("/api/minute", code=code)
            return [float(p["price"]) for p in (j.get("points") or []) if p.get("price") is not None]
        run_bg(work, on_done=cb, on_error=lambda e: cb([]))

    # ---------- alerts ----------
    def _load_alerts(self) -> dict:
        try:
            return json.loads(ALERT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_alerts(self):
        try:
            # 只保留最近 5 天
            keep = sorted(self._alerts.keys())[-5:]
            self._alerts = {k: self._alerts[k] for k in keep}
            tmp = ALERT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._alerts, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, ALERT_PATH)
        except Exception:  # noqa: BLE001
            pass

    def _check_alerts(self, data: dict):
        try:
            step = float(cfg.get("stocks.pct_alert") or 0)
        except (TypeError, ValueError):
            step = 0.0
        rules = cfg.get("stocks.alerts") or {}
        rules_l = {k.lower(): v for k, v in rules.items() if isinstance(v, dict)}
        status = data.get("status") or {}
        dirty = False
        for m, rows in (data.get("markets") or {}).items():
            if status.get(m) != "交易中":
                continue  # 收盘后的涨跌幅不提醒（避免启动时把昨天的行情报一遍）
            day = self._alerts.setdefault(trade_day(m), {})
            for q in rows:
                price, pct = q.get("price"), q.get("pct")
                if price is None or pct is None or not price:
                    continue
                code, name = q["code"], q.get("name") or q["code"]
                st = day.setdefault(code, {})
                if step > 0 and abs(pct) >= step:
                    direction = "up" if pct > 0 else "down"
                    level = int(math.floor(abs(pct) / step + 1e-9))
                    if level > int(st.get(direction, 0)):
                        st[direction] = level
                        dirty = True
                        self.alert.emit({"code": code, "name": name, "price": price, "pct": pct,
                                         "reason": "pct", "text": f"{name} {fmt_pct(pct)}"})
                rule = rules_l.get(code.lower()) or {}
                for kind in ("above", "below"):
                    th = rule.get(kind)
                    if th is None or st.get(kind):
                        continue
                    try:
                        th = float(th)
                    except (TypeError, ValueError):
                        continue
                    hit = price >= th if kind == "above" else price <= th
                    if hit:
                        st[kind] = True
                        dirty = True
                        word = "突破" if kind == "above" else "跌破"
                        self.alert.emit({"code": code, "name": name, "price": price, "pct": pct,
                                         "reason": kind,
                                         "text": f"{name} {word} {th:g}（{price:g}，{fmt_pct(pct)}）"})
        if dirty:
            self._save_alerts()
