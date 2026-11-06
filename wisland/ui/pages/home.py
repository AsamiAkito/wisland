# -*- coding: utf-8 -*-
"""主页（正在播放 + 快捷操作 + 自定义启动器）与消息页（QQ / 微信 / 邮件提醒）。"""
from __future__ import annotations

import os
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.bg import launch
from ...core.config import cfg, expand
from ..anim import Spring, clamp
from ..theme import KIND_COLORS, G, W, icon_font, theme, ui_font
from ..widgets import (Card, Hoverable, IconButton, Label, PillButton, ScrollArea, Themed, fmt_time, hbox,
                       painter, rounded_pixmap, vbox)
from .base import Page

Qt = QtCore.Qt


# ======================================================================== 播放卡片
class Progress(QtWidgets.QWidget, Themed):
    seek = QtCore.Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.pos_ = 0.0
        self.dur = 0.0
        self.stamp = 0.0
        self.playing = False
        self.hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hook_theme()

    def set_state(self, m: dict):
        self.pos_ = float(m.get("position") or 0)
        self.dur = float(m.get("duration") or 0)
        self.stamp = float(m.get("stamp") or time.monotonic())
        self.playing = bool(m.get("playing"))
        self.update()

    def current(self) -> float:
        p = self.pos_ + ((time.monotonic() - self.stamp) if self.playing else 0)
        return clamp(p, 0, self.dur) if self.dur else p

    def mouseReleaseEvent(self, e):
        if self.dur > 0:
            self.seek.emit(clamp(e.position().x() / max(1, self.width()), 0, 1) * self.dur)

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        y = 6
        th = 4 + 3 * self.hover
        tr = QtCore.QRectF(0, y - th / 2, r.width(), th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.track())
        p.drawRoundedRect(tr, th / 2, th / 2)
        cur = self.current()
        if self.dur > 0:
            p.setBrush(theme.text(0.85 + 0.15 * self.hover))
            p.drawRoundedRect(QtCore.QRectF(0, y - th / 2, max(th, r.width() * cur / self.dur), th), th / 2, th / 2)
        p.setFont(ui_font(10.5, num=True))
        p.setPen(theme.text3())
        p.drawText(QtCore.QRectF(0, 10, 60, 12), Qt.AlignmentFlag.AlignLeft, fmt_time(cur))
        if self.dur > 0:
            p.drawText(QtCore.QRectF(r.width() - 60, 10, 60, 12), Qt.AlignmentFlag.AlignRight, fmt_time(self.dur))


class CoverView(QtWidgets.QWidget):
    """封面：悬停时轻微放大上浮，并在身后透出封面主色的柔光。"""
    def __init__(self, size: int, parent=None):
        super().__init__(parent)
        self.size_ = size
        self.pm: QtGui.QPixmap | None = None
        self.glow = QtGui.QColor(255, 255, 255, 0)
        self.h = 0.0
        self.setFixedSize(size + 12, size + 12)

    def set_pixmap(self, pm, glow: QtGui.QColor | None):
        self.pm = pm
        self.glow = glow or QtGui.QColor(255, 255, 255, 0)
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        c = QtCore.QRectF(self.rect()).center()
        s = 1.0 + 0.07 * self.h
        r = QtCore.QRectF(0, 0, self.size_ * s, self.size_ * s)
        r.moveCenter(c - QtCore.QPointF(0, 2.5 * self.h))
        if self.h > 0.01 and self.glow.alpha():
            g = QtGui.QRadialGradient(r.center() + QtCore.QPointF(0, 6), r.width() * 0.75)
            col = QtGui.QColor(self.glow)
            col.setAlpha(round(150 * self.h))
            g.setColorAt(0, col)
            col2 = QtGui.QColor(self.glow)
            col2.setAlpha(0)
            g.setColorAt(1, col2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(r.adjusted(-8, -4, 8, 14))
        if self.pm is not None:
            p.drawPixmap(r.toRect(), self.pm)


class CoverTitle(QtWidgets.QWidget):
    """封面在上方居中（正对播放键），标题压在封面下沿、向两侧延伸，但不超出播放卡片。"""
    COVER = 58

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pm: QtGui.QPixmap | None = None
        self.glow = QtGui.QColor(255, 255, 255, 0)
        self.h = 0.0
        self.title = "未在播放"
        self.sub = ""
        self.setFixedHeight(self.COVER + 30)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        theme.changed.connect(self.update)

    def set_pixmap(self, pm, glow):
        self.pm = pm
        self.glow = glow or QtGui.QColor(255, 255, 255, 0)
        self.update()

    def set_text(self, title, sub):
        self.title, self.sub = title, sub
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        W_ = self.width()
        s = 1.0 + 0.07 * self.h
        size = self.COVER * s
        cr = QtCore.QRectF(0, 0, size, size)
        cr.moveCenter(QtCore.QPointF(W_ / 2, 4 + self.COVER / 2 - 2.5 * self.h))
        if self.h > 0.01 and self.glow.alpha():
            g = QtGui.QRadialGradient(cr.center() + QtCore.QPointF(0, 6), cr.width() * 0.8)
            c1 = QtGui.QColor(self.glow)
            c1.setAlpha(round(150 * self.h))
            c2 = QtGui.QColor(self.glow)
            c2.setAlpha(0)
            g.setColorAt(0, c1)
            g.setColorAt(1, c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(cr.adjusted(-10, -6, 10, 14))
        if self.pm is not None:
            p.drawPixmap(cr.toRect(), self.pm)
        # 标题：压住封面下沿，左右延伸（限制在卡片内），底下垫一块半透明玻璃保证可读
        f = ui_font(13.5, W.DemiBold)
        fm = QtGui.QFontMetrics(f)
        text = self.title + (f"  ·  {self.sub}" if self.sub else "")
        maxw = W_ - 16
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(maxw - 20))
        tw = fm.horizontalAdvance(text) + 20
        tr = QtCore.QRectF((W_ - tw) / 2, cr.bottom() - 12, tw, fm.height() + 8)
        bg = QtGui.QColor(theme.tint())
        bg.setAlpha(min(255, bg.alpha() + 90))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(tr, tr.height() / 2, tr.height() / 2)
        p.setFont(f)
        p.setPen(theme.text())
        p.drawText(tr, Qt.AlignmentFlag.AlignCenter, text)


class MediaCard(Card):
    def __init__(self, ctx, parent=None):
        super().__init__(parent, 20)
        self.ctx = ctx
        self.cover_px: QtGui.QPixmap | None = None
        self.cover = CoverTitle(self)
        self._hov = Spring(0, 260, 0.6, 0.004)
        self._ht = QtCore.QTimer(self, interval=16, timeout=self._hover_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.title = Label("未在播放", 15, W.DemiBold)
        self.artist = Label("", 12.5, level=2)
        self.app = Label("", 11, level=3)
        self.prog = Progress()
        self.prev = IconButton(G.PREV, 34, 15, "上一首")
        self.play = IconButton(G.PLAY, 40, 18, "播放 / 暂停", filled=True)
        self.next = IconButton(G.NEXT, 34, 15, "下一首")
        self.open_btn = PillButton("打开网易云", G.MUSIC, height=28)
        self.prev.clicked.connect(lambda: ctx.media.prev())
        self.next.clicked.connect(lambda: ctx.media.next())
        self.play.clicked.connect(lambda: ctx.media.play_pause())
        self.prog.seek.connect(lambda s: ctx.media.seek(s))
        self.open_btn.clicked.connect(self._open_player)
        self.open_btn.hide()
        self.title.hide()
        self.artist.hide()
        self.app.hide()
        ctrls = hbox(None, self.prev, self.play, self.next, None, spacing=8)   # 播放键正对封面
        self.setLayout(vbox(self.cover, self.prog, ctrls, None, spacing=0, margins=(10, 6, 10, 6)))
        self._tick = QtCore.QTimer(self, interval=500, timeout=self.prog.update)
        self._set_cover(None)

    # --- 悬停动画 ---
    def enterEvent(self, e):
        self._hov.to(1.0, 260, 0.6)
        self._ht.start()

    def leaveEvent(self, e):
        self._hov.to(0.0, 220, 0.8)
        self._ht.start()

    def _hover_tick(self):
        self._hov.step(0.016)
        h = max(0.0, self._hov.x)
        self.cover.h = h
        self.cover.update()
        self.prog.hover = h
        self.prog.update()
        self.update()
        if self._hov.rest:
            self._ht.stop()

    def paintEvent(self, e):
        p = painter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.card(1.0 + 0.7 * max(0.0, self._hov.x)))
        p.drawRoundedRect(QtCore.QRectF(self.rect()), self.radius, self.radius)

    def _open_player(self):
        items = cfg.get("launcher") or []
        it = next((i for i in items if "cloudmusic" in str(i.get("path", "")).lower()), items[0] if items else None)
        if it:
            self.ctx.windows.open_or_focus(expand(it["path"]), it.get("exe"))

    def _set_cover(self, img):
        px = rounded_pixmap(img, 58, 13) if img is not None else None
        glow = None
        if img is not None:
            q = img if isinstance(img, QtGui.QImage) else img.toImage()
            glow = q.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
            h, s_, v, _ = glow.getHsv()
            glow = QtGui.QColor.fromHsv(max(0, h), min(255, int(s_ * 1.4) + 40), max(170, v))
        if px is None:
            pm = QtGui.QPixmap(116, 116)
            pm.fill(Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.card(1.6))
            p.drawRoundedRect(QtCore.QRectF(0, 0, 116, 116), 26, 26)
            p.setFont(icon_font(44))
            p.setPen(theme.text(0.6))
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, G.MUSIC)
            p.end()
            pm.setDevicePixelRatio(2)
            px = pm
        self.cover.set_pixmap(px, glow)

    def set_cover(self, img):
        self._set_cover(img)

    def set_media(self, m: dict):
        has = bool(m.get("has_session") and m.get("title"))
        self.cover.set_text(m.get("title") or "未在播放", m.get("artist") or "")
        self.play.set_glyph(G.PAUSE if m.get("playing") else G.PLAY)
        self.prog.set_state(m)
        self.prog.setVisible(True)
        self.prog.setEnabled(has and (m.get("duration") or 0) > 0)
        for b in (self.prev, self.next, self.play):
            b.setEnabled(has)

        if m.get("playing"):
            self._tick.start()
        else:
            self._tick.stop()


# ======================================================================== 快捷操作 / 启动器
class Action(Hoverable):
    def __init__(self, glyph: str, title: str, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.title = title
        self.on = False
        self.setFixedHeight(58)

    def set_on(self, on: bool, glyph: str | None = None, title: str | None = None):
        self.on = on
        if glyph:
            self.glyph = glyph
        if title:
            self.title = title
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.on_color() if self.on else theme.card(1.0 + 0.6 * self._hover.x))
        p.drawRoundedRect(r, 16, 16)
        p.setFont(icon_font(17))
        p.setPen(theme.on_fg() if self.on else theme.text(0.9))
        p.drawText(QtCore.QRectF(r.x(), r.y() + 6, r.width(), 26), Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.setFont(ui_font(11.5, W.Medium))
        p.setPen(theme.on_fg(0.9) if self.on else theme.text2())
        p.drawText(QtCore.QRectF(r.x(), r.y() + 32, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.title)


_icon_provider = None


def file_icon(path: str, size: int = 64) -> QtGui.QPixmap | None:
    global _icon_provider
    if _icon_provider is None:
        _icon_provider = QtWidgets.QFileIconProvider()
    if not path or not os.path.exists(path):
        return None
    ic = _icon_provider.icon(QtCore.QFileInfo(path))
    pm = ic.pixmap(size, size)
    return None if pm.isNull() else pm


_appx_cache: dict[str, str] = {}


def resolve_appx(spec: str, cb):
    """"appx:包名/相对路径" -> 安装目录下的文件路径（后台查询，结果缓存）。"""
    from ...core.bg import run_bg, run_hidden
    name, _, rel = spec[5:].partition("/")
    if name in _appx_cache:
        cb(os.path.join(_appx_cache[name], rel))
        return

    def q():
        r = run_hidden(["powershell", "-NoProfile", "-Command", f"(Get-AppxPackage -Name '{name}').InstallLocation"])
        return (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()[0] if r.stdout else ""

    def done(loc):
        if loc:
            _appx_cache[name] = loc
            cb(os.path.join(loc, rel))
    run_bg(q, on_done=done)


class LaunchIcon(Hoverable):
    def __init__(self, item: dict, opener=None, parent=None):
        super().__init__(parent)
        self.item = item
        self.opener = opener
        self.path = expand(item.get("path", ""))
        icon_spec = item.get("icon", "")
        self.pm = None
        if str(icon_spec).startswith("appx:"):
            resolve_appx(icon_spec, self._set_icon)
        else:
            self.pm = file_icon(expand(icon_spec) or self.path)
        self.setFixedSize(44, 44)
        self.setToolTip(item.get("name", os.path.basename(self.path)))
        self.clicked.connect(self.run)

    def _set_icon(self, path):
        self.pm = file_icon(path)
        self.update()

    def run(self):
        args = self.item.get("args") or []
        if isinstance(args, str):
            args = args.split()
        if self.opener is not None:
            self.opener(self.path, self.item.get("exe"), args)
        else:
            launch(self.path, args)

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale() * (1 + 0.06 * self._hover.x)
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        if self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, 12, 12)
        if self.pm is not None:
            ir = QtCore.QRectF(r.center().x() - 14, r.center().y() - 14, 28, 28)
            p.drawPixmap(ir.toRect(), self.pm)
        else:
            p.setFont(ui_font(15, W.Bold))
            p.setPen(theme.text(0.8))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, (self.item.get("name") or "?")[:1])


class PetWidget(QtWidgets.QWidget):
    """主页中间的大号小形象：眼睛跟随光标，鼠标靠近时开心。"""
    def __init__(self, parent=None, scale: float = 2.3):
        super().__init__(parent)
        from ..compact import PetFace
        self.face = PetFace()
        self.scale = scale
        self.setMinimumWidth(round(64 * scale))
        self._t = QtCore.QTimer(self, interval=33, timeout=self.update)
        theme.changed.connect(self.update)

    def showEvent(self, e):
        self._t.start()

    def hideEvent(self, e):
        self._t.stop()

    def paintEvent(self, e):
        p = painter(self)
        c = QtCore.QRectF(self.rect()).center()
        gc = QtCore.QPointF(self.mapToGlobal(c.toPoint()))
        look = QtCore.QPointF(QtGui.QCursor.pos()) - gc
        near = self.rect().adjusted(-24, -24, 24, 24).contains(self.mapFromGlobal(QtGui.QCursor.pos()))
        self.face.paint(p, c.x(), c.y() - 4, self.scale, 1.0, look, near)


class LaunchStrip(QtWidgets.QScrollArea):
    """启动器横条：应用多到放不下时，滚轮横向平滑滚动（不显示滚动条）。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setFixedHeight(46)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QScrollArea{background:transparent;border:none}")
        self.viewport().setStyleSheet("background:transparent")
        self.inner = QtWidgets.QWidget()
        self.inner.setObjectName("stripInner")
        self.inner.setStyleSheet("#stripInner{background:transparent}")
        self.setWidget(self.inner)
        self.inner.setAutoFillBackground(False)
        self.row = QtWidgets.QHBoxLayout(self.inner)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(2)
        self._anim = QtCore.QPropertyAnimation(self.horizontalScrollBar(), b"value", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

    def wheelEvent(self, e):
        bar = self.horizontalScrollBar()
        if bar.maximum() <= 0:
            e.ignore()
            return
        d = e.angleDelta().y() or e.angleDelta().x()
        start = self._anim.endValue() if self._anim.state() == QtCore.QAbstractAnimation.State.Running else bar.value()
        target = max(0, min(bar.maximum(), int(start) - d))
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(target)
        self._anim.start()
        e.accept()


class HomePage(Page):
    key, glyph, title, height = "home", G.HOME, "主页", 240

    def __init__(self, ctx):
        super().__init__(ctx)
        self.media = MediaCard(ctx)
        self.media.setFixedHeight(156)
        self.a_pin = Action(G.UNPIN, "保持展开")
        self.a_pt = Action(G.POINTER, "可穿透")
        self.a_mute = Action(G.VOLUME, "静音")
        self.a_live = Action(G.LIVE, "实时字幕")
        self.a_mute.clicked.connect(lambda: ctx.audio.toggle_mute())
        self.a_live.clicked.connect(self._toggle_live)
        ctx.live.running_changed.connect(lambda on: self.a_live.set_on(on, None, "聆听中" if on else "实时字幕"))
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        grid.addWidget(self.a_pin, 0, 0)
        grid.addWidget(self.a_pt, 0, 1)
        grid.addWidget(self.a_mute, 1, 0)
        grid.addWidget(self.a_live, 1, 1)
        gw = QtWidgets.QWidget()
        gw.setLayout(grid)
        gw.setFixedWidth(172)
        self.strip = LaunchStrip()
        self.launch_row = self.strip.row
        self.build_launcher()
        self.pet = PetWidget()
        self.media.setFixedWidth(236)
        # 播放卡与右侧按钮收窄，小形象居中，两边留出更大的空间
        lay = vbox(hbox(self.media, (self.pet, 1), gw, spacing=10), self.strip, spacing=8)
        self.setLayout(lay)

    def build_launcher(self):
        while self.launch_row.count():
            it = self.launch_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for item in cfg.get("launcher") or []:
            self.launch_row.addWidget(LaunchIcon(item, self._open))
        self.launch_row.addStretch(1)

    def _open(self, path, exe=None, args=None):
        res = self.ctx.windows.open_or_focus(path, exe, args)
        if res == "focused" and self.island:
            self.island.collapse()

    def _toggle_live(self):
        self.ctx.live.toggle()
        if self.ctx.live.running and self.island:
            self.ctx.pages["trans"].show_live()
            self.island.show_page("trans")

    def set_muted(self, muted: bool):
        self.a_mute.set_on(muted, G.MUTE if muted else G.VOLUME, "已静音" if muted else "静音")


def cfg_path():
    from ...core.config import CONFIG_PATH
    return CONFIG_PATH


# ======================================================================== 消息
class AppTile(Hoverable):
    def __init__(self, kind: str, name: str, glyph: str, parent=None):
        super().__init__(parent)
        self.kind, self.name, self.glyph = kind, name, glyph
        self.count = 0
        self.last = ""
        self.setFixedHeight(84)

    def set_count(self, n: int, last: str = ""):
        self.count, self.last = n, last
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.card(1.0 + 0.6 * self._hover.x))
        p.drawRoundedRect(r, 18, 18)
        col = KIND_COLORS.get(self.kind, KIND_COLORS["other"])
        cr = QtCore.QRectF(r.x() + 12, r.y() + 12, 30, 30)
        p.setBrush(col)
        p.drawEllipse(cr)
        p.setFont(icon_font(14))
        p.setPen(QtGui.QColor("white"))
        p.drawText(cr, Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.setFont(ui_font(13, W.DemiBold))
        p.setPen(theme.text())
        p.drawText(QtCore.QRectF(cr.right() + 8, cr.y(), 120, 30), Qt.AlignmentFlag.AlignVCenter, self.name)
        p.setFont(ui_font(22, W.Bold, num=True))
        p.setPen(col if self.count else theme.text3())
        p.drawText(QtCore.QRectF(r.x() + 14, r.y() + 46, r.width() - 28, 30), Qt.AlignmentFlag.AlignVCenter,
                   str(self.count) if self.count else "0")
        p.setFont(ui_font(11))
        p.setPen(theme.text3())
        p.drawText(QtCore.QRectF(r.x() + 14, r.y() + 46, r.width() - 26, 30),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   self.last or "")


class HistoryList(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self._hook_theme()

    def set_rows(self, rows):
        self.rows = rows[:30]
        self.setFixedHeight(max(30, len(self.rows) * 30))
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        if not self.rows:
            p.setFont(ui_font(12))
            p.setPen(theme.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无")
            return
        for i, r in enumerate(self.rows):
            y = i * 30
            col = KIND_COLORS.get(r.get("kind"), KIND_COLORS["other"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QtCore.QPointF(10, y + 15), 4, 4)
            p.setFont(ui_font(12.5, W.Medium))
            p.setPen(theme.text())
            p.drawText(QtCore.QRectF(24, y, 80, 30), Qt.AlignmentFlag.AlignVCenter, r.get("name", ""))
            kind = r.get("kind")
            desc = "新邮件" if kind == "mail" else (f"{r['sender']} 发来消息" if r.get("sender") else "新消息")
            p.setFont(ui_font(12.5))
            p.setPen(theme.text2())
            p.drawText(QtCore.QRectF(100, y, self.width() - 170, 30), Qt.AlignmentFlag.AlignVCenter,
                       QtGui.QFontMetrics(p.font()).elidedText(desc, Qt.TextElideMode.ElideRight, self.width() - 170))
            p.setFont(ui_font(11.5, num=True))
            p.setPen(theme.text3())
            p.drawText(QtCore.QRectF(self.width() - 64, y, 60, 30), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       time.strftime("%H:%M", time.localtime(r.get("time", 0))))


class MessagesPage(Page):
    key, glyph, title, height = "msg", G.MESSAGE, "消息", 270

    def __init__(self, ctx):
        super().__init__(ctx)
        self.tiles = {
            "qq": AppTile("qq", "QQ", G.CHAT),
            "wechat": AppTile("wechat", "微信", G.MESSAGE),
            "mail": AppTile("mail", "邮件", G.MAIL),
        }
        for k, t in self.tiles.items():
            t.clicked.connect(lambda k=k: self._open(k))
        self.hist = HistoryList()
        sc = ScrollArea()
        lay = QtWidgets.QVBoxLayout(sc.inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.hist)
        lay.addStretch(1)
        clear = PillButton("全部已读", G.CHECK, height=26)
        clear.clicked.connect(self._clear_all)
        head = hbox(None, clear)
        self.setLayout(vbox(hbox(*self.tiles.values(), spacing=8), head, (sc, 1), spacing=8))

    def _open(self, kind):
        self.ctx.notify.open(kind)
        if self.island:
            self.island.collapse()

    def _clear_all(self):
        for k in list(self.ctx.notify.counts):
            self.ctx.notify.clear(k)

    def refresh(self):
        counts = self.ctx.notify.counts
        hist = list(self.ctx.notify.history)
        for k, t in self.tiles.items():
            last = next((h for h in hist if h.get("kind") == k), None)
            t.set_count(counts.get(k, 0), time.strftime("%H:%M", time.localtime(last["time"])) if last else "")
        self.hist.set_rows(hist)

    def on_show(self):
        super().on_show()
        self.refresh()
