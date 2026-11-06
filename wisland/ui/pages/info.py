# -*- coding: utf-8 -*-
"""股票（本地盯盘后端）与天气（OpenWeather）。"""
from __future__ import annotations

import datetime as dt

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.config import cfg
from ..anim import clamp
from ..theme import G, W, theme, ui_font
from ..widgets import (Card, Hoverable, IconButton, Label, LineEdit, PillButton, ScrollArea, Segmented,
                       Sparkline, Themed, hbox, painter, vbox)
from .base import Page

Qt = QtCore.Qt
MARKETS = [("a", "A股"), ("hk", "港股"), ("us", "美股")]


def _pct_color(pct):
    if pct is None or abs(pct) < 1e-9:
        return theme.text2()
    return theme.up() if pct > 0 else theme.down()


class StockList(QtWidgets.QWidget, Themed):
    picked = QtCore.Signal(dict)
    ROW = 36

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self.sel = None
        self.hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hook_theme()

    def set_rows(self, rows):
        self.rows = rows
        self.setFixedHeight(max(40, len(rows) * self.ROW))
        self.update()

    def mouseMoveEvent(self, e):
        i = int(e.position().y() // self.ROW)
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e):
        self.hover = -1
        self.update()

    def mouseReleaseEvent(self, e):
        i = int(e.position().y() // self.ROW)
        if 0 <= i < len(self.rows):
            self.sel = self.rows[i].get("code")
            self.picked.emit(self.rows[i])
            self.update()

    def paintEvent(self, e):
        p = painter(self)
        if not self.rows:
            p.setFont(ui_font(12))
            p.setPen(theme.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "自选为空")
            return
        w = self.width()
        for i, s in enumerate(self.rows):
            y = i * self.ROW
            r = QtCore.QRectF(0, y + 1, w, self.ROW - 2)
            if i == self.hover or s.get("code") == self.sel:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.hover() if i == self.hover else theme.card(0.8))
                p.drawRoundedRect(r, 12, 12)
            p.setFont(ui_font(13, W.Medium))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(12, y, 150, self.ROW), Qt.AlignmentFlag.AlignVCenter, s.get("name") or s.get("code"))
            p.setFont(ui_font(10.5, num=True))
            p.setPen(theme.text3())
            p.drawText(QtCore.QRectF(150, y, 120, self.ROW), Qt.AlignmentFlag.AlignVCenter, s.get("code", ""))
            price, pct = s.get("price"), s.get("pct")
            p.setFont(ui_font(13.5, W.DemiBold, num=True))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(w - 210, y, 110, self.ROW), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       "--" if price is None else (f"{price:.3f}" if price < 10 else f"{price:.2f}"))
            # 涨跌幅胶囊
            pr = QtCore.QRectF(w - 88, y + 7, 78, self.ROW - 14)
            col = _pct_color(pct)
            bg = QtGui.QColor(col)
            bg.setAlpha(46)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(pr, pr.height() / 2, pr.height() / 2)
            p.setPen(col)
            p.setFont(ui_font(12.5, W.DemiBold, num=True))
            p.drawText(pr, Qt.AlignmentFlag.AlignCenter, "--" if pct is None else f"{pct:+.2f}%")


class StocksPage(Page):
    key, glyph, title, height = "stock", G.STOCK, "股票", 330

    def __init__(self, ctx):
        super().__init__(ctx)
        self.data: dict = {}
        self.market = "a"
        self.seg = Segmented([n for _, n in MARKETS])
        self.seg.setFixedWidth(180)
        self.seg.changed.connect(self._market)
        self.status = Label("", 11.5, level=3, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # 分时卡片
        self.detail = Card(radius=16)
        self.d_name = Label("", 13, W.DemiBold)
        self.d_price = Label("", 13, W.DemiBold, num=True, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.spark = Sparkline()
        self.spark.setFixedHeight(56)
        self.detail.setLayout(vbox(hbox(self.d_name, self.d_price), self.spark, spacing=2, margins=(12, 8, 12, 8)))
        self.detail.hide()
        self.list = StockList()
        self.list.picked.connect(self._pick)
        sc = ScrollArea()
        lay = QtWidgets.QVBoxLayout(sc.inner)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.addWidget(self.list)
        lay.addStretch(1)
        self.err = Label("", 12, level=2, align=Qt.AlignmentFlag.AlignCenter)
        self.err.hide()
        self.setLayout(vbox(hbox(self.seg, None, self.status), self.detail, self.err, (sc, 1), spacing=8))

    def _market(self, i):
        self.market = MARKETS[i][0]
        self.detail.hide()
        self.list.sel = None
        self._render()

    def _pick(self, s):
        self.d_name.setText(s.get("name") or s.get("code"))
        pct = s.get("pct")
        self.d_price.setText(f"{s.get('price') or '--'}   {pct:+.2f}%" if pct is not None else "--")
        self.d_price.set_color(_pct_color(pct))
        self.detail.show()
        code = s.get("code")
        prev = s.get("prev_close")
        self.ctx.stocks.minute(code, lambda pts, prev=prev: self._minute(code, pts, prev))

    def _minute(self, code, pts, prev):
        if self.list.sel != code:
            return
        col = _pct_color((pts[-1] - prev) if pts and prev else 0)
        self.spark.color = col
        self.spark.set_values(pts, baseline=prev)

    def set_data(self, d: dict):
        self.data = d
        if self.active:
            self._render()

    def _render(self):
        d = self.data or {}
        rows = (d.get("markets") or {}).get(self.market) or []
        st = (d.get("status") or {}).get(self.market, "")
        t = d.get("time")
        ts = dt.datetime.fromtimestamp(t).strftime("%H:%M:%S") if isinstance(t, (int, float)) else (t or "")
        self.status.setText(f"{st}  {ts}".strip())
        if not d.get("ok") and d.get("error"):
            self.err.setText(d["error"])
            self.err.show()
        else:
            self.err.hide()
        self.list.set_rows(rows)

    def on_show(self):
        super().on_show()
        self._render()


# ======================================================================== 天气
class GlyphLabel(QtWidgets.QWidget, Themed):
    def __init__(self, px: int, parent=None):
        super().__init__(parent)
        self.px = px
        self.glyph = ("", "Segoe Fluent Icons")
        self.setFixedSize(px + 10, px + 10)
        self._hook_theme()

    def set_glyph(self, g):
        self.glyph = g
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        f = QtGui.QFont()
        f.setFamilies([self.glyph[1], "Segoe Fluent Icons", "Segoe UI Symbol"])
        f.setPixelSize(self.px)
        p.setFont(f)
        p.setPen(theme.text(0.9))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph[0])


class Hourly(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.tz = None
        self.setFixedHeight(78)
        self._hook_theme()

    def set_items(self, items):
        self.items = items[:8]
        self.update()

    def paintEvent(self, e):
        from ...services.weather import weather_glyph
        p = painter(self)
        n = max(1, len(self.items))
        cw = self.width() / n
        for i, it in enumerate(self.items):
            x = i * cw
            p.setFont(ui_font(11, num=True))
            p.setPen(theme.text3())
            p.drawText(QtCore.QRectF(x, 0, cw, 16), Qt.AlignmentFlag.AlignCenter,
                       (dt.datetime.fromtimestamp(it.get("time", 0) + self.tz, dt.timezone.utc) if self.tz is not None
                        else dt.datetime.fromtimestamp(it.get("time", 0))).strftime("%H时"))
            g, fam = weather_glyph(it.get("icon"))
            f = QtGui.QFont()
            f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            f.setPixelSize(19)
            p.setFont(f)
            p.setPen(theme.text(0.9))
            p.drawText(QtCore.QRectF(x, 18, cw, 28), Qt.AlignmentFlag.AlignCenter, g)
            p.setFont(ui_font(13, W.DemiBold, num=True))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(x, 46, cw, 16), Qt.AlignmentFlag.AlignCenter, f"{round(it.get('temp', 0))}°")
            pop = it.get("pop") or 0
            if pop >= 0.2:
                p.setFont(ui_font(10, num=True))
                p.setPen(QtGui.QColor("#4f9cff"))
                p.drawText(QtCore.QRectF(x, 62, cw, 14), Qt.AlignmentFlag.AlignCenter, f"{round(pop * 100)}%")


class Daily(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self._hook_theme()

    def set_items(self, items):
        self.items = items[:5]
        self.update()

    def paintEvent(self, e):
        from ...services.weather import weather_glyph
        if not self.items:
            return
        p = painter(self)
        lo = min(i["min"] for i in self.items)
        hi = max(i["max"] for i in self.items)
        rh = self.height() / len(self.items)
        wk = "一二三四五六日"
        for k, it in enumerate(self.items):
            y = k * rh
            d = it.get("date")
            try:
                day = dt.date.fromisoformat(str(d))
                name = "今天" if day == dt.date.today() else "周" + wk[day.weekday()]
            except Exception:  # noqa: BLE001
                name = str(d)
            p.setFont(ui_font(12.5, W.Medium))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(0, y, 44, rh), Qt.AlignmentFlag.AlignVCenter, name)
            g, fam = weather_glyph(it.get("icon"))
            f = QtGui.QFont()
            f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            f.setPixelSize(16)
            p.setFont(f)
            p.drawText(QtCore.QRectF(44, y, 26, rh), Qt.AlignmentFlag.AlignCenter, g)
            p.setFont(ui_font(12, num=True))
            p.setPen(theme.text2())
            p.drawText(QtCore.QRectF(74, y, 30, rh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{round(it['min'])}°")
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(self.width() - 32, y, 32, rh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{round(it['max'])}°")
            bx, bw = 112, self.width() - 112 - 40
            span = max(1e-6, hi - lo)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.track())
            p.drawRoundedRect(QtCore.QRectF(bx, y + rh / 2 - 2.5, bw, 5), 2.5, 2.5)
            x0 = bx + bw * (it["min"] - lo) / span
            x1 = bx + bw * (it["max"] - lo) / span
            g2 = QtGui.QLinearGradient(bx, 0, bx + bw, 0)
            g2.setColorAt(0, QtGui.QColor("#5ac8fa"))
            g2.setColorAt(1, QtGui.QColor("#ff9f0a"))
            p.setBrush(g2)
            p.drawRoundedRect(QtCore.QRectF(x0, y + rh / 2 - 2.5, max(5, x1 - x0), 5), 2.5, 2.5)


class CityChip(Hoverable):
    """城市胶囊：名称 + 当前温度；选中时半透明白底。"""
    def __init__(self, idx: int, name: str, parent=None):
        super().__init__(parent)
        self.idx, self.name = idx, name
        self.temp = ""
        self.glyph = ("", "Segoe Fluent Icons")
        self.selected = False
        self.default = idx == 0
        self.setFixedHeight(30)
        self._fit()

    def _fit(self):
        fm = QtGui.QFontMetrics(ui_font(12.5, W.DemiBold))
        self.setFixedWidth(fm.horizontalAdvance(self.name) + fm.horizontalAdvance(self.temp or "--°") + 50)

    def set_info(self, temp: str, glyph):
        self.temp, self.glyph = temp, glyph
        self._fit()
        self.update()

    def paintEvent(self, e):
        from ..widgets import painter as _p
        p = _p(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.active_pill())
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        elif self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        f = QtGui.QFont()
        f.setFamilies([self.glyph[1], "Segoe Fluent Icons", "Segoe UI Symbol"])
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(theme.text(0.9))
        p.drawText(QtCore.QRectF(r.x() + 8, r.y(), 18, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph[0])
        p.setFont(ui_font(12.5, W.DemiBold))
        p.setPen(theme.pill_text() if self.selected else theme.text(0.75))
        fm = QtGui.QFontMetrics(p.font())
        x = r.x() + 30
        p.drawText(QtCore.QRectF(x, r.y(), 200, r.height()), Qt.AlignmentFlag.AlignVCenter, self.name)
        x += fm.horizontalAdvance(self.name) + 6
        p.setFont(ui_font(12, W.Medium, num=True))
        p.setPen(theme.text2())
        p.drawText(QtCore.QRectF(x, r.y(), 60, r.height()), Qt.AlignmentFlag.AlignVCenter, self.temp or "--°")
        if self.default:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.on_color())
            p.drawEllipse(QtCore.QPointF(r.right() - 6, r.y() + 6), 2.5, 2.5)


def _fmt_clock(ts, tz_off):
    if not ts:
        return "--"
    return dt.datetime.fromtimestamp(int(ts) + int(tz_off or 0), dt.timezone.utc).strftime("%H:%M")


def _wind_dir(deg):
    if deg is None:
        return ""
    return ["北", "东北", "东", "东南", "南", "西南", "西", "西北"][int(((deg + 22.5) % 360) // 45)] + "风"


class WeatherPage(Page):
    key, glyph, title, height = "weather", G.CLOUD, "天气", 318

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sel = 0
        self.data: list[dict] = []
        self.chips: list[CityChip] = []
        # 顶部：城市胶囊 + 添加
        self.chip_row = QtWidgets.QHBoxLayout()
        self.chip_row.setSpacing(4)
        self.chip_row.setContentsMargins(0, 0, 0, 0)
        self.add_btn = IconButton(G.ADD, 28, 12, "添加城市")
        self.add_btn.clicked.connect(self._start_add)
        self.add_edit = LineEdit("城市名")
        self.add_edit.setFixedWidth(220)
        self.add_edit.setFixedHeight(30)
        self.add_edit.hide()
        self.add_edit.focus_wanted.connect(self.want_focus)
        self.add_edit.returnPressed.connect(self._do_add)
        self.add_edit.installEventFilter(self)
        refresh = IconButton(G.REFRESH, 28, 12, "刷新")
        refresh.clicked.connect(lambda: ctx.weather.refresh())
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(0, 0, 0, 0)
        head.addLayout(self.chip_row)
        head.addWidget(self.add_btn)
        head.addWidget(self.add_edit)
        head.addStretch(1)
        self.msg = Label("", 11.5, level=3)
        head.addWidget(self.msg)
        head.addWidget(refresh)

        # 详情
        self.icon = GlyphLabel(44)
        self.temp = Label("--°", 44, W.Light, num=True)
        self.temp.setFixedHeight(56)
        self.desc = Label("", 14, W.Medium)
        self.city = Label("", 12, level=2)
        self.hourly = Hourly()
        self.daily = Daily()
        self.daily.setFixedWidth(250)
        self.facts = Facts()
        left = vbox(hbox(self.icon, self.temp, None, spacing=6), self.desc, self.city, None, spacing=2)
        self.body = QtWidgets.QWidget()
        self.body.setLayout(vbox(hbox((left, 1), self.daily, spacing=16), self.facts, self.hourly, spacing=6))
        self.err = Label("", 13, level=2, align=Qt.AlignmentFlag.AlignCenter, wrap=True)
        self.err.hide()
        self.setLayout(vbox(head, (self.body, 1), self.err, spacing=8))
        ctx.weather.cities_updated.connect(self.set_cities)
        ctx.weather.city_added.connect(self._added)

    # ---------- 城市 ----------
    def set_data(self, d: dict):
        """兼容：第一个城市的数据 / 错误。"""
        if not d.get("ok") and not self.data:
            self.err.setText(d.get("error") or "暂无天气数据")
            self.err.show()
            self.body.hide()

    def set_cities(self, data: list):
        from ...services.weather import weather_glyph
        self.data = data
        names = [d.get("name", "?") for d in data]
        if [c.name for c in self.chips] != names:
            for c in self.chips:
                c.deleteLater()
            self.chips = []
            for i, n in enumerate(names):
                c = CityChip(i, n)
                c.clicked.connect(lambda i=i: self.select(i))
                c.right_clicked.connect(lambda i=i: self._menu(i))
                self.chip_row.addWidget(c)
                self.chips.append(c)
        for c, d in zip(self.chips, data):
            c.default = c.idx == 0
            c.set_info(f"{round(d['temp'])}°" if d.get("temp") is not None else "--°", weather_glyph(d.get("icon")))
        self.sel = min(self.sel, max(0, len(data) - 1))
        self.select(self.sel)

    def select(self, i: int):
        self.sel = i
        for c in self.chips:
            c.selected = c.idx == i
            c.update()
        if not self.data:
            return
        self._show(self.data[i])

    def _show(self, d: dict):
        from ...services.weather import weather_glyph
        if d.get("temp") is None:
            self.err.setText(d.get("error") or "暂无数据")
            self.err.show()
            self.body.hide()
            return
        self.err.hide()
        self.body.show()
        self.msg.setText(d.get("error") or "")
        self.icon.set_glyph(weather_glyph(d.get("icon")))
        self.temp.setText(f"{round(d.get('temp', 0))}°")
        self.desc.setText(d.get("desc", ""))
        lo, hi = d.get("temp_min"), d.get("temp_max")
        rng = f"   最低 {round(lo)}° 最高 {round(hi)}°" if lo is not None and hi is not None else ""
        self.city.setText(f"{d.get('name', '')}{rng}")
        tz = d.get("tz_offset", 0)
        vis = d.get("visibility")
        self.facts.set_items([
            ("体感", f"{round(d.get('feels_like', 0))}°"),
            ("湿度", f"{d.get('humidity', '--')}%"),
            ("风", f"{_wind_dir(d.get('wind_deg'))} {d.get('wind', '--')}m/s"),
            ("气压", f"{d.get('pressure', '--')}hPa"),
            ("能见度", f"{vis / 1000:.0f}km" if vis else "--"),
            ("云量", f"{d.get('clouds', '--')}%"),
            ("日出", _fmt_clock(d.get("sunrise"), tz)),
            ("日落", _fmt_clock(d.get("sunset"), tz)),
        ])
        self.hourly.tz = tz
        self.hourly.set_items(d.get("hourly") or [])
        self.daily.set_items(d.get("daily") or [])

    def _menu(self, i: int):
        from .system import styled_menu
        m = styled_menu(self)
        if i > 0:
            m.addAction("设为默认").triggered.connect(lambda: self._default(i))
        a = m.addAction("删除该城市")
        a.setEnabled(len(self.data) > 1)
        a.triggered.connect(lambda: self._remove(i))
        if self.island:
            self.island.locks.add("menu")
            m.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        m.popup(QtGui.QCursor.pos())

    def _default(self, i):
        self.sel = 0
        self.ctx.weather.make_default(i)

    def _remove(self, i):
        if self.sel >= i and self.sel > 0:
            self.sel -= 1
        self.ctx.weather.remove_city(i)

    def _start_add(self):
        self.add_btn.hide()
        self.add_edit.clear()
        self.add_edit.show()
        self.want_focus()
        self.add_edit.setFocus()

    def _end_add(self):
        self.add_edit.hide()
        self.add_btn.show()

    def _do_add(self):
        q = self.add_edit.text().strip()
        if q:
            self.msg.setText(f"正在查找 {q}…")
            self.ctx.weather.add_city(q)
        self._end_add()

    def _added(self, ok: bool, text: str):
        self.msg.setText(text)
        if ok:
            self.sel = len(self.data)       # 新城市在末尾，刷新后选中它
        QtCore.QTimer.singleShot(4000, lambda: self.msg.setText(""))

    def eventFilter(self, obj, ev):
        if obj is self.add_edit and ev.type() == QtCore.QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
            self._end_add()
            return True
        if obj is self.add_edit and ev.type() == QtCore.QEvent.Type.FocusOut and not self.add_edit.text().strip():
            self._end_add()
        return False

    def on_show(self):
        super().on_show()
        if self.data:
            self.select(self.sel)


class Facts(QtWidgets.QWidget, Themed):
    """详情网格：两行四列的小标签。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[tuple[str, str]] = []
        self.setFixedHeight(44)
        self._hook_theme()

    def set_items(self, items):
        self.items = items
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        cols = 4
        cw = self.width() / cols
        rh = self.height() / 2
        for i, (k, v) in enumerate(self.items[:8]):
            x, y = (i % cols) * cw, (i // cols) * rh
            p.setFont(ui_font(11))
            p.setPen(theme.text3())
            p.drawText(QtCore.QRectF(x, y, 44, rh), Qt.AlignmentFlag.AlignVCenter, k)
            p.setFont(ui_font(12, W.Medium, num=True))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(x + 44, y, cw - 46, rh), Qt.AlignmentFlag.AlignVCenter, v)


_ = (cfg, clamp)
