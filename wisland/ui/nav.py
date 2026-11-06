# -*- coding: utf-8 -*-
"""顶部导航：透明顶栏 + 图标组。

- 标签没有背景色和分割线，图标/文字直接落在玻璃上；
- 当前项展开显示文字，并加半透明白色圆角底；其余只留图标；
- 切换时每项宽度都由弹簧驱动（欠阻尼 -> 弹性缓动），底块跟着宽度一起流动；
- 长按空白处 -> drag_requested，用来拖动整个灵动岛。
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from .anim import Spring, clamp
from .theme import W, icon_font, theme, ui_font
from .widgets import IconButton, painter

Qt = QtCore.Qt

ICON_W = 34.0
GAP = 2.0
LONG_PRESS_MS = 380


class NavBar(QtWidgets.QWidget):
    changed = QtCore.Signal(str)
    drag_requested = QtCore.Signal(QtCore.QPoint)

    def __init__(self, items: list[tuple[str, str, str]], parent=None, height: int = 36):
        super().__init__(parent)
        self.items = items                       # (key, glyph, label)
        self.index = 0
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.font_ = ui_font(12.5, W.DemiBold)
        fm = QtGui.QFontMetrics(self.font_)
        self.full_w = [ICON_W + fm.horizontalAdvance(lbl) + 14 for _, _, lbl in items]
        self.w = [Spring(ICON_W, 260, 0.56, 0.05) for _ in items]
        self.hover = [Spring(0, 400, 1.0, 0.01) for _ in items]
        self.badges: dict[str, int] = {}
        self.w[0].snap(self.full_w[0])
        self._hover_i = -1
        self._press_pos: QtCore.QPoint | None = None
        self._lp = QtCore.QTimer(self, singleShot=True, interval=LONG_PRESS_MS, timeout=self._long_press)
        self._t = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._last = time.perf_counter()
        theme.changed.connect(self.update)

        # 右侧按钮（不属于图标组）
        self.extra = QtWidgets.QHBoxLayout()
        self.right_buttons: list[IconButton] = []

    def add_right_button(self, glyph: str, tip: str, cb) -> IconButton:
        b = IconButton(glyph, 30, 13, tip, self)
        b.clicked.connect(cb)
        self.right_buttons.append(b)
        self._place_right()
        return b

    def resizeEvent(self, e):
        self._place_right()

    def _place_right(self):
        x = self.width()
        for b in reversed(self.right_buttons):
            x -= b.width()
            b.move(x, (self.height() - b.height()) // 2)
            x -= 2

    # --- 状态 ---
    def keys(self) -> list[str]:
        return [k for k, _, _ in self.items]

    def set_current(self, key: str, emit: bool = False):
        keys = self.keys()
        if key not in keys:
            return
        i = keys.index(key)
        if i == self.index:
            return
        self.index = i
        for j, s in enumerate(self.w):
            s.to(self.full_w[j] if j == i else ICON_W)
        self._kick()
        if emit:
            self.changed.emit(key)

    def current(self) -> str:
        return self.items[self.index][0]

    def set_badge(self, key: str, n: int):
        if self.badges.get(key, 0) != n:
            self.badges[key] = n
            self.update()

    def _kick(self):
        if not self._t.isActive():
            self._last = time.perf_counter()
            self._t.start()

    def _tick(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._last)
        self._last = now
        rest = True
        for s in self.w + self.hover:
            s.step(dt)
            rest = rest and s.rest
        self.update()
        if rest:
            self._t.stop()

    def _rects(self) -> list[QtCore.QRectF]:
        out = []
        x = 2.0
        h = self.height()
        for s in self.w:
            w = max(ICON_W * 0.7, s.x)
            out.append(QtCore.QRectF(x, 2, w, h - 4))
            x += w + GAP
        return out

    def _hit(self, pos: QtCore.QPointF) -> int:
        for i, r in enumerate(self._rects()):
            if r.contains(pos):
                return i
        return -1

    # --- 交互 ---
    def mouseMoveEvent(self, e):
        i = self._hit(e.position())
        if i != self._hover_i:
            if self._hover_i >= 0:
                self.hover[self._hover_i].to(0)
            if i >= 0:
                self.hover[i].to(1)
            self._hover_i = i
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self._kick()
        if self._press_pos is not None and (e.position().toPoint() - self._press_pos).manhattanLength() > 4:
            # 在空白处按住并移动 -> 立即拖动灵动岛
            self._press_pos = None
            self.drag_requested.emit(QtGui.QCursor.pos())

    def leaveEvent(self, e):
        if self._hover_i >= 0:
            self.hover[self._hover_i].to(0)
            self._hover_i = -1
            self._kick()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        i = self._hit(e.position())
        if i >= 0:
            self.set_current(self.items[i][0], emit=True)
            self._press_pos = None
        else:
            self._press_pos = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        self._lp.stop()
        self._press_pos = None

    def _long_press(self):
        if self._press_pos is not None:
            self._press_pos = None
            self.drag_requested.emit(QtGui.QCursor.pos())

    def wheelEvent(self, e):
        d = -1 if e.angleDelta().y() > 0 else 1
        self.set_current(self.items[int(clamp(self.index + d, 0, len(self.items) - 1))][0], emit=True)

    # --- 绘制 ---
    def paintEvent(self, e):
        p = painter(self)
        rects = self._rects()
        # 当前项底块：半透明白色圆角
        ar = rects[self.index]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.active_pill())
        p.drawRoundedRect(ar, ar.height() / 2, ar.height() / 2)
        for i, (key, glyph, label) in enumerate(self.items):
            r = rects[i]
            active = i == self.index
            # 悬停：极淡的圆
            if not active and self.hover[i].x > 0.01:
                c = theme.hover()
                c.setAlphaF(c.alphaF() * self.hover[i].x * 0.8)
                p.setBrush(c)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            a = 1.0 if active else 0.55 + 0.4 * self.hover[i].x
            p.setFont(icon_font(15))
            p.setPen(theme.pill_text() if active else theme.text(a))
            p.drawText(QtCore.QRectF(r.x(), r.y(), ICON_W, r.height()), Qt.AlignmentFlag.AlignCenter, glyph)
            # 文字：随宽度展开渐显，裁剪在本项矩形内
            extra = self.full_w[i] - ICON_W
            prog = clamp((r.width() - ICON_W) / extra, 0, 1) if extra > 0 else 0
            if prog > 0.02:
                p.save()
                p.setClipRect(r.adjusted(0, 0, -4, 0))
                p.setFont(self.font_)
                p.setPen(theme.pill_text(prog))
                p.drawText(QtCore.QRectF(r.x() + ICON_W - 4, r.y(), extra + 8, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
                p.restore()
            n = self.badges.get(key, 0)
            if n:
                p.setBrush(QtGui.QColor("#ff453a"))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QtCore.QPointF(r.x() + ICON_W / 2 + 7, r.y() + 8), 3.2, 3.2)
