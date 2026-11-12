# -*- coding: utf-8 -*-
"""天气：默认用 Open-Meteo（免费、无需 Key）；填了 weather.api_key 则改用 OpenWeatherMap。

添加城市时依次用 OpenWeather（有 Key）/ Open-Meteo / Photon / Nominatim 查询坐标，直连失败会
自动改走 Windows 系统代理——个别网络下 open-meteo 的地理编码连不上时也能添加城市。
默认城市按公网 IP 所在城市自动选择并跟随网络变化；右键「绑定主城市」可固定，绑定后当前 IP
城市仍会作为第二项显示。用户自己添加的城市保存在 weather.cities 里，不受影响。

信号 updated(dict):
  {ok, error, city, temp, feels_like, desc, icon('10d'), humidity, wind, sunrise, sunset(epoch),
   hourly: [{time, temp, icon, pop}] 接下来 8 个 3 小时步长,
   daily:  [{date('YYYY-MM-DD'), min, max, icon}] 接下来最多 5 天（按城市当地日期聚合）,
   time(epoch 获取时间)}

图标：
  fluent_icon(code)   -> Segoe Fluent Icons 字形。Fluent 字体里只有 3 个天气相关字形（已在本机字体中逐一
                         渲染核对，官方列表中没有雨/雪/雷等）：E706 Brightness(太阳) / E708 QuietHours(月亮) /
                         E753 Cloud(云)。所以只能区分 晴(日)/晴(夜)/其它=云。
  weather_glyph(code) -> (字符, 字体族) 更细的映射：雨、雪、雷、雾等用 "Segoe UI Symbol" 的单色天气符号
                         （U+2600 ☀、U+2601 ☁、U+1F324 🌤、U+1F327 🌧、U+1F326 🌦、U+1F329 🌩、U+2744 ❄、U+1F32B 🌫，
                         均在本机 Segoe UI Symbol 中以单色渲染核对过）；晴夜用 Fluent 的月亮 E708
                         （Segoe UI Symbol 无单色月亮，会回落到彩色 emoji）。
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from PySide6 import QtCore

from wisland.core.bg import run_bg, ui
from wisland.core.config import cfg
from wisland.live.http_util import system_proxy

API = "https://api.openweathermap.org/data/2.5"
OM_API = "https://api.open-meteo.com/v1/forecast"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
# 备用地理编码源（数据 © OpenStreetMap contributors）：和 open-meteo 不是同一台服务器，
# 个别网络下只有它们能连通
PHOTON = "https://photon.komoot.io/api/"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OWM_GEO = "https://api.openweathermap.org/geo/1.0/direct"
# 公网 IP 定位（默认城市跟随它变化）：bilibili / ipip 国内可直连，ipwho / ip.sb 海外或代理环境兜底
IP_BILI = "https://api.bilibili.com/x/web-interface/zone"
IP_WHO = "https://ipwho.is/"
IP_SB = "https://api.ip.sb/geoip"
IP_IPIP = "https://myip.ipip.net/"
IP_TIMEOUT = httpx.Timeout(5.0, connect=2.5)
UA = "wisland/1.6.0 (weather; +https://github.com/Misaka273/wisland)"
GEO_TIMEOUT = httpx.Timeout(10.0, connect=4.0)

_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，\s]\s*(-?\d+(?:\.\d+)?)\s*$")
# Photon 的搜索结果里可能混有街道、车站、小区，按类型挑最像城市的一个
_PHOTON_RANK = {"city": 6, "municipality": 5, "town": 5, "village": 4,
                "county": 4, "state": 3, "district": 2, "locality": 2}
_PROXY_BAD = (407, 502, 503, 504)     # 代理自己的错误：别当成城市的真实响应
_NOMINATIM_LOCK = threading.Lock()
_nominatim_next = 0.0


def _parse_coords(text: str) -> tuple[float, float] | None:
    """「31.23,121.47」这种手填坐标不用再联网查城市。"""
    m = _COORDS.match(str(text or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _same_city(c: dict, name: str, lat=None, lon=None) -> bool:
    if str(c.get("name") or "").strip().casefold() == str(name or "").strip().casefold():
        return True
    if lat in (None, "") or lon in (None, ""):
        return False
    try:
        return abs(float(c.get("lat")) - float(lat)) < 0.02 and abs(float(c.get("lon")) - float(lon)) < 0.02
    except (TypeError, ValueError):
        return False


def _norm_city_name(name) -> str:
    return re.sub(r"(特别行政区|自治州|市|省)$", "", str(name or "").strip()).casefold()


def _same_ip_city(a: dict, b: dict) -> bool:
    """IP 定位结果是否还是同一个城市；不同接口的坐标可能差几十公里，放宽到 0.5°。"""
    if not a or not b:
        return False
    try:
        return (abs(float(a.get("lat")) - float(b.get("lat"))) <= 0.5
                and abs(float(a.get("lon")) - float(b.get("lon"))) <= 0.5)
    except (TypeError, ValueError):
        return _norm_city_name(a.get("name")) == _norm_city_name(b.get("name"))


def _better_ip_city(old: dict | None, new: dict | None) -> bool:
    """同一城市不换名字（避免不同接口来回跳）；旧结果没坐标、新结果有时升级一次。"""
    if not new:
        return False
    if not old:
        return True
    if not _same_ip_city(new, old):
        return True
    return old.get("lat") in (None, "") and new.get("lat") not in (None, "")


def _ip_city(name, lat=None, lon=None, country="") -> dict | None:
    """IP 定位接口的返回 -> 城市 dict；只拿到国家名时不算（避免定位到国家中心）。"""
    name = str(name or "").strip()
    if not name or name.casefold() in ("中国", "china", "未知", "unknown"):
        return None
    out = {"name": name, "country": str(country or "").strip()}
    try:
        if lat not in (None, "") and lon not in (None, ""):
            la, lo = float(lat), float(lon)
            if -90 <= la <= 90 and -180 <= lo <= 180:
                out.update(lat=round(la, 4), lon=round(lo, 4))
    except (TypeError, ValueError):
        pass
    return out


def _nominatim_pace():
    """Nominatim 要求每秒最多一次请求；一次刷新要查多个城市时自动错开。"""
    global _nominatim_next
    with _NOMINATIM_LOCK:
        now = time.monotonic()
        wait = _nominatim_next - now
        _nominatim_next = max(now, _nominatim_next) + 1.1
    if wait > 0:
        time.sleep(wait)


# WMO 天气代码 -> (中文描述, OpenWeather 图标前缀)；图标前缀复用现有的字形映射
_WMO = {
    0: ("晴", "01"), 1: ("晴间多云", "02"), 2: ("多云", "03"), 3: ("阴", "04"),
    45: ("雾", "50"), 48: ("雾凇", "50"),
    51: ("小毛毛雨", "09"), 53: ("毛毛雨", "09"), 55: ("大毛毛雨", "09"), 56: ("冻毛毛雨", "09"), 57: ("冻毛毛雨", "09"),
    61: ("小雨", "10"), 63: ("中雨", "10"), 65: ("大雨", "10"), 66: ("冻雨", "13"), 67: ("冻雨", "13"),
    71: ("小雪", "13"), 73: ("中雪", "13"), 75: ("大雪", "13"), 77: ("雪粒", "13"),
    80: ("阵雨", "09"), 81: ("阵雨", "09"), 82: ("强阵雨", "09"), 85: ("阵雪", "13"), 86: ("强阵雪", "13"),
    95: ("雷暴", "11"), 96: ("雷暴伴冰雹", "11"), 99: ("雷暴伴冰雹", "11"),
}


def _wmo(code, day=True) -> tuple[str, str]:
    desc, base = _WMO.get(int(code or 0), ("", "03"))
    return desc, base + ("d" if day else "n")


def parse_open_meteo(j: dict) -> dict:
    """Open-Meteo /v1/forecast（timeformat=unixtime）-> 与 parse() 相同结构。"""
    cur = j.get("current") or {}
    daily = j.get("daily") or {}
    hourly = j.get("hourly") or {}
    tz_off = int(j.get("utc_offset_seconds") or 0)
    desc, icon = _wmo(cur.get("weather_code"), bool(cur.get("is_day", 1)))
    d0 = lambda k: (daily.get(k) or [None])[0]  # noqa: E731
    out = {
        "ok": True, "error": "", "city": "",
        "temp": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
        "desc": desc, "icon": icon, "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"), "wind_deg": cur.get("wind_direction_10m"),
        "sunrise": d0("sunrise"), "sunset": d0("sunset"),
        "pressure": cur.get("pressure_msl"), "visibility": cur.get("visibility"),
        "clouds": cur.get("cloud_cover"),
        "temp_min": d0("temperature_2m_min"), "temp_max": d0("temperature_2m_max"),
        "tz_offset": tz_off, "hourly": [], "daily": [], "time": time.time(),
    }
    now = time.time()
    times = hourly.get("time") or []
    start = next((i for i, t in enumerate(times) if t >= now - 1800), 0)
    for i in range(start + 1, min(len(times), start + 1 + 24), 3):        # 从下一个整点起，3 小时一格
        hd, hicon = _wmo((hourly.get("weather_code") or [0])[i], bool((hourly.get("is_day") or [1] * len(times))[i]))
        pop = (hourly.get("precipitation_probability") or [0] * len(times))[i] or 0
        out["hourly"].append({"time": times[i], "temp": (hourly.get("temperature_2m") or [None])[i],
                              "icon": hicon, "pop": float(pop) / 100.0})
    tz = timezone(timedelta(seconds=tz_off))
    for i, t in enumerate((daily.get("time") or [])[:5]):
        _, dicon = _wmo((daily.get("weather_code") or [0])[i], True)
        out["daily"].append({"date": datetime.fromtimestamp(int(t), tz).strftime("%Y-%m-%d"),
                             "min": (daily.get("temperature_2m_min") or [None])[i],
                             "max": (daily.get("temperature_2m_max") or [None])[i], "icon": dicon})
    return out
NO_KEY_MSG = "未设置 OpenWeather API Key（右键 → 设置… → 天气）"

FLUENT_FONT = "Segoe Fluent Icons"
SYMBOL_FONT = "Segoe UI Symbol"
G_SUN = "\uE706"     # Brightness
G_MOON = "\uE708"    # QuietHours（月牙）
G_CLOUD = "\uE753"   # Cloud


def fluent_icon(owm_code: str) -> str:
    """OWM 图标代码（如 '10d'）-> Segoe Fluent Icons 字形（只有 日/月/云 三种）。"""
    c = str(owm_code or "")
    if c.startswith("01"):
        return G_MOON if c.endswith("n") else G_SUN
    return G_CLOUD


_SYMBOL = {
    "01d": ("\u2600", SYMBOL_FONT),        # ☀
    "01n": (G_MOON, FLUENT_FONT),
    "02d": ("\U0001F324", SYMBOL_FONT),    # 🌤 少云
    "02n": ("\u2601", SYMBOL_FONT),
    "03": ("\u2601", SYMBOL_FONT),         # ☁
    "04": ("\u2601", SYMBOL_FONT),
    "09": ("\U0001F327", SYMBOL_FONT),     # 🌧 阵雨
    "10d": ("\U0001F326", SYMBOL_FONT),    # 🌦 晴间雨
    "10n": ("\U0001F327", SYMBOL_FONT),
    "11": ("\U0001F329", SYMBOL_FONT),     # 🌩 雷
    "13": ("\u2744", SYMBOL_FONT),         # ❄
    "50": ("\U0001F32B", SYMBOL_FONT),     # 🌫 雾/霾
}


def weather_glyph(owm_code: str) -> tuple[str, str]:
    """OWM 图标代码 -> (字符, 字体族)。"""
    c = str(owm_code or "")
    return _SYMBOL.get(c) or _SYMBOL.get(c[:2]) or (G_CLOUD, FLUENT_FONT)


def _icon_rank(icon: str) -> int:
    """聚合一天的图标时，优先显示"更坏"的天气。"""
    order = {"11": 7, "13": 6, "09": 5, "10": 5, "50": 3, "04": 2, "03": 2, "02": 1, "01": 0}
    return order.get(str(icon)[:2], 0)


def parse(cur: dict, fc: dict | None) -> dict:
    """把 /weather 与 /forecast 的 JSON 解析成 updated 信号的载荷。"""
    w = (cur.get("weather") or [{}])[0]
    main = cur.get("main") or {}
    sys_ = cur.get("sys") or {}
    out = {
        "ok": True, "error": "",
        "city": cur.get("name") or "",
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "desc": w.get("description") or w.get("main") or "",
        "icon": w.get("icon") or "",
        "humidity": main.get("humidity"),
        "wind": (cur.get("wind") or {}).get("speed"),
        "sunrise": sys_.get("sunrise"),
        "sunset": sys_.get("sunset"),
        "pressure": main.get("pressure"),
        "visibility": cur.get("visibility"),
        "clouds": (cur.get("clouds") or {}).get("all"),
        "wind_deg": (cur.get("wind") or {}).get("deg"),
        "temp_min": main.get("temp_min"),
        "temp_max": main.get("temp_max"),
        "tz_offset": int(cur.get("timezone") or 0),
        "hourly": [], "daily": [],
        "time": time.time(),
    }
    if not fc:
        return out
    tz_off = int((fc.get("city") or {}).get("timezone", cur.get("timezone", 0)) or 0)
    tz = timezone(timedelta(seconds=tz_off))
    items = fc.get("list") or []
    for it in items[:8]:
        out["hourly"].append({
            "time": it.get("dt"),
            "temp": (it.get("main") or {}).get("temp"),
            "icon": ((it.get("weather") or [{}])[0]).get("icon", ""),
            "pop": float(it.get("pop") or 0.0),
        })
    days: dict[str, dict] = defaultdict(lambda: {"min": None, "max": None, "icons": []})
    order: list[str] = []
    for it in items:
        dt = it.get("dt")
        if dt is None:
            continue
        local = datetime.fromtimestamp(int(dt), tz)
        key = local.strftime("%Y-%m-%d")
        if key not in days:
            order.append(key)
        d = days[key]
        m = it.get("main") or {}
        lo, hi = m.get("temp_min", m.get("temp")), m.get("temp_max", m.get("temp"))
        if lo is not None:
            d["min"] = lo if d["min"] is None else min(d["min"], lo)
        if hi is not None:
            d["max"] = hi if d["max"] is None else max(d["max"], hi)
        icon = ((it.get("weather") or [{}])[0]).get("icon", "")
        if icon and 6 <= local.hour <= 21:  # 白天时段决定当天图标
            d["icons"].append(icon)
        elif icon and not d["icons"]:
            d["icons"].append(icon)
    for key in order[:5]:
        d = days[key]
        icons = d["icons"]
        icon = ""
        if icons:
            cnt = Counter(i[:2] for i in icons)
            # 最常见的；若有降水/雷暴出现 2 次以上，则显示更坏的那种
            common = cnt.most_common(1)[0][0]
            worst = max(cnt, key=lambda k: (_icon_rank(k), cnt[k]))
            pick = worst if _icon_rank(worst) >= 5 and cnt[worst] >= 2 else common
            icon = pick + "d"
        out["daily"].append({"date": key, "min": d["min"], "max": d["max"], "icon": icon})
    # 当前天气补充当地时区偏移（UI 显示日出日落可用）
    out["tz_offset"] = tz_off
    return out


class WeatherService(QtCore.QObject):
    """多城市天气。cities_updated(list) 每个城市一份 parse() 结果（含 name / index）；
    updated(dict) 只发第一个城市（灵动岛收起态显示它）。"""
    updated = QtCore.Signal(dict)
    cities_updated = QtCore.Signal(list)
    city_added = QtCore.Signal(bool, str)      # 成功?, 提示

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self._client = self._make_client()
        self._proxy_url: str | None = None          # 直连失败后找到的可用代理
        self._proxy_client: httpx.Client | None = None
        auto = cfg.get("weather.ip_city")           # 上次 IP 定位结果，启动后立刻可用
        self._auto_city: dict | None = dict(auto) if isinstance(auto, dict) and auto.get("name") else None
        self._ip_checked_at = 0.0
        self._ip_lock = threading.Lock()
        self._busy = False
        self._again = False
        self._running = False
        self._geo_cache: dict[str, dict] = {}
        self.last: dict = {}
        self.cities_data: list[dict] = []
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        # 整批失败后 60 秒再试一次，不必等下一个 15 分钟周期（瞬时超时很常见）
        self._retry = QtCore.QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self.refresh)
        # 换 Wi-Fi / 代理后默认城市要跟得上，不必等下一次 15 分钟的天气刷新
        self._ip_timer = QtCore.QTimer(self)
        self._ip_timer.setInterval(5 * 60_000)
        self._ip_timer.timeout.connect(self._check_ip)

    # ---------- 城市列表 ----------
    def _user_cities(self) -> list[dict]:
        """用户自己添加的城市（不含跟随 IP / 绑定的主城市）。"""
        cs = cfg.get("weather.cities")
        if not isinstance(cs, list):
            return []
        return [dict(c) for c in cs if isinstance(c, dict) and str(c.get("name") or "").strip()]

    def _pinned_city(self) -> dict | None:
        c = cfg.get("weather.pinned_city")
        return dict(c) if isinstance(c, dict) and str(c.get("name") or "").strip() else None

    def _fallback_city(self) -> dict:
        lat, lon = cfg.get("weather.lat"), cfg.get("weather.lon")
        c = {"name": cfg.get("weather.city") or "Shanghai"}
        if lat not in (None, "") and lon not in (None, ""):
            c.update(lat=lat, lon=lon)
        return c

    def main_city(self) -> dict:
        """主城市：绑定 > IP 定位 > 设置里的默认城市。"""
        return self._pinned_city() or dict(self._auto_city or self._fallback_city())

    def follows_network(self) -> bool:
        return self._pinned_city() is None

    def cities(self) -> list[dict]:
        """实际显示 / 请求的城市列表：主城市第一；绑定主城市时第二项是当前网络城市；
        后面是用户自己添加的城市（与前面重复的会隐藏）。"""
        main = self.main_city()
        auto = self._auto_city
        out = [main]
        if auto and not _same_city(auto, main.get("name"), main.get("lat"), main.get("lon")):
            out.append(dict(auto))
        for c in self._user_cities():
            if _same_city(c, main.get("name"), main.get("lat"), main.get("lon")):
                continue
            if auto and _same_city(c, auto.get("name"), auto.get("lat"), auto.get("lon")):
                continue
            out.append(c)
        return out

    def ip_city_index(self) -> int | None:
        """当前网络城市在显示列表里的位置；它本身就是主城市时返回 None。"""
        auto = self._auto_city
        if not auto:
            return None
        main = self.main_city()
        if _same_city(auto, main.get("name"), main.get("lat"), main.get("lon")):
            return None
        return 1                                # cities() 固定把网络城市放在主城市后面

    def bind_main(self, i: int):
        """右键「绑定主城市」：固定第 i 个城市，不再跟随 IP。"""
        cs = self.cities()
        if not 0 <= i < len(cs):
            return
        cfg.set("weather.pinned_city", dict(cs[i]))
        self._rebuild_view()
        self.refresh()

    def unbind_main(self):
        """右键「取消绑定」：恢复跟随 IP 的默认城市。"""
        if self._pinned_city() is None:
            return
        cfg.set("weather.pinned_city", None)
        self._ip_checked_at = 0.0                 # 取消绑定后立刻按当前 IP 重新定位
        self._rebuild_view()
        self.refresh()

    def _save_user(self, cs: list[dict]):
        cfg.set("weather.cities", cs)

    def remove_city(self, i: int):
        """删除用户添加的城市；主城市和当前网络城市不在这里删。"""
        cs = self.cities()
        if i <= 0 or i >= len(cs) or i == self.ip_city_index():
            return
        target = cs[i]
        users = self._user_cities()
        for j, c in enumerate(users):
            if _same_city(c, target.get("name"), target.get("lat"), target.get("lon")):
                users.pop(j)
                break
        else:
            return
        self._save_user(users)
        self.cities_data = [d for d in self.cities_data
                            if not _same_city(d, target.get("name"), target.get("lat"), target.get("lon"))]
        self._publish()

    def add_city(self, query: str):
        query = (query or "").strip()
        key = str(cfg.get("weather.api_key") or "").strip()
        if not query:
            self.city_added.emit(False, "请输入城市名")
            return

        def geo():
            return self._geocode(query, key)

        def done(g):
            if not g:
                self.city_added.emit(False, f"找不到城市：{query}（可试试英文名或「纬度,经度」）")
                return
            lat, lon = round(float(g["lat"]), 4), round(float(g["lon"]), 4)
            main = self.main_city()
            if any(_same_city(c, query, lat, lon) for c in self.cities()):
                if _same_city(main, query, lat, lon):
                    self.city_added.emit(False, f"{query} 已是当前默认城市")
                else:
                    self.city_added.emit(False, f"{query} 已在城市列表里")
                return
            users = self._user_cities()
            users.append({"name": query, "lat": lat, "lon": lon, "country": g.get("country", "")})
            self._save_user(users)
            self._rebuild_view()
            country = str(g.get("country") or "")
            self.city_added.emit(True, f"已添加 {query}（{country}）" if country else f"已添加 {query}")
            self.refresh()

        def fail(e):
            msg = self._friendly(e)
            if isinstance(e, (httpx.TimeoutException, httpx.TransportError)):
                msg += "，请检查网络或代理"
            self.city_added.emit(False, "查询失败：" + msg)

        run_bg(geo, on_done=done, on_error=fail)

    # ---------- 刷新 ----------
    def start(self):
        self._running = True
        try:
            mins = max(5.0, float(cfg.get("weather.interval_min") or 15))
        except (TypeError, ValueError):
            mins = 15.0
        self._timer.start(int(mins * 60_000))
        self._ip_timer.start()
        self.refresh()

    def stop(self):
        self._running = False
        self._timer.stop()
        self._retry.stop()
        self._ip_timer.stop()

    def refresh(self):
        self._retry.stop()
        if self._busy:
            self._again = True
            return
        key = str(cfg.get("weather.api_key") or "").strip()      # 空 = 用 Open-Meteo
        self._busy = True
        run_bg(self._fetch_all, key, on_done=self._done, on_error=self._fail)

    @staticmethod
    def _make_client(proxy: str | None = None) -> httpx.Client:
        opts = {"timeout": httpx.Timeout(20.0, connect=8.0), "follow_redirects": True,
                "headers": {"User-Agent": UA}}
        if not proxy:
            try:
                return httpx.Client(**opts)
            except ImportError:                 # 环境里 ALL_PROXY=socks5:// 但没装 socksio
                return httpx.Client(trust_env=False, **opts)
        try:
            return httpx.Client(proxy=proxy, **opts)
        except TypeError:                       # httpx < 0.26 用 proxies=
            return httpx.Client(proxies=proxy, **opts)

    @staticmethod
    def _request(client: httpx.Client, url: str, params: dict, tries: int = 2,
                 timeout: httpx.Timeout | None = None,
                 headers: dict | None = None) -> httpx.Response:
        """带重试的 GET：超时 / 连接失败 / 5xx 时再试一次（第一次常因 TLS 握手慢而超时）。"""
        last: Exception | None = None
        total = max(1, int(tries))
        for attempt in range(total):
            try:
                opts = {}
                if timeout is not None:
                    opts["timeout"] = timeout
                if headers:
                    opts["headers"] = headers
                r = client.get(url, params=params, **opts)
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as e:
                last = e
                if e.response.status_code < 500 or attempt == total - 1:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                if attempt == total - 1:
                    raise
            time.sleep(0.4 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _get(self, url: str, params: dict, tries: int = 2,
             timeout: httpx.Timeout | None = None,
             headers: dict | None = None) -> httpx.Response:
        """带重试的 GET；直连失败时自动探测 Windows 系统代理并重试。

        有些网络（公司 / 校园网、只开了 Clash 系统代理）下 Python 不读 Windows 的代理设置，
        直连天气接口会失败；这里失败后自动改走代理，成功后记住优先使用它。
        """
        last: Exception | None = None
        attempts = []
        tried: set[str] = set()
        if self._proxy_client is not None and self._proxy_url:
            attempts.append((self._proxy_client, self._proxy_url))
            tried.add(self._proxy_url)
        attempts.append((self._client, ""))
        for client, proxy in attempts:
            try:
                r = self._request(client, url, params, tries, timeout, headers)
            except httpx.HTTPStatusError as e:
                if proxy and e.response.status_code in _PROXY_BAD:
                    last = e
                    self._proxy_url, self._proxy_client = None, None
                    continue
                raise
            except Exception as e:  # noqa: BLE001  超时 / 连接失败 / 环境里的代理库不可用
                last = e
                if proxy:                       # 代理失效：下次重新探测
                    self._proxy_url, self._proxy_client = None, None
                continue
            if not proxy and self._proxy_url:   # 直连恢复：不再优先走代理
                self._proxy_url, self._proxy_client = None, None
            return r

        try:
            proxy = system_proxy(refresh=True)
        except Exception:  # noqa: BLE001
            proxy = None
        if proxy and proxy not in tried:
            try:
                client = self._make_client(proxy)
            except Exception:  # noqa: BLE001
                client = None
            if client is not None:
                try:
                    r = self._request(client, url, params, tries, timeout, headers)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code not in _PROXY_BAD:
                        raise
                    last = e
                except Exception as e:  # noqa: BLE001  代理不可用 / 协议不支持
                    last = e
                else:
                    self._proxy_url, self._proxy_client = proxy, client
                    return r
        if last is not None:
            raise last
        raise RuntimeError("天气请求失败")

    # ---------- 公网 IP 定位（默认城市） ----------
    def _check_ip(self):
        """定时检查公网 IP 城市；只有变化时才重新刷新天气。"""
        if not self._running or self._busy:
            return
        run_bg(self._detect_ip_city, on_done=self._ip_changed, on_error=lambda e: None)

    def _ip_changed(self, auto: dict | None):
        if not auto or not self._running:
            return
        if not _better_ip_city(self._auto_city, auto):
            return
        self._auto_city = auto
        try:
            cfg.set("weather.ip_city", auto)
        except Exception:  # noqa: BLE001  config.json 只读时也别影响天气
            pass
        self._rebuild_view()
        self.refresh()

    def _detect_ip_city(self) -> dict | None:
        """按公网 IP 猜当前城市；多个源依次试，全部失败返回 None（保留上次结果）。"""
        if not self._ip_lock.acquire(blocking=False):     # 定时器 / 天气刷新别同时查
            return None
        try:
            now = time.monotonic()
            if now - self._ip_checked_at < 120:           # 刚查过，短时间内不重复请求
                return None
            self._ip_checked_at = now
            deadline = now + 9.0
            for fetch in (self._ip_from_bili, self._ip_from_ipwho, self._ip_from_ipsb, self._ip_from_ipip):
                if time.monotonic() > deadline:
                    break
                try:
                    g = fetch()
                except Exception:  # noqa: BLE001  这个源不可用就换下一个
                    continue
                if g:
                    return g
            return None
        finally:
            self._ip_lock.release()

    def _ip_from_bili(self) -> dict | None:
        d = (self._get(IP_BILI, {}, tries=1, timeout=IP_TIMEOUT).json() or {}).get("data") or {}
        return _ip_city(d.get("city") or d.get("province") or d.get("country"),
                        d.get("latitude"), d.get("longitude"), d.get("country", ""))

    def _ip_from_ipwho(self) -> dict | None:
        j = self._get(IP_WHO, {}, tries=1, timeout=IP_TIMEOUT).json() or {}
        if not j.get("success"):
            return None
        return _ip_city(j.get("city") or j.get("region"), j.get("latitude"), j.get("longitude"),
                        j.get("country_code", ""))

    def _ip_from_ipsb(self) -> dict | None:
        j = self._get(IP_SB, {}, tries=1, timeout=IP_TIMEOUT).json() or {}
        return _ip_city(j.get("city") or j.get("region"), j.get("latitude"), j.get("longitude"),
                        j.get("country_code", ""))

    def _ip_from_ipip(self) -> dict | None:
        """myip.ipip.net 是文本：「当前 IP：x.x.x.x 来自于：中国 浙江 杭州 电信」。"""
        text = self._get(IP_IPIP, {}, tries=1, timeout=IP_TIMEOUT).text or ""
        m = re.search(r"来自于[：:]\s*(\S+)\s+(.+)", text)
        if not m:
            return None
        tokens = [m.group(1)] + [p for p in re.split(r"\s+", m.group(2).strip()) if p]
        isps = {"电信", "联通", "移动", "铁通", "教育网", "广电网", "长城宽带", "鹏博士"}
        while len(tokens) > 2 and (tokens[-1] in isps or "." in tokens[-1] or "/" in tokens[-1]):
            tokens.pop()
        return _ip_city(tokens[-1]) if len(tokens) >= 2 else None

    def _params(self, key: str, c: dict) -> dict:
        p = {"appid": key, "units": cfg.get("weather.units") or "metric", "lang": cfg.get("weather.lang") or "zh_cn"}
        if c.get("lat") not in (None, "") and c.get("lon") not in (None, ""):
            p["lat"], p["lon"] = c["lat"], c["lon"]
        else:
            p["q"] = c.get("q") or c.get("name")
        return p

    # ---------- 地理编码（多源） ----------
    def _geocode(self, name: str, key: str = "") -> dict | None:
        """城市名 -> {lat, lon, country, name}；依次尝试多个地理编码服务。

        这些服务不在同一台服务器上：个别网络里 open-meteo 的地理编码连不上（或 DNS 被污染），
        但天气接口和其它编码服务正常，添加城市就会一直失败。全部源都网络失败时抛出最后一个
        错误；只要有源正常应答（哪怕没查到）就返回 None。
        """
        q = str(name or "").strip()
        if not q:
            return None
        coords = _parse_coords(q)
        if coords:
            return {"lat": coords[0], "lon": coords[1], "country": "", "name": q}
        sources: list[tuple[bool, Callable[[], dict | None]]] = []
        if key:
            sources.append((True, lambda: self._geocode_owm(q, key)))
        sources += [
            (False, lambda: self._geocode_open_meteo(q)),
            (False, lambda: self._geocode_photon(q)),
            (False, lambda: self._geocode_nominatim(q)),
        ]
        last: Exception | None = None
        answered = False
        for fatal_auth, call in sources:
            try:
                g = call()
            except httpx.HTTPStatusError as e:
                if fatal_auth and e.response.status_code in (401, 403):
                    raise
                last = e
            except Exception as e:  # noqa: BLE001  这个源超时 / 数据异常，换下一个
                last = e
            else:
                answered = True
                if g:
                    return g
        if last is not None and not answered:
            raise last
        return None

    def _geocode_open_meteo(self, q: str) -> dict | None:
        r = self._get(OM_GEO, {"name": q, "count": 1, "language": "zh", "format": "json"},
                      tries=1, timeout=GEO_TIMEOUT)
        res = r.json().get("results") or []
        if not res:
            return None
        g = res[0]
        return {"lat": g["latitude"], "lon": g["longitude"],
                "country": str(g.get("country_code") or "").upper(),
                "name": g.get("name") or q}

    def _geocode_photon(self, q: str) -> dict | None:
        r = self._get(PHOTON, {"q": q, "limit": 5, "lang": "en"}, tries=1, timeout=GEO_TIMEOUT)
        best, rank = None, 0
        for f in r.json().get("features") or []:
            p = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            kind = _PHOTON_RANK.get(str(p.get("type") or ""), 0)
            if len(coords) >= 2 and kind > rank:   # 别把同名街道 / 车站 / 酒店当成城市
                best, rank = (coords, p), kind
        if best is None:
            return None
        coords, p = best
        return {"lat": float(coords[1]), "lon": float(coords[0]),
                "country": str(p.get("countrycode") or "").upper(),
                "name": p.get("name") or q}

    def _geocode_nominatim(self, q: str) -> dict | None:
        _nominatim_pace()
        r = self._get(NOMINATIM, {"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1,
                                  "accept-language": "zh-CN,zh;q=0.9,en;q=0.5"},
                      tries=1, timeout=GEO_TIMEOUT)
        res = r.json()
        if not isinstance(res, list) or not res:
            return None
        g = res[0]
        addr = g.get("address") or {}
        return {"lat": float(g["lat"]), "lon": float(g["lon"]),
                "country": str(addr.get("country_code") or "").upper(),
                "name": addr.get("city") or addr.get("town") or addr.get("village")
                        or addr.get("state") or g.get("name") or q}

    def _geocode_owm(self, q: str, key: str) -> dict | None:
        r = self._get(OWM_GEO, {"q": q, "limit": 1, "appid": key}, tries=1, timeout=GEO_TIMEOUT)
        js = r.json() or []
        if not js:
            return None
        g = js[0]
        return {"lat": g["lat"], "lon": g["lon"], "country": str(g.get("country") or "").upper(),
                "name": g.get("name") or q}

    def _geocode_cached(self, name: str) -> dict | None:
        """城市名 -> 坐标，成功结果在本次运行内缓存（默认城市没有写 lat/lon，避免每次刷新都查一次）。"""
        key = str(name or "").strip()
        if key in self._geo_cache:
            return self._geo_cache[key]
        g = self._geocode(key)
        if g:
            self._geo_cache[key] = g
        return g

    def _fetch_open_meteo(self, c: dict) -> dict:
        lat, lon = c.get("lat"), c.get("lon")
        if lat in (None, "") or lon in (None, ""):
            g = self._geocode_cached(c.get("q") or c.get("name") or "Shanghai")
            if not g:
                raise ValueError(f"找不到城市：{c.get('name')}")
            lat, lon = g["lat"], g["lon"]
        r = self._get(OM_API, {
            "latitude": lat, "longitude": lon, "timezone": "auto", "timeformat": "unixtime",
            "wind_speed_unit": "ms", "forecast_days": 6,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,weather_code,"
                       "cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,visibility",
            "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset",
        })
        d = parse_open_meteo(r.json())
        d["name"] = d["city"] = c.get("name") or ""
        return d

    def _fetch_one(self, key: str, c: dict) -> dict:
        if not key:
            return self._fetch_open_meteo(c)
        p = self._params(key, c)
        r = self._get(API + "/weather", p)
        cur = r.json()
        try:
            r2 = self._get(API + "/forecast", p)
            fc = r2.json()
        except Exception:  # noqa: BLE001
            fc = None
        d = parse(cur, fc)
        d["name"] = c.get("name") or d.get("city")
        return d

    def _fetch_all(self, key: str):
        auto = None
        try:
            auto = self._detect_ip_city()                # 绑定主城市时也要更新网络城市
        except Exception:  # noqa: BLE001
            auto = None
        if _better_ip_city(self._auto_city, auto):
            self._auto_city = auto
        out = []
        for i, c in enumerate(self.cities()):
            try:
                d = self._fetch_one(key, c)
            except Exception as e:  # noqa: BLE001
                d = {"ok": False, "error": self._friendly(e), "name": c.get("name", "?")}
            d["index"] = i
            if c.get("lat") not in (None, ""):
                d["lat"], d["lon"] = c.get("lat"), c.get("lon")
            out.append(d)
        return out, auto

    def _align_data(self, data: list[dict]) -> list[dict]:
        """按当前城市列表重排缓存数据；刚出现的主城市先放占位，等这次刷新补上。"""
        by_name = {str(d.get("name") or ""): d for d in data}
        out = []
        for i, c in enumerate(self.cities()):
            name = str(c.get("name") or "")
            d = by_name.get(name)
            if d is None:
                d = {"ok": False, "error": "正在获取天气…", "name": name or "?"}
            d = dict(d)
            d["index"] = i
            out.append(d)
        return out

    def _rebuild_view(self):
        """绑定 / 取消绑定 / 加城市后立刻更新城市胶囊，不用等下一次网络请求。"""
        self.cities_data = self._align_data(self.cities_data)
        self._publish()

    def _done(self, result):
        data, auto = result
        self._busy = False
        if auto:
            if _better_ip_city(self._auto_city, auto):
                self._auto_city = auto
            cached = cfg.get("weather.ip_city")
            if _better_ip_city(cached if isinstance(cached, dict) else None, auto):
                try:
                    cfg.set("weather.ip_city", auto)      # 缓存给下次启动 / 断网时用
                except Exception:  # noqa: BLE001
                    pass
        old = {d.get("name"): d for d in self.cities_data if d.get("ok")}
        merged = []
        for d in data:
            if not d.get("ok") and d.get("name") in old:      # 失败的城市保留上次数据
                keep = dict(old[d["name"]])
                keep.update(error=d.get("error"), index=d.get("index"))
                merged.append(keep)
            else:
                merged.append(d)
        self.cities_data = self._align_data(merged)
        self._publish()
        if self._again:
            self._again = False
            self.refresh()
        else:
            self._retry_after_failure(data)

    def _retry_after_failure(self, data: list[dict]):
        """整批都是可重试的网络错误时，60 秒后再试（否则等 15 分钟太久了）。"""
        if not self._running or not data or any(d.get("ok") for d in data):
            return
        err = " ".join(str(d.get("error") or "") for d in data)
        if any(k in err for k in ("超时", "无法连接", "服务错误", "服务繁忙")):
            self._retry.start(60_000)

    def _publish(self):
        for i, d in enumerate(self.cities_data):
            d["index"] = i
        self.cities_updated.emit(list(self.cities_data))
        if self.cities_data:
            self._emit_first(self.cities_data[0])

    def _fail(self, e: Exception):
        self._busy = False
        d = dict(self.last) if self.last.get("ok") else {}
        d.update({"ok": False, "error": self._friendly(e)})
        self._emit_first(d)
        if self._running:
            self._retry.start(60_000)

    @staticmethod
    def _friendly(e: Exception) -> str:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            return {401: "OpenWeather API Key 无效（新 Key 需等待最多 2 小时生效）",
                    404: "找不到该城市", 429: "请求过于频繁"}.get(code, f"天气服务错误 HTTP {code}")
        if isinstance(e, httpx.TimeoutException):
            return "天气请求超时"
        if isinstance(e, httpx.TransportError):
            return "无法连接天气服务"
        if isinstance(e, ValueError) and str(e):
            return str(e)
        return f"天气解析失败：{str(e)[:60]}"

    def _emit_first(self, d: dict):
        if d.get("ok"):
            self.last = d
        self.updated.emit(d)
