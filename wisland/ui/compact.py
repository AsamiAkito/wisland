# -*- coding: utf-8 -*-
"""收起状态的内容（直接画在灵动岛上，不用子控件）。

三种内容，优先级：临时活动 > 正在播放 > 常态（时间 + 天气 + 未读小圆点）。
"""
from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui

from .anim import clamp, ease_out
from .brand import kind_pixmap, paint_icon_chip
from .theme import KIND_COLORS, G, W, icon_font, theme, ui_font
from .widgets import rounded_pixmap

Qt = QtCore.Qt
H_IDLE = 34
H_MEDIA = 36
H_ACT = 40
H_SUB = 60          # 实时字幕：上日语、下中文两行
SUB_HOLD_S = 7.0    # 最后一句字幕之后保留多久再回到原来的视图
W_TEXT = 640        # 显示歌词 / 实时字幕时的胶囊宽度（约为普通收起态的 3 倍）
BADGE_ORDER = ("mail", "wechat", "qq", "feishu", "dingtalk")
_PUNCT = set("，。！？、；：,.!?;:…")


def _wrap_lines(fm: QtGui.QFontMetrics, text: str, width: float, max_lines: int,
                tail: bool = False) -> list[str]:
    """按字符宽度折行（中日文没有空格也能断），最多 max_lines 行；tail 保留最新一段。"""
    text = " ".join((text or "").split())
    if not text or width < 8:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        if not cur or fm.horizontalAdvance(cur + ch) <= width:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[-max_lines:] if tail else lines[:max_lines]
        if lines:
            lines[-1] = lines[-1].rstrip() + "…"
    # 只剩一个标点的孤行（如末尾的「。」）并到上一行显示，避免最后一行只有一个点
    while len(lines) >= 2 and lines[-1] and all(ch in _PUNCT for ch in lines[-1]):
        lines.pop()
    if not lines and text:
        lines = [text[:1]]
    return lines



def _draw_lines(p: QtGui.QPainter, x: float, y: float, w: float, lines: list[str],
                line_h: float, flags: Qt.AlignmentFlag):
    for i, line in enumerate(lines):
        p.drawText(QtCore.QRectF(x, y + i * line_h, w, line_h), flags, line)



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
        self.spread = 0.0          # 被按压时两眼额外分开的距离（s=1 时的像素）
        self.tilt = 0.0            # 被按压时每只眼向外倾斜的角度

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
            p.save()
            try:
                # 被按压时两只眼睛各自向外倾斜（按在中间时最明显）
                _ex0 = cx + side * (gap / 2 + base_w / 2 + self.spread * s / 2) + self.ex.x * 5.0 * s
                _ey0 = cy + self.ey.x * 3.0 * s
                if abs(self.tilt) > 0.05:
                    p.translate(_ex0, _ey0)
                    p.rotate(side * self.tilt)
                    p.translate(-_ex0, -_ey0)
                hap = clamp(self.lh.x if side < 0 else self.rh.x, 0, 1)
                curious = 1.0 + 0.22 * max(0.0, lx * side)
                w = base_w * (1 + 0.12 * wide) * (1 + 0.25 * b)
                h = base_h * curious * (1 + 0.18 * wide) * (1 - 0.88 * b)
                ex = cx + side * (gap / 2 + base_w / 2 + self.spread * s / 2) + lx * 5.0 * s
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

            finally:
                p.restore()
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
        self.idle_order: list[str] = []       # 来消息的顺序：收起态中间优先显示最近的两个
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
        # 歌词（由 app 设置：services.lyrics.Lyrics 或 None）
        self.lyrics = None
        self._lyr_w = 0.0
        self._lyr_idx = -2
        self._lyr_prev = ""
        self._lyr_t = 0.0
        self._lyr_cur = ""
        # 实时字幕（收起态两行：日语 / 中文）
        self.sub_on = False
        self.sub_jp = ""
        self.sub_zh = ""
        self.sub_t = -100.0
        # 收起态音乐视图右侧的播放 / 暂停键（灵动岛坐标，绘制时更新）
        self.play_rect: QtCore.QRectF | None = None
        self.play_hover = 0.0

    # --- 数据 ---
    def set_media(self, m: dict):
        self.media = m or {}

    def set_lyrics(self, lyr):
        """设置当前歌曲的歌词；按最长一行确定胶囊宽度（避免每句都改变宽度）。"""
        self.lyrics = lyr if lyr else None
        self._lyr_idx, self._lyr_prev = -2, ""
        if self.lyrics:
            fm = QtGui.QFontMetrics(ui_font(13, W.Medium))
            widest = max(fm.horizontalAdvance(x) for x in self.lyrics.lines)
            self._lyr_w = clamp(widest + 160, W_TEXT, 760)      # 宽度按整首最长一句固定，不随每句变化
        else:
            self._lyr_w = 0.0

    def media_pos(self) -> float:
        m = self.media
        pos = float(m.get("position") or 0)
        if m.get("playing"):
            pos += time.monotonic() - float(m.get("stamp") or time.monotonic())
        dur = float(m.get("duration") or 0)
        return min(pos, dur) if dur > 0 else pos

    def lyric_now(self) -> tuple[int, str, str]:
        """-> (行号, 歌词, 翻译)；没有歌词 / 没在播放时行号为 -1。"""
        if not self.lyrics or not self.media.get("title"):
            return -1, "", ""
        return self.lyrics.at(self.media_pos())

    # --- 实时字幕 ---
    def set_sub_running(self, on: bool):
        self.sub_on = bool(on)
        if not on:
            self.sub_jp = self.sub_zh = ""
            self.sub_t = -100.0

    def set_sub(self, jp: str | None = None, zh: str | None = None):
        if jp is not None:
            self.sub_jp = jp
        if zh is not None:
            self.sub_zh = zh
        self.sub_t = time.monotonic()

    def sub_active(self) -> bool:
        return self.sub_on and bool(self.sub_jp or self.sub_zh) and time.monotonic() - self.sub_t < SUB_HOLD_S

    def set_cover(self, img):
        # 竖排时封面要画到 44px，横排只用 26px：统一生成大一点的圆角图，两边都是缩小显示
        self.cover = rounded_pixmap(img, 52, 12) if img is not None else None

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
        elif self.sub_active():
            m = "subtitle"            # 有人在说话时字幕优先；安静几秒后回到原视图（音乐 / 时间 / 小形象）
            self.activity = None
        else:
            self.activity = None
            m = {"music": "media", "pet": "pet"}.get(self.view, "idle")
        if m != self._mode:
            self._mode = m
            self._mode_t = now
        return m

    def busy(self) -> bool:
        """是否需要持续动画帧。"""
        return self.mode() in ("media", "activity", "pet", "subtitle") or time.monotonic() - self._view_t < 1.4

    # --- 尺寸 ---
    def _badge_count(self) -> int:
        return sum(1 for v in self.badges.values() if v)

    def _idle_kinds(self) -> list[str]:
        """收起时间天气时中间要显示的图标：最近来消息的在前，最多两个。"""
        order = [k for k in self.idle_order if self.badges.get(k)]
        order += [k for k in BADGE_ORDER if k not in order and self.badges.get(k)]
        order += [k for k in self.badges if k not in order and self.badges.get(k)]
        return order[:2]

    def _idle_icons(self, p, rect: QtCore.QRectF, kinds: list[str], a: float, size: float):
        if not kinds:
            return
        gap = 5.0 if size >= 18 else 4.0
        total = len(kinds) * size + (len(kinds) - 1) * gap
        x = rect.center().x() - total / 2
        y = rect.center().y() - size / 2
        for kind in kinds:
            ir = QtCore.QRectF(x, y, size, size)
            pm = kind_pixmap(kind)
            if pm is not None:
                paint_icon_chip(p, ir, pm, a)
            else:
                c = QtGui.QColor(KIND_COLORS.get(kind, KIND_COLORS["other"]))
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QtCore.QPointF(ir.center().x(), ir.center().y()), size / 4, size / 4)
            x += size + gap

    def _v_size(self, m: str) -> tuple[float, float]:
        """贴左 / 右边缘时的竖排尺寸：细长条，整体比上下吸附的胶囊还小一点。"""
        nb = self._badge_count()
        bw = 4 + 5 * nb + 4.5 * (nb - 1) if nb else 0
        if m == "activity":
            a = self.activity or {}
            if a.get("type") in ("volume", "brightness"):
                return 44, 140
            return (max(80, bw), 138) if a.get("sub") else (max(72, bw), 116)
        if m == "subtitle":
            return max(88, bw), 156
        if m == "media":
            if not self.playing() and not self.media.get("title"):
                return max(52, bw), 96
            return max(58, bw), 150
        if m == "pet":
            return max(50, bw), 100
        # idle：时间在上、天气在下；有未读时中间放最多两个图标
        return 42, 100 if self.weather else 86

    def size_hint(self, vertical: bool = False) -> tuple[float, float]:
        m = self.mode()
        if vertical:
            return self._v_size(m)
        nb = sum(1 for v in self.badges.values() if v)
        if m == "activity":
            a = self.activity
            t = a.get("type")
            if t in ("volume", "brightness"):
                return 280, H_ACT
            fm = QtGui.QFontMetrics(ui_font(13, W.DemiBold))
            w = 70 + fm.horizontalAdvance(a.get("title", "")) + QtGui.QFontMetrics(ui_font(12)).horizontalAdvance(a.get("sub", ""))
            return clamp(w + 24, 220, 460), H_ACT
        if m == "subtitle":
            return W_TEXT + nb * 12, H_SUB                        # 固定宽度：说话时不会一直伸缩
        if m == "media":
            if not self.playing() and not self.media.get("title"):
                return 190 + nb * 12, H_MEDIA
            return max(300, self._lyr_w) + 34 + nb * 12, H_MEDIA
        if m == "pet":
            return 150 + nb * 14, H_IDLE
        w = 150 + (64 if self.weather else 0)
        return w, H_IDLE

    # --- 绘制 ---
    def paint(self, p: QtGui.QPainter, r: QtCore.QRectF, alpha: float, peak: float,
              vertical: bool = False):
        if alpha <= 0.01 or r.width() < 40 or r.height() < 24:
            return
        m = self.mode()
        fade = ease_out((time.monotonic() - self._mode_t) / 0.28)
        a = alpha * fade
        self._peak = self._peak * 0.6 + peak * 0.4
        p.save()
        if vertical:
            p.setClipRect(r.adjusted(2, 5, -2, -5), Qt.ClipOperation.IntersectClip)
        else:
            p.setClipRect(r.adjusted(6, 0, -6, 0), Qt.ClipOperation.IntersectClip)
        if m != "media":
            self.play_rect = None
        if vertical:
            self._paint_vertical(p, r, a, m)
        elif m == "activity":
            self._paint_activity(p, r, a)
        elif m == "subtitle":
            self._paint_subtitle(p, r, a)
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

    # --- 竖排（贴左 / 右边时） ---
    def _paint_vertical(self, p, r, a, m):
        if m == "activity":
            self._v_activity(p, r, a)
        elif m == "subtitle":
            self._v_subtitle(p, r, a)
        elif m == "media":
            if not self.media.get("title"):
                self._v_no_media(p, r, a)
            else:
                self._v_media(p, r, a)
        elif m == "pet":
            self._v_pet(p, r, a)
        else:
            self._v_idle(p, r, a)

    def _v_badges(self, p, r, a):
        kinds = [k for k in BADGE_ORDER if self.badges.get(k)]
        kinds += [k for k in self.badges if k not in BADGE_ORDER and self.badges.get(k)]
        if not kinds:
            return
        gap = 4.5
        x = r.center().x() - (len(kinds) - 1) * gap / 2
        y = r.bottom() - 6.5
        for kind in kinds:
            c = QtGui.QColor(KIND_COLORS.get(kind, KIND_COLORS["other"]))
            c.setAlphaF(a)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QtCore.QPointF(x, y), 2.2, 2.2)
            x += gap

    def _v_idle(self, p, r, a):
        """时间在上、天气在下；有未读时中间放最多两个图标。"""
        kinds = self._idle_kinds()
        has_w = bool(self.weather)
        if kinds:
            block = 18 + 6 + 16 + (5 + 14 if has_w else 0)
            y = r.center().y() - block / 2
            self._idle_icons(p, QtCore.QRectF(r.x(), y + 24, r.width(), 16), kinds, a, 16)
            wy = y + 45
        else:
            block = 18 + (19 if has_w else 0)
            y = r.center().y() - block / 2 - 3
            wy = y + 19
        p.setFont(ui_font(12, W.DemiBold, num=True))
        p.setPen(theme.text(a))
        p.drawText(QtCore.QRectF(r.x() + 3, y, r.width() - 6, 18),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   time.strftime("%H:%M"))
        if self.weather:
            glyph, temp = self.weather[0], self.weather[1]
            fam = self.weather[2] if len(self.weather) > 2 else None
            fm = QtGui.QFontMetrics(ui_font(10.5, W.Medium, num=True))
            tw = fm.horizontalAdvance(temp)
            total = 11 + 3 + tw
            x = r.center().x() - total / 2
            p.setPen(theme.text2(a))
            f = icon_font(10.5)
            if fam:
                f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            p.setFont(f)
            p.drawText(QtCore.QRectF(x, wy, 11, 14), Qt.AlignmentFlag.AlignCenter, glyph)
            p.setFont(ui_font(10.5, W.Medium, num=True))
            p.drawText(QtCore.QRectF(x + 14, wy, tw + 2, 14),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, temp)

    def _v_pet(self, p, r, a):
        cx, cy = r.center().x(), r.center().y() - 3
        sx, sy = getattr(self, "squash", (1.0, 1.0))
        amount, rel = getattr(self, "press", (0.0, 0.0))
        amount = clamp(amount, 0.0, 1.2)
        side_w = abs(rel)
        lean = -rel * min(1.0, amount * 1.6)
        self.pet.spread = amount * (1 - side_w) * 7.0
        self.pet.tilt = amount * (1 - side_w) * 12.0
        p.save()
        p.translate(cx, cy)
        p.translate(0, lean * 12.0)          # 贴侧边时按在上下哪一端，就往另一端偏
        p.rotate(lean * 6.0)
        p.scale(sx, sy)
        p.translate(-cx, -cy)
        self.pet.paint(p, cx, cy, 0.62, a, self.look, self.near)
        p.restore()
        self._v_badges(p, r, a)

    def _v_no_media(self, p, r, a):
        p.setFont(icon_font(14))
        p.setPen(theme.text(0.75 * a))
        p.drawText(QtCore.QRectF(r.x(), r.y() + 20, r.width(), 22),
                   Qt.AlignmentFlag.AlignCenter, G.MUSIC)
        p.setFont(ui_font(9.5, W.Medium))
        p.setPen(theme.text2(a))
        p.drawText(QtCore.QRectF(r.x() + 3, r.y() + 42, r.width() - 6, 16),
                   Qt.AlignmentFlag.AlignCenter, "未在播放")
        self._v_badges(p, r, a)

    def _v_media(self, p, r, a):
        cx = r.center().x()
        top = r.y() + 9
        d = 28
        cr = QtCore.QRectF(cx - d / 2, top, d, d)
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
        w = r.width() - 8
        x = r.x() + 4
        idx, line, trans = self.lyric_now()
        title = self.media.get("title", "")
        artist = self.media.get("artist", "")
        txt_y = top + d + 4
        if line:
            p.setFont(ui_font(8.5, W.Medium))
            p.setPen(theme.text2(a))
            _draw_lines(p, x, txt_y, w, _wrap_lines(QtGui.QFontMetrics(p.font()), title, w, 1),
                        10, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            p.setFont(ui_font(9.5, W.Medium))
            p.setPen(theme.text(a))
            _draw_lines(p, x, txt_y + 10, w,
                        _wrap_lines(QtGui.QFontMetrics(p.font()), line, w, 2), 11,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        else:
            p.setFont(ui_font(9.5, W.DemiBold))
            p.setPen(theme.text(a))
            tl = _wrap_lines(QtGui.QFontMetrics(p.font()), title, w, 2)
            _draw_lines(p, x, txt_y, w, tl, 11,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            if artist:
                p.setFont(ui_font(8.5, W.Normal))
                p.setPen(theme.text2(a))
                _draw_lines(p, x, txt_y + 2 + len(tl) * 11, w,
                            _wrap_lines(QtGui.QFontMetrics(p.font()), artist, w, 1), 10,
                            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        # 底部：律动条 + 播放键
        pr = QtCore.QRectF(cx - 11, r.bottom() - 38, 22, 22)
        self.play_rect = pr
        hov = self.play_hover
        if hov > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.hover())
            p.setOpacity(a * hov)
            p.drawEllipse(pr)
            p.setOpacity(1.0)
        p.setFont(icon_font(11))
        p.setPen(theme.text(a))
        p.drawText(pr, Qt.AlignmentFlag.AlignCenter, G.PAUSE if self.media.get("playing") else G.PLAY)
        t = time.monotonic()
        pk = clamp(self._peak * 1.6, 0, 1)
        base = pr.top() - 7
        bx = cx - (4 * 2.6 + 3 * 3.0) / 2
        for i in range(4):
            target = 0.18 + pk * (0.55 + 0.45 * math.sin(t * (7.3 + i * 2.1) + i * 1.7) ** 2)
            self._bars[i] += (target - self._bars[i]) * 0.35
            h = 3 + 9 * self._bars[i]
            br = QtCore.QRectF(bx + i * 5.6, base - h, 2.6, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.text(0.85 * a))
            p.drawRoundedRect(br, 1.3, 1.3)
        self._v_badges(p, r, a)

    def _v_subtitle(self, p, r, a):
        w = r.width() - 8
        x = r.x() + 4
        fj, fz = ui_font(9.5), ui_font(10.5, W.DemiBold)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        jl = _wrap_lines(fmj, self.sub_jp, w, 3, tail=True)
        zl = _wrap_lines(fmz, self.sub_zh, w, 3, tail=True)
        if not zl and not jl:
            return
        lhj, lhz = 12, 13
        total = len(jl) * lhj + (4 if jl and zl else 0) + len(zl) * lhz
        y = r.center().y() - total / 2 - 3
        if jl:
            p.setFont(fj)
            p.setPen(theme.text(0.62 * a))
            _draw_lines(p, x, y, w, jl, lhj,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            y += len(jl) * lhj + 4
        if zl:
            p.setFont(fz)
            p.setPen(theme.text(a))
            _draw_lines(p, x, y, w, zl, lhz,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._v_badges(p, r, a)

    def _v_activity(self, p, r, a):
        act = self.activity or {}
        enter = ease_out((time.monotonic() - act.get("start", 0)) / 0.35)
        if act.get("type") in ("volume", "brightness"):
            self._v_volume(p, r, a, act, enter)
            return
        title = act.get("title", "")
        sub = act.get("sub", "")
        w = r.width() - 8
        p.setFont(ui_font(9.5, W.DemiBold))
        tl = _wrap_lines(QtGui.QFontMetrics(p.font()), title, w, 2)
        zl = []
        if sub:
            p.setFont(ui_font(9))
            zl = _wrap_lines(QtGui.QFontMetrics(p.font()), sub, w, 3)
        d = 22
        block = d + (5 if tl else 0) + len(tl) * 11 + ((2 + len(zl) * 10.5) if zl else 0)
        y0 = r.center().y() - block / 2
        cr = QtCore.QRectF(r.center().x() - d / 2, y0, d, d)
        color = act.get("color")
        img = act.get("icon")
        if isinstance(img, QtGui.QPixmap) and not img.isNull():
            paint_icon_chip(p, cr, img, a)
        else:
            if color is not None:
                c = QtGui.QColor(color)
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(cr)
                p.setPen(QtGui.QColor(255, 255, 255, round(255 * a)))
            else:
                p.setPen(theme.text(a))
            p.setFont(icon_font(10 if color is not None else 12))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, act.get("glyph", G.BELL))
        y = cr.bottom() + 5
        tc = act.get("title_color")
        p.setFont(ui_font(9.5, W.DemiBold))
        p.setPen(QtGui.QColor(tc) if tc else theme.text(a))
        _draw_lines(p, r.x() + 4, y, w, tl, 11,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        if zl:
            p.setFont(ui_font(9))
            sc = act.get("sub_color")
            p.setPen(QtGui.QColor(sc) if sc else theme.text2(a))
            _draw_lines(p, r.x() + 4, y + len(tl) * 11 + 2, w, zl, 10.5,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

    def _v_volume(self, p, r, a, act: dict, enter: float):
        cx = r.center().x()
        v = clamp(act.get("value", 0.0), 0, 1)
        muted = bool(act.get("muted"))
        p.setFont(icon_font(14))
        p.setPen(theme.text(a))
        p.drawText(QtCore.QRectF(r.x(), r.y() + 10, r.width(), 20),
                   Qt.AlignmentFlag.AlignCenter, act.get("glyph", G.VOLUME))
        p.setFont(ui_font(11.5, W.DemiBold, num=True))
        p.setPen(theme.text(a))
        p.drawText(QtCore.QRectF(r.x(), r.y() + 32, r.width(), 16),
                   Qt.AlignmentFlag.AlignCenter, "静音" if muted else str(round(v * 100)))
        track = QtCore.QRectF(cx - 2.5, r.y() + 52, 5, r.height() - 84)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.track())
        p.drawRoundedRect(track, 2.5, 2.5)
        fh = track.height() * v * (0 if muted else enter)
        if fh > 0.5:
            p.setBrush(theme.text(a))
            p.drawRoundedRect(QtCore.QRectF(track.x(), track.bottom() - fh, track.width(), fh), 2.5, 2.5)
        p.setFont(ui_font(9))
        p.setPen(theme.text3(a))
        p.drawText(QtCore.QRectF(r.x(), r.bottom() - 18, r.width(), 12),
                   Qt.AlignmentFlag.AlignCenter, "亮度" if act.get("type") == "brightness" else "音量")


    def _paint_dots(self, p, r, alpha):
        """（已关闭）切换视图时不显示页码小圆点。"""
        return
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

    def _paint_subtitle(self, p, r, a):
        """两行字幕：上面日语（小字，较淡），下面中文（大字）；太长时保留结尾（最新的话）。"""
        x = r.x() + 18
        w = r.width() - 36 - (self._badges_w() if any(self.badges.values()) else 0)
        if any(self.badges.values()):
            self._badges(p, r.right() - 14, r.y() + 16, a)
        cy = r.center().y()
        fj, fz = ui_font(12), ui_font(15, W.DemiBold)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)

        def tail(fm, text):
            text = " ".join((text or "").split())
            if fm.horizontalAdvance(text) <= w:
                return text
            return fm.elidedText(text, Qt.TextElideMode.ElideLeft, int(w))
        jp = tail(fmj, self.sub_jp)
        zh = tail(fmz, self.sub_zh) or "…"
        top = QtCore.QRectF(x, cy - 22, w, 18)
        bot = QtCore.QRectF(x, cy - 3, w, 24)
        p.setFont(fj)
        p.setPen(theme.text(0.62 * a))
        p.drawText(top, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, jp)
        p.setFont(fz)
        p.setPen(theme.text(a))
        p.drawText(bot, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, zh)

    def _badges_w(self) -> float:
        return 12 + 12 * sum(1 for v in self.badges.values() if v)

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
        amount, rel = getattr(self, "press", (0.0, 0.0))
        amount = clamp(amount, 0.0, 1.2)
        side_w = abs(rel)                     # 0 = 按在中间，1 = 按在一端
        lean = -rel * min(1.0, amount * 1.6)   # 向没被按的一侧倾斜
        self.pet.spread = amount * (1 - side_w) * 9.0
        self.pet.tilt = amount * (1 - side_w) * 14.0
        p.save()
        p.translate(cx, cy)
        p.translate(lean * 24.0, 0)            # 明显地偏向没被按的一侧
        p.rotate(lean * 18.0)
        p.scale(sx, sy)                       # 灵动岛被按压时表情一起被压扁
        p.translate(-cx, -cy)
        self.pet.paint(p, cx, cy - 1, 1.0, a, self.look, self.near)
        p.restore()
        x = r.right() - 14
        self._badges(p, x, cy, a)

    def _badges(self, p, right: float, cy: float, a: float) -> float:
        x = right
        order = list(BADGE_ORDER) + [k for k in self.badges if k not in BADGE_ORDER]
        for kind in order:
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
        fm = QtGui.QFontMetrics(p.font())
        tw_time = fm.horizontalAdvance(txt)
        p.drawText(QtCore.QRectF(r.x() + 18, r.y(), tw_time + 4, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
        x = r.right() - 16
        wx = None
        if self.weather:
            glyph, temp = self.weather[0], self.weather[1]
            fam = self.weather[2] if len(self.weather) > 2 else None
            p.setFont(ui_font(13, W.Medium, num=True))
            p.setPen(theme.text2(a))
            fmw = QtGui.QFontMetrics(p.font())
            tw = fmw.horizontalAdvance(temp)
            wx = x - tw - 22
            p.drawText(QtCore.QRectF(x - tw, r.y(), tw + 2, r.height()), Qt.AlignmentFlag.AlignVCenter, temp)
            f = icon_font(13)
            if fam:
                f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            p.setFont(f)
            p.drawText(QtCore.QRectF(wx, r.y(), 20, r.height()), Qt.AlignmentFlag.AlignCenter, glyph)
        kinds = self._idle_kinds()
        right = (wx - 6) if wx is not None else (r.right() - 12)
        left = r.x() + 18 + tw_time + 8
        if kinds and right > left:
            self._idle_icons(p, QtCore.QRectF(left, r.y() + 4, right - left, r.height() - 8), kinds, a, 18)

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
        # 最右侧：播放 / 暂停键（收起时也能直接点）；左边是音频律动条（跟随系统输出峰值）
        right = self._badges(p, r.right() - 14, cy, a) - 4 if any(self.badges.values()) else r.right() - 8
        pr = QtCore.QRectF(right - 28, cy - 14, 28, 28)
        self.play_rect = pr
        hov = self.play_hover
        if hov > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.hover() if hasattr(theme, "hover") else theme.card(1.2))
            p.setOpacity(a * hov)
            p.drawEllipse(pr.adjusted(1, 1, -1, -1))
            p.setOpacity(1.0)
        p.setFont(icon_font(14))
        p.setPen(theme.text(a))
        p.drawText(pr, Qt.AlignmentFlag.AlignCenter, G.PAUSE if self.media.get("playing") else G.PLAY)
        bars_w = 26
        bx = pr.left() - 6 - bars_w
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
        # 中间：当前歌词（有歌词时）或 标题 · 歌手，过长时滚动
        tx = cr.right() + 10
        tw = bx - 10 - tx
        title = self.media.get("title", "")
        artist = self.media.get("artist", "")
        text = f"{title}  ·  {artist}" if artist else title
        idx, line, _tr = self.lyric_now()
        if line:
            text = line
        if idx != self._lyr_idx:                      # 换行：旧行上移淡出，新行从下方淡入
            self._lyr_prev = self._lyr_cur if self._lyr_idx != -2 else ""
            self._lyr_idx, self._lyr_t = idx, t
        self._lyr_cur = text
        k = ease_out((t - self._lyr_t) / 0.32)
        p.setFont(ui_font(13, W.Medium))
        fm = QtGui.QFontMetrics(p.font())
        full = fm.horizontalAdvance(text)
        p.save()
        p.setClipRect(QtCore.QRectF(tx, r.y(), tw, r.height()))
        if k < 1 and self._lyr_prev:
            p.setPen(theme.text(a * (1 - k)))
            p.drawText(QtCore.QRectF(tx, r.y() - 9 * k, tw, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignHCenter if line else Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(self._lyr_prev, Qt.TextElideMode.ElideRight, int(tw)))
        p.translate(0, 9 * (1 - k) if self._lyr_prev else 0)
        p.setPen(theme.text(a * (k if self._lyr_prev else 1.0)))
        t = t - self._lyr_t                           # 滚动从这一行出现时开始计时
        if full <= tw:
            # 歌词居中显示；歌名 · 歌手靠左
            h_al = Qt.AlignmentFlag.AlignHCenter if line else Qt.AlignmentFlag.AlignLeft
            p.drawText(QtCore.QRectF(tx, r.y(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter | h_al, text)
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
        # 左侧品牌图标；没有素材时退回色块 + 字体图标
        d = 26
        cr = QtCore.QRectF(r.x() + 9, cy - d / 2, d, d)
        img = act.get("icon")
        if isinstance(img, QtGui.QPixmap) and not img.isNull():
            paint_icon_chip(p, cr, img, a)
        else:
            if color is not None:
                c = QtGui.QColor(color)
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(cr)
                p.setPen(QtGui.QColor(255, 255, 255, round(255 * a)))
            else:
                p.setPen(theme.text(a))
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
