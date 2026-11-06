# -*- coding: utf-8 -*-
"""收起状态的内容（直接画在灵动岛上，不用子控件）。

三种内容，优先级：临时活动 > 正在播放 > 常态（时间 + 天气 + 未读小圆点）。
"""
from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui

from .anim import clamp, ease_out
from .theme import KIND_COLORS, G, W, icon_font, theme, ui_font
from .widgets import rounded_pixmap

Qt = QtCore.Qt
H_IDLE = 34
H_MEDIA = 36
H_ACT = 40


class CompactView:
    def __init__(self):
        self.media: dict = {}
        self.cover: QtGui.QPixmap | None = None
        self.activity: dict | None = None
        self.badges: dict[str, int] = {}
        self.weather: tuple | None = None   # (glyph, "18°", font_family)
        self.ticker: list[tuple[str, float, float]] = []  # (name, price, pct)
        self._mode = "idle"
        self._mode_t = 0.0
        # 收起态视图（滚轮切换）：clock = 时间天气，pet = 可爱形象，music = 音乐
        self.views = ["clock", "pet", "music"]
        self.view = "clock"
        self._view_t = -10.0
        self.look = QtCore.QPointF(0, 0)      # 光标相对胶囊中心的位置（形象的眼睛会看过去）
        self.near = False                      # 光标在附近（形象开心）
        self._eye = [0.0, 0.0]
        self._blink_at = time.monotonic() + 2.5
        self._happy = 0.0
        self._bars = [0.0] * 4
        self._peak = 0.0

    # --- 数据 ---
    def set_media(self, m: dict):
        self.media = m or {}

    def set_cover(self, img):
        self.cover = rounded_pixmap(img, 26, 7) if img is not None else None

    def show_activity(self, act: dict, duration: float = 2.4):
        a = dict(act)
        a["until"] = time.monotonic() + duration
        a["start"] = time.monotonic()
        self.activity = a

    def update_activity(self, **kw):
        """原地更新（例如连续调音量时不重新入场）。"""
        if self.activity:
            self.activity.update(kw)

    def set_view(self, v: str):
        if v in self.views:
            self.view = v

    def cycle_view(self, d: int):
        i = self.views.index(self.view) if self.view in self.views else 0
        self.view = self.views[(i + (1 if d < 0 else -1)) % len(self.views)]
        self._view_t = time.monotonic()
        return self.view

    def playing(self) -> bool:
        return bool(self.media.get("has_session") and self.media.get("playing") and self.media.get("title"))

    def mode(self) -> str:
        now = time.monotonic()
        if self.activity and now < self.activity["until"]:
            m = "activity"
        else:
            self.activity = None
            m = {"music": "media", "pet": "pet"}.get(self.view, "idle")
        if m != self._mode:
            self._mode = m
            self._mode_t = now
        return m

    def busy(self) -> bool:
        """是否需要持续动画帧。"""
        return self.mode() in ("media", "activity", "pet") or time.monotonic() - self._view_t < 1.4

    # --- 尺寸 ---
    def size_hint(self) -> tuple[float, float]:
        m = self.mode()
        nb = sum(1 for v in self.badges.values() if v)
        if m == "activity":
            a = self.activity
            t = a.get("type")
            if t in ("volume", "brightness"):
                return 280, H_ACT
            fm = QtGui.QFontMetrics(ui_font(13, W.DemiBold))
            w = 70 + fm.horizontalAdvance(a.get("title", "")) + QtGui.QFontMetrics(ui_font(12)).horizontalAdvance(a.get("sub", ""))
            return clamp(w + 24, 220, 460), H_ACT
        if m == "media":
            if not self.playing() and not self.media.get("title"):
                return 190 + nb * 12, H_MEDIA
            return 300 + nb * 12, H_MEDIA
        if m == "pet":
            return 150 + nb * 14, H_IDLE
        w = 150 + (64 if self.weather else 0) + nb * 14
        return w, H_IDLE

    # --- 绘制 ---
    def paint(self, p: QtGui.QPainter, r: QtCore.QRectF, alpha: float, peak: float):
        if alpha <= 0.01 or r.width() < 40:
            return
        m = self.mode()
        fade = ease_out((time.monotonic() - self._mode_t) / 0.28)
        a = alpha * fade
        self._peak = self._peak * 0.6 + peak * 0.4
        p.save()
        p.setClipRect(r.adjusted(6, 0, -6, 0))
        if m == "activity":
            self._paint_activity(p, r, a)
        elif m == "media":
            if not self.media.get("title"):
                self._paint_no_media(p, r, a)
            else:
                self._paint_media(p, r, a)
        elif m == "pet":
            self._paint_pet(p, r, a)
        else:
            self._paint_idle(p, r, a)
        p.restore()
        if m != "activity":
            self._paint_dots(p, r, alpha)

    def _paint_dots(self, p, r, alpha):
        """切换视图后短暂显示的页码小圆点。"""
        k = time.monotonic() - self._view_t
        if k > 1.4:
            return
        a = alpha * (1.0 if k < 0.9 else max(0.0, 1 - (k - 0.9) / 0.5))
        n = len(self.views)
        cx = r.center().x()
        y = r.bottom() - 4.5 if r.height() > 30 else r.bottom() - 3
        for i, v in enumerate(self.views):
            on = v == self.view
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.text((0.9 if on else 0.3) * a))
            w = 9 if on else 3.4
            x = cx + (i - (n - 1) / 2) * 9
            p.drawRoundedRect(QtCore.QRectF(x - w / 2, y - 1.7, w, 3.4), 1.7, 1.7)

    def _paint_no_media(self, p, r, a):
        cy = r.center().y()
        p.setFont(icon_font(14))
        p.setPen(theme.text(0.75 * a))
        p.drawText(QtCore.QRectF(r.x() + 16, r.y(), 22, r.height()), Qt.AlignmentFlag.AlignCenter, G.MUSIC)
        p.setFont(ui_font(13, W.Medium))
        p.setPen(theme.text2(a))
        p.drawText(QtCore.QRectF(r.x() + 46, r.y(), r.width() - 60, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "未在播放")
        _ = cy

    def _paint_pet(self, p, r, a):
        """原创小形象：两只圆润的眼睛，会眨眼、看向光标，靠近时眯成开心的弧线，带一点腮红。"""
        now = time.monotonic()
        cx, cy = r.center().x(), r.center().y()
        # 视线：朝光标方向，限制在小范围内
        lx = clamp(self.look.x() / 60.0, -1, 1) * 4.5
        ly = clamp(self.look.y() / 40.0, -1, 1) * 2.5
        if not self.near and abs(self.look.x()) > 400:
            # 光标很远：自己慢慢东张西望
            lx = math.sin(now * 0.7) * 3.5
            ly = math.sin(now * 0.43) * 1.2
        self._eye[0] += (lx - self._eye[0]) * 0.18
        self._eye[1] += (ly - self._eye[1]) * 0.18
        self._happy += ((1.0 if self.near else 0.0) - self._happy) * 0.15
        # 眨眼
        if now >= self._blink_at + 0.16:
            self._blink_at = now + 2.2 + (math.sin(now * 12.9898) * 0.5 + 0.5) * 3.0
        b = 0.0
        if self._blink_at <= now < self._blink_at + 0.16:
            t = (now - self._blink_at) / 0.16
            b = 1 - abs(t * 2 - 1)
        ew, eh = 9.5, 13.5 * (1 - 0.85 * b)
        gap = 24
        col = theme.text(0.92 * a)
        for side in (-1, 1):
            ex = cx + side * gap / 2 + self._eye[0]
            ey = cy - 1 + self._eye[1]
            if self._happy > 0.5:
                # 开心：^ ^
                path = QtGui.QPainterPath()
                path.moveTo(ex - ew / 2 - 0.5, ey + 2.5)
                path.quadTo(ex, ey - 6.5, ex + ew / 2 + 0.5, ey + 2.5)
                p.setPen(QtGui.QPen(col, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QtCore.QRectF(ex - ew / 2, ey - eh / 2, ew, max(1.6, eh)), ew / 2, min(ew, eh) / 2)
                # 高光
                if eh > 6:
                    p.setBrush(QtGui.QColor(255, 255, 255, round(150 * a)) if not theme.dark else QtGui.QColor(40, 40, 46, round(120 * a)))
                    p.drawEllipse(QtCore.QPointF(ex + 1.8, ey - eh * 0.22), 1.6, 1.6)
            # 腮红
            blush = QtGui.QColor(255, 120, 150, round((60 + 70 * self._happy) * a))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(blush)
            p.drawEllipse(QtCore.QPointF(cx + side * (gap / 2 + 9) + self._eye[0] * 0.5, cy + 6), 4.2, 2.4)
        x = r.right() - 14
        self._badges(p, x, cy, a)

    def _badges(self, p, right: float, cy: float, a: float) -> float:
        x = right
        for kind in ("mail", "wechat", "qq"):
            if self.badges.get(kind):
                c = QtGui.QColor(KIND_COLORS.get(kind, KIND_COLORS["other"]))
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QtCore.QPointF(x - 4, cy), 4, 4)
                x -= 13
        return x

    def _paint_idle(self, p, r, a):
        cy = r.center().y()
        now = time.localtime()
        p.setFont(ui_font(15, W.DemiBold, num=True))
        p.setPen(theme.text(a))
        txt = time.strftime("%H:%M", now)
        p.drawText(QtCore.QRectF(r.x() + 18, r.y(), 80, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
        x = r.right() - 16
        x = self._badges(p, x, cy, a) - 4
        if self.weather:
            glyph, temp = self.weather[0], self.weather[1]
            fam = self.weather[2] if len(self.weather) > 2 else None
            p.setFont(ui_font(13, W.Medium, num=True))
            p.setPen(theme.text2(a))
            fm = QtGui.QFontMetrics(p.font())
            tw = fm.horizontalAdvance(temp)
            p.drawText(QtCore.QRectF(x - tw, r.y(), tw + 2, r.height()), Qt.AlignmentFlag.AlignVCenter, temp)
            f = icon_font(13)
            if fam:
                f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            p.setFont(f)
            p.drawText(QtCore.QRectF(x - tw - 22, r.y(), 20, r.height()), Qt.AlignmentFlag.AlignCenter, glyph)

    def _paint_media(self, p, r, a):
        cy = r.center().y()
        x = r.x() + 6
        cr = QtCore.QRectF(x + 2, cy - 13, 26, 26)
        p.setOpacity(a)
        if self.cover is not None:
            p.drawPixmap(cr.toRect(), self.cover)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.card(1.4))
            p.drawRoundedRect(cr, 7, 7)
            p.setFont(icon_font(13))
            p.setPen(theme.text(0.8))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, G.MUSIC)
        p.setOpacity(1.0)
        # 右侧：音频律动条（跟随系统输出峰值）
        bars_w = 26
        bx = r.right() - 16 - bars_w
        bx = self._badges(p, r.right() - 14, cy, a) - bars_w - 6 if any(self.badges.values()) else bx
        t = time.monotonic()
        pk = clamp(self._peak * 1.6, 0, 1)
        for i in range(4):
            target = 0.18 + pk * (0.55 + 0.45 * math.sin(t * (7.3 + i * 2.1) + i * 1.7) ** 2)
            self._bars[i] += (target - self._bars[i]) * 0.35
            h = 4 + 14 * self._bars[i]
            br = QtCore.QRectF(bx + i * 7, cy - h / 2, 3.6, h)
            c = theme.text(0.85 * a)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(br, 1.8, 1.8)
        # 中间：标题 · 歌手，过长时滚动
        tx = cr.right() + 10
        tw = bx - 10 - tx
        title = self.media.get("title", "")
        artist = self.media.get("artist", "")
        text = f"{title}  ·  {artist}" if artist else title
        p.setFont(ui_font(13, W.Medium))
        fm = QtGui.QFontMetrics(p.font())
        full = fm.horizontalAdvance(text)
        p.save()
        p.setClipRect(QtCore.QRectF(tx, r.y(), tw, r.height()))
        p.setPen(theme.text(a))
        if full <= tw:
            p.drawText(QtCore.QRectF(tx, r.y(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            gap = 40
            period = (full + gap) / 34.0      # 34 px/s
            ph = (t % (period + 2.0))
            off = 0 if ph < 2.0 else (ph - 2.0) * 34.0
            for k in (0, 1):
                p.drawText(QtCore.QRectF(tx - off + k * (full + gap), r.y(), full + 4, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            # 两端渐隐
            for left in (True, False):
                g = QtGui.QLinearGradient(tx if left else tx + tw - 14, 0, tx + 14 if left else tx + tw, 0)
                g.setColorAt(0 if left else 1, QtGui.QColor(0, 0, 0, 255))
                g.setColorAt(1 if left else 0, QtGui.QColor(0, 0, 0, 0))
            # （渐隐需要独立图层，代价较高，这里省略，靠裁剪即可）
        p.restore()

    def _paint_activity(self, p, r, a):
        act = self.activity or {}
        cy = r.center().y()
        t = act.get("type")
        enter = ease_out((time.monotonic() - act.get("start", 0)) / 0.35)
        color = act.get("color")
        # 左侧图标圆
        d = 26
        cr = QtCore.QRectF(r.x() + 9, cy - d / 2, d, d)
        if color is not None:
            c = QtGui.QColor(color)
            c.setAlphaF(a)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(cr)
            p.setPen(QtGui.QColor(255, 255, 255, round(255 * a)))
        else:
            p.setPen(theme.text(a))
        img = act.get("icon")
        if isinstance(img, QtGui.QPixmap):
            p.setOpacity(a)
            p.drawPixmap(cr.toRect(), img)
            p.setOpacity(1)
        else:
            p.setFont(icon_font(13 if color is not None else 16))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, act.get("glyph", G.BELL))
        x = cr.right() + 10
        if t in ("volume", "brightness"):
            v = clamp(act.get("value", 0.0), 0, 1)
            bw = r.right() - 52 - x
            track = QtCore.QRectF(x, cy - 3, bw, 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.track())
            p.drawRoundedRect(track, 3, 3)
            p.setBrush(theme.text(a))
            p.drawRoundedRect(QtCore.QRectF(x, cy - 3, max(6, bw * v * enter), 6), 3, 3)
            p.setFont(ui_font(12.5, W.DemiBold, num=True))
            p.setPen(theme.text(a))
            p.drawText(QtCore.QRectF(r.right() - 48, r.y(), 34, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       "静音" if act.get("muted") else f"{round(v * 100)}")
            return
        title = act.get("title", "")
        sub = act.get("sub", "")
        p.setFont(ui_font(13, W.DemiBold))
        fm = QtGui.QFontMetrics(p.font())
        tw = fm.horizontalAdvance(title)
        avail = r.right() - 14 - x
        tc = act.get("title_color")
        p.setPen(QtGui.QColor(tc) if tc else theme.text(a))
        p.drawText(QtCore.QRectF(x, r.y(), min(tw + 2, avail), r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(title, Qt.TextElideMode.ElideRight, int(avail)))
        if sub and tw + 10 < avail:
            p.setFont(ui_font(12))
            fm2 = QtGui.QFontMetrics(p.font())
            sc = act.get("sub_color")
            p.setPen(QtGui.QColor(sc) if sc else theme.text2(a))
            sw = avail - tw - 10
            p.drawText(QtCore.QRectF(x + tw + 10, r.y(), sw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm2.elidedText(sub, Qt.TextElideMode.ElideRight, int(sw)))
