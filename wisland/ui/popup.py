# -*- coding: utf-8 -*-
"""新消息桌面提示框：圆角卡片、淡入淡出、六种位置、可同时显示多条。

每条提示框都有自己的 3 秒计时，后到的消息不会重置前面提示框的时间。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import cfg
from .brand import kind_pixmap, paint_icon_chip
from .theme import G, KIND_COLORS, W, icon_font, theme, ui_font

Qt = QtCore.Qt

# 位置顺序和设置页保持一致
POPUP_POSITIONS = [
    ("center_bottom", "中心底部"),
    ("center_top", "中心顶部"),
    ("bottom_left", "左下角"),
    ("bottom_right", "右下角"),
    ("top_left", "左上角"),
    ("top_right", "右上角"),
]
POSITION_KEYS = {k for k, _ in POPUP_POSITIONS}

CARD_W, CARD_H = 360, 84
SHADOW = 10
WIN_W, WIN_H = CARD_W + SHADOW * 2, CARD_H + SHADOW * 2
EDGE = 16
GAP = 4
MAX_POPUPS = 5
DURATION_MS = 3000


def _size_scale() -> float:
    try:
        pct = int(cfg.get("notify.popup.size", 100) or 100)
    except (TypeError, ValueError):
        pct = 100
    return max(70, min(160, pct)) / 100.0


class Popup(QtWidgets.QWidget):
    closed = QtCore.Signal()

    def __init__(self, kind: str, title: str, sub: str, screen: QtGui.QScreen | None = None,
                 down: bool = False, scale: float = 1.0):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.kind = kind
        self.title = title or ""
        self.sub = sub or ""
        self.screen = screen or QtGui.QGuiApplication.primaryScreen()
        self.down = down
        self.scale = max(0.5, min(2.0, float(scale)))
        self._vanish = 0.0
        self._vanish_anim = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(max(1, round(WIN_W * self.scale)), max(1, round(WIN_H * self.scale)))
        self._pm = kind_pixmap(kind)
        self._anim = None
        self._move = None
        self._finished = False
        self._hold = QtCore.QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade_out)
        self._theme_slot = self.update
        theme.changed.connect(self._theme_slot)

    def _get_vanish(self):
        return self._vanish

    def _set_vanish(self, v: float):
        self._vanish = float(v)
        self.update()

    vanish = QtCore.Property(float, _get_vanish, _set_vanish)

    # ---------------- 生命周期 ----------------
    def start(self):
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._anim = QtCore.QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._anim.start()
        self._hold.start(DURATION_MS)

    def _fade_out(self):
        if self._finished:
            return
        self._hold.stop()
        if self._anim is not None:
            self._anim.stop()
        # 朝屏幕边缘上移 / 下移，同时缩小并渐隐
        self._vanish_anim = QtCore.QPropertyAnimation(self, b"vanish", self)
        self._vanish_anim.setDuration(260)
        self._vanish_anim.setStartValue(self._vanish)
        self._vanish_anim.setEndValue(1.0)
        self._vanish_anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._vanish_anim.start()
        self._anim = QtCore.QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(260)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.InCubic)
        self._anim.finished.connect(self._done)
        self._anim.start()

    def _done(self):
        if self._finished:
            return
        self._finished = True
        try:
            theme.changed.disconnect(self._theme_slot)
        except (RuntimeError, TypeError):
            pass
        self.closed.emit()
        self.close()
        self.deleteLater()

    def kill_now(self):
        if self._finished:
            return
        self._finished = True
        self._hold.stop()
        if self._anim is not None:
            self._anim.stop()
        if self._vanish_anim is not None:
            self._vanish_anim.stop()
        try:
            theme.changed.disconnect(self._theme_slot)
        except (RuntimeError, TypeError):
            pass
        self.closed.emit()
        self.close()
        self.deleteLater()

    def move_to(self, pos: QtCore.QPoint):
        if self._finished:
            return
        if self._move is not None:
            self._move.stop()
        self._move = QtCore.QPropertyAnimation(self, b"pos", self)
        self._move.setDuration(180)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(pos)
        self._move.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._move.start()

    # ---------------- 绘制 ----------------
    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        # 以默认尺寸绘制，再整体缩放到用户设置的大小
        p.scale(self.width() / WIN_W, self.height() / WIN_H)
        v = max(0.0, min(1.0, self._vanish))
        if v > 0.001:
            # 向上 / 向下移动，并以贴边一侧为锚点缩小
            p.translate(0, (-10.0 if not self.down else 10.0) * v)
            scale = 1.0 - 0.14 * v
            anchor_y = SHADOW if not self.down else SHADOW + CARD_H
            p.translate(WIN_W / 2, anchor_y)
            p.scale(scale, scale)
            p.translate(-WIN_W / 2, -anchor_y)
        card = QtCore.QRectF(SHADOW, SHADOW, CARD_W, CARD_H)
        # 柔和阴影
        for spread, alpha in ((12, 12 if theme.dark else 16), (8, 20 if theme.dark else 26), (4, 30 if theme.dark else 36)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(0, 0, 0, alpha))
            r = card.adjusted(-spread / 2, -spread / 2, spread / 2, spread / 2)
            p.drawRoundedRect(r, 18 + spread / 2, 18 + spread / 2)
        # 背景沿用当前主题的玻璃色调（晴空主题保留蓝→杏渐变）
        pal = theme.pal
        if pal.get("tint2"):
            grad = QtGui.QLinearGradient(card.left(), card.top(), card.right(), card.bottom())
            c1 = QtGui.QColor(*pal["tint"])
            c1.setAlpha(245)
            c2 = QtGui.QColor(*pal["tint2"])
            c2.setAlpha(245)
            grad.setColorAt(0, c1)
            grad.setColorAt(1, c2)
            p.setBrush(QtGui.QBrush(grad))
        else:
            bg = QtGui.QColor(*pal["tint"])
            bg.setAlpha(245)
            p.setBrush(bg)
        p.setPen(QtGui.QPen(QtGui.QColor(*pal["outline"]), 1))
        p.drawRoundedRect(card, 18, 18)
        # 主题的卡片色 + 边缘高光
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(*pal["card"]))
        p.drawRoundedRect(card.adjusted(1, 1, -1, -1), 17, 17)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor(*pal["rim1"]), 1))
        p.drawRoundedRect(card.adjusted(1.5, 1.5, -1.5, -1.5), 16.5, 16.5)
        # 左侧主题强调色
        accent = theme.on_color()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QtCore.QRectF(card.left() + 2.5, card.top() + 16, 3, CARD_H - 32), 1.5, 1.5)
        # 品牌图标
        ir = QtCore.QRectF(card.x() + 14, card.y() + (CARD_H - 38) / 2, 38, 38)
        if self._pm is not None:
            paint_icon_chip(p, ir, self._pm, 1.0)
        else:
            c = KIND_COLORS.get(self.kind, KIND_COLORS["other"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(ir, 10, 10)
            p.setFont(icon_font(16))
            p.setPen(QtGui.QColor("white"))
            p.drawText(ir, Qt.AlignmentFlag.AlignCenter, G.MESSAGE)
        # 标题 / 正文
        tx = ir.right() + 12
        tw = card.right() - tx - 14
        title_c = theme.text()
        sub_c = theme.text2()
        p.setFont(ui_font(13.5, W.DemiBold))
        p.setPen(title_c)
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(tx, card.y() + 14, tw, 20),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self.title, Qt.TextElideMode.ElideRight, int(tw)))
        p.setFont(ui_font(11.5))
        p.setPen(sub_c)
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(tx, card.y() + 40, tw, 20),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self.sub, Qt.TextElideMode.ElideRight, int(tw)))


class MessagePopups:
    """管理同时存在的多条提示框；每条由自己显示 3 秒。"""

    def __init__(self):
        self.items: list[Popup] = []

    @staticmethod
    def _position_key() -> str:
        key = str(cfg.get("notify.popup.position", "bottom_right") or "bottom_right")
        return key if key in POSITION_KEYS else "bottom_right"

    def show(self, kind: str, title: str, sub: str, screen: QtGui.QScreen | None = None):
        if not cfg.get("notify.popup.enabled", True):
            return
        key = self._position_key()
        p = Popup(kind, title, sub, screen,
                  down=key in ("center_bottom", "bottom_left", "bottom_right"),
                  scale=_size_scale())
        p.closed.connect(lambda p=p: self._remove(p))
        self.items.append(p)
        scr = screen or QtGui.QGuiApplication.primaryScreen()
        avail_h = scr.availableGeometry().height() if scr is not None else 1080
        while len(self.items) > MAX_POPUPS:
            old = self.items.pop(0)
            old.kill_now()
        while len(self.items) > 1:
            total = sum(item.height() for item in self.items) + GAP * (len(self.items) - 1)
            if total <= avail_h - 2 * EDGE:
                break
            old = self.items.pop(0)
            old.kill_now()
        self._layout(initial=p)
        p.start()

    def _remove(self, p: Popup):
        if p in self.items:
            self.items.remove(p)
            self._layout()

    def _layout(self, initial: Popup | None = None):
        if not self.items:
            return
        key = self._position_key()
        newest = self.items[-1]
        screen = newest.screen or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        bottom = key in ("center_bottom", "bottom_left", "bottom_right")
        cursor = (avail.bottom() - EDGE) if bottom else (avail.top() + EDGE)
        for p in self.items:
            p.down = bottom
            w, h = p.width(), p.height()
            if bottom:
                y = cursor - h + 1
            else:
                y = cursor
            if key.endswith("_left"):
                x = avail.left() + EDGE
            elif key.endswith("_right"):
                x = avail.right() - EDGE - w + 1
            else:
                x = avail.center().x() - w // 2
            pos = QtCore.QPoint(int(x), int(y))
            if p is initial:
                p.move(pos)
            else:
                p.move_to(pos)
            cursor = (y - GAP) if bottom else (y + h + GAP)

    def close_all(self):
        for p in list(self.items):
            p.kill_now()
        self.items.clear()
