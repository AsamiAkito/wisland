# -*- coding: utf-8 -*-
"""灵动岛主窗口。

几何有三种模式：
- anchored：吸附在某条屏幕边上，贴边一侧与屏幕边缘通过两侧"表面张力"内凹圆角相连；
- drag：长按后跟随鼠标（弹簧跟随产生拖拽惯性），本体按速度变成泪滴形；
         离开原边缘时液桥被拉长、变细，超过阈值后断开，边缘残留的小水滴缩回；
         靠近任意边缘时会重新"伸出"液桥；
- snap：松手后吸附到最近的边，垂直方向欠阻尼弹簧带回弹，冲过边缘时转化为挤压形变。

窗口本身是固定大小的透明分层窗口，跟着本体移动；透明区域不接收鼠标。
毛玻璃 = 截取身后画面（本窗口已排除在截图之外）-> 缩小模糊 -> 纹理画刷填充形状。
"""
from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import cfg
from .anim import Spring, clamp, lerp, smooth
from .compact import CompactView
from .glass import Backdrop, exclude_from_capture
from .nav import NavBar
from .shape import body_path, body_points, deform, drip_path, edge_corners, neck_path, poly_path
from .theme import G, theme

Qt = QtCore.Qt
user32 = ctypes.WinDLL("user32")

WIN_W, WIN_H = 900, 680          # 窗口（逻辑像素），需容纳展开尺寸 + 液桥 + 阴影
EXP_W = 640
PAD = 12
NAV_H = 36
FILLET = 13.0                    # 贴边两侧内凹圆角
BREAK = 64.0                     # 液桥拉断距离
JOIN = 30.0                      # 靠近到这个距离时液桥伸出
EDGE_MARGIN = 6

# 新消息呼吸灯的光带剖面 (t, α)：t = 离吸附边的距离 / 光带厚度。
# 最前面 7% 是贴着边的那根亮线，之后迅速衰减成余光，末端收到 0 —— 渐变到 0 才不会留下一条硬边。
BREATH_STOPS = ((0.00, 1.00), (0.07, 0.92), (0.17, 0.55), (0.30, 0.26), (0.52, 0.09), (1.00, 0.00))
BREATH_BAND = (0.55, 10.0, 34.0)  # 余光厚度：本体厚度 × 比例，再夹到 [最小, 最大] px

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
WM_HOTKEY = 0x0312
HSHELL_FLASH = 0x8006
MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.RegisterWindowMessageW.restype = wintypes.UINT


def parse_hotkey(s: str) -> tuple[int, int] | None:
    parts = [x.strip().lower() for x in (s or "").split("+") if x.strip()]
    if not parts:
        return None
    mods = 0
    vk = 0
    for x in parts:
        if x in MOD:
            mods |= MOD[x]
        elif len(x) == 1 and x.isalnum():
            vk = ord(x.upper())
        elif x.startswith("f") and x[1:].isdigit():
            vk = 0x70 + int(x[1:]) - 1
        elif x == "space":
            vk = 0x20
    return (mods | MOD_NOREPEAT, vk) if vk else None


class _RightClickHook:
    """穿透模式下窗口对鼠标完全透明；用低级鼠标钩子只截获落在灵动岛上的右键。
    只在穿透模式开启时安装，回调对非右键消息立即放行。"""
    WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205

    def __init__(self, island):
        self.island = island
        self.handle = None
        self._proc = None

    def install(self):
        if self.handle:
            return
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        self._u = u
        isl = self.island

        def proc(code, wparam, lparam):
            if code == 0 and wparam == 0x020A:        # WM_MOUSEWHEEL
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                dpr = isl.scr.devicePixelRatio()
                sg = isl.scr.geometry()
                lx = sg.x() + (info.pt.x - sg.x()) / dpr
                ly = sg.y() + (info.pt.y - sg.y()) / dpr
                if isl.isVisible() and not isl.expanded and isl._inside(QtCore.QPoint(round(lx), round(ly))):
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    QtCore.QTimer.singleShot(0, lambda d=delta: isl.wheel_compact.emit(1 if d > 0 else -1))
                    return 1
            if code == 0 and wparam in (self.WM_RBUTTONDOWN, self.WM_RBUTTONUP):
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                # 钩子坐标是物理像素，换算成 Qt 逻辑坐标
                dpr = isl.scr.devicePixelRatio()
                sg = isl.scr.geometry()
                lx = sg.x() + (info.pt.x - sg.x()) / dpr
                ly = sg.y() + (info.pt.y - sg.y()) / dpr
                if isl.isVisible() and isl._inside(QtCore.QPoint(round(lx), round(ly))):
                    if wparam == self.WM_RBUTTONUP:
                        pos = QtCore.QPoint(round(lx), round(ly))
                        QtCore.QTimer.singleShot(0, lambda: isl.menu_requested.emit(pos))
                    return 1          # 吞掉右键，不传给下面的窗口
            return u.CallNextHookEx(self.handle, code, wparam, lparam)

        self._proc = HOOKPROC(proc)
        self.handle = u.SetWindowsHookExW(14, self._proc, k.GetModuleHandleW(None), 0)

    def remove(self):
        if self.handle:
            self._u.UnhookWindowsHookEx(self.handle)
        self.handle = None
        self._proc = None


class _Veil(QtWidgets.QWidget):
    """内容渐显用的"玻璃面纱"：盖在内容上，画和灵动岛完全相同的玻璃，不透明度从 1 降到 0。
    普通子控件、与灵动岛同一次绘制，不会像 QGraphicsOpacityEffect 那样出现没有玻璃底的帧。"""

    def __init__(self, island):
        super().__init__(island)
        self.island = island
        self.alpha = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, e):
        if self.alpha <= 0.002:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(self.alpha)
        p.translate(-self.x(), -self.y())
        self.island._paint_glass(p, self.island.path, crossfade=False)
        p.end()


class _BlankDoubleClick(QtCore.QObject):
    """灵动岛展开后，双击空白区域（不是按钮 / 输入框等控件）也切换 悬停展开 ↔ 长关。"""

    def __init__(self, island):
        super().__init__(island)
        self.island = island

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.Type.MouseButtonDblClick or not isinstance(obj, QtWidgets.QWidget):
            return False
        isl = self.island
        if obj is isl or obj.window() is not isl or ev.button() != Qt.MouseButton.LeftButton:
            return False
        if self._blank(obj, ev):
            isl.toggle_long_off()
            return True
        return False

    @staticmethod
    def _blank(w, ev) -> bool:
        from .nav import NavBar
        from .pages.base import Page
        from .widgets import Card
        if isinstance(w, NavBar):
            return w._hit(ev.position()) < 0
        if isinstance(w, (Page, Card, QtWidgets.QStackedWidget)):
            return True
        if type(w) is QtWidgets.QWidget:                 # 普通容器（内容区、滚动区内层等）
            return True
        p = w.parentWidget()
        if isinstance(p, QtWidgets.QAbstractScrollArea) and w is p.viewport():
            return True
        return False


class Island(QtWidgets.QWidget):
    expanded_changed = QtCore.Signal(bool)
    hotkey = QtCore.Signal(str)
    flash = QtCore.Signal(int)
    wheel_compact = QtCore.Signal(int)
    menu_requested = QtCore.Signal(QtCore.QPoint)
    lock_changed = QtCore.Signal(bool)
    passthrough_changed = QtCore.Signal(bool)
    pin_changed = QtCore.Signal(bool)
    mode_changed = QtCore.Signal(str)

    play_pause_requested = QtCore.Signal()      # 收起态音乐视图的播放 / 暂停键

    def __init__(self, pages: list, peak_fn=None, visible_fn=None):
        # 置顶由 apply_keep_on_top() / _raise_topmost() 通过 SetWindowPos 控制，
        # 不再使用 WindowStaysOnTopHint：这样「开启保持置顶」关闭后，Qt 不会在显示 / 激活时把 TopMost 加回来。
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowTitle("wisland")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(WIN_W, WIN_H)
        self.peak_fn = peak_fn or (lambda: 0.0)

        self.backdrop = Backdrop()
        self.compact = CompactView()
        self.all_pages = list(pages)
        self._visible_fn = visible_fn or (lambda key: True)
        self.pages = [pg for pg in self.all_pages if self._visible_fn(pg.key)]
        pages = self.pages                        # 下面构建导航 / 页面栈只用可见的板块

        # ---- 几何状态 ----
        self.edge: str = cfg.get("island.edge", "top")
        if self.edge not in ("top", "bottom", "left", "right"):
            self.edge = "top"
        self.pos_frac: float = float(cfg.get("island.pos", 0.5))
        self.scr: QtGui.QScreen = self._pick_screen()
        self.mode = "anchored"
        self.expanded = False
        cw, ch = self.compact.size_hint(vertical=self.edge in ("left", "right"))
        self.sw = Spring(cw, 300, 0.62, 0.2)
        self.sh = Spring(ch, 300, 0.62, 0.2)
        self.fx = Spring(0, 900, 0.78, 0.2)
        self.fy = Spring(0, 900, 0.78, 0.2)
        self.along = Spring(0, 260, 0.72, 0.2)
        self.gap = Spring(0, 320, 0.40, 0.2)
        self.neck_edge: str | None = self.edge
        self.neck_s = Spring(1, 520, 1.0, 0.01)
        self.drips: list[dict] = []
        self.content_a = Spring(0, 700, 1.0, 0.01)
        self.compact_a = Spring(1, 700, 1.0, 0.01)
        self.drag_off = QtCore.QPointF()
        self.locks: set[str] = set()
        self._t0 = time.perf_counter()
        self._last = self._t0
        self._last_cap = 0.0
        self._breath_color = QtGui.QColor(255, 255, 255)
        self._breath_t0 = 0.0
        self._breath_t1 = 0.0
        self._win_origin = QtCore.QPoint(0, 0)
        self.path = QtGui.QPainterPath()
        self.body = QtCore.QRectF()
        self._hover_since = None
        self._out_since = None
        self._press: QtCore.QPointF | None = None
        self._fs_hidden = False
        self._prev_fg = None
        self._registered_hotkeys: dict[int, str] = {}
        self._rhook = _RightClickHook(self)
        self._mask_rect = None
        self._floor_t: dict = {}
        self.poke = Spring(0, 380, 0.55, 0.004)
        self._poke_pt = QtCore.QPointF()
        self._poke_sm = QtCore.QPointF()          # 平滑后的按压位置（凹陷跟着鼠标滑动）
        self._floor_on: dict = {}
        self._bd_shown = None
        self._bd_prev = None
        self._bd_fade_t0 = 0.0
        self._shell_msg = 0
        self._dirty = QtCore.QRect()

        # ---- 展开内容 ----
        self.content = QtWidgets.QWidget(self)
        self.nav = NavBar([(pg.key, pg.glyph, pg.title) for pg in pages], self.content, NAV_H)
        self.stack = QtWidgets.QStackedWidget(self.content)
        for pg in pages:
            self.stack.addWidget(pg)
            pg.island = self
        lay = QtWidgets.QVBoxLayout(self.content)
        lay.setSpacing(8)
        self._content_lay = lay
        self._nav_bottom = None
        self._place_nav()
        self.nav.changed.connect(self.show_page)
        self.nav.drag_requested.connect(self.begin_drag)
        self.content.hide()
        self.veil = _Veil(self)
        self._opacity: QtWidgets.QGraphicsOpacityEffect | None = None
        default = cfg.get("island.last_tab") or cfg.get("island.default_tab", "home")
        self._page_key = default if default in self.nav.keys() else self.nav.keys()[0]
        self.nav.set_current(self._page_key)
        self.stack.setCurrentIndex(self.nav.keys().index(self._page_key))

        # ---- 计时器 ----
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.poll = QtCore.QTimer(self, interval=25, timeout=self._poll_hover)
        self.topmost_timer = QtCore.QTimer(self, interval=500, timeout=self._ensure_topmost)
        self.lp_timer = QtCore.QTimer(self, singleShot=True, interval=420, timeout=self._long_press)
        self._click_timer = QtCore.QTimer(self, singleShot=True, timeout=self._single_click)
        self._dbl_filter = _BlankDoubleClick(self)
        QtWidgets.QApplication.instance().installEventFilter(self._dbl_filter)
        self.backdrop.ready.connect(self._on_backdrop)
        theme.changed.connect(self.update)

        self._place_initial()

    # ================================================================ 屏幕 / 位置
    def _pick_screen(self) -> QtGui.QScreen:
        screens = QtGui.QGuiApplication.screens()
        i = int(cfg.get("island.screen", 0) or 0)
        return screens[i] if 0 <= i < len(screens) else QtGui.QGuiApplication.primaryScreen()

    def _avail(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.scr.availableGeometry())

    def _line(self, edge: str, R: QtCore.QRectF | None = None) -> float:
        R = R or self._avail()
        return {"top": R.top(), "bottom": R.bottom() + 1, "left": R.left(), "right": R.right() + 1}[edge]

    def _along_range(self, edge: str, w: float, h: float) -> tuple[float, float]:
        R = self._avail()
        if edge in ("top", "bottom"):
            half = w / 2 + FILLET + EDGE_MARGIN
            return R.left() + half, R.right() + 1 - half
        half = h / 2 + FILLET + EDGE_MARGIN
        return R.top() + half, R.bottom() + 1 - half

    def _anchor_along(self, w: float, h: float) -> float:
        R = self._avail()
        if self.edge in ("top", "bottom"):
            a = R.left() + self.pos_frac * R.width()
        else:
            a = R.top() + self.pos_frac * R.height()
        lo, hi = self._along_range(self.edge, w, h)
        return clamp(a, lo, hi) if lo <= hi else (lo + hi) / 2

    def _center_on_edge(self, edge: str, along: float, gap: float, w: float, h: float) -> QtCore.QPointF:
        line = self._line(edge)
        if edge == "top":
            return QtCore.QPointF(along, line + gap + h / 2)
        if edge == "bottom":
            return QtCore.QPointF(along, line - gap - h / 2)
        if edge == "left":
            return QtCore.QPointF(line + gap + w / 2, along)
        return QtCore.QPointF(line - gap - w / 2, along)

    def _place_initial(self):
        self._update_geometry(force=True)

    # ================================================================ 展开 / 收起
    def expanded_size(self) -> tuple[float, float]:
        """打开时的尺寸（按上次停留的页面的高度）。"""
        pg = self.pages[self.stack.currentIndex()]
        return EXP_W, PAD - 2 + NAV_H + 8 + pg.preferred_height() + PAD

    def target_size(self) -> tuple[float, float]:
        if self.expanded:
            pg = self.pages[self.stack.currentIndex()]
            h = PAD - 2 + NAV_H + 8 + pg.preferred_height() + PAD
            return EXP_W, h
        return self.compact.size_hint(vertical=self.edge in ("left", "right"))

    def expand(self, page: str | None = None):
        if self.mode != "anchored" or self._fs_hidden:
            return
        # 悬停展开前先压到置顶层最前面：避免被其他置顶窗口盖住，只有点击后才浮上来
        self._raise_topmost()
        if not self.expanded:
            # 打开时停留在上次收起前的页面（热键 / 托盘等指定了页面的除外）
            self.show_page(page or self._page_key)
        elif page:
            self.show_page(page)
        if not self.expanded:
            self.expanded = True
            self.compact_a.to(0, 900, 1.0)
            self.poke.snap(0.0)                       # 展开时清掉按压形变
            w, h = self.target_size()
            self.sw.to(w, 420, 0.62)
            self.sh.to(h, 420, 0.62)
            self.content_a.snap(0)
            tw, th = self.target_size()
            self.content.setGeometry(self._content_rect(tw, th))   # 先隐藏，形状长大后再显示
            if self.passthrough():
                self._apply_passthrough()
            self._defer_show(self.pages[self.stack.currentIndex()])
            self.expanded_changed.emit(True)
        self._kick()

    def collapse(self):
        if not self.expanded or self.pinned():     # 图钉：保持展开
            return
        self.expanded = False
        self.content_a.snap(0)
        self.content.hide()                   # 立即隐藏内容，玻璃形状再收回胶囊
        self.veil.hide()
        self.release_focus()
        if self.passthrough():
            self._apply_passthrough()
        self.locks.discard("pin")
        self.pages[self.stack.currentIndex()].on_hide()
        if cfg.get("island.last_tab") != self._page_key:
            cfg.set("island.last_tab", self._page_key)          # 下次（包括重启后）打开仍是这一页
        self.expanded_changed.emit(False)
        self._kick()

    def toggle(self, page: str | None = None):
        if self.expanded and (page is None or page == self._page_key):
            if self.pinned():
                self.set_pinned(False)
            self.collapse()
        else:
            self.expand(page)
            self.locks.add("pin")

    def show_page(self, key: str):
        keys = self.nav.keys()
        if key not in keys:
            return
        old = self.stack.currentIndex()
        i = keys.index(key)
        self._page_key = key
        self.nav.set_current(key)
        if i != old:
            if self.expanded:
                self.pages[old].on_hide()
            self.stack.setCurrentIndex(i)
            if self.expanded:
                self._defer_show(self.pages[i])
        if self.expanded:
            w, h = self.target_size()
            self.sw.to(w, 440, 0.6)
            self.sh.to(h, 440, 0.6)
        self._kick()

    def _defer_show(self, pg):
        """页面的数据刷新稍后执行，让形变动画先跑起来。"""
        QtCore.QTimer.singleShot(40, lambda: pg.on_show() if self.expanded and self.pages[self.stack.currentIndex()] is pg else None)

    def apply_page_visibility(self):
        """按 visible_fn 重建标签组：运行时开启 / 隐藏某个板块（如「股票」）。"""
        vis = [pg for pg in self.all_pages if self._visible_fn(pg.key)]
        if [pg.key for pg in vis] == [pg.key for pg in self.pages]:
            return
        cur = self._page_key
        before = self.stack.currentWidget()
        removed = False
        for pg in list(self.pages):
            if pg in vis:
                continue
            removed = True
            if before is pg:
                pg.on_hide()
            self.stack.removeWidget(pg)
            pg.island = None
        if removed:
            self.release_focus()
        for i, pg in enumerate(vis):
            if self.stack.indexOf(pg) < 0:
                self.stack.insertWidget(i, pg)
            pg.island = self
        self.pages = vis
        self.nav.set_items([(pg.key, pg.glyph, pg.title) for pg in vis])
        if cur not in self.nav.keys():
            cur = vis[0].key
            cfg.set("island.last_tab", cur, save=False)
        self._page_key = cur
        self.nav.set_current(cur)
        self.stack.setCurrentWidget(next(pg for pg in vis if pg.key == cur))
        self._floor_t.clear()
        self._floor_on.clear()
        if self.expanded:
            w, h = self.target_size()
            self.sw.to(w, 440, 0.6)
            self.sh.to(h, 440, 0.6)
            if self.stack.currentWidget() is not before:
                self._defer_show(self.stack.currentWidget())
        self._kick()

    def page_size_changed(self):
        """页面内容高度变化时由页面调用。"""
        if self.expanded:
            w, h = self.target_size()
            self.sh.to(h, 440, 0.62)
            self._kick()

    def activity(self, act: dict, duration: float = 2.4):
        self.compact.show_activity(act, duration)
        self._kick()

    def breath(self, color, duration: float = 5.0):
        """新消息呼吸灯：沿吸附边亮起，持续 duration 秒后自动消失。

        灯还亮着时又来一条：保持当前的呼吸相位，只换成最新的颜色、把结束时间往后推。
        不然每次都要重新淡入，连收几条消息时灯会先暗半拍再亮起，看着一闪一闪。
        """
        now = time.perf_counter()
        self._breath_color = QtGui.QColor(color)
        if now >= self._breath_t1:
            self._breath_t0 = now
        self._breath_t1 = now + max(0.2, float(duration))
        self._kick()
        self.update()

    def _set_content_opacity(self, a: float):
        """不再使用 QGraphicsOpacityEffect：在透明分层窗口里，带效果的子控件会单独重绘，
        偶尔出现"有内容、没有玻璃底"的帧。内容改为：形状长到约 70% 后直接显示（被形状裁剪），收起时立即隐藏。"""
        return

    # ================================================================ 拖动
    def begin_drag(self, gpos: QtCore.QPoint | None = None):
        if self.mode == "drag" or self.passthrough():
            return
        gpos = QtCore.QPointF(gpos or QtGui.QCursor.pos())
        c = self.body.center() + QtCore.QPointF(self._win_origin)
        if self.expanded:
            self.expanded = False
            self.content_a.snap(0)
            self._set_content_opacity(0)
            self.content.hide()
            self.release_focus()
            self.locks.discard("pin")
            self.pages[self.stack.currentIndex()].on_hide()
            self.expanded_changed.emit(False)
            # 收成胶囊，中心向鼠标靠拢
            c = QtCore.QPointF(gpos)
        self.compact_a.to(1, 500, 1.0)
        cw, ch = self.compact.size_hint(vertical=self.edge in ("left", "right"))
        self.sw.to(cw, 300, 0.55)
        self.sh.to(ch, 300, 0.55)
        self.mode = "drag"
        self.fx.snap(c.x())
        self.fy.snap(c.y())
        self.drag_off = c - gpos
        if not (self.body.contains(QtCore.QPointF(gpos) - QtCore.QPointF(self._win_origin))):
            self.drag_off = QtCore.QPointF(0, 0)
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self._kick()

    def _end_drag(self):
        QtWidgets.QApplication.restoreOverrideCursor()
        c = QtCore.QPointF(self.fx.x, self.fy.x)
        scr = QtGui.QGuiApplication.screenAt(c.toPoint()) or self.scr
        self.scr = scr
        R = self._avail()
        w, h = self.sw.target, self.sh.target
        body = QtCore.QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
        gaps = {
            "top": body.top() - R.top(),
            "bottom": R.bottom() + 1 - body.bottom(),
            "left": body.left() - R.left(),
            "right": R.right() + 1 - body.right(),
        }
        # 横向的边更"吸"一点（胶囊横放时贴左右边比较少见）
        weights = {"top": 1.0, "bottom": 1.0, "left": 1.35, "right": 1.35}
        edge = min(gaps, key=lambda k: gaps[k] * weights[k])
        self.edge = edge
        horiz = edge in ("top", "bottom")
        along0 = c.x() if horiz else c.y()
        v_along = self.fx.v if horiz else self.fy.v
        # 速度投影：朝边缘运动 => gap 减小
        v_toward = (-self.fy.v if edge == "top" else self.fy.v if edge == "bottom"
                    else -self.fx.v if edge == "left" else self.fx.v)
        self.along.snap(along0)
        self.along.v = v_along
        lo, hi = self._along_range(edge, w, h)
        self.along.to(clamp(along0, lo, hi), 240, 0.74)
        self.gap.snap(max(-4.0, gaps[edge]))
        self.gap.v = -v_toward
        self.gap.to(0, 300, 0.38)
        self.mode = "snap"
        if self.neck_edge and self.neck_edge != edge:
            self._break_neck()
        self._kick()

    def _break_neck(self):
        if not self.neck_edge:
            return
        e = self.neck_edge
        horiz = e in ("top", "bottom")
        c = self.body.center() + QtCore.QPointF(self._win_origin)
        s = Spring(7.5, 160, 0.45, 0.05)
        s.to(0)
        self.drips.append({"edge": e, "line": self._line(e), "c": c.x() if horiz else c.y(), "r": s})
        self.neck_edge = None
        self.neck_s.snap(0)

    # ================================================================ 每帧
    def _kick(self):
        if not self.timer.isActive():
            self._last = time.perf_counter()
            self.timer.start()

    def _needs_frames(self) -> bool:
        if time.perf_counter() < self._breath_t1:
            return True
        if self.mode != "anchored" or self.drips or self._bd_prev is not None:
            return True
        for s in (self.sw, self.sh, self.content_a, self.compact_a, self.neck_s):
            if not s.rest:
                return True
        if not self.expanded and not self._fs_hidden:
            return True                  # 收起态：常态动画（圆角呼吸 + 边缘流光）持续运行
        return False

    def _tick(self):
        now = time.perf_counter()
        dt = min(1 / 30, max(1 / 240, now - self._last))
        self._last = now

        if self.mode == "drag":
            if not (user32.GetAsyncKeyState(0x01) & 0x8000):
                self._end_drag()
            else:
                cp = QtCore.QPointF(QtGui.QCursor.pos()) + self.drag_off
                self.fx.to(cp.x())
                self.fy.to(cp.y())
        if not self.expanded:
            cw, ch = self.compact.size_hint(vertical=self.edge in ("left", "right"))
            if abs(self.sw.target - cw) > 0.5 or abs(self.sh.target - ch) > 0.5:
                if self.content_a.x < 0.12:
                    self.sw.to(cw, 460, 0.64)
                    self.sh.to(ch, 460, 0.64)
                    if self.compact_a.target < 1:
                        self.compact_a.to(1, 420, 1.0)
        else:
            w, h = self.target_size()
            # 形状长到完整尺寸后才显示内容（之后不会再缩到比内容小），全程不需要遮罩
            if self.sh.x >= h - 1 and self.sw.x >= w - 1 and self.content_a.target < 1:
                self.content_a.snap(0)
                self.content_a.to(1, 700, 1.0)
                tw, th = self.target_size()
                self.content.setGeometry(self._content_rect(tw, th, self.body))
                self.content.show()
                self.veil.setGeometry(self.content.geometry())
                self.veil.alpha = 1.0
                self.veil.show()
                self.veil.raise_()

        for s in (self.sw, self.sh, self.fx, self.fy, self.along, self.gap, self.neck_s,
                  self.content_a, self.compact_a, self.poke):
            s.step(dt)
        if abs(self.poke.x) > 0.01 or self.poke.target:
            if self._poke_sm.isNull():
                self._poke_sm = QtCore.QPointF(self._poke_pt)
            self._poke_sm += (self._poke_pt - self._poke_sm) * 0.3
        if not self.expanded:
            # 小形象看向光标；靠近时开心
            c = self.body.center() + QtCore.QPointF(self._win_origin)
            rel = QtCore.QPointF(QtGui.QCursor.pos()) - c
            self.compact.look = rel
            self.compact.near = abs(rel.x()) < self.body.width() / 2 + 40 and abs(rel.y()) < self.body.height() / 2 + 40
        if self.expanded:
            # 展开态：越过目标后不再回落到目标以下（保留过冲回弹，但不会比内容小）
            for sp, key in ((self.sw, "w"), (self.sh, "h")):
                if self._floor_t.get(key) != sp.target:
                    self._floor_t[key] = sp.target
                    self._floor_on[key] = sp.x >= sp.target
                if sp.x >= sp.target:
                    self._floor_on[key] = True
                elif self._floor_on.get(key):
                    sp.x, sp.v = sp.target, 0.0
        for d in self.drips:
            d["r"].step(dt)
        self.drips = [d for d in self.drips if not (d["r"].rest or d["r"].x < 0.3 and d["r"].v < 0)]

        if self.mode == "snap" and self.along.rest and self.gap.rest:
            self.mode = "anchored"
            self._place_nav()
            if self.pinned():
                QtCore.QTimer.singleShot(80, self.expand)
            R = self._avail()
            horiz = self.edge in ("top", "bottom")
            self.pos_frac = clamp(((self.along.x - R.left()) / R.width()) if horiz
                                  else ((self.along.x - R.top()) / R.height()), 0, 1)
            cfg.set("island.edge", self.edge, save=False)
            cfg.set("island.pos", round(self.pos_frac, 4), save=False)
            screens = QtGui.QGuiApplication.screens()
            cfg.set("island.screen", screens.index(self.scr) if self.scr in screens else 0)
            self.neck_edge = self.edge
            self.neck_s.to(1)

        # 收起时：内容淡出完成后隐藏
        if not self.expanded and self.content.isVisible() and self.content_a.x < 0.05:
            self.content.hide()
            self._set_content_opacity(0)
        if self.veil.isVisible():
            a = clamp(1.0 - self.content_a.x, 0.0, 1.0)
            if a <= 0.01 or not self.content.isVisible():
                self.veil.hide()
            else:
                self.veil.alpha = a
                if self.veil.geometry() != self.content.geometry():
                    self.veil.setGeometry(self.content.geometry())
                self.veil.update()

        self._update_geometry()
        if not self._needs_frames() and self.mode == "anchored":
            self.timer.stop()
            self.timer.setInterval(16)
        elif self.mode == "anchored" and self.sw.rest and self.sh.rest and not self.expanded:
            # 只有常态动画：表情 / 音乐律动 / 提醒时 60fps，空闲（只有圆角呼吸）时 30fps
            self.timer.setInterval(16 if self.compact.busy() else 33)
        else:
            self.timer.setInterval(16)

    # ================================================================ 形状计算
    def _nearest_edge(self, body: QtCore.QRectF) -> tuple[str, float]:
        R = self._avail()
        gaps = {
            "top": body.top() - R.top(),
            "bottom": R.bottom() + 1 - body.bottom(),
            "left": body.left() - R.left(),
            "right": R.right() + 1 - body.right(),
        }
        e = min(gaps, key=gaps.get)
        return e, gaps[e]

    @staticmethod
    def _gap_to(edge: str, body: QtCore.QRectF, line: float) -> float:
        return {"top": body.top() - line, "bottom": line - body.bottom(),
                "left": body.left() - line, "right": line - body.right()}[edge]

    def _update_geometry(self, force: bool = False):
        w, h = max(40.0, self.sw.x), max(24.0, self.sh.x)
        vx = vy = 0.0
        squash = 0.0
        if self.mode == "anchored":
            pk = clamp(self.poke.x, -0.5, 1.2)
            if abs(pk) > 0.002 and not self.expanded:
                # 可穿透时被鼠标"按住"：整颗水滴朝屏幕边压扁、略微变宽，松开后带回弹
                if self.edge in ("top", "bottom"):
                    h, w = h * (1 - 0.16 * pk), w * (1 + 0.05 * pk)
                else:
                    w, h = w * (1 - 0.16 * pk), h * (1 + 0.05 * pk)
            a = self._anchor_along(w, h)
            c = self._center_on_edge(self.edge, a, 0, w, h)
            self.neck_edge = self.edge
        elif self.mode == "snap":
            g = self.gap.x
            if g < 0:                       # 冲过边缘 -> 挤压
                squash = -g
                g = 0.0
            horiz = self.edge in ("top", "bottom")
            if horiz:
                h2 = max(h * 0.62, h - squash)
                w = w + squash * 1.1
                h = h2
            else:
                w2 = max(w * 0.62, w - squash)
                h = h + squash * 1.1
                w = w2
            c = self._center_on_edge(self.edge, self.along.x, g, w, h)
            va, vg = self.along.v, self.gap.v
            vx, vy = {"top": (va, vg), "bottom": (va, -vg), "left": (vg, va), "right": (-vg, va)}[self.edge]
            if self.neck_edge is None and g < JOIN:
                self.neck_edge = self.edge
                self.neck_s.snap(0)
                self.neck_s.to(1, 520, 1.0)
        else:  # drag
            c = QtCore.QPointF(self.fx.x, self.fy.x)
            vx, vy = self.fx.v, self.fy.v
        body = QtCore.QRectF(c.x() - w / 2, c.y() - h / 2, w, h)

        if self.mode == "drag":
            if self.neck_edge:
                gn = self._gap_to(self.neck_edge, body, self._line(self.neck_edge))
                if gn > BREAK:
                    self._break_neck()
            if not self.neck_edge:
                e, g = self._nearest_edge(body)
                if g < JOIN:
                    self.neck_edge = e
                    self.neck_s.snap(0)
                    self.neck_s.to(1, 520, 1.0)

        # ---- 窗口位置：让本体大致居中，并夹在屏幕内（避免跨屏缩放） ----
        sg = self.scr.geometry()
        if self.mode == "anchored":
            # 吸附时窗口贴住吸附边、位置固定：展开 / 收起 / 切页都不移动窗口
            # （移动分层窗口与重绘不是原子操作，会出现内容错位、没有玻璃底的帧）
            R = self._avail()
            line = self._line(self.edge)
            if self.edge in ("top", "bottom"):
                a = R.left() + self.pos_frac * R.width()
                cx_, cy_ = a, (line - 4 + WIN_H / 2) if self.edge == "top" else (line + 4 - WIN_H / 2)
            else:
                a = R.top() + self.pos_frac * R.height()
                cx_, cy_ = ((line - 4 + WIN_W / 2) if self.edge == "left" else (line + 4 - WIN_W / 2)), a
            wx = int(clamp(round(cx_ - WIN_W / 2), sg.left(), sg.right() + 1 - WIN_W))
            wy = int(clamp(round(cy_ - WIN_H / 2), sg.top(), sg.bottom() + 1 - WIN_H))
        else:
            wx = int(clamp(round(c.x() - WIN_W / 2), sg.left(), sg.right() + 1 - WIN_W))
            wy = int(clamp(round(c.y() - WIN_H / 2), sg.top(), sg.bottom() + 1 - WIN_H))
        origin = QtCore.QPoint(wx, wy)
        if origin != self._win_origin or force:
            self._win_origin = origin
            self.move(origin)
        off = QtCore.QPointF(-wx, -wy)
        lbody = body.translated(off)

        # ---- 半径 ----
        base_r = min(h / 2, w / 2, 30.0)
        if self.expanded or h > 60:
            base_r = min(30.0, h / 2, w / 2)
        t_now = time.perf_counter() - self._t0
        fillet = FILLET
        if self.mode == "anchored" and not self.expanded:
            fillet *= 1.0 + 0.07 * math.sin(t_now * 1.7)            # 常态：圆角轻微"呼吸"
        fillet += min(12.0, (abs(self.sw.v) + abs(self.sh.v)) * 0.012)   # 形变越快，液面拉得越开

        path = QtGui.QPainterPath()
        neck = None
        gap_n = 0.0
        if self.neck_edge:
            ne = self.neck_edge
            line = self._line(ne)
            gap_n = max(0.0, self._gap_to(ne, body, line))
            t = clamp(gap_n / BREAK, 0, 1)
            corner_r = base_r * smooth(t * 1.8)
            corner_r = lerp(base_r, corner_r, self.neck_s.x)
            radii = tuple(corner_r if flag else base_r for flag in edge_corners(ne))
            horiz = ne in ("top", "bottom")
            along_c = body.center().x() if horiz else body.center().y()
            length = w if horiz else h
            lline = line + (-wy if horiz else -wx)
            neck = neck_path(ne, lline, along_c + (-wx if horiz else -wy), length, gap_n, t,
                             self.neck_s.x, fillet, corner_r)
        else:
            radii = (base_r,) * 4

        speed = math.hypot(vx, vy)
        playing_wave = (self.mode == "anchored" and not self.expanded and self.compact.mode() == "media"
                        and self.compact.playing())
        poking = self.mode == "anchored" and abs(self.poke.x) > 0.01
        if speed > 30 or playing_wave or poking:
            pts = body_points(lbody, radii, 4.0)
            wave = None
            poke_params = None
            if poking:
                # 按压：沿"远离屏幕边"的方向整体位移（不是沿中心放射方向），外侧半边（含两端圆角）都参与
                ppt = self._poke_sm - QtCore.QPointF(wx, wy)
                amp = clamp(self.poke.x, -0.6, 1.2) * 8.0
                farv = {"top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0)}[self.edge]
                horiz = farv[0] == 0
                c_u = lbody.center().x() if horiz else lbody.center().y()
                half = max(20.0, (lbody.width() if horiz else lbody.height()) / 2)
                depth = lbody.height() if horiz else lbody.width()
                u0 = clamp((ppt.x() if horiz else ppt.y()), c_u - half, c_u + half)
                sig = half * 0.5
                near_line = {"top": lbody.top(), "bottom": lbody.bottom(), "left": lbody.left(), "right": lbody.right()}[self.edge]
                poke_params = (amp, farv, horiz, u0, sig, near_line, depth)
            else:
                poke_params = None
            if playing_wave:
                pk = clamp(self.peak_fn() * 1.5, 0, 1)
                far = {"top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0)}[self.edge]
                tt = t_now

                prev = wave

                def wave(px, py, nx, ny, pk=pk, far=far, tt=tt, prev=prev):
                    extra = prev(px, py, nx, ny) if prev else 0.0
                    wgt = max(0.0, nx * far[0] + ny * far[1])
                    if wgt < 0.3:
                        return extra
                    u = px if far[0] == 0 else py
                    return extra + pk * 2.4 * wgt * wgt * math.sin(u * 0.09 - tt * 7.0)
            pts = deform(pts, lbody.center().x(), lbody.center().y(), vx, vy,
                         min(w, h) / 2 + 12, wave)
            if poke_params is not None:
                amp, farv, horiz, u0, sig, near_line, depth = poke_params
                out = []
                for px, py in pts:
                    v = abs((py if horiz else px) - near_line)          # 离屏幕边的距离
                    wgt = clamp((v - depth * 0.3) / (depth * 0.7), 0.0, 1.0)
                    if wgt > 0:
                        d = ((px if horiz else py) - u0) / sig
                        g = math.exp(-0.5 * d * d)
                        disp = amp * wgt * wgt * (-g + 0.35 * (1.0 - g))   # 光标处凹进，其余略鼓（体积守恒）
                        px += farv[0] * disp
                        py += farv[1] * disp
                    out.append((px, py))
                pts = out
            body_p = poly_path(pts)
        else:
            body_p = body_path(lbody, radii)
        path = body_p
        if neck is not None and not neck.isEmpty():
            path = path.united(neck)
        for d in self.drips:
            e = d["edge"]
            horiz = e in ("top", "bottom")
            lline = d["line"] + (-wy if horiz else -wx)
            dp = drip_path(e, lline, d["c"] + (-wx if horiz else -wy), max(0.0, d["r"].x))
            if not dp.isEmpty():
                path = path.united(dp)
        path.setFillRule(Qt.FillRule.WindingFill)

        old = self.path.boundingRect().toAlignedRect()
        self.path = path
        self.body = lbody
        new = path.boundingRect().toAlignedRect()
        dirty = old.united(new).adjusted(-16, -16, 16, 16)

        # ---- 内容区 ----
        if self.content.isVisible() and self.expanded:   # 收起淡出时保持原尺寸，不再挤压内容
            tw, th = self.target_size()
            cr = self._content_rect(tw, th, lbody)
            if cr != self.content.geometry():
                self.content.setGeometry(cr)
            vis = lbody.toAlignedRect().intersected(cr).translated(-cr.topLeft())
            # 只在遮罩真正变化时设置：每帧 setMask/clearMask 会让内容单独重绘一遍，出现没有玻璃底的帧
            want = vis if vis.size() != cr.size() else None
            if want != self._mask_rect:
                self._mask_rect = want
                if want is None:
                    self.content.clearMask()
                else:
                    self.content.setMask(QtGui.QRegion(want))

        # ---- 毛玻璃采样（后台线程）：覆盖当前形状 + 目标形状，一次截图够整段展开动画用 ----
        self._request_backdrop()
        self.update(dirty)

    def _content_rect(self, tw, th, lbody: QtCore.QRectF | None = None) -> QtCore.QRect:
        """内容区：贴着吸附边、沿边居中（窗口坐标）。"""
        if lbody is None:
            c = self._center_on_edge(self.edge, self._anchor_along(tw, th), 0, tw, th) - QtCore.QPointF(self._win_origin)
            return QtCore.QRect(round(c.x() - tw / 2), round(c.y() - th / 2), round(tw), round(th))
        cr = QtCore.QRect(round(lbody.center().x() - tw / 2), round(lbody.top()), round(tw), round(th))
        if self.edge == "bottom" and self.mode == "anchored":
            cr.moveBottom(round(lbody.bottom()))
        return cr

    def _place_nav(self):
        """吸附在屏幕底边时，导航栏放到面板最下方（靠近吸附边），其余位置放在最上方。"""
        bottom = self.edge == "bottom"
        if bottom == self._nav_bottom:
            return
        self._nav_bottom = bottom
        lay = self._content_lay
        lay.removeWidget(self.nav)
        lay.removeWidget(self.stack)
        if bottom:
            lay.setContentsMargins(PAD, PAD, PAD, PAD - 2)
            lay.addWidget(self.stack, 1)
            lay.addWidget(self.nav)
        else:
            lay.setContentsMargins(PAD, PAD - 2, PAD, PAD)
            lay.addWidget(self.nav)
            lay.addWidget(self.stack, 1)

    def _request_backdrop(self):
        moving = self.mode != "anchored" or not (self.sw.rest and self.sh.rest)
        if self.mode == "anchored":
            tw, th = self.target_size()
            tc = self._center_on_edge(self.edge, self._anchor_along(tw, th), 0, tw, th)
            g = QtCore.QRectF(tc.x() - tw / 2, tc.y() - th / 2, tw, th).toAlignedRect()
            if not self.expanded:
                # 收起时也预先截取展开尺寸的背景：打开的第一帧就有正确的玻璃，不会闪
                ew, eh = self.expanded_size()
                ec = self._center_on_edge(self.edge, self._anchor_along(ew, eh), 0, ew, eh)
                g = g.united(QtCore.QRectF(ec.x() - ew / 2, ec.y() - eh / 2, ew, eh).toAlignedRect())
            if not (self.sw.rest and self.sh.rest):
                g = g.united(self.body.toAlignedRect().translated(self._win_origin))
            pad = 40                      # 覆盖两侧表面张力圆角
        else:
            g = self.path.boundingRect().toAlignedRect().translated(self._win_origin)
            pad = 90
        g = g.adjusted(-pad, -pad, pad, pad)
        q = 16                            # 对齐网格：区域不随轮廓的细微变化而抖动
        x0, y0 = (g.left() // q) * q, (g.top() // q) * q
        x1, y1 = -((-(g.right() + 1)) // q) * q, -((-(g.bottom() + 1)) // q) * q
        g = QtCore.QRect(x0, y0, x1 - x0, y1 - y0)
        if moving:
            interval = 1 / 40
        else:
            fps = max(2.0, float(cfg.get("island.blur_fps", 10)))
            interval = 1 / (fps if self.expanded else min(fps, 3.0))   # 收起静止时身后画面变化不敏感
            if self.capture_visible():
                # 参与截图时，每次取背景都要临时把自己排除约 17ms：静止时降低频率，
                # 让截图 / 录屏几乎总能拍到灵动岛（动画、拖动时仍高频刷新）
                interval = 1 / (1.5 if self.expanded else 1.0)
        need = self.path.boundingRect().toAlignedRect().translated(self._win_origin).intersected(self.scr.geometry())
        self.backdrop.request(self.scr, g, interval, urgent=not self.backdrop.covers(need))

    def _on_backdrop(self, changed: bool):
        theme.feed_luminance(self.backdrop.luminance)
        if not changed:
            return
        if self._bd_shown is not None and self._bd_shown[0] is not self.backdrop.state[0]:
            self._bd_prev = self._bd_shown            # 旧背景淡出、新背景淡入
            self._bd_fade_t0 = time.perf_counter()
        self._kick()

    # ================================================================ 绘制
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        path = self.path
        if path.isEmpty():
            return
        # 1) 柔和阴影（几层半透明描边，廉价近似）
        sp = path.translated(0, 1.5)
        for wdt, a in ((14, 5), (8, 9), (4, 14)):
            p.strokePath(sp, QtGui.QPen(QtGui.QColor(0, 0, 0, a), wdt))
        self._paint_glass(p, path)
        bb = self.body
        # 4) 轮廓：外侧极淡暗线 + 内侧高光边
        p.strokePath(path, QtGui.QPen(theme.outline(), 1.6))
        rg = QtGui.QLinearGradient(0, bb.top(), 0, bb.bottom())
        rg.setColorAt(0, theme.rim_top())
        rg.setColorAt(1, theme.rim_bottom())
        p.strokePath(path, QtGui.QPen(QtGui.QBrush(rg), 1.0))
        self._paint_breath(p, path, bb)
        self._paint_rest(p, path, bb)

    def _paint_glass(self, p: QtGui.QPainter, path: QtGui.QPainterPath, crossfade: bool = True):
        """毛玻璃底 + 色调 + 光泽（灵动岛本体与内容渐显面纱共用）。"""
        base_opacity = p.opacity()
        # 2) 毛玻璃底（新截图与上一张交叉淡入，避免跳变）
        state = self.backdrop.state
        br = self.backdrop.brush_for(state, self._win_origin)
        fade = (time.perf_counter() - self._bd_fade_t0) / 0.25 if self._bd_prev is not None else 1.0
        if fade < 1.0:
            prev = self.backdrop.brush_for(self._bd_prev, self._win_origin)
            if prev is not None:
                p.fillPath(path, prev)
            if br is not None:
                p.setOpacity(base_opacity * max(0.0, fade))
                p.fillPath(path, br)
                p.setOpacity(base_opacity)
        else:
            if crossfade:
                self._bd_prev = None
            if br is not None:
                p.fillPath(path, br)
            else:
                p.fillPath(path, QtGui.QColor(128, 128, 128, 200))
        if crossfade:
            self._bd_shown = state
        # 3) 色调 + 纵向光泽
        p.fillPath(path, theme.tint_brush(self.body))
        bb = self.body
        g = QtGui.QLinearGradient(0, bb.top(), 0, bb.bottom())
        g.setColorAt(0, QtGui.QColor(255, 255, 255, 26 if not theme.dark else 14))
        g.setColorAt(0.5, QtGui.QColor(255, 255, 255, 0))
        g.setColorAt(1, QtGui.QColor(255, 255, 255, 10))
        p.fillPath(path, g)

    def _paint_breath(self, p: QtGui.QPainter, path: QtGui.QPainterPath, bb: QtCore.QRectF):
        """新消息呼吸灯：贴着屏幕的吸附边一呼一吸，5 秒后自动淡出。

        光要盖住"整个接触面"：吸附时贴边两侧还有表面张力外扩的液脚，只按胶囊本体铺，
        两侧就会留下没被点亮的直角，看着像颜色没盖满。所以横向铺满形状的外接矩形、
        纵向从吸附边往里迅速衰减 —— 贴边的亮线 + 余光都做在同一条渐变里，轮廓上没有硬边。
        """
        now = time.perf_counter()
        if now >= self._breath_t1:
            return
        elapsed = max(0.0, now - self._breath_t0)
        remain = max(0.0, self._breath_t1 - now)
        env = smooth(elapsed / 0.45) * smooth(remain / 1.1)     # 起 / 收都慢一点，不会"啪"地亮灭
        if env <= 0.01:
            return
        wave = 0.5 - 0.5 * math.cos(elapsed * math.tau * 0.5)   # 0.5Hz：一次呼吸约 2s，像呼吸不像心跳
        k = env * (0.26 + 0.74 * wave)
        if k <= 0.01:
            return
        col = QtGui.QColor(self._breath_color)
        edge = self.edge
        horiz = edge in ("top", "bottom")
        shp = path.boundingRect()
        depth = bb.height() if horiz else bb.width()
        ratio, lo, hi = BREATH_BAND
        band = clamp(depth * ratio, lo, hi)
        # 纵向（离开吸附边）从本体边缘起算：贴边的一侧液桥会多冒出去 1.5px，那一条在屏幕外
        # （或压在任务栏下），从形状边缘起算的话最亮的那一线正好被裁掉，剩下的余光就显得发灰。
        # 横向则铺满形状的外接矩形，液脚也一起点亮，不然贴边两侧会留下没盖到的直角。
        if edge == "top":
            line = bb.top()
            y0 = min(shp.top(), line)
            rect = QtCore.QRectF(shp.left(), y0, shp.width(), band + (line - y0))
            grad = QtGui.QLinearGradient(0, line, 0, line + band)
        elif edge == "bottom":
            line = bb.bottom()
            y1 = max(shp.bottom(), line)
            rect = QtCore.QRectF(shp.left(), line - band, shp.width(), band + (y1 - line))
            grad = QtGui.QLinearGradient(0, line, 0, line - band)
        elif edge == "left":
            line = bb.left()
            x0 = min(shp.left(), line)
            rect = QtCore.QRectF(x0, shp.top(), band + (line - x0), shp.height())
            grad = QtGui.QLinearGradient(line, 0, line + band, 0)
        else:
            line = bb.right()
            x1 = max(shp.right(), line)
            rect = QtCore.QRectF(line - band, shp.top(), band + (x1 - line), shp.height())
            grad = QtGui.QLinearGradient(line, 0, line - band, 0)
        for t, af in BREATH_STOPS:
            c = QtGui.QColor(col)
            c.setAlphaF(min(1.0, k * af))
            grad.setColorAt(t, c)
        p.save()
        p.setClipPath(path)
        p.fillRect(rect, grad)
        p.restore()

    def _paint_rest(self, p: QtGui.QPainter, path: QtGui.QPainterPath, bb: QtCore.QRectF):
        # 5) 常态动画：沿边缘缓慢游走的流光
        t = time.perf_counter() - self._t0
        if not self.expanded:
            ph = (math.sin(t * 0.55) * 0.5 + 0.5)
            far_y = bb.bottom() if self.edge != "bottom" else bb.top()
            pt = QtCore.QPointF(bb.left() + bb.width() * ph, far_y)
            if self.edge in ("left", "right"):
                pt = QtCore.QPointF(bb.right() if self.edge == "left" else bb.left(), bb.top() + bb.height() * ph)
            rad = QtGui.QRadialGradient(pt, max(60.0, bb.width() * 0.35))
            rad.setColorAt(0, QtGui.QColor(255, 255, 255, 80 if not theme.dark else 55))
            rad.setColorAt(1, QtGui.QColor(255, 255, 255, 0))
            p.strokePath(path, QtGui.QPen(QtGui.QBrush(rad), 1.0))
        # 6) 收起状态内容
        if self.compact_a.x > 0.01:
            pk = clamp(self.poke.x, -0.5, 1.2) if (self.mode == "anchored" and not self.expanded) else 0.0
            _horiz = self.edge in ("top", "bottom")
            _cu = bb.center().x() if _horiz else bb.center().y()
            _half = max(1.0, (bb.width() if _horiz else bb.height()) / 2)
            _pp = self._poke_sm - QtCore.QPointF(self._win_origin)
            _rel = clamp(((_pp.x() if _horiz else _pp.y()) - _cu) / _half, -1.0, 1.0) if pk > 0.005 else 0.0
            self.compact.press = (max(0.0, pk), _rel)
            horiz = self.edge in ("top", "bottom")
            self.compact.squash = ((1 + 0.05 * pk, 1 - 0.16 * pk) if horiz else (1 - 0.16 * pk, 1 + 0.05 * pk))
            p.save()
            p.setClipPath(path)               # 内容（含表情）不超出灵动岛
            self.compact.paint(p, bb, self.compact_a.x, self.peak_fn() if self.compact.mode() == "media" else 0.0,
                               vertical=self.edge in ("left", "right"))
            p.restore()
        p.end()

    # ================================================================ 鼠标
    def _inside(self, gp: QtCore.QPoint) -> bool:
        lp = QtCore.QPointF(gp - self._win_origin)
        if self.path.contains(lp):
            return True
        return self.body.adjusted(-4, -4, 4, 4).contains(lp)

    def _poll_hover(self):
        # 输入框的键盘焦点：切到别的窗口后立即释放，否则会一直保持展开
        if "focus" in self.locks and user32.GetForegroundWindow() != int(self.winId()):
            self._prev_fg = None
            self.release_focus()
        if self._fs_hidden or self.mode != "anchored":
            self._hover_since = self._out_since = None
            return
        inside = self._inside(QtGui.QCursor.pos())
        now = time.monotonic()
        if not self.expanded and self.mode == "anchored" and not self._fs_hidden:
            # 收起时（任何展开方式）：鼠标靠近就把水滴"挤"一下，离开后带回弹地恢复
            # 只有鼠标进入灵动岛范围才挤压；越靠近中心挤压越强（边缘处几乎不变形）。
            # 靠近但没碰到时只有表情变化（由小形象自己处理），不挤压。
            gp = QtCore.QPointF(QtGui.QCursor.pos())
            lp = gp - QtCore.QPointF(self._win_origin)
            b = self.body
            if b.contains(lp):
                nx = (lp.x() - b.center().x()) / max(1.0, b.width() / 2)
                ny = (lp.y() - b.center().y()) / max(1.0, b.height() / 2)
                depth = 1.0 - min(1.0, math.hypot(nx, ny))           # 0 = 边缘，1 = 中心
                target = depth ** 1.4
                self._poke_pt = gp
                if abs(self.poke.target - target) > 0.01:
                    self.poke.to(target, 260, 0.75)
                    self._kick()
            elif self.poke.target != 0.0:
                self.poke.to(0.0, 300, 0.22)          # 欠阻尼：弹回时会外鼓几下
                self._kick()
        on_btn = inside and self._on_play_button(QtCore.QPointF(QtGui.QCursor.pos()) - QtCore.QPointF(self._win_origin))
        hv = 1.0 if on_btn and not self.passthrough() else 0.0
        if abs(self.compact.play_hover - hv) > 0.01:
            self.compact.play_hover += (hv - self.compact.play_hover) * 0.5
            self._kick()
        if on_btn and not self.expanded:
            self._hover_since = None              # 在播放键上停留不展开
            return
        if inside and self.passthrough():
            inside = False                        # 可穿透：鼠标视为不在灵动岛上（只响应右键）
            self._hover_since = None
            if not self.expanded:
                return
        if inside:
            self._out_since = None
            if not self.expanded and self.expand_mode() != "never":
                if not self.hover_expand():
                    self._hover_since = None   # 关闭悬停展开：不累计计时，重新打开时不会立刻展开
                elif self._hover_since is None:
                    self._hover_since = now
                elif (now - self._hover_since) * 1000 >= cfg.get("island.hover_expand_ms", 220) \
                        and not (user32.GetAsyncKeyState(0x01) & 0x8000):
                    self._hover_since = None
                    self.expand()
        else:
            self._hover_since = None
            if self.expanded:
                if self.locks or self.pinned():
                    self._out_since = None
                    return
                if self._out_since is None:
                    self._out_since = now
                elif (now - self._out_since) * 1000 >= cfg.get("island.collapse_delay_ms", 450):
                    self._out_since = None
                    self.collapse()

    def mousePressEvent(self, e):
        if not self._inside(e.globalPosition().toPoint()):
            e.ignore()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position()
        elif e.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        # 按住并移动即开始拖动（不需要长按）；固定时不允许拖动
        if self._press is not None and (e.position() - self._press).manhattanLength() > 4:
            self.lp_timer.stop()
            self._press = None
            self.begin_drag(QtGui.QCursor.pos())

    def _on_play_button(self, local: QtCore.QPointF) -> bool:
        r = self.compact.play_rect
        return (r is not None and not self.expanded and self.compact.mode() == "media"
                and r.adjusted(-6, -8, 6, 8).contains(local))

    def mouseReleaseEvent(self, e):
        self.lp_timer.stop()
        if self._press is not None and self._on_play_button(e.position()) and self._on_play_button(self._press):
            self._press = None
            self.play_pause_requested.emit()
            return
        if self._press is not None and self.mode == "anchored" and not self.expanded \
                and self.expand_mode() != "never":
            self._click_timer.start(QtWidgets.QApplication.doubleClickInterval())
        self._press = None

    def _single_click(self):
        if self.mode == "anchored" and not self.expanded and self.expand_mode() != "never":
            self.expand()
            self.locks.add("pin")
            QtCore.QTimer.singleShot(600, lambda: self.locks.discard("pin"))

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._on_play_button(e.position()):
            self.play_pause_requested.emit()       # 快速连点播放键：当作第二次点击，不切换长关
            return
        if e.button() == Qt.MouseButton.LeftButton and self._inside(e.globalPosition().toPoint()):
            self._click_timer.stop()
            self._press = None
            self.toggle_long_off()

    def toggle_long_off(self):
        """双击：悬停展开 ↔ 长关（常驻展开时双击也切到长关）。"""
        new = "hover" if self.expand_mode() == "never" else "never"
        self.set_expand_mode(new)
        if new == "never" and self.expanded:
            # 双击长关：立即收起，不等鼠标移出；也不显示任何提示文字
            self.locks.discard("pin")
            self.release_focus()
            self.collapse()

    def _long_press(self):
        pass

    # ---------------- 穿透模式 ----------------
    def passthrough(self) -> bool:
        return bool(cfg.get("island.passthrough", False))

    def set_passthrough(self, on: bool):
        cfg.set("island.passthrough", bool(on))
        self._apply_passthrough()
        if on and self.expanded and not self.pinned():
            self.collapse()
        self.passthrough_changed.emit(bool(on))

    def _apply_passthrough(self):
        """穿透 = 鼠标完全穿过（WS_EX_TRANSPARENT），只靠钩子响应右键。"""
        hwnd = int(self.winId())
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        on = self.passthrough()
        transparent = on
        ex = (ex | WS_EX_TRANSPARENT) if transparent else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
        if on:
            self._rhook.install()
        else:
            self._rhook.remove()

    # ---------------- 保持置顶 ----------------
    def keep_on_top(self) -> bool:
        return bool(cfg.get("island.keep_on_top", True))

    def set_keep_on_top(self, on: bool):
        cfg.set("island.keep_on_top", bool(on))
        self.apply_keep_on_top()

    def _set_topmost(self, on: bool):
        """原生设置 / 取消 TopMost：会同时把它放到置顶层最前面，但不会抢焦点。"""
        hwnd = int(self.winId())
        user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def apply_keep_on_top(self):
        """让窗口的 TopMost 状态与设置保持一致（设置窗口保存后调用）。"""
        if self.isVisible():
            self._set_topmost(self.keep_on_top())

    def _raise_topmost(self):
        """展开 / 重新显示时重新压到置顶层最前面，避免悬停展开被其他窗口盖住。"""
        if self.keep_on_top():
            self._set_topmost(True)

    def _ensure_topmost(self):
        """系统或别的程序偶尔会改掉置顶状态：定时校正，避免只有点击后才回到前面。"""
        if not self.isVisible():
            return
        hwnd = int(self.winId())
        actual = bool(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
        if actual != self.keep_on_top():
            self._set_topmost(self.keep_on_top())

    def capture_visible(self) -> bool:
        return not bool(cfg.get("island.exclude_from_capture", False))

    def set_capture_visible(self, on: bool):
        cfg.set("island.exclude_from_capture", not on)
        self.apply_capture_mode()

    def apply_capture_mode(self):
        """参与屏幕捕获：截图 / 录屏里能看到灵动岛；毛玻璃取背景时只在那一瞬间临时排除自己。
        不参与：一直排除（截图里看不到灵动岛）。"""
        hwnd = int(self.winId())
        visible = self.capture_visible()
        exclude_from_capture(hwnd, not visible)
        self.backdrop.hwnd = hwnd
        self.backdrop.self_exclude = visible

    def apply_layout_settings(self):
        """设置窗口改了吸附边 / 位置 / 显示器后重新吸附，不需要重启。"""
        edge = cfg.get("island.edge", "top")
        if edge not in ("top", "bottom", "left", "right"):
            edge = "top"
        screens = QtGui.QGuiApplication.screens()
        i = int(cfg.get("island.screen", 0) or 0)
        scr = screens[i] if 0 <= i < len(screens) else QtGui.QGuiApplication.primaryScreen()
        try:
            pos = clamp(float(cfg.get("island.pos", 0.5)), 0.0, 1.0)
        except (TypeError, ValueError):
            pos = 0.5
        if edge == self.edge and scr is self.scr and abs(pos - self.pos_frac) < 1e-4:
            return
        self.edge = edge
        self.scr = scr
        self.pos_frac = pos
        self.neck_edge = edge
        self.mode = "anchored"
        self._place_nav()
        self._kick()

    def hover_expand(self) -> bool:
        """True = 鼠标悬停自动展开（默认）；False = 单击展开，按住拖拽不算点击。"""
        return bool(cfg.get("island.hover_expand", True))

    def expand_mode(self) -> str:
        m = cfg.get("island.expand_mode")
        if m not in ("hover", "always", "never"):
            m = "always" if cfg.get("island.pinned", False) else "hover"
        return m

    def set_expand_mode(self, mode: str):
        """hover = 鼠标悬停展开、离开收起；always = 一直展开；never = 一直收起（悬停 / 单击都不展开）。"""
        if mode not in ("hover", "always", "never"):
            return
        cfg.set("island.expand_mode", mode, save=False)
        cfg.set("island.pinned", mode == "always")
        if mode == "always":
            self.expand()
        elif mode == "never":
            # 不立即收起：鼠标离开灵动岛后才收起（之后悬停 / 单击都不再展开）
            self.locks.discard("pin")
            if not self._inside(QtGui.QCursor.pos()):
                self.collapse()
        self.mode_changed.emit(mode)
        self.pin_changed.emit(mode == "always")

    def pinned(self) -> bool:
        return self.expand_mode() == "always"

    def set_pinned(self, on: bool):
        self.set_expand_mode("always" if on else "hover")
        return

    def _set_pinned_old(self, on: bool):
        """图钉：保持展开（不会因鼠标移开而收起），仍可拖动。"""
        cfg.set("island.pinned", bool(on))
        if on and not self.expanded:
            self.expand()
        self.pin_changed.emit(bool(on))

    def wheelEvent(self, e):
        if not self.expanded:
            self.wheel_compact.emit(1 if e.angleDelta().y() > 0 else -1)

    # ================================================================ 焦点（输入框需要键盘）
    def request_focus(self):
        hwnd = int(self.winId())
        fg = user32.GetForegroundWindow()
        if fg and fg != hwnd:
            self._prev_fg = fg
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_NOACTIVATE)
        cur = user32.GetCurrentThreadId() if hasattr(user32, "GetCurrentThreadId") else ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_tid and fg_tid != cur:
            user32.AttachThreadInput(cur, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        self.activateWindow()
        if fg_tid and fg_tid != cur:
            user32.AttachThreadInput(cur, fg_tid, False)
        self.locks.add("focus")

    def release_focus(self):
        if "focus" not in self.locks:
            return
        self.locks.discard("focus")
        hwnd = int(self.winId())
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        fw = QtWidgets.QApplication.focusWidget()
        if fw is not None:
            fw.clearFocus()
        if self._prev_fg and user32.IsWindow(self._prev_fg):
            user32.SetForegroundWindow(self._prev_fg)
        self._prev_fg = None

    # ================================================================ 原生：热键、闪烁、样式
    def showEvent(self, e):
        super().showEvent(e)
        hwnd = int(self.winId())
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        self.apply_capture_mode()
        if not self._shell_msg:
            self._shell_msg = user32.RegisterWindowMessageW("SHELLHOOK")
            user32.RegisterShellHookWindow(hwnd)
        if not self._registered_hotkeys:
            self.register_hotkeys()
        self._apply_passthrough()
        # 重新显示（例如全屏隐藏后）时恢复置顶；关闭置顶设置时不使用 WindowStaysOnTopHint，避免 Qt 自动加回来
        self._set_topmost(self.keep_on_top())
        self.topmost_timer.start()
        if self.pinned():
            QtCore.QTimer.singleShot(600, self.expand)
        self.poll.start()
        self.backdrop.start()
        self._kick()

    def register_hotkeys(self):
        hwnd = int(self.winId())
        for hid in list(self._registered_hotkeys):
            user32.UnregisterHotKey(hwnd, hid)
        self._registered_hotkeys.clear()
        for i, (name, spec) in enumerate((cfg.get("hotkeys") or {}).items()):
            hk = parse_hotkey(spec)
            if not hk:
                continue
            hid = 0xB000 + i
            if user32.RegisterHotKey(hwnd, hid, hk[0], hk[1]):
                self._registered_hotkeys[hid] = name
            else:
                print(f"热键注册失败（可能被占用）：{name} = {spec}")

    def nativeEvent(self, etype, message):
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                name = self._registered_hotkeys.get(int(msg.wParam))
                if name:
                    self.hotkey.emit(name)
                    return True, 0
            elif self._shell_msg and msg.message == self._shell_msg:
                if int(msg.wParam) == HSHELL_FLASH:
                    self.flash.emit(int(msg.lParam or 0))
        except Exception:  # noqa: BLE001
            pass
        return False, 0

    def set_fullscreen_hidden(self, hidden: bool):
        if hidden == self._fs_hidden:
            return
        self._fs_hidden = hidden
        if hidden:
            self.collapse()
            self.hide()
        else:
            self.show()
            self._kick()

    def closeEvent(self, e):
        hwnd = int(self.winId())
        for hid in self._registered_hotkeys:
            user32.UnregisterHotKey(hwnd, hid)
        try:
            user32.DeregisterShellHookWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        self.backdrop.stop()
        self.topmost_timer.stop()
        self._rhook.remove()
        cfg.save()
        super().closeEvent(e)



_ = G
