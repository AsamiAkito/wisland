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


class PetFace:
    """原创小形象（极简「发光双眼」风格）。

    常态：默认（东张西望）· 好奇（看向哪边那只眼变高）· 困倦（一分钟没人理）· 眨眼 · 惊讶（新消息）。
    鼠标靠近时随机换一种表情（不与上次重复）：
      开心 · 眨眼 · 爱心 · 星星眼 · 害羞 · 嘟嘴 · 眯眼
    s 为缩放（灵动岛上 1.0，主页上更大）。"""

    MOODS = ["happy", "wink", "love", "star", "shy", "pout", "squint"]

    def __init__(self):
        from .anim import Spring
        self.ex = Spring(0, 140, 0.8, 0.01)        # 视线
        self.ey = Spring(0, 140, 0.8, 0.01)
        self.lh = Spring(0, 160, 0.75, 0.003)      # 左眼下眼睑（开心月牙）
        self.rh = Spring(0, 160, 0.75, 0.003)      # 右眼下眼睑
        self.tired = Spring(0, 60, 1.0, 0.003)     # 上眼睑（困倦）
        self.wide = Spring(0, 220, 0.45, 0.003)    # 放大（惊讶 / 星星眼）
        self.morph = Spring(0, 200, 0.7, 0.003)    # 特殊形状（爱心 / 眯眼 / 嘟嘴）出现的程度
        self.blush = Spring(0, 120, 0.9, 0.003)
        self.mood = "default"
        self._last_mood = ""
        self._near = False
        self.blink_at = time.monotonic() + 2.5
        self.last_attention = time.monotonic()
        self.surprise_until = 0.0
        self._t = time.monotonic()

    # ---------- 事件 ----------
    def react(self, kind: str = "surprise"):
        self.last_attention = time.monotonic()
        if kind == "surprise":
            self.surprise_until = time.monotonic() + 1.4
            self.wide.v += 9.0

    def _pick(self):
        import random
        choices = [m for m in self.MOODS if m != self._last_mood]
        self.mood = random.choice(choices)
        self._last_mood = self.mood

    # ---------- 状态推进 ----------
    def _step(self, look: QtCore.QPointF, near: bool, s: float):
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._t))
        self._t = now
        if near and not self._near:
            self._pick()                       # 鼠标刚靠近：随机换表情
        elif not near and self._near:
            self.mood = "default"
        self._near = near
        if near:
            self.last_attention = now
        m = self.mood if near else "default"
        if now < self.surprise_until:
            m = "surprise"
        self._m = m
        # 视线
        if not near and abs(look.x()) > 420 * s:
            tx = math.sin(now * 0.55) * 0.8 + math.sin(now * 1.7) * 0.15
            ty = math.sin(now * 0.37) * 0.35
        else:
            tx = clamp(look.x() / (90.0 * s), -1, 1)
            ty = clamp(look.y() / (60.0 * s), -1, 1)
        if m == "shy":
            tx, ty = tx * 0.3 + 0.5, 0.75          # 害羞：看向斜下方
        self.ex.to(tx)
        self.ey.to(ty)
        self.lh.to({"happy": 1, "laugh": 1, "shy": 0.55, "wink": 1}.get(m, 0) if m != "wink" else 0)
        self.rh.to({"happy": 1, "laugh": 1, "shy": 0.55, "wink": 1}.get(m, 0))
        self.tired.to(0.55 if (now - self.last_attention > 60 and m == "default") else 0.0)
        self.wide.to({"star": 0.3, "surprise": 0.35}.get(m, 0.0))
        self.morph.to(1.0 if m in ("love", "squint", "pout") else 0.0)
        self.blush.to({"shy": 1.0, "love": 0.8, "happy": 0.5, "laugh": 0.6, "wink": 0.5}.get(m, 0.0))
        for sp in (self.ex, self.ey, self.lh, self.rh, self.tired, self.wide, self.morph, self.blush):
            sp.step(dt)
        # 眨眼（特殊表情时不眨）
        dur = 0.16 + 0.18 * self.tired.x
        if now >= self.blink_at + dur:
            gap = 2.4 + (math.sin(now * 12.9898) * 0.5 + 0.5) * 3.2
            self.blink_at = now + gap * (1.6 if self.tired.x > 0.3 else 1.0)
        b = 0.0
        if self.blink_at <= now < self.blink_at + dur and m in ("default", "pout", "shy", "star"):
            b = math.sin((now - self.blink_at) / dur * math.pi)
        return b

    # ---------- 绘制 ----------
    @staticmethod
    def _heart(cx, cy, size) -> QtGui.QPainterPath:
        p = QtGui.QPainterPath()
        w = size
        p.moveTo(cx, cy + w * 0.42)
        p.cubicTo(cx - w * 0.9, cy - w * 0.05, cx - w * 0.45, cy - w * 0.75, cx, cy - w * 0.28)
        p.cubicTo(cx + w * 0.45, cy - w * 0.75, cx + w * 0.9, cy - w * 0.05, cx, cy + w * 0.42)
        p.closeSubpath()
        return p

    @staticmethod
    def _sparkle(cx, cy, r) -> QtGui.QPainterPath:
        p = QtGui.QPainterPath()
        p.moveTo(cx, cy - r)
        p.quadTo(cx, cy, cx + r, cy)
        p.quadTo(cx, cy, cx, cy + r)
        p.quadTo(cx, cy, cx - r, cy)
        p.quadTo(cx, cy, cx, cy - r)
        return p

    def paint(self, p: QtGui.QPainter, cx: float, cy: float, s: float, a: float,
              look: QtCore.QPointF, near: bool):
        b = self._step(look, near, s)
        m = getattr(self, "_m", "default")
        now = time.monotonic()
        tired = clamp(self.tired.x, 0, 1)
        wide = clamp(self.wide.x, -0.3, 1.0)
        morph = clamp(self.morph.x, 0, 1)
        lx, ly = self.ex.x, self.ey.x
        bounce = math.sin(now * 16) * 1.6 * s if m == "laugh" else 0.0
        base_w, base_h, gap = 11.0 * s, 15.0 * s, 15.0 * s
        eye_col = theme.text(0.95 * a)
        glow_col = QtGui.QColor(theme.on_color()) if theme.dark else QtGui.QColor(0, 0, 0, 0)
        pink = QtGui.QColor(255, 92, 138)
        centers = []
        for side in (-1, 1):
            hap = clamp(self.lh.x if side < 0 else self.rh.x, 0, 1)
            curious = 1.0 + 0.22 * max(0.0, lx * side)
            w = base_w * (1 + 0.12 * wide) * (1 + 0.25 * b)
            h = base_h * curious * (1 + 0.18 * wide) * (1 - 0.88 * b)
            ex = cx + side * (gap / 2 + base_w / 2) + lx * 5.0 * s
            ey = cy + ly * 3.0 * s + bounce
            centers.append((ex, ey))
            r = QtCore.QRectF(ex - w / 2, ey - h / 2, w, max(1.6 * s, h))
            # 光晕
            if glow_col.alpha() and a > 0.05:
                g = QtGui.QRadialGradient(r.center(), max(r.width(), r.height()) * 0.85)
                c1 = QtGui.QColor(pink if m == "love" else glow_col)
                c1.setAlpha(round(85 * a))
                c2 = QtGui.QColor(c1)
                c2.setAlpha(0)
                g.setColorAt(0.0, c1)
                g.setColorAt(1.0, c2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(g)
                p.drawEllipse(r.center(), r.width() * 0.95, r.height() * 0.75 + 3 * s)
            # 特殊形状：爱心 / 眯眼 ><
            if m == "love" and morph > 0.05:
                pulse = 1.0 + 0.08 * math.sin(now * 7)
                p.setPen(Qt.PenStyle.NoPen)
                hc = QtGui.QColor(pink)
                hc.setAlphaF(a * morph)
                p.setBrush(hc)
                p.drawPath(self._heart(ex, ey, 15 * s * pulse * (0.6 + 0.4 * morph)))
                continue
            if m == "squint" and morph > 0.05:
                pen = QtGui.QPen(eye_col, 2.6 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                d = -side                               # 左眼 ">"、右眼 "<"
                k = 5.5 * s * morph
                path = QtGui.QPainterPath()
                path.moveTo(ex - d * k, ey - k)
                path.lineTo(ex + d * k, ey)
                path.lineTo(ex - d * k, ey + k)
                p.drawPath(path)
                continue
            rad = min(w, r.height()) * 0.42
            eye = QtGui.QPainterPath()
            eye.addRoundedRect(r, rad, rad)
            if tired > 0.01:
                lid = QtGui.QPainterPath()
                lid.addRect(QtCore.QRectF(r.left() - 2, r.top() - 2, r.width() + 4, 2 + r.height() * 0.5 * tired))
                eye = eye.subtracted(lid)
            if m == "pout" and morph > 0.05:
                # 嘟嘴：内侧上眼角斜切出"小眉头"
                lid = QtGui.QPainterPath()
                lid.addRect(QtCore.QRectF(-r.width(), -r.height(), r.width() * 2.2, r.height() * 1.0))
                tr = QtGui.QTransform()
                tr.translate(r.center().x() - side * r.width() * 0.1, r.top() + r.height() * 0.32 * morph)
                tr.rotate(side * -24 * morph)
                eye = eye.subtracted(tr.map(lid))
            if hap > 0.01:
                cut_r = r.width() * 0.9
                cc = QtCore.QPointF(r.center().x(), r.bottom() + cut_r - r.height() * 0.62 * hap)
                lid = QtGui.QPainterPath()
                lid.addEllipse(cc, cut_r, cut_r)
                eye = eye.subtracted(lid)
            if not eye.isEmpty():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(eye_col)
                p.drawPath(eye)
            if m == "star" and r.height() > 6 * s:
                sc = QtGui.QColor(20, 20, 26, round(220 * a)) if not theme.dark else QtGui.QColor(30, 30, 40, round(200 * a))
                p.setBrush(sc)
                p.drawPath(self._sparkle(ex + 1.4 * s, ey - 2.2 * s, 3.4 * s))
                p.drawPath(self._sparkle(ex - 2.2 * s, ey + 3.0 * s, 1.6 * s))
        # 腮红
        bl = clamp(self.blush.x, 0, 1)
        if bl > 0.02:
            for (ex, ey), side in zip(centers, (-1, 1)):
                c = QtGui.QColor(255, 124, 162, round(150 * bl * a))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QtCore.QPointF(ex + side * 7.5 * s, ey + base_h * 0.55), 4.4 * s, 2.4 * s)
        # 嘴
        mx = cx + lx * 5.0 * s
        my = cy + base_h * 0.66 + ly * 3.0 * s + bounce
        mc = theme.text(0.9 * a)
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QtGui.QPen(mc, 1.8 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        if m in ("happy", "wink", "love", "shy"):
            path = QtGui.QPainterPath()
            mw = 7.0 * s
            path.moveTo(mx - mw / 2, my)
            path.quadTo(mx, my + 4.2 * s, mx + mw / 2, my)
            p.setPen(pen)
            p.drawPath(path)
        elif m == "laugh":
            path = QtGui.QPainterPath()
            mw = 9.0 * s
            path.moveTo(mx - mw / 2, my - 0.5 * s)
            path.quadTo(mx, my + 8.5 * s, mx + mw / 2, my - 0.5 * s)
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mc)
            p.drawPath(path)
        elif m in ("star", "surprise"):
            p.setPen(pen)
            p.drawEllipse(QtCore.QPointF(mx, my + 1.5 * s), 2.4 * s, 2.8 * s)
        elif m == "pout":
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(mx - 3 * s, my + 1.5 * s), QtCore.QPointF(mx + 3 * s, my + 1.5 * s))
        elif m == "squint":
            path = QtGui.QPainterPath()
            mw = 6.0 * s
            path.moveTo(mx - mw / 2, my)
            path.quadTo(mx, my + 3.5 * s, mx + mw / 2, my)
            p.setPen(pen)
            p.drawPath(path)


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
        self.pet = PetFace()
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
        p.setClipRect(r.adjusted(6, 0, -6, 0), Qt.ClipOperation.IntersectClip)
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
        cx, cy = r.center().x(), r.center().y()
        sx, sy = getattr(self, "squash", (1.0, 1.0))
        p.save()
        p.translate(cx, cy)
        p.scale(sx, sy)                       # 灵动岛被按压时表情一起被压扁
        p.translate(-cx, -cy)
        self.pet.paint(p, cx, cy - 1, 1.0, a, self.look, self.near)
        p.restore()
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
