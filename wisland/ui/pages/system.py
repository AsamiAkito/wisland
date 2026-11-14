# -*- coding: utf-8 -*-
"""控制中心（音量 / 亮度 / WiFi / 蓝牙 / Clash Verge）、窗口切换、性能监控。"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.bg import run_bg
from ..anim import clamp
from ..theme import G, W, icon_font, theme, ui_font
from ..widgets import (Card, Hoverable, IconButton, Label, PillButton, ScrollArea, Segmented, Slider, Sparkline,
                       Themed, Tile, UsageBar, fmt_bytes, hbox, painter, vbox)
from .base import Page

Qt = QtCore.Qt
MODES = [("rule", "规则"), ("global", "全局"), ("direct", "直连")]


def short_node(name: str) -> str:
    """"JMS-1411881@c51s2.xxx:443" -> "c51s2"（@ 后面 5 个字符）。"""
    if "@" in name:
        return name.split("@", 1)[1][:5]
    return name


def _close_on_outside_click(m: QtWidgets.QMenu):
    """灵动岛不抢焦点，Qt 的"点外面关闭菜单"不会触发；这里轮询鼠标键，点到菜单外就关闭。"""
    import ctypes
    u32 = ctypes.WinDLL("user32")
    st = {"armed": False}
    t = QtCore.QTimer(m)
    t.setInterval(25)

    def down() -> bool:
        return any(u32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04))

    def tick():
        if not m.isVisible():
            t.stop()
            return
        if not st["armed"]:
            st["armed"] = not down()          # 先等打开菜单的那次按键松开
            return
        if down():
            pos = QtGui.QCursor.pos()
            menus = [w for w in QtWidgets.QApplication.topLevelWidgets()
                     if isinstance(w, QtWidgets.QMenu) and w.isVisible()]
            if not any(w.geometry().contains(pos) for w in menus):
                m.close()
                t.stop()
    t.timeout.connect(tick)
    m.aboutToShow.connect(lambda: (st.update(armed=False), t.start()))
    m.aboutToHide.connect(t.stop)


def styled_menu(parent=None) -> QtWidgets.QMenu:
    m = QtWidgets.QMenu(parent)
    _close_on_outside_click(m)
    m.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    m.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    m.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
    bg, fg, sel = theme.menu_qss()
    m.setStyleSheet(
        f"QMenu{{background:{bg};border-radius:14px;padding:6px;color:{fg};"
        f"font-family:{theme.font_css()};font-size:13px}}"
        f"QMenu::item{{padding:6px 22px 6px 14px;border-radius:9px}}"
        f"QMenu::item:selected{{background:{sel}}}"
        f"QMenu::item:disabled{{color:gray}}"
        "QMenu::separator{height:6px;background:transparent}"
        "QMenu::indicator{width:0}")
    return m


class ControlsPage(Page):
    key, glyph, title, height = "ctl", G.SLIDERS, "控制", 202

    def __init__(self, ctx):
        super().__init__(ctx)
        self.vol = Slider(G.VOLUME)
        self.bri = Slider(G.SUN)
        self.vol.moved.connect(self._set_vol)
        self.vol.icon_clicked.connect(lambda: ctx.audio.toggle_mute())
        self.bri.moved.connect(lambda v: ctx.brightness.set(None, round(v * 100)))
        self.wifi = Tile(G.WIFI, "WLAN")
        self.bt = Tile(G.BT, "蓝牙")
        self.proxy = Tile(G.GLOBE, "系统代理")
        self.tun = Tile(G.BOLT, "TUN 模式")
        self._radio = {}
        self._clash = {}
        self.wifi.clicked.connect(lambda: self._toggle("wifi"))
        self.bt.clicked.connect(lambda: self._toggle("bt"))
        self.proxy.clicked.connect(lambda: ctx.clash.set_sysproxy(not self._clash.get("sysproxy")))
        self.tun.clicked.connect(lambda: ctx.clash.set_tun(not self._clash.get("tun")))
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        for i, t in enumerate((self.wifi, self.bt, self.proxy, self.tun)):
            grid.addWidget(t, i // 2, i % 2)

        # Clash 卡片
        self.card = Card(radius=18)
        self.mode = Segmented([n for _, n in MODES])
        self.mode.setFixedWidth(168)
        self.mode.changed.connect(lambda i: ctx.clash.set_mode(MODES[i][0]))
        self.node = PillButton("节点", G.GLOBE, height=28)
        self.node.clicked.connect(self._node_menu)
        self.node.setMaximumWidth(190)
        self.speed = Label("", 11.5, level=2, num=True)
        self.clash_state = Label("Clash Verge", 13, W.DemiBold)
        openv = IconButton(G.OPEN, 28, 12, "打开 Clash Verge")
        openv.clicked.connect(lambda: ctx.clash.open_app())
        self.card.setLayout(vbox(hbox(self.clash_state, None, self.speed, openv, spacing=6),
                                 hbox(self.mode, None, self.node, spacing=8), spacing=6, margins=(14, 10, 10, 10)))
        # 订阅流量：仅 Clash 运行中且读得到配额时显示
        self.sub_text = Label("", 11, level=2, num=True)
        self.sub_pct = Label("", 11, level=2, num=True,
                             align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sub_bar = UsageBar()
        self.sub_usage = QtWidgets.QWidget()
        self.sub_usage.setLayout(vbox(hbox(self.sub_text, None, self.sub_pct, spacing=6),
                                      self.sub_bar, spacing=4, margins=(14, 0, 10, 0)))
        self.sub_usage.hide()
        left = vbox(self.vol, self.bri, self.card, self.sub_usage, None, spacing=8)
        for w in (self.card, self.proxy, self.tun):
            w.hide()
        gw = QtWidgets.QWidget()
        gw.setLayout(grid)
        gw.setFixedWidth(270)
        self.setLayout(hbox((left, 1), vbox(None, gw, None), spacing=10))

    def _toggle(self, kind: str):
        """可用就切换开关；检测不到（台式机没蓝牙 / 驱动不支持）时打开对应的系统设置页。"""
        import os
        if not self._radio.get(f"{kind}_available", False):
            os.startfile("ms-settings:" + ("bluetooth" if kind == "bt" else "network-wifi"))  # noqa: S606
            if self.island:
                self.island.collapse()
            return
        if kind == "bt":
            self.ctx.radio.set_bluetooth(not self._radio.get("bt_on"))
        else:
            self.ctx.radio.set_wifi(not self._radio.get("wifi_on"))

    def _set_vol(self, v: float):
        a = self.ctx.audio
        a.set_volume(v)
        if v > 0.001 and a.muted():
            a.set_muted(False)          # 调大音量时顺便取消静音（否则看起来"没反应"）

    # --- 数据 ---
    def set_volume(self, v: float, muted: bool):
        self.vol.glyph = G.MUTE if muted else (G.VOLUME_LOW if v < 0.34 else G.VOLUME)
        self.vol.label = "静音" if muted else ""
        self.vol.set_value(v)

    def set_brightness(self, mons: list):
        if not mons:
            self.bri.label = "不支持"
            self.bri.update()
            return
        vals = [m.get("brightness") for m in mons if m.get("brightness") is not None]
        if vals:
            self.bri.label = ""
            self.bri.set_value(sum(vals) / len(vals) / 100)

    def set_radio(self, d: dict):
        self._radio = d
        self.wifi.set_state(bool(d.get("wifi_on")),
                            "已开启" if d.get("wifi_on") else "已关闭",
                            d.get("wifi_available", True))
        bt_ok = d.get("bt_available", True)
        self.bt.set_state(bool(d.get("bt_on")),
                          ("已开启" if d.get("bt_on") else "已关闭") if bt_ok else "未检测到 · 点击打开设置", bt_ok)

    def set_clash(self, d: dict):
        self._clash = d
        ok = d.get("ok")
        # 只有检测到 Clash Verge 在运行时才显示代理相关的卡片和磁贴（没装的用户看不到无用的面板）
        for w in (self.card, self.proxy, self.tun):
            w.setVisible(bool(ok))
        self.proxy.set_state(bool(d.get("sysproxy")), "127.0.0.1" if d.get("sysproxy") else "已关闭")
        self.tun.set_state(bool(d.get("tun")), "已开启" if d.get("tun") else "已关闭", bool(ok))
        if ok:
            self.clash_state.setText("Clash Verge")
            self.speed.setText(f"↑ {fmt_bytes(d.get('up', 0), True)}   ↓ {fmt_bytes(d.get('down', 0), True)}")
            keys = [k for k, _ in MODES]
            if d.get("mode") in keys:
                self.mode.set_index(keys.index(d["mode"]))
            delay = d.get("delay")
            node = short_node(d.get("node") or "—")
            self.node.set_text(f"{node}" + (f"  {delay}ms" if delay else ""))
        else:
            self.clash_state.setText("Clash 未运行")
            self.speed.setText(d.get("error") or "")
            self.node.set_text("—")
        sub = d.get("sub") or {}
        used, total = int(sub.get("used") or 0), int(sub.get("total") or 0)
        if ok and total > 0:
            self.sub_usage.show()
            self.sub_text.setText(f"已用 {fmt_bytes(used)} / {fmt_bytes(total)}")
            self.sub_pct.setText(f"{round(used / total * 100)}%")
            self.sub_bar.set_value(used / total)
        else:
            self.sub_usage.hide()

    def _node_menu(self):
        d = self._clash
        nodes = d.get("nodes") or []
        if not nodes:
            return
        m = styled_menu(self)
        group = d.get("group")
        for n in nodes[:60]:
            a = m.addAction(("✓  " if n == d.get("node") else "    ") + short_node(n) + ("" if "@" not in n else f"    {n.split('@')[0]}"))
            a.triggered.connect(lambda _=False, n=n: self.ctx.clash.select(group, n))
        m.addSeparator()
        t = m.addAction("    测试当前节点延迟")
        t.triggered.connect(lambda: self.ctx.clash.test_delay(d.get("node"), lambda ms: self.node.set_text(
            f"{short_node(d.get('node') or '')}  {ms}ms" if ms else f"{short_node(d.get('node') or '')}  超时")))
        if self.island:
            self.island.locks.add("menu")
            m.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        m.popup(QtGui.QCursor.pos())

    def on_show(self):
        super().on_show()
        self.ctx.brightness.refresh()
        self.ctx.radio.refresh()


# ======================================================================== 窗口切换
class WinCard(Hoverable):
    closed = QtCore.Signal()

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        img = info.get("icon")
        self.pm = QtGui.QPixmap.fromImage(img) if isinstance(img, QtGui.QImage) and not img.isNull() else None
        self.setFixedHeight(54)
        self.setToolTip(f"{info.get('title')}\n{info.get('exe')}")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton or (
                e.button() == Qt.MouseButton.LeftButton and self._close_rect().contains(e.position())):
            self.closed.emit()
            return
        super().mouseReleaseEvent(e)

    def _close_rect(self):
        return QtCore.QRectF(self.width() - 26, 6, 20, 20)

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.card(1.0 + 0.7 * self._hover.x) if not self.info.get("active") else theme.active_pill())
        p.drawRoundedRect(r, 14, 14)
        ir = QtCore.QRectF(r.x() + 10, r.center().y() - 14, 28, 28)
        if self.pm is not None:
            p.drawPixmap(ir.toRect(), self.pm)
        else:
            p.setFont(icon_font(18))
            p.setPen(theme.text(0.8))
            p.drawText(ir, Qt.AlignmentFlag.AlignCenter, G.APPS)
        tx = ir.right() + 9
        tw = r.right() - tx - (26 if self._hover.x > 0.3 else 8)
        p.setFont(ui_font(12.5, W.Medium))
        p.setPen(theme.text())
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(tx, r.y() + 8, tw, 20), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.info.get("title", ""), Qt.TextElideMode.ElideRight, int(tw)))
        p.setFont(ui_font(10.5))
        p.setPen(theme.text3())
        p.drawText(QtCore.QRectF(tx, r.y() + 27, tw, 18), Qt.AlignmentFlag.AlignVCenter,
                   (self.info.get("exe") or "").removesuffix(".exe"))
        if self._hover.x > 0.05:
            cr = self._close_rect()
            c = theme.text(0.55 * self._hover.x)
            p.setFont(icon_font(10))
            p.setPen(c)
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, G.CLOSE)


class TasksPage(Page):
    key, glyph, title, height = "tasks", G.WINDOWS, "窗口", 300

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sc = ScrollArea()
        self.grid = QtWidgets.QGridLayout(self.sc.inner)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(8)
        self.count = Label("", 12, level=3)
        self.setLayout(vbox(hbox(Label("点击切换 · 中键或 × 关闭", 12, level=2), None, self.count), (self.sc, 1), spacing=8))
        self.timer = QtCore.QTimer(self, interval=2000, timeout=self.refresh)
        self._sig = None

    def refresh(self):
        wins = self.ctx.windows.list_windows()
        sig = tuple((w["hwnd"], w["title"], w.get("active")) for w in wins)
        if sig == self._sig:
            return
        self._sig = sig
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cols = 3
        for i, w in enumerate(wins):
            c = WinCard(w)
            c.clicked.connect(lambda h=w["hwnd"]: self._activate(h))
            c.closed.connect(lambda h=w["hwnd"]: self._close(h))
            self.grid.addWidget(c, i // cols, i % cols)
        for k in range(cols):
            self.grid.setColumnStretch(k, 1)
        self.grid.setRowStretch(len(wins) // cols + 1, 1)
        self.count.setText(f"{len(wins)} 个窗口")
        rows = (len(wins) + cols - 1) // cols
        self.set_height(int(clamp(30 + rows * 62, 120, 330)))

    def _activate(self, hwnd):
        if self.island:
            self.island.collapse()
        QtCore.QTimer.singleShot(60, lambda: self.ctx.windows.activate(hwnd))

    def _close(self, hwnd):
        self.ctx.windows.close(hwnd)
        QtCore.QTimer.singleShot(400, self.refresh)

    def on_show(self):
        super().on_show()
        self._sig = None
        self.refresh()
        self.timer.start()

    def on_hide(self):
        super().on_hide()
        self.timer.stop()


# ======================================================================== 性能
class Meter(Card):
    def __init__(self, title: str, color: str, parent=None):
        super().__init__(parent, 18)
        self.t = Label(title, 12, W.DemiBold, level=2)
        self.v = Label("--", 22, W.DemiBold, num=True)
        self.v.setFixedHeight(30)
        self.sub = Label("", 10.5, level=3)
        self.spark = Sparkline(color=QtGui.QColor(color), fixed_max=100)
        self.spark.setFixedHeight(40)
        self.setLayout(vbox(hbox(self.t, None, self.sub), self.v, self.spark, spacing=0, margins=(12, 8, 12, 8)))


class PerfPage(Page):
    key, glyph, title, height = "perf", G.PULSE, "性能", 296

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cpu = Meter("CPU", "#4f8cff")
        self.gpu = Meter("GPU", "#bf5af2")
        self.mem = Meter("内存", "#30d158")
        self.net = Card(radius=18)
        self.net_up = Label("", 12, W.Medium, num=True)
        self.net_dn = Label("", 12, W.Medium, num=True)
        self.disk = Label("", 11, level=3, num=True)
        self.sp_up = Sparkline(color=QtGui.QColor("#ff9f0a"))
        self.sp_dn = Sparkline(color=QtGui.QColor("#64d2ff"))
        for s in (self.sp_up, self.sp_dn):
            s.setFixedHeight(28)
        self.net.setLayout(vbox(hbox(Label("网络", 12, W.DemiBold, level=2), None, self.disk),
                                hbox(self.net_up, (self.sp_up, 1), spacing=6),
                                hbox(self.net_dn, (self.sp_dn, 1), spacing=6), spacing=2, margins=(12, 8, 12, 8)))
        self.top = Label("", 11.5, level=2, wrap=True)
        self.top.setFixedHeight(84)
        topcard = Card(radius=18)
        topcard.setLayout(vbox(Label("占用最高", 12, W.DemiBold, level=2), self.top, spacing=2, margins=(12, 8, 12, 8)))
        self.setLayout(vbox(hbox(self.cpu, self.gpu, self.mem, spacing=8),
                            hbox((self.net, 3), (topcard, 2), spacing=8), spacing=8))
        self.ttimer = QtCore.QTimer(self, interval=3000, timeout=self._top)
        for l in (self.net_up, self.net_dn):
            l.setFixedWidth(84)

    def set_data(self, d: dict):
        if not self.active:
            return
        h = d.get("history") or {}
        self.cpu.v.setText(f"{d.get('cpu', 0):.0f}%")
        self.cpu.spark.set_values(h.get("cpu") or [])
        cores = d.get("cpu_per_core") or []
        self.cpu.sub.setText(f"{len(cores)} 线程" if cores else "")
        g = d.get("gpu")
        self.gpu.v.setText("--" if g is None else f"{g:.0f}%")
        self.gpu.spark.set_values([x for x in (h.get("gpu") or []) if x is not None])
        gm = d.get("gpu_mem")
        self.gpu.sub.setText(f"显存 {fmt_bytes(gm)}" if gm else "")
        self.mem.v.setText(f"{d.get('mem_pct', 0):.0f}%")
        self.mem.spark.set_values(h.get("mem") or [])
        self.mem.sub.setText(f"{fmt_bytes(d.get('mem_used', 0))} / {fmt_bytes(d.get('mem_total', 0))}")
        self.net_up.setText(f"↑ {fmt_bytes(d.get('net_up', 0), True)}")
        self.net_dn.setText(f"↓ {fmt_bytes(d.get('net_down', 0), True)}")
        self.sp_up.set_values(h.get("net_up") or [])
        self.sp_dn.set_values(h.get("net_down") or [])
        self.disk.setText(f"磁盘 读 {fmt_bytes(d.get('disk_r', 0), True)} · 写 {fmt_bytes(d.get('disk_w', 0), True)}")

    def _top(self):
        def done(rows):
            if rows:
                self.top.setText("\n".join(f"{r['name'].removesuffix('.exe')[:18]:<18}  {r['cpu']:.1f}%  {fmt_bytes(r['mem'])}"
                                           for r in rows[:4]))
        run_bg(self.ctx.perf.top_processes, 4, on_done=done)

    def on_show(self):
        super().on_show()
        st = getattr(self.ctx.perf, "state", None)
        if st:
            self.set_data(st)
        self._top()
        self.ttimer.start()

    def on_hide(self):
        super().on_hide()
        self.ttimer.stop()
