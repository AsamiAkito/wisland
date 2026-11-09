# -*- coding: utf-8 -*-
"""自选股行情：直接请求腾讯财经公开行情接口（无需 Key、无需后端）。

自选列表在 config.json → stocks.watchlist（如 ["sh000001", "hk00700", "usAAPL"]），也可在股票页点 + 搜索添加、右键删除。

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
import time
from typing import Callable

import httpx
from PySide6 import QtCore

from wisland.core.bg import run_bg, ui
from wisland.core.config import DATA_DIR, cfg

CN_TZ = _dt.timezone(_dt.timedelta(hours=8))  # 中国无夏令时，固定 UTC+8
ALERT_PATH = DATA_DIR / "stock_alerts.json"
IDLE_INTERVAL_S = 60
QUOTE_URL = "https://qt.gtimg.cn/q="
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app"
SEARCH_URL = "https://smartbox.gtimg.cn/s3/"
DEFAULT_WATCHLIST = ["sh000001", "sz399001", "sz399006", "hk00700", "usAAPL", "usNVDA", "usQQQ"]


def norm_code(code: str) -> str:
    """规范化代码：SH600519 -> sh600519；usAAPL.OQ -> usAAPL。"""
    c = (code or "").strip()
    if len(c) > 2 and c[:2].lower() in ("sh", "sz", "bj", "hk", "us"):
        c = c[:2].lower() + c[2:]
    if c.startswith("us"):
        c = "us" + c[2:].split(".")[0].upper()
    return c


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_quotes(text: str) -> dict:
    """解析 qt.gtimg.cn：v_sh600519="1~名称~代码~现价~昨收~今开~...~时间~涨跌~涨跌幅~最高~最低~..." """
    out = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        f = val.strip().strip('"').split("~")
        if len(f) < 35:
            continue
        out[key[2:].lower()] = {"name": f[1], "price": _f(f[3]), "prev_close": _f(f[4]), "open": _f(f[5]),
                                "chg": _f(f[31]), "pct": _f(f[32]), "high": _f(f[33]), "low": _f(f[34]),
                                "time": f[30], "sym": f[2]}
    return out


def parse_search(text: str) -> list[dict]:
    """解析 smartbox：v_hint="sh~600519~贵州茅台~gzmt~GP-A^hk~00700~..." """
    body = text.partition("=")[2].strip().strip(";").strip('"')
    try:
        body = body.encode("latin-1").decode("unicode_escape")
    except Exception:  # noqa: BLE001
        pass
    out = []
    for item in body.split("^"):
        f = item.split("~")
        if len(f) < 3 or f[0] not in ("sh", "sz", "bj", "hk", "us"):
            continue
        out.append({"code": norm_code(f[0] + f[1]), "name": f[2]})
    return out


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
        self._busy = False
        self._running = False
        self._fail_count = 0
        self.last: dict = {}
        self._alerts = self._load_alerts()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

    # ---------- lifecycle ----------
    def start(self):
        if self._running:
            return                       # 重复调用（重新加载配置 / 再次显示板块）时不打断轮询
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        self._timer.stop()

    def refresh(self):
        """立即刷新。"""
        if not self._busy:
            self._timer.stop()
            self._tick()

    # ---------- 自选列表（config.json → stocks.watchlist） ----------
    @staticmethod
    def watchlist() -> list[str]:
        wl = cfg.get("stocks.watchlist")
        if not isinstance(wl, list):
            wl = list(DEFAULT_WATCHLIST)
        return [norm_code(c) for c in wl if isinstance(c, str) and c.strip()]

    def add(self, code: str):
        code = norm_code(code)
        wl = self.watchlist()
        if code and code.lower() not in [c.lower() for c in wl]:
            wl.append(code)
            cfg.set("stocks.watchlist", wl)
            self.refresh()

    def remove(self, code: str):
        wl = [c for c in self.watchlist() if c.lower() != norm_code(code).lower()]
        cfg.set("stocks.watchlist", wl)
        self.refresh()

    def search(self, query: str, cb: Callable[[list], None]):
        """按名称 / 拼音 / 代码搜索 -> cb([{code, name}])（主线程回调）。"""
        def work():
            r = self._client.get(SEARCH_URL, params={"v": 2, "q": query, "t": "all"})
            r.raise_for_status()
            return parse_search(r.text)
        run_bg(work, on_done=cb, on_error=lambda e: cb([]))

    # ---------- polling ----------
    def _schedule(self):
        if not self._running:
            return
        markets = {market_of(c) for c in self.watchlist()} or {"a"}
        active = any(market_status(m) in ("交易中", "集合竞价") for m in markets)
        try:
            iv = float(cfg.get("stocks.interval") or 5)
        except (TypeError, ValueError):
            iv = 5.0
        sec = max(2.0, iv) if active else IDLE_INTERVAL_S
        if not self.last.get("ok"):
            sec = min(sec, 15.0)  # 网络失败时 15 秒重试
        self._timer.start(int(sec * 1000))

    def _tick(self):
        if self._busy or not self._running:
            return
        self._busy = True
        run_bg(self._fetch, self.watchlist(), on_done=self._on_data, on_error=self._on_err)

    def _fetch(self, codes: list[str]) -> dict:
        quotes: dict[str, dict] = {}
        for i in range(0, len(codes), 50):
            part = codes[i:i + 50]
            r = self._client.get(QUOTE_URL + ",".join(part))
            r.raise_for_status()
            quotes.update(parse_quotes(r.content.decode("gbk", "replace")))
        out: dict[str, list[dict]] = {"a": [], "hk": [], "us": []}
        for c in codes:
            q = quotes.get(c.lower())
            row = dict(q) if q else {"name": c, "price": None, "pct": None, "chg": None}
            row["code"] = c
            out[market_of(c)].append(row)
        return {"markets": out, "status": {m: market_status(m) for m in out},
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
        msg = "网络连接失败" if isinstance(e, httpx.TransportError) else f"行情获取失败：{str(e)[:80]}"
        self.last = {"markets": self.last.get("markets", {}), "status": {}, "time": time.time(),
                     "ok": False, "error": msg}
        self.updated.emit(self.last)
        self._schedule()

    # ---------- minute ----------
    def minute(self, code: str, cb: Callable[[list], None]):
        """异步取当日分时价格 -> cb(list[float])（主线程回调；失败给空列表）。"""
        def work():
            m = market_of(code)
            path = {"us": "UsMinute", "hk": "hkMinute"}.get(m, "minute")
            key = norm_code(code)
            if m == "us":       # 美股分时需要带交易所后缀（如 QQQ.OQ / BABA.N），从行情里取
                r0 = self._client.get(QUOTE_URL + key)
                sym = (parse_quotes(r0.content.decode("gbk", "replace")).get(key.lower()) or {}).get("sym") or ""
                key = "us" + sym if "." in sym else key + ".OQ"
            r = self._client.get(f"{MINUTE_URL}/{path}/query", params={"code": key})
            r.raise_for_status()
            data = r.json().get("data") or {}
            block = next(iter(data.values()), {}) if isinstance(data, dict) and data else {}
            rows = ((block.get("data") or {}).get("data")) or []
            pts = []
            for line in rows:
                parts = str(line).split()
                if len(parts) >= 2:
                    try:
                        pts.append(float(parts[1]))
                    except ValueError:
                        pass
            return pts
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
