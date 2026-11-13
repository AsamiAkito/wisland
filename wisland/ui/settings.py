# -*- coding: utf-8 -*-
"""设置窗口：把 config.json 做成可视表单。

右键灵动岛 →「设置…」打开。左侧分类、右侧表单；每个控件绑定 config.json 里的一个点号路径，
保存时先校验全部字段，再把改动过的项写回（窗口打开期间别处改的配置不会被旧表单覆盖），
然后调用 on_apply(prev_data) 让改动立即生效（部分项需要重启对应服务）。

文件里的三个层次：
- 控件：Switch / Select / Spin / Input / Hotkey / Chips / 列表编辑器，全部跟随主题自绘；
- Form：一个分类页，负责把控件放进 Row → Section → 滚动区，并登记绑定；
- SettingsWindow：窗口外框、分类导航、保存 / 校验 / 导入导出。

以后 config.json 新增一项，只要在对应 _page_* 里加一行 f.switch / f.number / f.text 即可。
"""
from __future__ import annotations

import copy
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .. import (APP_NAME, APP_NAME_CN, APP_TAGLINE, AUTHOR_NAME, AUTHOR_URL,
                LICENSE_NAME, LICENSE_URL, ORIGINAL_AUTHOR_NAME, ORIGINAL_LICENSE_NAME,
                ORIGINAL_LICENSE_URL, ORIGINAL_REPO_URL, REPO_URL,
                __version__ as APP_VERSION)
from ..core.bg import run_bg, ui as bg_ui
from ..core.config import CONFIG_PATH, DATA_DIR, DEFAULTS, ROOT, cfg, expand
from ..services import packaged_update, updater
from .brand import asset_path
from .popup import POPUP_POSITIONS
from .theme import G, THEME_NAMES, W, icon_font, theme, ui_font
from .widgets import Card, Hoverable, IconButton, ScrollArea, Themed, fmt_bytes, painter

Qt = QtCore.Qt
Signal = QtCore.Signal
MISSING = object()

# 可显示 / 隐藏的板块（主页与消息固定显示，不在此列）
SECTION_OPTIONS = [
    ("stock", "股票", G.STOCK),
    ("weather", "天气", G.CLOUD),
    ("ctl", "控制", G.SLIDERS),
    ("clip", "剪贴板", G.CLIP),
    ("trans", "翻译", G.TRANSLATE),
]

KIND_OPTIONS = [("qq", "QQ"), ("wechat", "微信"), ("mail", "邮件"),
                ("feishu", "飞书"), ("dingtalk", "钉钉"), ("dsh", "DeepSeek")]
# 正文预览只对读 toast 的来源有意义；DeepSeek 的正文来自会话日志，始终显示
BODY_KIND_OPTIONS = [("qq", "QQ"), ("wechat", "微信"), ("mail", "邮件"),
                     ("feishu", "飞书"), ("dingtalk", "钉钉")]

# (key, 标题, 图标)
PAGES = [
    ("look", "外观与交互", G.EYE),
    ("hotkeys", "全局热键", G.BOLT),
    ("media", "媒体与歌词", G.MUSIC),
    ("notify", "消息与邮件", G.MESSAGE),
    ("stocks", "股票", G.STOCK),
    ("weather", "天气", G.CLOUD),
    ("translate", "翻译与单词本", G.TRANSLATE),
    ("live", "实时字幕", G.LIVE),
    ("launcher", "剪贴板与启动器", G.CLIP),
    ("system", "系统与控制", G.SLIDERS),
    ("advanced", "高级", G.GEAR),
    ("about", "关于", G.INFO),
]

WIN_W, WIN_H = 920, 660
SIDE_W = 208
HEADER_H = 64
FOOTER_H = 62
RADIUS = 24
FIELD_H = 34


# ==================================================================== 小工具
def _rgba(spec: str) -> QtGui.QColor:
    """把 theme.menu_qss() 里的 "rgba(36,36,40,240)" 解析成 QColor。"""
    m = re.search(r"rgba?\(([^)]+)\)", str(spec))
    if not m:
        return QtGui.QColor(36, 36, 40)
    parts = [p.strip() for p in m.group(1).split(",")]
    try:
        vals = [float(p) for p in parts[:4]]
    except ValueError:
        return QtGui.QColor(36, 36, 40)
    if len(vals) == 3:
        vals.append(255)
    return QtGui.QColor(round(vals[0]), round(vals[1]), round(vals[2]), round(vals[3]))


def panel_color() -> QtGui.QColor:
    return _rgba(theme.menu_qss()[0])


def card_color(alpha: float = 1.0) -> QtGui.QColor:
    """表单分组底色：深色主题用一层白雾，浅色主题用一层淡灰。"""
    c = QtGui.QColor(255, 255, 255, 24) if theme.dark else QtGui.QColor(15, 20, 30, 15)
    c.setAlpha(max(0, min(255, round(c.alpha() * alpha))))
    return c


def field_color() -> QtGui.QColor:
    """输入框 / 次级按钮底色。"""
    return QtGui.QColor(255, 255, 255, 34) if theme.dark else QtGui.QColor(255, 255, 255, 220)


def _role_color(role: str) -> QtGui.QColor:
    return {
        "title": theme.text(),
        "desc": theme.text2(),
        "hint": theme.text3(),
        "accent": theme.link(),
        "danger": theme.up(),
        "ok": theme.ok(),
    }.get(role, theme.text())


def _get_path(data, path: str, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(data: dict, path: str, value):
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def default_of(key: str):
    return copy.deepcopy(_get_path(DEFAULTS, key))


def _jsonable(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _as_float(v, fallback: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _as_int(v, fallback: int) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return fallback


def _merge(base: dict, over: dict) -> dict:
    """导入配置用：DEFAULTS 打底，用户配置覆盖。"""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
               "win": "win", "meta": "win"}
MOD_LABELS = [("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("win", "Win")]


def _norm_key(tok: str) -> str | None:
    t = (tok or "").strip().lower()
    if len(t) == 1 and t.isalnum():
        return t.upper()
    if t == "space":
        return "Space"
    m = re.fullmatch(r"f(\d{1,2})", t)
    if m and 1 <= int(m.group(1)) <= 24:
        return f"F{int(m.group(1))}"
    return None


def hotkey_problem(spec: str) -> str | None:
    """返回错误说明；空字符串（不启用）算合法。"""
    s = str(spec or "").strip()
    if not s:
        return None
    mods: set[str] = set()
    key: str | None = None
    for part in [p.strip() for p in s.split("+")]:
        lp = part.lower()
        if lp in MOD_ALIASES:
            mods.add(MOD_ALIASES[lp])
            continue
        k = _norm_key(part)
        if k is None:
            return f"无法识别的按键：{part or '空'}"
        if key is not None:
            return "只能有一个主按键，例如 Ctrl+Alt+I"
        key = k
    if key is None:
        return "还缺少一个主按键"
    if not mods and not key.startswith("F"):
        return "至少需要一个 Ctrl / Alt / Shift / Win 修饰键"
    return None


def normalize_hotkey(spec: str) -> str:
    s = str(spec or "").strip()
    if not s:
        return ""
    mods, key = set(), ""
    for part in [p.strip() for p in s.split("+")]:
        lp = part.lower()
        if lp in MOD_ALIASES:
            mods.add(MOD_ALIASES[lp])
        else:
            k = _norm_key(part)
            if k:
                key = k
    order = [name for name, _label in MOD_LABELS if name in mods]
    labels = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    return "+".join([labels[m] for m in order] + ([key] if key else []))


def _url_problem(text: str) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None
    if not re.match(r"^https?://[^\s/]+", s):
        return "接口地址需要以 http:// 或 https:// 开头"
    return None


def _stock_code_problem(code: str) -> str | None:
    c = str(code or "").strip()
    if not c:
        return "股票代码不能为空"
    if re.fullmatch(r"(sh|sz|bj)\d{6}", c, re.I):
        return None
    if re.fullmatch(r"hk\d{5}", c, re.I):
        return None
    if re.fullmatch(r"us[A-Za-z0-9.\-]+", c):
        return None
    return f"代码格式不对：{c}（A 股 sh/sz/bj + 6 位，港股 hk + 5 位，美股 us + 代码）"


def _reveal(path: Path):
    subprocess.Popen(["explorer", "/select,", str(path)])


def _open_path(path: Path):
    os.startfile(str(path))  # noqa: S606


def _open_url(url: str) -> bool:
    """用系统默认浏览器打开链接；QDesktopServices 失败时退回 Windows 的 os.startfile。"""
    try:
        if QtGui.QDesktopServices.openUrl(QtCore.QUrl(str(url))):
            return True
    except Exception:  # noqa: BLE001 - 某些环境没有桌面服务
        pass
    try:
        os.startfile(str(url))  # noqa: S606
        return True
    except OSError:
        return False


# ==================================================================== 基础控件
class TLabel(QtWidgets.QLabel, Themed):
    """跟随主题的普通文字；role = title / desc / hint / accent / danger / ok。"""

    def __init__(self, text: str = "", px: float = 12.5, weight=W.Normal, role: str = "desc",
                 wrap: bool = False, parent=None):
        super().__init__(text, parent)
        self._role = role
        self.setFont(ui_font(px, weight))
        self.setWordWrap(wrap)
        if wrap:
            self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        self._hook_theme()
        self._restyle()

    def _on_theme(self):
        try:
            self._restyle()
        except RuntimeError:
            pass

    def _restyle(self):
        self.setStyleSheet(f"color:{theme.qss_color(_role_color(self._role))};background:transparent")

    def set_role(self, role: str):
        if role != self._role:
            self._role = role
            self._restyle()


class Dot(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None, size: int = 8):
        super().__init__(parent)
        self._color: QtGui.QColor | None = None
        self.setFixedSize(size, size)
        self._hook_theme()

    def set_color(self, c: QtGui.QColor | None):
        self._color = c
        self.update()

    def paintEvent(self, _e):
        p = painter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color or theme.text3())
        p.drawEllipse(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class Field(QtWidgets.QWidget, Themed):
    """输入类控件的公共外壳：圆角底 + 极淡描边（校验失败时变红）。"""

    height = FIELD_H
    radius = 10

    def __init__(self, parent=None, width: int | None = None):
        super().__init__(parent)
        self.setFixedHeight(self.height)
        if width:
            self.setFixedWidth(width)
        self._error = False
        self._hook_theme()

    def set_error(self, on: bool):
        if on != self._error:
            self._error = on
            self.update()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = theme.up() if self._error else theme.text(0.11)
        p.setPen(QtGui.QPen(border, 1))
        p.setBrush(field_color())
        p.drawRoundedRect(r, self.radius, self.radius)


class Switch(Hoverable):
    """开关。点击 / 空格切换，带弹性滑动。"""
    changed = Signal()

    W_, H_ = 46, 26

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._on = bool(checked)
        self._t = 1.0 if checked else 0.0
        self.setFixedSize(self.W_, self.H_)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.valueChanged.connect(self._step)
        self.clicked.connect(self.toggle)

    def _step(self, v):
        self._t = float(v)
        self.update()

    def value(self) -> bool:
        return self._on

    def set_value(self, on):
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()
        self.update()

    def toggle(self):
        self.set_value(not self._on)
        self.changed.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rr = r.height() / 2
        t = max(0.0, min(1.0, self._t))
        off, on = theme.track(), theme.on_color()
        col = QtGui.QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
            round(off.alpha() + (on.alpha() - off.alpha()) * t))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, rr, rr)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - 6 - d) * t
        kr = QtCore.QRectF(x, r.top() + 3, d, d)
        p.setBrush(QtGui.QColor(0, 0, 0, 40))
        p.drawEllipse(kr.translated(0, 1))
        p.setBrush(QtGui.QColor(255, 255, 255, 245))
        p.drawEllipse(kr)
        if self.hasFocus():
            p.setPen(QtGui.QPen(theme.on_color(), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), rr + 1.5, rr + 1.5)


class Select(Hoverable):
    """下拉选择：点击弹出主题菜单，当前项打勾。"""
    changed = Signal()

    def __init__(self, options: list[tuple], value=None, width: int = 180, parent=None):
        super().__init__(parent)
        self.options = [(v, str(label)) for v, label in options]
        self._value = value if value is not None else (self.options[0][0] if self.options else None)
        self.setFixedHeight(FIELD_H)
        self.setFixedWidth(width)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self._open_menu)

    def value(self):
        return self._value

    def set_value(self, v):
        if any(v == opt for opt, _ in self.options):
            self._value = v
            self.update()

    def label(self) -> str:
        return next((lab for opt, lab in self.options if opt == self._value), str(self._value or ""))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
            self._open_menu()
            e.accept()
            return
        super().keyPressEvent(e)

    def _open_menu(self):
        from .pages.system import styled_menu
        m = styled_menu(self)
        for opt, label in self.options:
            a = m.addAction(("✓  " if opt == self._value else "    ") + label)
            a.triggered.connect(lambda _=False, o=opt: self._choose(o))
        m.exec(self.mapToGlobal(QtCore.QPoint(0, self.height() + 4)))

    def _choose(self, opt):
        if opt != self._value:
            self._value = opt
            self.changed.emit()
        self.update()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(theme.text(0.11), 1))
        p.setBrush(field_color())
        p.drawRoundedRect(r, 10, 10)
        if self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 9, 9)
        p.setFont(ui_font(12.5))
        p.setPen(theme.text(0.92))
        text_r = r.adjusted(12, 0, -28, 0)
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(text_r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self.label(), Qt.TextElideMode.ElideRight, int(text_r.width())))
        p.setFont(icon_font(9))
        p.setPen(theme.text(0.45))
        p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, G.DOWN)


class Spin(Field):
    """数字输入：左右 ± 与键盘输入；range 由保存前校验兜底。"""
    changed = Signal()

    def __init__(self, value, lo, hi, step: float = 1, decimals: int = 0, suffix: str = "",
                 width: int | None = None, parent=None):
        self.lo, self.hi, self.step, self.decimals = float(lo), float(hi), float(step), int(decimals)
        w = width or (158 if suffix else 138)
        super().__init__(parent, width=w)
        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setFrame(False)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.setFont(ui_font(12.5, W.Medium, num=True))
        self.edit.setValidator(QtGui.QRegularExpressionValidator(
            QtCore.QRegularExpression(r"^-?\d{0,9}(\.\d{0,4})?$"), self))
        self.minus = _StepButton("−", "减少")
        self.plus = _StepButton("+", "增加")
        self.minus.clicked.connect(lambda: self._nudge(-1))
        self.plus.clicked.connect(lambda: self._nudge(1))
        self.edit.editingFinished.connect(self.changed.emit)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        lay.addWidget(self.minus)
        lay.addWidget(self.edit, 1)
        if suffix:
            lab = TLabel(suffix, 11, W.Normal, "hint")
            lay.addWidget(lab)
        lay.addWidget(self.plus)
        self.set_value(value)

    def _fmt(self, v: float) -> str:
        if self.decimals:
            return f"{v:.{self.decimals}f}"
        return str(int(round(v)))

    def value(self):
        t = self.edit.text().strip()
        try:
            v = float(t)
        except ValueError:
            v = self.lo
        return v if self.decimals else int(round(v))

    def set_value(self, v):
        v = max(self.lo, min(self.hi, _as_float(v, self.lo)))
        self.edit.setText(self._fmt(v))

    def _nudge(self, d: int):
        v = max(self.lo, min(self.hi, _as_float(self.value(), self.lo) + self.step * d))
        self.set_value(v)
        self.changed.emit()

    def mousePressEvent(self, e):
        self.edit.setFocus()
        e.accept()


class _StepButton(Hoverable):
    def __init__(self, char: str, tip: str = "", parent=None):
        super().__init__(parent)
        self.char = char
        self.setFixedSize(22, 26)
        if tip:
            self.setToolTip(tip)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, 7, 7)
        p.setFont(ui_font(15, W.Medium))
        p.setPen(theme.text(0.80))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.char)


class Input(Field):
    """单行输入：支持占位、密码显示 / 隐藏、选择文件或文件夹。"""
    changed = Signal()

    def __init__(self, text: str = "", placeholder: str = "", secret: bool = False,
                 browse: str | None = None, width: int | None = None, parent=None,
                 numeric: bool = False):
        super().__init__(parent, width=width)
        self._secret = bool(secret)
        self._browse = browse
        self.setMinimumWidth(110 if width is None else 0)
        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setFrame(False)
        self.edit.setFont(ui_font(12.5, W.Normal, num=numeric))
        self.edit.setText(str(text if text is not None else ""))
        self.edit.setPlaceholderText(placeholder)
        if numeric:
            self.edit.setValidator(QtGui.QRegularExpressionValidator(
                QtCore.QRegularExpression(r"^-?\d{0,9}(\.\d{0,6})?$"), self))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 5, 0)
        lay.setSpacing(2)
        lay.addWidget(self.edit, 1)
        self.eye: IconButton | None = None
        if secret:
            self.eye = IconButton(G.EYE, 24, 12, "显示 / 隐藏")
            self.eye.clicked.connect(self._toggle_eye)
            lay.addWidget(self.eye)
            self.edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pick: IconButton | None = None
        if browse:
            self.pick = IconButton(G.OPEN, 24, 12, "浏览…")
            self.pick.clicked.connect(self._pick)
            lay.addWidget(self.pick)
        self.edit.textEdited.connect(self._edited)
        theme.changed.connect(self._style)
        self._style()

    def _style(self):
        try:
            self.edit.setStyleSheet(
                f"QLineEdit{{background:transparent;border:none;color:{theme.qss_color(theme.text())};"
                f"selection-background-color:rgba(79,140,255,110)}}")
            pal = self.edit.palette()
            pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, theme.text3())
            self.edit.setPalette(pal)
        except RuntimeError:
            pass

    def _edited(self, _t):
        self.changed.emit()

    def _toggle_eye(self):
        on = self.edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        self.edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if on
                              else QtWidgets.QLineEdit.EchoMode.Password)
        if self.eye is not None:
            self.eye.set_glyph(G.EYE_OFF if on else G.EYE)

    def _pick(self):
        raw = self.edit.text().strip()
        start = expand(raw) if raw else str(ROOT)
        if self._browse == "dir":
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹", start)
        else:
            filters = ("所有文件 (*.*);;程序 (*.exe)" if self._browse == "any"
                       else "程序 (*.exe);;所有文件 (*.*)")
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "选择程序或文件", start, filters)
        if path:
            self.set_value(Path(path).as_posix())
            self.changed.emit()

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, v):
        self.edit.setText(str(v if v is not None else "").strip())
        self.edit.deselect()
        self.edit.setCursorPosition(0)

    def keyPressEvent(self, e):
        self.edit.setFocus()
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        self.edit.setFocus()
        e.accept()


class Hotkey(Field):
    """点击后录制快捷键：按下组合键写入，Esc 取消，Backspace 清空。"""
    changed = Signal()

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent, width=186)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._value = normalize_hotkey(value)
        self._capturing = False
        self._flash = ""
        self.label = TLabel(self._value or "点击录制", 12.5, W.Medium, "title" if self._value else "hint")
        self.clear_btn = IconButton(G.CLOSE, 22, 9, "清除")
        self.clear_btn.clicked.connect(self._clear)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(4)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.clear_btn)
        self._refresh()

    # ---- 值 ----
    def value(self) -> str:
        return self._value

    def set_value(self, v):
        self._value = normalize_hotkey(str(v or ""))
        self._capturing = False
        self._refresh()

    def _refresh(self):
        if self._capturing:
            self.label.setText(self._flash or "按下快捷键…")
            self.label.set_role("accent")
            self.clear_btn.hide()
        else:
            if self._flash:
                self.label.setText(self._flash)
                self.label.set_role("danger")
            else:
                self.label.setText(self._value or "点击录制")
                self.label.set_role("title" if self._value else "hint")
            self.clear_btn.setVisible(bool(self._value))
        self.update()

    def _clear(self):
        self._value = ""
        self._capturing = False
        self._flash = ""
        self._refresh()
        self.changed.emit()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._capturing:
            p = painter(self)
            r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(QtGui.QPen(theme.on_color(), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, self.radius, self.radius)

    # ---- 交互 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._begin()
        e.accept()

    def _begin(self):
        self._capturing = True
        self._flash = ""
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._refresh()

    def focusOutEvent(self, e):
        if self._capturing:
            self._capturing = False
            self._flash = ""
            self._refresh()
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        if not self._capturing:
            if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._begin()
                e.accept()
                return
            super().keyPressEvent(e)
            return
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self._capturing = False
            self._flash = ""
            self._refresh()
            self.clearFocus()
            e.accept()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._capturing = False
            self._flash = ""
            self._refresh()
            e.ignore()               # 交还焦点，继续 Tab 导航
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._clear()
            e.accept()
            return
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
                   Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock):
            e.accept()
            return
        mods = e.modifiers()
        names = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            names.append("Ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            names.append("Alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            names.append("Shift")
        if mods & Qt.KeyboardModifier.MetaModifier:
            names.append("Win")
        main = ""
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            main = chr(ord("A") + key - Qt.Key.Key_A)
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            main = chr(ord("0") + key - Qt.Key.Key_0)
        elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            main = f"F{key - Qt.Key.Key_F1 + 1}"
        elif key == Qt.Key.Key_Space:
            main = "Space"
        if not main:
            self._fail("这个键不能用作热键")
            e.accept()
            return
        if not names and not main.startswith("F"):
            self._fail("需要 Ctrl / Alt / Shift / Win")
            e.accept()
            return
        spec = "+".join(names + [main])
        self._value = spec
        self._capturing = False
        self._flash = ""
        self._refresh()
        self.changed.emit()
        self.clearFocus()
        e.accept()

    def _fail(self, msg: str):
        self._flash = msg
        self._refresh()
        QtCore.QTimer.singleShot(1300, self._unflash)

    def _unflash(self):
        if self._capturing and self._flash:
            self._flash = ""
            self._refresh()


class Button(Hoverable):
    """胶囊按钮：primary = 强调色，ghost = 浅底，danger = 红字。"""
    changed = Signal()

    def __init__(self, text: str, glyph: str = "", variant: str = "ghost",
                 height: int = 32, parent=None):
        super().__init__(parent)
        self.text = text
        self.glyph = glyph
        self.variant = variant
        self.setFixedHeight(height)
        self.setMinimumWidth(self._fit())
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text)
        self.clicked.connect(lambda: self.changed.emit())

    def _fit(self) -> int:
        fm = QtGui.QFontMetrics(ui_font(12.5, W.Medium))
        return fm.horizontalAdvance(self.text) + 26 + (20 if self.glyph else 0)

    def set_text(self, t: str):
        self.text = t
        self.setAccessibleName(t)
        self.setMinimumWidth(self._fit())
        self.update()

    def set_variant(self, variant: str):
        if variant != self.variant:
            self.variant = variant
            self.update()

    def refresh_fonts(self):
        self.setMinimumWidth(self._fit())
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        rr = r.height() / 2
        if self.variant == "primary":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.primary_fill())
            p.drawRoundedRect(r, rr, rr)
            if self._hover.x > 0.01:
                c = QtGui.QColor(255, 255, 255, round(26 * self._hover.x))
                p.setBrush(c)
                p.drawRoundedRect(r, rr, rr)
            fg = theme.on_fg(1.0)
        elif self.variant == "danger":
            p.setPen(QtGui.QPen(theme.up(), 1))
            p.setBrush(QtGui.QColor(0, 0, 0, 0))
            p.drawRoundedRect(r, rr, rr)
            fg = theme.up()
        else:
            p.setPen(QtGui.QPen(theme.text(0.10), 1))
            p.setBrush(field_color())
            p.drawRoundedRect(r, rr, rr)
            fg = theme.text(0.92)
        if not self.isEnabled():
            fg = QtGui.QColor(fg)
            fg.setAlphaF(fg.alphaF() * 0.35)
        p.setPen(fg)
        x = r.x() + 13
        if self.glyph:
            p.setFont(icon_font(12))
            p.drawText(QtCore.QRectF(x, r.y(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
            x += 19
        p.setFont(ui_font(12.5, W.Medium))
        p.drawText(QtCore.QRectF(x, r.y(), r.right() - x - 10, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QtGui.QPen(theme.link(), 1))
            p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), rr - 1.5, rr - 1.5)


class Chip(Hoverable):
    def __init__(self, label: str, checked: bool = False, glyph: str = "", parent=None):
        super().__init__(parent)
        self.label = label
        self.glyph = glyph
        self.checked = bool(checked)
        self.setFixedHeight(28)
        self.setMinimumWidth(self._fit())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _fit(self) -> int:
        fm = QtGui.QFontMetrics(ui_font(12.5, W.Medium))
        return fm.horizontalAdvance(self.label) + 26 + (19 if self.glyph else 0) + (16 if self.checked else 0)

    def set_checked(self, on: bool):
        if on != self.checked:
            self.checked = bool(on)
            self.setMinimumWidth(self._fit())
            self.update()

    def refresh_fonts(self):
        self.setMinimumWidth(self._fit())
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.set_checked(not self.checked)
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rr = r.height() / 2
        if self.checked:
            bg = QtGui.QColor(theme.on_color())
            bg.setAlpha(42)
            p.setBrush(bg)
            p.setPen(QtGui.QPen(theme.on_color(), 1))
        else:
            p.setBrush(field_color())
            p.setPen(QtGui.QPen(theme.text(0.10), 1))
            if self._hover.x > 0.01:
                c = theme.hover()
                p.setBrush(c)
        p.drawRoundedRect(r, rr, rr)
        fg = theme.on_color() if self.checked else theme.text(0.80)
        p.setPen(fg)
        x = r.x() + 12
        if self.glyph:
            p.setFont(icon_font(12))
            p.drawText(QtCore.QRectF(x, r.y(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
            x += 19
        p.setFont(ui_font(12.5, W.Medium if self.checked else W.Normal))
        p.drawText(QtCore.QRectF(x, r.y(), r.right() - x - 10 - (16 if self.checked else 0), r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.label)
        if self.checked:
            p.setFont(icon_font(9))
            p.drawText(QtCore.QRectF(r.right() - 22, r.y(), 14, r.height()),
                       Qt.AlignmentFlag.AlignCenter, G.CHECK)


class FlowLayout(QtWidgets.QLayout):
    """自动换行的横向布局（板块 / 模块选择用）。"""

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 8, vspacing: int = 8):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._items: list[QtWidgets.QLayoutItem] = []
        self._h = hspacing
        self._v = vspacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, int(width), 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QtCore.QRect, test: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = eff.x(), eff.y()
        line_h = 0
        for it in self._items:
            hint = it.sizeHint()
            w, h = hint.width(), hint.height()
            if x + w > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y += line_h + self._v
                line_h = 0
            if not test:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += w + self._h
            line_h = max(line_h, h)
        return y + line_h - rect.y() + m.bottom()


class Chips(QtWidgets.QWidget):
    """多选标签组，value() 按选项顺序返回选中值。"""
    changed = Signal()

    def __init__(self, options: list[tuple], selected=None, parent=None):
        super().__init__(parent)
        self._chips: list[tuple[object, Chip]] = []
        selected = set(selected or [])
        lay = FlowLayout(self, 0, 8, 8)
        for opt in options:
            value, label = opt[0], opt[1]
            glyph = opt[2] if len(opt) > 2 else ""
            chip = Chip(label, value in selected, glyph)
            chip.clicked.connect(lambda c=chip, v=value: self._toggle(v, c))
            self._chips.append((value, chip))
            lay.addWidget(chip)

    def _toggle(self, value, chip: Chip):
        chip.set_checked(not chip.checked)
        self.changed.emit()

    def value(self) -> list:
        return [v for v, c in self._chips if c.checked]

    def set_value(self, values):
        want = set(values or [])
        for v, c in self._chips:
            c.set_checked(v in want)


# ==================================================================== 字体选择
FONT_ROLE = Qt.ItemDataRole.UserRole          # 行数据：字体 family（空 = 默认字体）
CJK_ROLE = Qt.ItemDataRole.UserRole + 1       # 行数据：是否含中文字形


def installed_fonts() -> list[str]:
    """读取本机字体库（Windows 已安装字体）；没有 QApplication 时返回空列表。"""
    try:
        families = QtGui.QFontDatabase.families()
    except Exception:  # noqa: BLE001
        return []
    return sorted(families, key=str.casefold)


def fonts_with_chinese() -> set[str]:
    """含中文字形的字体名，用来标记 / 过滤。"""
    try:
        ws = QtGui.QFontDatabase.WritingSystem.SimplifiedChinese
        return set(QtGui.QFontDatabase.families(ws))
    except Exception:  # noqa: BLE001
        return set()


class _FontRowDelegate(QtWidgets.QStyledItemDelegate):
    """字体列表的一行：左边字体名（用它自己的字形），右边同一字体的预览字。"""

    ROW_H = 38

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), self.ROW_H)

    def paint(self, p, option, index):
        fam = str(index.data(FONT_ROLE) or "")
        zh = bool(index.data(CJK_ROLE))
        sel = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hot = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        r = QtCore.QRectF(option.rect).adjusted(2, 2, -2, -2)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        if sel or hot:
            bg = QtGui.QColor(theme.on_color() if sel else theme.hover())
            if sel:
                bg.setAlpha(46)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 9, 9)

        name_font = ui_font(14, W.Medium)
        if fam:
            name_font.setFamilies([fam] + name_font.families())
        sample = "字体预览 Aa 0123"
        sample_font = QtGui.QFont(name_font)
        sample_font.setPixelSize(12)
        p.setFont(sample_font)
        p.setPen(theme.text2())
        sw = QtGui.QFontMetrics(sample_font).horizontalAdvance(sample)
        sample_rect = QtCore.QRectF(r.right() - sw - 12, r.y(), sw + 2, r.height())
        p.drawText(sample_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, sample)

        x = r.x() + 10
        if zh:
            tag = QtCore.QRectF(x, r.center().y() - 7.5, 17, 15)
            tag_bg = QtGui.QColor(theme.on_color())
            tag_bg.setAlpha(42)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tag_bg)
            p.drawRoundedRect(tag, 5, 5)
            p.setFont(ui_font(10, W.DemiBold))
            p.setPen(theme.on_color())
            p.drawText(tag, Qt.AlignmentFlag.AlignCenter, "中")
            x = tag.right() + 8

        name = fam or "默认字体（内置字体栈）"
        avail = max(10.0, sample_rect.left() - x - 10)
        p.setFont(name_font)
        p.setPen(theme.text(0.96 if sel else 0.86))
        p.drawText(QtCore.QRectF(x, r.y(), avail, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   QtGui.QFontMetrics(name_font).elidedText(name, Qt.TextElideMode.ElideRight, int(avail)))
        p.restore()


class _FilterChip(Chip):
    """过滤器用的 Chip：状态由外部统一切换，键盘 Space / Enter 不会自己先切一次。"""

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)


class FontDialog(QtWidgets.QDialog):
    """本机字体库：搜索、只看支持中文的字体、实时预览。"""

    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("wisland 选择字体")
        # 和确认框一样置顶，否则会被置顶的设置窗口压在后面
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(560, 500)
        self.resize(620, 560)
        self._families = installed_fonts()
        self._cjk = {f.casefold() for f in fonts_with_chinese()}
        self._shown = 0
        self._build()
        self._select(current)
        self._apply_filter()
        self._update_preview()
        if parent is not None:
            g = parent.window().frameGeometry()
            self.move(g.center() - self.rect().center())

    # ---------- 结构 ----------
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(10)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(TLabel("选择字体", 16, W.DemiBold, "title"))
        head.addStretch(1)
        close = IconButton(G.CLOSE, 26, 10, "关闭")
        close.clicked.connect(self.reject)
        head.addWidget(close)
        lay.addLayout(head)

        self.count = TLabel("", 11.5, W.Normal, "hint", wrap=True)
        lay.addWidget(self.count)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.search = Input(placeholder="搜索字体名字…")
        self.search.edit.textChanged.connect(lambda _t: self._apply_filter())
        self.search.edit.installEventFilter(self)
        row.addWidget(self.search, 1)
        self.zh = _FilterChip("只看中文字体")
        self.zh.clicked.connect(self._toggle_zh)
        row.addWidget(self.zh, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(row)

        self.list = QtWidgets.QListWidget()
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setItemDelegate(_FontRowDelegate(self.list))
        self._style_list()
        self.list.currentItemChanged.connect(lambda *_: self._update_preview())
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())
        lay.addWidget(self.list, 1)

        self.preview = QtWidgets.QLabel()
        self.preview.setMinimumHeight(56)
        self.preview.setStyleSheet(self._preview_qss())
        lay.addWidget(self.preview)

        self.preview_note = TLabel("", 11, W.Normal, "hint", wrap=True)
        lay.addWidget(self.preview_note)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        cancel = Button("取消")
        cancel.clicked.connect(self.reject)
        self.ok = Button("使用这个字体", variant="primary")
        self.ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(self.ok)
        lay.addLayout(btns)

        self._populate()

    def _style_list(self):
        bar = theme.qss_color(theme.text(0.25))
        self.list.setStyleSheet(
            "QListWidget{background:transparent;border:none;outline:0;padding:0 2px}"
            "QListWidget::item{background:transparent;border:none}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:4px 0 4px 0}"
            f"QScrollBar::handle:vertical{{background:{bar};border-radius:3px;min-height:24px}}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent}")

    def _preview_qss(self) -> str:
        return (f"QLabel{{background:{theme.qss_color(theme.card())};border-radius:12px;"
                f"padding:9px 14px;color:{theme.qss_color(theme.text())}}}")

    def _populate(self):
        self.list.clear()
        rows = [("", False)] + [(f, f.casefold() in self._cjk) for f in self._families]
        for fam, zh in rows:
            it = QtWidgets.QListWidgetItem(fam or "默认字体")
            it.setData(FONT_ROLE, fam)
            it.setData(CJK_ROLE, zh)
            it.setSizeHint(QtCore.QSize(0, _FontRowDelegate.ROW_H))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.list.addItem(it)

    # ---------- 选择 / 过滤 ----------
    def _select(self, family: str):
        family = str(family or "")
        for i in range(self.list.count()):
            it = self.list.item(i)
            if str(it.data(FONT_ROLE) or "") == family:
                self.list.setCurrentItem(it)
                return
        self.list.setCurrentRow(0)          # 原来的字体已经不在本机字体库了

    def _toggle_zh(self):
        self.zh.set_checked(not self.zh.checked)
        self._apply_filter()

    def _apply_filter(self):
        query = self.search.edit.text().strip().casefold()
        zh_only = self.zh.checked
        self._shown = 0
        for i in range(self.list.count()):
            it = self.list.item(i)
            fam = str(it.data(FONT_ROLE) or "")
            text = fam or "默认字体"
            ok = (not zh_only or not fam or bool(it.data(CJK_ROLE))) and (not query or query in text.casefold())
            it.setHidden(not ok)
            if ok:
                self._shown += 1
        extra = f" · 匹配 {self._shown} 个" if (query or zh_only) else ""
        self.count.setText(f"读取到本机 {len(self._families)} 个字体{extra}。"
                           "选中的字体会应用到灵动岛、设置窗口和右键菜单。")
        cur = self.list.currentItem()
        if cur is None or cur.isHidden():
            for i in range(self.list.count()):
                it = self.list.item(i)
                if not it.isHidden():
                    self.list.setCurrentItem(it)
                    self.list.scrollToItem(it, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
                    break

    def chosen(self) -> str:
        it = self.list.currentItem()
        return str(it.data(FONT_ROLE) or "") if it is not None else ""

    def _update_preview(self):
        if not hasattr(self, "preview"):
            return
        fam = self.chosen()
        f = ui_font(19, W.Medium)
        if fam:
            f.setFamilies([fam] + f.families())
        self.preview.setFont(f)
        self.preview.setText("中文字体预览  AaBbCc  0123456789")
        if not self._shown:
            self.preview_note.setText("没有匹配的字体，换个关键词试试。")
            self.preview_note.set_role("danger")
        elif not fam:
            self.preview_note.setText("默认字体：内置字体栈（HarmonyOS Sans → MiSans → 微软雅黑 UI）")
            self.preview_note.set_role("hint")
        elif fam.casefold() in self._cjk:
            self.preview_note.setText(f"{fam} · 含中文字形，灵动岛和设置窗口都会换用它。")
            self.preview_note.set_role("ok")
        else:
            self.preview_note.setText(f"{fam} · 不含中文字形，中文会自动回退到系统字体。")
            self.preview_note.set_role("accent")

    # ---------- 交互 ----------
    def showEvent(self, e):
        super().showEvent(e)
        QtCore.QTimer.singleShot(0, self._focus_search)

    def _focus_search(self):
        self.search.edit.setFocus(Qt.FocusReason.PopupFocusReason)
        self.search.edit.selectAll()
        cur = self.list.currentItem()
        if cur is not None:
            self.list.scrollToItem(cur, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def eventFilter(self, obj, e):
        if obj is self.search.edit and e.type() == QtCore.QEvent.Type.KeyPress \
                and e.key() == Qt.Key.Key_Down:
            self.list.setFocus()
            return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return
        super().keyPressEvent(e)

    def accept(self):
        if self._shown <= 0:
            return
        super().accept()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(theme.text(0.14), 1))
        p.setBrush(panel_color())
        p.drawRoundedRect(r, 20, 20)


class FontPicker(Hoverable):
    """字体控件：显示当前字体名（用它自己的字形），点击打开本机字体库。"""
    changed = Signal()

    def __init__(self, family: str = "", parent=None):
        super().__init__(parent)
        self._value = str(family or "")
        self.setFixedHeight(FIELD_H)
        self.setFixedWidth(226)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self.open_dialog)

    def value(self) -> str:
        return self._value

    def set_value(self, family):
        fam = str(family or "").strip()
        if fam != self._value:
            self._value = fam
            self.update()

    def label(self) -> str:
        return self._value or "默认字体"

    def font_for_label(self) -> QtGui.QFont:
        f = ui_font(13, W.Medium)
        if self._value:
            f.setFamilies([self._value] + f.families())
        return f

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
            self.open_dialog()
            e.accept()
            return
        super().keyPressEvent(e)

    def open_dialog(self):
        dlg = FontDialog(self._value, self.window())
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.set_value(dlg.chosen())
            self.changed.emit()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(theme.text(0.11), 1))
        p.setBrush(field_color())
        p.drawRoundedRect(r, 10, 10)
        if self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 9, 9)
        p.setFont(self.font_for_label())
        p.setPen(theme.text(0.92))
        text_r = r.adjusted(12, 0, -28, 0)
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(text_r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self.label(), Qt.TextElideMode.ElideRight, int(text_r.width())))
        p.setFont(icon_font(9))
        p.setPen(theme.text(0.45))
        p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, G.DOWN)


# ==================================================================== 列表编辑器
class StringList(QtWidgets.QWidget):
    """字符串列表：逐项输入、可选上下移动与删除。"""
    changed = Signal()

    def __init__(self, values, placeholder: str = "添加一项…", add_label: str = "添加",
                 reorderable: bool = False, parent=None):
        super().__init__(parent)
        self._values = [str(v) for v in (values or []) if isinstance(v, str) or isinstance(v, (int, float))]
        self._placeholder = placeholder
        self._add_label = add_label
        self._reorderable = reorderable
        self._box = QtWidgets.QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(6)
        self._rows = QtWidgets.QVBoxLayout()
        self._rows.setSpacing(6)
        self._box.addLayout(self._rows)
        self._add_box = QtWidgets.QHBoxLayout()
        self._add_box.setSpacing(6)
        self._add_edit = Input(placeholder=placeholder)
        self._add_btn = Button(add_label, G.ADD)
        self._add_edit.edit.returnPressed.connect(self._add)
        self._add_btn.clicked.connect(self._add)
        self._add_box.addWidget(self._add_edit, 1)
        self._add_box.addWidget(self._add_btn)
        self._box.addLayout(self._add_box)
        self._rebuild()

    def _clear(self, layout):
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self):
        self._clear(self._rows)
        for i, val in enumerate(self._values):
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            inp = Input(text=val, placeholder=self._placeholder)
            inp.changed.connect(lambda i=i, w=inp: self._item_changed(i, w.value()))
            h.addWidget(inp, 1)
            if self._reorderable:
                up = IconButton(G.UP, 24, 10, "上移")
                down = IconButton(G.DOWN, 24, 10, "下移")
                up.clicked.connect(lambda _=False, i=i: self._move(i, -1))
                down.clicked.connect(lambda _=False, i=i: self._move(i, 1))
                up.setEnabled(i > 0)
                down.setEnabled(i < len(self._values) - 1)
                h.addWidget(up)
                h.addWidget(down)
            dele = IconButton(G.TRASH, 24, 10, "删除")
            dele.clicked.connect(lambda _=False, i=i: self._remove(i))
            h.addWidget(dele)
            self._rows.addWidget(row)

    def _item_changed(self, i, text):
        if 0 <= i < len(self._values):
            self._values[i] = text
            self.changed.emit()

    def _move(self, i, d):
        j = i + d
        if 0 <= j < len(self._values):
            self._values[i], self._values[j] = self._values[j], self._values[i]
            self._rebuild()
            self.changed.emit()

    def _remove(self, i):
        if 0 <= i < len(self._values):
            self._values.pop(i)
            self._rebuild()
            self.changed.emit()

    def _add(self):
        text = self._add_edit.value()
        if not text:
            return
        self._values.append(text)
        self._add_edit.set_value("")
        self._rebuild()
        self.changed.emit()
        last = self._rows.itemAt(self._rows.count() - 1)
        w = last.widget() if last else None
        if w is not None:
            inp = w.findChild(Input)
            if inp is not None:
                inp.edit.setFocus()

    def value(self) -> list[str]:
        return [str(v).strip() for v in self._values]

    def set_value(self, values):
        self._values = [str(v).strip() for v in (values or [])]
        self._rebuild()


class RecordEditor(QtWidgets.QWidget):
    """结构化列表（城市 / IMAP 账户 / 启动器）：一行一条，字段由 columns 描述。"""
    changed = Signal()

    def __init__(self, items, columns: list[dict], defaults: dict, add_label: str = "添加",
                 empty_hint: str = "", parent=None):
        super().__init__(parent)
        self._items = [dict(it) for it in (items or []) if isinstance(it, dict)]
        self.columns = columns
        self._defaults = defaults
        self._add_label = add_label
        self._box = QtWidgets.QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(6)
        self._rows = QtWidgets.QVBoxLayout()
        self._rows.setSpacing(6)
        self._box.addLayout(self._rows)
        self._empty = TLabel(empty_hint, 11.5, W.Normal, "hint", wrap=True) if empty_hint else None
        if self._empty is not None:
            self._box.addWidget(self._empty)
        foot = QtWidgets.QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        self._add_btn = Button(add_label, G.ADD)
        self._add_btn.clicked.connect(self._add)
        foot.addWidget(self._add_btn)
        foot.addStretch(1)
        self._box.addLayout(foot)
        self._rebuild()

    def _clear(self, layout):
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self):
        self._clear(self._rows)
        for i, item in enumerate(self._items):
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            for col in self.columns:
                key = col["key"]
                kind = col.get("kind", "text")
                if kind == "switch":
                    w = Switch(bool(item.get(key, col.get("default", True))))
                    w.setToolTip(col.get("tip", "启用"))
                    w.changed.connect(lambda i=i, k=key, w=w: self._set(i, k, w.value()))
                    h.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
                elif kind == "int":
                    w = Spin(item.get(key, col.get("default", 0)), col.get("lo", 0), col.get("hi", 99999),
                             1, 0, "", width=col.get("width", 92))
                    w.changed.connect(lambda i=i, k=key, w=w: self._set(i, k, w.value()))
                    h.addWidget(w)
                elif kind == "secret":
                    w = Input(str(item.get(key) or ""), col.get("placeholder", ""), secret=True,
                              width=col.get("width"))
                    w.changed.connect(lambda i=i, k=key, w=w: self._set(i, k, w.value()))
                    h.addWidget(w, col.get("stretch", 1))
                elif kind == "path":
                    w = Input(str(item.get(key) or ""), col.get("placeholder", ""),
                              browse=col.get("browse", "file"), width=col.get("width"))
                    w.changed.connect(lambda i=i, k=key, w=w: self._set(i, k, w.value()))
                    h.addWidget(w, col.get("stretch", 1))
                else:
                    w = Input(str(item.get(key) if item.get(key) is not None else ""),
                              col.get("placeholder", ""), width=col.get("width"),
                              numeric=kind == "number")
                    w.changed.connect(lambda i=i, k=key, w=w: self._set(i, k, w.value()))
                    h.addWidget(w, col.get("stretch", 0) or (1 if not col.get("width") else 0))
            dele = IconButton(G.TRASH, 24, 10, "删除")
            dele.clicked.connect(lambda _=False, i=i: self._remove(i))
            h.addWidget(dele)
            self._rows.addWidget(row)
        if self._empty is not None:
            self._empty.setVisible(not self._items)

    def _set(self, i, key, value):
        if 0 <= i < len(self._items):
            self._items[i][key] = value
            self.changed.emit()

    def _add(self):
        self._items.append(copy.deepcopy(self._defaults))
        self._rebuild()
        self.changed.emit()

    def _remove(self, i):
        if 0 <= i < len(self._items):
            self._items.pop(i)
            self._rebuild()
            self.changed.emit()

    def value(self) -> list[dict]:
        out = []
        for item in self._items:
            clean = {}
            for col in self.columns:
                key = col["key"]
                kind = col.get("kind", "text")
                v = item.get(key)
                if kind == "int":
                    clean[key] = _as_int(v, col.get("default", 0))
                elif kind == "switch":
                    clean[key] = bool(v)
                elif kind == "number":
                    text = str(v if v is not None else "").strip()
                    clean[key] = _as_float(text, None) if text else None
                else:
                    clean[key] = str(v if v is not None else "").strip()
            extra = {k: v for k, v in item.items() if k not in clean}
            if self._is_blank(clean):
                continue
            clean.update(extra)
            out.append(clean)
        return out

    def _is_blank(self, clean: dict) -> bool:
        """新加一行但什么都没填：不写进配置（端口 / 开关保持默认值也算没填）。"""
        for col in self.columns:
            key = col["key"]
            kind = col.get("kind", "text")
            v = clean.get(key)
            if kind == "switch":
                if bool(v) != bool(col.get("default", True)):
                    return False
            elif kind == "int":
                if _as_int(v, -1) != _as_int(col.get("default", 0), -2):
                    return False
            elif str(v or "").strip():
                return False
        return True

    def set_value(self, items):
        self._items = [dict(it) for it in (items or []) if isinstance(it, dict)]
        self._rebuild()


# ==================================================================== 表单骨架
class Group(QtWidgets.QFrame, Themed):
    """一组设置项：一层极淡的圆角底。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._hook_theme()

    def paintEvent(self, _e):
        p = painter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(card_color())
        p.drawRoundedRect(QtCore.QRectF(self.rect()), 16, 16)


class Row(QtWidgets.QWidget, Themed):
    """标题 + 说明 + 右侧控件；控件太宽时改到下方整行（wide）。"""

    def __init__(self, title: str, desc: str = "", control: QtWidgets.QWidget | None = None,
                 wide: bool = False, parent=None):
        super().__init__(parent)
        self._error = ""
        self.control = control
        lay = QtWidgets.QVBoxLayout(self) if wide else QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 10, 9)
        lay.setSpacing(10 if wide else 18)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        self.title = TLabel(title, 13, W.DemiBold, "title", wrap=True)
        self.desc = TLabel(desc, 11.5, W.Normal, "desc", wrap=True)
        if wide:
            # wide 行文字在上、控件在下；限制行宽避免说明太长。
            # 非 wide 行不能设上限：QLabel 的 maximumWidth 会把整段文字布局限死，
            # 剩下的空间被 QHBoxLayout 分到左右，形成明显的左侧空白。
            self.title.setMaximumWidth(620)
            self.desc.setMaximumWidth(620)
        text.addWidget(self.title)
        if desc:
            text.addWidget(self.desc)
        self.error = TLabel("", 11.5, W.Normal, "danger", wrap=True)
        if wide:
            self.error.setMaximumWidth(620)
        self.error.hide()
        text.addWidget(self.error)
        if wide:
            lay.addLayout(text)
            if control is not None:
                lay.addWidget(control)
        else:
            lay.addLayout(text, 1)
            if control is not None:
                lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        if control is not None:
            try:
                if not control.accessibleName():
                    control.setAccessibleName(title)
            except Exception:  # noqa: BLE001
                pass
        self._hook_theme()

    def _on_theme(self):
        try:
            self.update()
        except RuntimeError:
            pass

    def set_error(self, msg: str):
        if not msg:
            self.clear_error()
            return
        self._error = msg
        self.error.setText(msg)
        self.error.show()
        if isinstance(self.control, Field):
            self.control.set_error(True)
        self.update()

    def clear_error(self):
        if not self._error:
            return
        self._error = ""
        self.error.hide()
        if isinstance(self.control, Field):
            self.control.set_error(False)
        self.update()

    @property
    def has_error(self) -> bool:
        return bool(self._error)

    def paintEvent(self, _e):
        if not self._error:
            return
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        c = theme.up()
        fill = QtGui.QColor(c)
        fill.setAlpha(16)
        p.setBrush(fill)
        p.setPen(QtGui.QPen(c, 1))
        p.drawRoundedRect(r, 12, 12)


class Section(QtWidgets.QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.group = Group()
        self.rows = QtWidgets.QVBoxLayout(self.group)
        self.rows.setContentsMargins(8, 4, 8, 4)
        self.rows.setSpacing(0)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addSpacing(14)
        head = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(head)
        h.setContentsMargins(8, 0, 8, 0)
        h.addWidget(TLabel(title, 11.5, W.DemiBold, "hint"))
        h.addStretch(1)
        lay.addWidget(head)
        lay.addSpacing(7)
        lay.addWidget(self.group)

    def add(self, row: QtWidgets.QWidget):
        self.rows.addWidget(row)


class Form(QtWidgets.QWidget):
    """一个分类页：负责建控件、放进 Row，并把绑定交给窗口。"""

    def __init__(self, win: "SettingsWindow", key: str, title: str, desc: str):
        super().__init__()
        self.win = win
        self.key = key
        self.scroll = ScrollArea()
        # ScrollArea 的 inner 默认没有布局，直接挂一个
        outer = QtWidgets.QVBoxLayout(self.scroll.inner)
        outer.setContentsMargins(4, 2, 10, 18)
        outer.setSpacing(0)
        self.outer = outer
        head = TLabel(title, 21, W.DemiBold, "title")
        sub = TLabel(desc, 12.5, W.Normal, "desc", wrap=True)
        sub.setMaximumWidth(620)
        head.setContentsMargins(8, 0, 0, 0)
        outer.addSpacing(6)
        outer.addWidget(head)
        outer.addSpacing(4)
        sub.setContentsMargins(8, 0, 0, 0)
        outer.addWidget(sub)
        self._section: Section | None = None
        self.rows: list[Row] = []
        self._tail = None

    # ---- 分组 ----
    def section(self, title: str):
        sec = Section(title)
        self.outer.addWidget(sec)
        self._section = sec
        self._tail = sec
        return sec

    def _ensure_section(self):
        if self._section is None:
            self.section("设置")

    def _add_row(self, row: Row):
        self._ensure_section()
        self._section.add(row)
        self.rows.append(row)
        return row

    # ---- 通用登记 ----
    def _bind(self, row: Row, control, key: str, getter, setter, default, validate=None):
        self.win.register(self.key, row, control, key, getter, setter, default, validate)

    # ---- 常用控件 ----
    def switch(self, key: str, title: str, desc: str = "", invert: bool = False, default=MISSING):
        real = cfg.get(key, default_of(key))
        ctrl = Switch(checked=(not bool(real)) if invert else bool(real))
        row = self._add_row(Row(title, desc, ctrl))
        dflt = default_of(key) if default is MISSING else default
        if invert:
            self._bind(row, ctrl, key, lambda: not ctrl.value(),
                       lambda v: ctrl.set_value(not v), bool(dflt))
        else:
            self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, bool(dflt))
        return ctrl

    def choice(self, key: str, title: str, desc: str, options: list[tuple], width: int = 180,
               default=MISSING):
        values = [v for v, _ in options]
        cur = cfg.get(key, default_of(key))
        if cur not in values:
            cur = values[0] if values else None
        ctrl = Select(options, cur, width)
        row = self._add_row(Row(title, desc, ctrl))
        dflt = default_of(key) if default is MISSING else default
        if dflt not in values:
            dflt = values[0] if values else None
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, dflt,
                   lambda v: None if v in values else "选项已失效，请重新选择")
        return ctrl

    def number(self, key: str, title: str, desc: str, lo, hi, step=1, decimals: int = 0,
               suffix: str = "", default=MISSING):
        cur = _as_float(cfg.get(key, default_of(key)), float(lo))
        ctrl = Spin(cur, lo, hi, step, decimals, suffix)
        row = self._add_row(Row(title, desc, ctrl))
        dflt = _as_float(default_of(key) if default is MISSING else default, float(lo))
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, dflt,
                   lambda v: None if float(lo) <= float(v) <= float(hi)
                   else f"请输入 {_fmt_range(lo, hi)} 之间的数字")
        return ctrl

    def percent(self, key: str, title: str, desc: str, default=MISSING):
        cur = _as_float(cfg.get(key, default_of(key)), 0.5) * 100
        ctrl = Spin(cur, 0, 100, 1, 0, "%")
        row = self._add_row(Row(title, desc, ctrl))
        base = _as_float(default_of(key) if default is MISSING else default, 0.5)
        self._bind(row, ctrl, key,
                   lambda: round(ctrl.value(), 2) / 100.0,
                   lambda v: ctrl.set_value(_as_float(v, 0.5) * 100),
                   base,
                   lambda v: None if 0.0 <= float(v) <= 1.0 else "取值范围是 0 ~ 100%")
        return ctrl

    def text(self, key: str, title: str, desc: str = "", placeholder: str = "", secret: bool = False,
             width: int | None = None, default=MISSING, validate=None, wide: bool = False):
        cur = cfg.get(key, default_of(key))
        ctrl = Input(str(cur or ""), placeholder, secret=secret, width=width)
        row = self._add_row(Row(title, desc, ctrl, wide=wide))
        dflt = default_of(key) if default is MISSING else default
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, str(dflt or ""), validate)
        return ctrl

    def font_picker(self, key: str, title: str, desc: str = "", default=MISSING):
        cur = str(cfg.get(key, default_of(key)) or "")
        ctrl = FontPicker(cur)
        row = self._add_row(Row(title, desc, ctrl))
        dflt = str(default_of(key) if default is MISSING else default or "")
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, dflt)
        return ctrl

    def path(self, key: str, title: str, desc: str = "", kind: str = "file", default=MISSING,
             placeholder: str = ""):
        cur = cfg.get(key, default_of(key))
        if not placeholder:
            placeholder = "%APPDATA%/…" if "%" in str(default_of(key) or "") else ""
        ctrl = Input(str(cur or ""), placeholder, browse=kind)
        row = self._add_row(Row(title, desc, ctrl))
        dflt = default_of(key) if default is MISSING else default
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value, str(dflt or ""))
        return ctrl

    def hotkey(self, key: str, title: str, desc: str = ""):
        ctrl = Hotkey(str(cfg.get(key, default_of(key)) or ""))
        row = self._add_row(Row(title, desc, ctrl))
        self._bind(row, ctrl, key, ctrl.value, ctrl.set_value,
                   normalize_hotkey(str(default_of(key) or "")),
                   lambda v: hotkey_problem(str(v or "")))
        return ctrl

    def multi(self, key: str, title: str, desc: str, options: list[tuple], default=MISSING):
        cur = cfg.get(key, default_of(key))
        cur = [v for v in cur if isinstance(v, str)] if isinstance(cur, list) else []
        ctrl = Chips(options, cur)
        row = self._add_row(Row(title, desc, ctrl, wide=True))
        dflt = default_of(key) if default is MISSING else default
        dflt = [v for v in (dflt or []) if isinstance(v, str)]
        valid = {v for v, *_ in options}
        self._bind(row, ctrl, key, ctrl.value,
                   lambda v: ctrl.set_value([x for x in (v or []) if x in valid]),
                   dflt, lambda v: None if isinstance(v, list) else "格式错误")
        return ctrl

    def sections(self):
        """显示哪些板块（config 里存的是 hidden_tabs，控件里显示为"要显示的"）。"""
        all_keys = [k for k, _l, _g in SECTION_OPTIONS]
        hidden = cfg.get("island.hidden_tabs")
        hidden = [k for k in hidden if isinstance(k, str)] if isinstance(hidden, list) else []
        ctrl = Chips(SECTION_OPTIONS, [k for k in all_keys if k not in hidden])
        row = self._add_row(Row("显示的板块",
                                "主页和消息固定显示；关闭的板块会从标签栏移除，股票还会停止行情轮询。",
                                ctrl, wide=True))
        self._bind(row, ctrl, "island.hidden_tabs",
                   lambda: [k for k in all_keys if k not in ctrl.value()],
                   lambda v: ctrl.set_value([k for k in all_keys if k not in (v or [])]),
                   [])
        return ctrl

    def custom(self, key: str, title: str, desc: str, editor: QtWidgets.QWidget,
               getter=None, setter=None, default=MISSING, validate=None):
        getter = getter or editor.value
        setter = setter or editor.set_value
        row = self._add_row(Row(title, desc, editor, wide=True))
        dflt = default_of(key) if default is MISSING else default
        self._bind(row, editor, key, getter, setter, dflt, validate)
        return editor

    def note(self, text: str):
        """分组里的一行纯说明（不参与保存）。"""
        self._ensure_section()
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(10, 6, 10, 10)
        lab = TLabel(text, 11.5, W.Normal, "hint", wrap=True)
        lab.setMaximumWidth(600)
        lay.addWidget(lab, 1)
        self._section.add(w)

    def finish(self):
        """页脚：「恢复本页默认值」小按钮。"""
        if not any(b.page == self.key for b in self.win._bindings):
            return
        self.outer.addSpacing(12)
        foot = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(foot)
        lay.setContentsMargins(8, 0, 8, 0)
        btn = Button("恢复本页默认值", height=28)
        btn.clicked.connect(self._reset_page)
        lay.addStretch(1)
        lay.addWidget(btn)
        self.outer.addWidget(foot)

    def _reset_page(self):
        if not Confirm.ask(self.win, "恢复本页默认值？",
                           "这一页的设置会全部恢复为出厂默认；点「保存并应用」之后才会写入 config.json。",
                           ok_text="恢复本页"):
            return
        self.win.reset_page(self.key)

    def action(self, title: str, desc: str, actions: list[tuple]):
        box = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for item in actions:
            label, callback = item[0], item[1]
            variant = item[2] if len(item) > 2 else "ghost"
            btn = Button(label, variant=variant)
            btn.clicked.connect(callback)
            h.addWidget(btn)
        h.addStretch(1)
        self._add_row(Row(title, desc, box, wide=True))

    def reveal(self, row: Row):
        self.win.show_page(self.key)
        QtCore.QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(row, 0, 60))


def _fmt_range(lo, hi) -> str:
    def f(v):
        v = float(v)
        return str(int(v)) if v == int(v) else f"{v:g}"
    return f"{f(lo)} ~ {f(hi)}"


# ==================================================================== 绑定
class Binding:
    __slots__ = ("page", "row", "control", "key", "get", "set", "default", "validate", "original")

    def __init__(self, page, row, control, key, get, set_, default, validate=None):
        self.page = page
        self.row = row
        self.control = control
        self.key = key
        self.get = get
        self.set = set_
        self.default = default
        self.validate = validate
        self.original = None


# ==================================================================== 确认框
class Confirm(QtWidgets.QDialog):
    """和设置窗口同风格的小确认框。"""

    def __init__(self, parent, title: str, text: str, ok_text: str = "确定", danger: bool = False):
        super().__init__(parent)
        # 设置窗口本身是置顶的 Tool 窗口；确认框也必须置顶，否则会被压在设置窗口后面。
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        width = 420
        self.setFixedWidth(width)
        text_width = width - 22 * 2
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(8)

        self.title_label = TLabel(title, 16, W.DemiBold, "title", wrap=True)
        self.title_label.setFixedWidth(text_width)
        title_h = self.title_label.heightForWidth(text_width)
        if title_h <= 0:
            title_h = self.title_label.sizeHint().height()
        self.title_label.setMinimumHeight(title_h)
        lay.addWidget(self.title_label)

        self.body = TLabel(text, 12, W.Normal, "desc", wrap=True)
        self.body.setFixedWidth(text_width)
        body_h = max(40, self.body.heightForWidth(text_width))
        self.body.setMinimumHeight(body_h)
        lay.addWidget(self.body, 1)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        self.cancel = Button("取消")
        self.cancel.clicked.connect(self.reject)
        self.ok = Button(ok_text, variant="danger" if danger else "primary")
        self.ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(self.cancel)
        btns.addWidget(self.ok)
        lay.addLayout(btns)

        # 标题 + 正文按实际换行高度算窗口高度，长文案不会压住按钮。
        content_h = 20 + 16 + title_h + body_h + 32 + 2 * 8
        self.resize(width, max(196, content_h))
        if parent is not None:
            g = parent.frameGeometry()
            self.move(g.center() - self.rect().center())

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(theme.text(0.14), 1))
        p.setBrush(panel_color())
        p.drawRoundedRect(r, 18, 18)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return
        super().keyPressEvent(e)

    @classmethod
    def ask(cls, parent, title: str, text: str, ok_text: str = "确定", danger: bool = False) -> bool:
        dlg = cls(parent, title, text, ok_text, danger)
        dlg.raise_()
        dlg.activateWindow()
        # exec() 进入模态循环后，再抬一次，确保在置顶的设置窗口前面。
        QtCore.QTimer.singleShot(0, lambda: (dlg.raise_(), dlg.activateWindow()))
        res = dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
        dlg.deleteLater()
        return res


# ==================================================================== 窗口
class NavItem(Hoverable):
    def __init__(self, glyph: str, text: str, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.text = text
        self.active = False
        self.setFixedHeight(38)
        self.setFixedWidth(SIDE_W - 8)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_active(self, on: bool):
        if on != self.active:
            self.active = on
            self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0, 1, 0, -1)
        rr = 11
        if self.active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.active_pill())
            p.drawRoundedRect(r, rr, rr)
        elif self._hover.x > 0.01:
            c = theme.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, rr, rr)
        fg = theme.pill_text(0.95) if self.active else theme.text(0.72)
        p.setFont(icon_font(14))
        p.setPen(fg)
        p.drawText(QtCore.QRectF(r.x() + 12, r.y(), 22, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.setFont(ui_font(12.5, W.DemiBold if self.active else W.Normal))
        p.drawText(QtCore.QRectF(r.x() + 42, r.y(), r.width() - 50, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)


class Logo(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None, size: int = 40):
        super().__init__(parent)
        self._size = max(16, int(size))
        self.setFixedSize(self._size, self._size)
        path = asset_path("logo.png")
        self._pm = QtGui.QPixmap(str(path)) if path is not None else QtGui.QPixmap()
        self._hook_theme()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._size * 0.275
        if not self._pm.isNull():
            path = QtGui.QPainterPath()
            path.addRoundedRect(r, radius, radius)
            p.save()
            p.setClipPath(path)
            p.drawPixmap(r, self._pm, QtCore.QRectF(self._pm.rect()))
            p.restore()
            p.setPen(QtGui.QPen(theme.text(0.14), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, radius, radius)
            return
        c = theme.on_color()
        fill = QtGui.QColor(c)
        fill.setAlpha(38)
        p.setBrush(fill)
        p.setPen(QtGui.QPen(QtGui.QColor(c.red(), c.green(), c.blue(), 110), 1))
        p.drawRoundedRect(r, radius, radius)
        p.setFont(icon_font(self._size * 0.475))
        p.setPen(c)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, G.WINDOWS)


class LinkText(Hoverable):
    """主题色文字链接：悬停出现下划线，支持鼠标点击与键盘 Enter / Space。"""

    def __init__(self, text: str, url: str = "", px: float = 12.5, parent=None):
        super().__init__(parent)
        self._text = text or url
        self.url = url or text
        self._px = px
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(self._text)
        self.setToolTip(self.url)
        self.setMinimumWidth(50)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(QtGui.QFontMetrics(self.font_).height() + 3)
        self.clicked.connect(self._activate)

    @property
    def font_(self) -> QtGui.QFont:
        return ui_font(self._px, W.Medium)

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self.font_)
        return QtCore.QSize(fm.horizontalAdvance(self._text) + 4, fm.height() + 3)

    def minimumSizeHint(self):
        return QtCore.QSize(min(60, self.sizeHint().width()), self.sizeHint().height())

    def setText(self, text: str):
        if text != self._text:
            self._text = text
            self.setAccessibleName(text)
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        return self._text

    def refresh_fonts(self):
        self.setFixedHeight(QtGui.QFontMetrics(self.font_).height() + 3)
        self.updateGeometry()
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate()
            e.accept()
            return
        super().keyPressEvent(e)

    def _activate(self):
        if self.url:
            _open_url(self.url)

    def paintEvent(self, _e):
        p = painter(self)
        p.setFont(self.font_)
        color = theme.link()
        p.setPen(color)
        fm = QtGui.QFontMetrics(self.font_)
        r = QtCore.QRectF(self.rect())
        text = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, max(1, int(r.width())))
        p.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        # 静止时也画一条很淡的下划线，明确这是可点击链接；悬停 / 聚焦时加深。
        active = max(self._hover.x, 1.0 if self.hasFocus() else 0.0)
        c = QtGui.QColor(color)
        c.setAlphaF(0.35 + 0.65 * min(1.0, active))
        p.setPen(QtGui.QPen(c, 1))
        width = min(float(fm.horizontalAdvance(text)), r.width())
        baseline = r.center().y() + (fm.ascent() - fm.descent()) / 2
        p.drawLine(QtCore.QPointF(r.left(), baseline + 2.5),
                   QtCore.QPointF(r.left() + width, baseline + 2.5))


class Tag(QtWidgets.QWidget, Themed):
    """版本 / 信息胶囊：一层强调色淡底 + 圆角文字，可选点击。

    带 ``url`` 时才会变成可点击控件；没有 url 的版本胶囊外观和行为保持原样。
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent=None, px: float = 11.5, height: int = 24,
                 url: str = ""):
        super().__init__(parent)
        self._text = text
        self._px = px
        self.url = str(url or "")
        self._hover = 0.0
        self._pressed = False
        self.setFixedHeight(height)
        self.setAccessibleName(text)
        if self.url:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fit()
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self._hook_theme()

    @property
    def font_(self) -> QtGui.QFont:
        return ui_font(self._px, W.DemiBold, num=True)

    def _fit(self):
        fm = QtGui.QFontMetrics(self.font_)
        self.setFixedWidth(fm.horizontalAdvance(self._text) + 20)

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self.font_)
        return QtCore.QSize(fm.horizontalAdvance(self._text) + 20, self.height())

    def setText(self, text: str):
        text = text or ""
        if text != self._text:
            self._text = text
            self.setAccessibleName(text)
            self._fit()
            self.update()

    def text(self) -> str:
        return self._text

    def refresh_fonts(self):
        self._fit()
        self.update()

    def enterEvent(self, e):
        if self.url:
            self._hover = 1.0
            self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = 0.0
        self._pressed = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if self.url and e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if self.url and e.button() == Qt.MouseButton.LeftButton:
            was_pressed = self._pressed
            self._pressed = False
            self.update()
            if was_pressed and self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if self.url and e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        accent = theme.link()
        hover = self._hover if self.url else 0.0
        if self._pressed:
            hover = 1.0
        bg = QtGui.QColor(accent)
        bg.setAlpha(32 + round(22 * hover))
        border = QtGui.QColor(accent)
        border.setAlpha(96 + round(48 * hover) + (36 if self.hasFocus() else 0))
        p.setPen(QtGui.QPen(border, 1))
        p.setBrush(bg)
        radius = r.height() / 2
        p.drawRoundedRect(r, radius, radius)
        p.setFont(self.font_)
        p.setPen(theme.text(0.95))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)


class TagRow(QtWidgets.QWidget):
    """标题 + 胶囊左对齐的一行（作者 / 开源协议）。"""

    def __init__(self, title: str, tag: Tag, parent=None):
        super().__init__(parent)
        self.tag = tag
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 10, 9)
        lay.setSpacing(6)
        self.title = TLabel(title, 13, W.DemiBold, "title")
        lay.addWidget(self.title, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(tag, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)


class ProgressLine(QtWidgets.QWidget, Themed):
    """细圆角进度条；总大小未知时可切换为来回扫动的 indeterminate 状态。"""

    def __init__(self, parent=None, height: int = 6):
        super().__init__(parent)
        self._value = 0.0
        self._indeterminate = False
        self._phase = 0.0
        self.setAccessibleName("更新进度")
        self.setFixedHeight(height)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self._timer = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._hook_theme()

    def set_value(self, value: float):
        self._value = max(0.0, min(1.0, float(value or 0.0)))
        self.update()

    def value(self) -> float:
        return self._value

    def set_indeterminate(self, on: bool):
        on = bool(on)
        if on == self._indeterminate:
            if on and not self._timer.isActive():
                self._timer.start()
            return
        self._indeterminate = on
        self._phase = 0.0
        if on:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def stop(self):
        self._timer.stop()
        self._indeterminate = False
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.025) % 2.0
        self.update()

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        if self._indeterminate and not self._timer.isActive():
            self._timer.start()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        radius = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.track())
        p.drawRoundedRect(r, radius, radius)
        accent = theme.on_color()
        if self._indeterminate:
            width = max(r.height() * 2.0, r.width() * 0.28)
            if r.width() <= width:
                p.setBrush(accent)
                p.drawRoundedRect(r, radius, radius)
                return
            t = abs(self._phase - 1.0)          # 0 → 1 → 0 来回扫
            x = (r.width() - width) * t
            bar = QtCore.QRectF(r.x() + x, r.y(), width, r.height())
        elif self._value > 0:
            bar = QtCore.QRectF(r.x(), r.y(), max(r.height(), r.width() * self._value), r.height())
        else:
            return
        p.setBrush(accent)
        p.drawRoundedRect(bar, radius, radius)


class UpdatePanel(QtWidgets.QWidget):
    """关于页的更新面板：检查版本、后台下载、显示进度并重启。

    所有后台回调都会先检查 ``_alive``；设置窗口关闭或 ``_reload()`` 时会调用
    ``shutdown()`` 设置取消事件，因此不会去碰已经销毁的控件。
    """

    def __init__(self, win: "SettingsWindow", parent=None):
        super().__init__(parent)
        self.win = win
        self._alive = True
        self._cancel = threading.Event()
        self._checking = False
        self._updating = False
        self._auto_checked = False
        self._remote = ""
        self._result: dict | None = None
        self._running = str(APP_VERSION)
        self._current = self._running       # 向后兼容：界面比较始终以运行版本为准
        self._pending = False
        self._pending_version = ""
        self._packaged = packaged_update.is_frozen()
        self._packaged_version = ""
        self._update_seq = 0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)

        info = QtWidgets.QHBoxLayout()
        info.setSpacing(8)
        info.addWidget(TLabel("当前版本", 11.5, W.Normal, "hint"))
        self.current_tag = Tag(f"v{self._running}")
        self.current_tag.setObjectName("currentVersionTag")
        info.addWidget(self.current_tag)
        info.addSpacing(16)
        info.addWidget(TLabel("新版本", 11.5, W.Normal, "hint"))
        self.remote_label = TLabel("尚未检查", 12, W.DemiBold, "title")
        self.remote_label.setObjectName("remoteVersionLabel")
        info.addWidget(self.remote_label)
        info.addStretch(1)
        lay.addLayout(info)

        self.status = TLabel("点「检查更新」查看 CNB 上的最新版本。", 11.5, W.Normal, "hint",
                             wrap=True)
        self.status.setObjectName("updateStatusLabel")
        lay.addWidget(self.status)

        self.progress = ProgressLine()
        self.progress.setObjectName("updateProgress")
        self.progress.hide()
        lay.addWidget(self.progress)

        self.detail = TLabel("", 11, W.Normal, "hint", wrap=True)
        self.detail.setObjectName("updateDetailLabel")
        self.detail.hide()
        lay.addWidget(self.detail)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        self.check_btn = Button("检查更新", G.REFRESH)
        self.check_btn.clicked.connect(self.check)
        self.download_btn = Button("下载并更新", G.DOWN)
        # 检查到更新前保持禁用；打包版在“同版本 / 更低版本”时也保持禁用
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._download_clicked)
        self.restart_btn = Button("立即重启", G.POWER, variant="primary")
        self.restart_btn.clicked.connect(self._restart_clicked)
        self.restart_btn.hide()
        self.cancel_btn = Button("取消更新", variant="danger")
        self.cancel_btn.clicked.connect(self._cancel_update)
        self.cancel_btn.hide()
        buttons.addWidget(self.check_btn)
        buttons.addWidget(self.download_btn)
        buttons.addWidget(self.restart_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        lay.addLayout(buttons)

        self.hint = TLabel(
            "更新会下载对应版本的压缩包，退出后自动替换 exe 与 _internal 并重启；"
            "config.json、data、models 会保留；旧版本备份在 data/updates，只保留最近一份。"
            "替换失败会自动回滚；若新版本启动失败，可用备份手动回滚。"
            "更新过程中 wisland 会退出并自动重启。"
            if self._packaged else
            "更新会保留 config.json、data、models、.venv；完成后会自动重启 wisland。"
            "更新前会备份到 data/updates，失败自动回滚。",
            11, W.Normal, "desc", wrap=True)
        self.hint.setObjectName("updateHintLabel")
        lay.addWidget(self.hint)

        self._restart_timer = QtCore.QTimer(self, singleShot=True)
        self._restart_timer.timeout.connect(self._auto_restart)

        if self._packaged:
            # 打包版的版本号编译在 exe 里，不存在“替换完只差重启”的 pending 状态；
            # 这里只展示上一次 helper 写回的更新结果。
            self._show_packaged_result()
        else:
            self._detect_pending()

    # ---- 生命周期 ----
    def showEvent(self, e):
        super().showEvent(e)
        if self._alive and not self._pending and not self._auto_checked:
            self._auto_checked = True
            QtCore.QTimer.singleShot(0, self.check)

    def shutdown(self):
        """窗口关闭 / 重建时调用：取消后台任务和延迟重启，之后所有回调整体忽略。"""
        self._alive = False
        self._cancel.set()
        try:
            self._restart_timer.stop()
        except RuntimeError:
            pass
        try:
            self.progress.stop()
        except RuntimeError:
            pass

    # ---- 待重启状态 ----
    def _detect_pending(self):
        """磁盘版本比正在运行的版本新：说明上次已经替换，只差重启。"""
        try:
            disk = updater.current_version()
            if updater.version_tuple(disk) > updater.version_tuple(self._running):
                self._enter_pending(disk)
        except updater.UpdateError:
            pass

    def _enter_pending(self, version: str, result: dict | None = None):
        self._pending = True
        self._pending_version = version
        self._remote = version
        self._result = result
        self._auto_checked = True
        try:
            self.current_tag.setText(f"v{self._running}")
            self.remote_label.setText(f"v{version}")
            self.remote_label.set_role("title")
            self.progress.show()
            self.progress.set_indeterminate(False)
            self.progress.set_value(1.0)
            self.check_btn.setEnabled(False)
            self.check_btn.set_variant("ghost")
            self.download_btn.setEnabled(False)
            self.download_btn.set_variant("ghost")
            self.cancel_btn.hide()
            self.cancel_btn.setEnabled(True)
            self.restart_btn.setEnabled(True)
            self.restart_btn.show()
        except RuntimeError:
            return

        if result:
            old = str(result.get("old_version") or "")
            backup = str(result.get("backup") or "")
            changed = result.get("changed") or []
            parts = [f"旧版本 {old} 已备份"] if old else []
            if backup:
                parts.append(f"备份位置：{backup}")
            if changed:
                parts.append(f"共替换 {len(changed)} 项程序文件")
            if "requirements.txt" in changed:
                parts.append("依赖清单有变化，重启前请重新运行 setup.bat")
            self._set_detail("；".join(parts) + "。" if parts else "")
            self._set_status(f"已下载并替换到 v{version}，重启 wisland 后生效", "accent")
        else:
            self._set_detail("")
            self._set_status(f"已下载 v{version}，重启 wisland 后生效", "accent")

    # ---- 状态展示 ----
    def _set_status(self, text: str, role: str, percent: int | None = None):
        if not self._alive:
            return
        try:
            self.status.setText(text)
            self.status.set_role(role)
            if percent is not None:
                self.progress.show()
                self.progress.set_indeterminate(False)
                self.progress.set_value(percent / 100.0)
        except RuntimeError:
            pass

    def _set_detail(self, text: str):
        if not self._alive:
            return
        try:
            if text:
                self.detail.setText(text)
                self.detail.show()
            else:
                self.detail.hide()
        except RuntimeError:
            pass

    def _can_download(self) -> bool:
        if self._pending:
            return False
        if not self._remote:
            return False
        if self._packaged:
            try:
                return updater.version_tuple(self._remote) > updater.version_tuple(self._running)
            except updater.UpdateError:
                return False
        try:
            return updater.version_tuple(self._remote) >= updater.version_tuple(self._running)
        except updater.UpdateError:
            return False

    # ---- 检查更新 ----
    def check(self):
        if not self._alive or self._pending or self._checking or self._updating:
            return
        self._checking = True
        self._result = None
        self.restart_btn.hide()
        self.cancel_btn.hide()
        self.cancel_btn.setEnabled(True)
        self._set_detail("")
        self.progress.hide()
        self._set_status("正在检查更新…", "accent")
        self.remote_label.setText("检查中…")
        self.remote_label.set_role("title")
        self.check_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.check_btn.set_variant("ghost")
        self.download_btn.set_variant("ghost")
        run_bg(updater.fetch_latest_version, on_done=self._checked,
               on_error=self._check_failed)

    def _checked(self, version):
        if not self._alive or self._pending:
            return
        self._checking = False
        try:
            remote = str(version or "").strip()
            remote_t = updater.version_tuple(remote)
            current_t = updater.version_tuple(self._running)
        except updater.UpdateError:
            self._check_failed(updater.UpdateError("新版本号格式不正确，请稍后重试。"))
            return
        self._remote = remote
        self.remote_label.setText(f"v{remote}")
        self.remote_label.set_role("title")
        self.check_btn.setEnabled(True)
        self.download_btn.set_variant("ghost")
        current_text = f"v{self._running}"

        if self._packaged:
            self.download_btn.set_text("下载并更新")
            self.download_btn.set_variant("ghost")
            if remote_t > current_t:
                self._set_status(f"发现新版本 v{remote}（当前 {current_text}）", "accent")
                self.download_btn.set_variant("primary")
                self.download_btn.setEnabled(True)
            elif remote_t == current_t:
                self._set_status(f"已是最新版本 {current_text}", "ok")
                self.download_btn.setEnabled(False)
            else:
                self._set_status(f"线上版本 v{remote} 低于当前版本 {current_text}", "hint")
                self.download_btn.setEnabled(False)
            return

        if remote_t > current_t:
            self._set_status(f"发现新版本 v{remote}（当前 {current_text}）", "accent")
            self.download_btn.set_text("下载并更新")
            self.download_btn.set_variant("primary")
            self.download_btn.setEnabled(True)
        elif remote_t == current_t:
            self._set_status(f"已是最新版本 {current_text}", "ok")
            self.download_btn.set_text("重新下载并覆盖")
            self.download_btn.setEnabled(True)
        else:
            self._set_status(f"线上版本 v{remote} 低于当前版本 {current_text}", "hint")
            self.download_btn.set_text("无需更新")
            self.download_btn.setEnabled(False)

    def _check_failed(self, exc):
        if not self._alive or self._pending:
            return
        self._checking = False
        self._remote = ""
        self.remote_label.setText("—")
        self.remote_label.set_role("title")
        message = str(exc) if isinstance(exc, updater.UpdateError) else "检查更新失败，请稍后重试。"
        self._set_status(f"检查更新失败：{message}", "danger")
        self.check_btn.setEnabled(True)
        self.check_btn.set_variant("ghost")
        self.download_btn.set_variant("ghost")
        self.download_btn.set_text("下载并更新")
        self.download_btn.setEnabled(False)

    # ---- 下载 / 更新 ----
    def _download_clicked(self):
        if not self._alive or self._pending or self._checking or self._updating:
            return
        if self._packaged:
            self._packaged_download_clicked()
            return
        if not self._can_download():
            return

        git_note = ""
        if (ROOT / ".git").exists():
            git_note = "\n当前是 Git 工作区：未提交的源码修改会被覆盖。"
        text = (f"将从 CNB 拉取最新源码 v{self._remote} 并替换程序文件。\n"
                "config.json、data/、models/、.venv 会保留，更新完成后会自动重启 wisland。"
                + git_note)
        if not Confirm.ask(self.win, "下载并更新？", text, ok_text="下载并更新", danger=True):
            return

        self._updating = True
        self._result = None
        self._cancel = threading.Event()
        self.check_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.check_btn.set_variant("ghost")
        self.download_btn.set_variant("ghost")
        self.restart_btn.hide()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.show()
        self._set_detail("")
        self.progress.show()
        self.progress.set_indeterminate(False)
        self.progress.set_value(0.0)
        self._set_status("正在准备更新…", "accent", 0)
        run_bg(updater.run_update, ROOT, self._cancel,
               self._progress_from_worker, self._status_from_worker,
               on_done=self._updated, on_error=self._update_failed)

    def _cancel_update(self):
        if not self._alive or not self._updating:
            return
        self._cancel.set()
        self.cancel_btn.setEnabled(False)
        self._set_status("正在取消更新…", "hint")

    def _progress_from_worker(self, done, total):
        if not self._alive:
            return
        bg_ui(lambda: self._on_progress(done, total))

    def _status_from_worker(self, text, percent=None):
        if not self._alive:
            return
        bg_ui(lambda: self._on_update_status(text, percent))

    def _on_progress(self, done, total):
        if not self._alive or not self._updating:
            return
        try:
            if total > 0:
                ratio = min(1.0, done / max(1, total))
                self.progress.set_indeterminate(False)
                self.progress.set_value(0.05 + 0.70 * ratio)
                self.status.setText(
                    f"正在下载更新包… {fmt_bytes(done)} / {fmt_bytes(total)}")
            else:
                self.progress.set_indeterminate(True)
                self.status.setText(f"正在下载更新包… 已下载 {fmt_bytes(done)}")
            self.status.set_role("accent")
        except RuntimeError:
            pass

    def _on_update_status(self, text, percent=None):
        if not self._alive or not self._updating:
            return
        if text:
            role = "ok" if "更新完成" in text else "accent"
            self._set_status(text, role, percent)
        elif percent is not None:
            self._set_status("", "accent", percent)

    def _updated(self, result):
        if not self._alive:
            return
        self._updating = False
        self._cancel.clear()
        result = result if isinstance(result, dict) else {}
        version = str(result.get("version") or self._remote or self._pending_version)
        if not version:
            version = self._running
        self._enter_pending(version, result=result)
        self._schedule_auto_restart()

    def _update_failed(self, exc):
        if not self._alive:
            return
        self._updating = False
        self._cancel.clear()
        self._pending = False
        try:
            self.progress.set_indeterminate(False)
            self.progress.set_value(0.0)
            self.progress.hide()
            self.cancel_btn.hide()
            self.cancel_btn.setEnabled(True)
            self.restart_btn.hide()
            self.check_btn.setEnabled(True)
            self.check_btn.set_variant("ghost")
            self.download_btn.set_variant("ghost")
            if self._packaged:
                self.download_btn.set_text("下载并更新")
            if self._can_download():
                self.download_btn.setEnabled(True)
            else:
                self.download_btn.setEnabled(False)
        except RuntimeError:
            return
        self.remote_label.set_role("title")
        if isinstance(exc, updater.UpdateCancelled):
            self._set_status(str(exc) or "更新已取消。", "hint")
            self._set_detail("")
            return
        message = str(exc) if isinstance(exc, updater.UpdateError) else "更新失败，请稍后重试。"
        self._set_status(f"更新失败：{message}", "danger")
        backup = getattr(exc, "backup", None)
        self._set_detail(f"备份位置：{backup}" if backup else "")

    # ---- 打包版：下载 → 退出 → helper 替换 ----
    def _show_packaged_result(self):
        """打开关于页时展示上次 helper 写回的结果；展示后立即清除，避免重复提示。"""
        try:
            result = packaged_update.read_result(ROOT)
        except Exception:  # noqa: BLE001 - 读结果失败不应影响设置窗口
            return
        if not result:
            return
        try:
            packaged_update.clear_result(ROOT)
        except Exception:  # noqa: BLE001
            pass
        # 让启动结果留在界面上，不马上被自动检查覆盖；用户仍可以手动点「检查更新」
        self._auto_checked = True

        version = str(result.get("version") or "")
        old = str(result.get("old_version") or "")
        backup = str(result.get("backup") or "")
        if result.get("status") == "ok":
            text = f"已更新到 v{version}" if version else "已更新到新版本"
            if backup:
                text += f"（旧版备份：{backup}）"
            self._set_status(text, "ok")
            parts = [f"从 v{old} 更新而来"] if old else []
            backups = result.get("backups") or []
            names = ([Path(str(path)).name for path in backups]
                     if isinstance(backups, list) else [])
            if names:
                parts.append("备份文件：" + "、".join(names[:4]))
            parts.append("如需回滚，请退出 wisland 后把备份文件改回原名")
            self._set_detail("；".join(parts) + "。")
            try:
                self.progress.show()
                self.progress.set_indeterminate(False)
                self.progress.set_value(1.0)
            except RuntimeError:
                pass
            return

        message = str(result.get("message") or "上次更新没有完成。")
        self._set_status(f"上次更新失败：{message}", "danger")
        parts = [f"备份位置：{backup}"] if backup else []
        parts.append("可退出 wisland 后把 data/updates 里的 *.old-* 备份改回原名恢复旧版本")
        self._set_detail("；".join(parts) + "。")

    def _packaged_download_clicked(self):
        """打包版：确认后后台下载并校验，成功后再启动 helper 退出替换。"""
        if not self._alive or self._checking or self._updating:
            return
        if not self._can_download():
            return
        version = self._remote
        dirty = 0
        try:
            dirty = len(self.win._changed())
        except Exception:  # noqa: BLE001 - 窗口正在销毁时按无未保存处理
            dirty = 0
        text = (f"将下载 wisland v{version} 的打包版压缩包（约 100 MB 以上），"
                "然后退出 wisland、替换 exe 与 _internal 并自动重启。\n"
                "config.json、data/、models/ 会保留；旧版本备份在 data/updates。")
        if dirty:
            text += f"\n还有 {dirty} 项设置修改未保存，更新重启后会丢失。"
        if not Confirm.ask(self.win, f"下载并更新到 v{version}？", text,
                           ok_text="下载并更新", danger=True):
            return

        self._updating = True
        self._update_seq += 1
        self._packaged_version = version
        self._result = None
        self._cancel = threading.Event()
        self.check_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.check_btn.set_variant("ghost")
        self.download_btn.set_variant("ghost")
        self.restart_btn.hide()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.show()
        self._set_detail("")
        self.progress.show()
        self.progress.set_indeterminate(False)
        self.progress.set_value(0.0)
        self._set_status(f"正在准备下载 v{version}…", "accent", 0)
        run_bg(packaged_update.prepare_update, version, self._cancel,
               self._progress_from_worker, self._status_from_worker,
               on_done=self._packaged_ready, on_error=self._packaged_failed)

    def _packaged_ready(self, prepared):
        """下载 / 解压 / 校验完成：写任务、启动 helper，然后退出 Qt。"""
        prepared = prepared if isinstance(prepared, dict) else {}
        version = str(prepared.get("version") or self._packaged_version or self._remote)
        if not self._alive:
            # 窗口在下载期间被关闭：不要启动 helper，直接清掉 stage
            self._discard_stage_async(version, self._update_seq)
            return
        if self._cancel.is_set():
            # prepare_update 已完成、on_done 才排队时用户点了取消：不能替换 / 退出
            self._packaged_failed(updater.UpdateCancelled("更新已取消。"), version)
            return
        self._updating = False
        self._cancel.clear()
        if not version or not prepared.get("stage"):
            self._packaged_failed(updater.UpdateError("更新包没有准备好，请重试。"), version)
            return
        try:
            started = packaged_update.spawn_helper(ROOT, prepared)
        except Exception:  # noqa: BLE001 - 启动助手失败不能把设置窗口带崩
            started = False
        if not started:
            packaged_update.clear_job(ROOT)
            self._packaged_failed(updater.UpdateError(
                "无法启动更新助手，请检查杀毒软件或目录权限后重试。"), version)
            return

        self.cancel_btn.hide()
        self.check_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.progress.show()
        self.progress.set_indeterminate(False)
        self.progress.set_value(1.0)
        self.remote_label.setText(f"v{version}")
        self._set_status(f"已下载 v{version}，正在退出并替换…", "accent", 100)
        self._set_detail(
            "更新过程中 wisland 会退出并自动重启；config.json、data、models 会保留。")
        app = QtWidgets.QApplication.instance()
        if app is not None:
            QtCore.QTimer.singleShot(200, app.quit)

    def _packaged_failed(self, exc, version: str | None = None):
        """打包版下载 / 校验 / 启动助手失败：恢复按钮并清理大文件。"""
        version = version or self._packaged_version or self._remote
        self._discard_stage_async(version, self._update_seq)
        if not self._alive:
            return
        self._updating = False
        self._cancel.clear()
        try:
            self.progress.set_indeterminate(False)
            self.progress.set_value(0.0)
            self.progress.hide()
            self.cancel_btn.hide()
            self.cancel_btn.setEnabled(True)
            self.restart_btn.hide()
            self.check_btn.setEnabled(True)
            self.check_btn.set_variant("ghost")
            self.download_btn.set_text("下载并更新")
            self.download_btn.set_variant("ghost")
            self.download_btn.setEnabled(self._can_download())
        except RuntimeError:
            return
        self.remote_label.set_role("title")
        if isinstance(exc, updater.UpdateCancelled):
            self._set_status(str(exc) or "更新已取消。", "hint")
            self._set_detail("")
            return
        message = str(exc) if isinstance(exc, updater.UpdateError) else "更新失败，请稍后重试。"
        self._set_status(f"更新失败：{message}", "danger")
        backup = getattr(exc, "backup", None)
        self._set_detail(f"备份位置：{backup}" if backup else "")

    def _discard_stage_async(self, version: str, seq: int | None = None):
        """后台删掉本次 stage，避免失败 / 取消后留下上百 MB 文件。

        如果用户马上又点了一次更新（``_update_seq`` 已经变化），就不要删，
        否则可能把新一轮刚下载的 stage 一起清掉；新一轮 prepare 会自己清理旧目录。
        """
        version = str(version or "")
        if not version:
            return
        panel = self

        def task():
            try:
                if seq is not None and panel._update_seq != seq:
                    return
                packaged_update.discard_stage(ROOT, version)
            except Exception:  # noqa: BLE001 - 清理失败留给下次启动的 cleanup_stale
                pass

        try:
            threading.Thread(target=task, name="wisland-update-cleanup", daemon=True).start()
        except RuntimeError:
            pass

    def _schedule_auto_restart(self):
        """仅本次更新成功后调用：稍等片刻让用户看到“正在重启”，然后自动重启。"""
        if not self._alive or not self._pending:
            return
        try:
            self.restart_btn.hide()
            self.check_btn.setEnabled(False)
            self.download_btn.setEnabled(False)
            self._set_status("更新完成，正在重启 wisland…", "accent")
        except RuntimeError:
            return
        self._restart_timer.start(1200)

    def _auto_restart(self):
        if not self._alive or not self._pending:
            return
        self._restart_clicked(auto=True)

    def _restart_clicked(self, auto: bool = False):
        if not self._alive:
            return
        if auto and not self._pending:
            return
        self._restart_timer.stop()
        try:
            dirty = len(self.win._changed())
        except Exception:  # noqa: BLE001 - 窗口正在销毁时按无未保存处理
            dirty = 0
        if dirty and not Confirm.ask(
                self.win,
                "更新完成，需要重启" if auto else "重启前有未保存的修改",
                f"还有 {dirty} 项设置修改未保存，重启后会丢失。",
                ok_text="直接重启", danger=True):
            self._set_status(
                "更新已完成；可保存设置后手动重启" if auto
                else "已取消重启；可保存设置后手动重启", "hint")
            self.restart_btn.setEnabled(True)
            self.restart_btn.show()
            return
        self.restart_btn.setEnabled(False)
        self._set_status("正在重启 wisland…", "accent")
        if updater.restart_after_update(ROOT):
            app = QtWidgets.QApplication.instance()
            if app is not None:
                QtCore.QTimer.singleShot(200, app.quit)
        else:
            self._set_status("无法自动重启，请手动关闭并重新打开 wisland。", "danger")
            self.restart_btn.setEnabled(True)
            self.restart_btn.show()


class SettingsWindow(QtWidgets.QWidget):
    """可视化设置窗口。

    on_apply(prev_data)：保存 / 导入 / 恢复默认后调用；prev_data 是写入前的 cfg.data 副本
    （设置窗口会先改内存里的 cfg.data，调用方需要它才知道"改之前是什么"）。
    """

    closed = Signal()

    def __init__(self, on_apply=None, parent=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowTitle("wisland 设置")
        logo = asset_path("logo.png")
        if logo is not None:
            self.setWindowIcon(QtGui.QIcon(str(logo)))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(760, 520)
        self._on_apply = on_apply
        self._bindings: list[Binding] = []
        self._forms: dict[str, Form] = {}
        self._navs: dict[str, NavItem] = {}
        self._current = PAGES[0][0]
        self._last_page = PAGES[0][0]
        self._root: QtWidgets.QWidget | None = None
        self._update_panel: UpdatePanel | None = None
        self._sticky = ""
        self._positioned = False
        self._closing = False
        self._build()
        self._snapshot()
        self._refresh_dirty(clear_message=True)
        theme.changed.connect(self.update)

    # ================================================================ 结构
    def _shutdown_update_panel(self):
        """先停掉关于页的后台任务，再让旧控件被销毁。"""
        panel = self._update_panel
        self._update_panel = None
        if panel is not None:
            try:
                panel.shutdown()
            except RuntimeError:
                pass

    def cleanup(self):
        """公开的退出清理入口：应用 aboutToQuit 时停掉更新 / 检查任务。"""
        self._shutdown_update_panel()

    def _build(self):
        self._shutdown_update_panel()
        self._bindings = []
        self._forms = {}
        self._navs = {}
        self._root = QtWidgets.QWidget(self)
        root = QtWidgets.QVBoxLayout(self._root)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_header())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(16, 0, 18, 0)
        body.setSpacing(14)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget{background:transparent}")
        self._build_pages()
        body.addWidget(self._make_sidebar())
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        root.addWidget(self._make_footer())

        self._root.setGeometry(self.rect())
        self._fit_sidebar()
        self.show_page(self._last_page if self._last_page in self._forms else PAGES[0][0],
                       scroll_top=True)

    def _make_header(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setFixedHeight(HEADER_H)
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(22, 8, 18, 8)
        lay.setSpacing(12)
        lay.addWidget(Logo())
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(1)
        t = TLabel("wisland 设置", 15, W.DemiBold, "title")
        s = TLabel("config.json · 修改后点「保存并应用」立即生效", 11, W.Normal, "hint")
        titles.addWidget(t)
        titles.addWidget(s)
        lay.addLayout(titles)
        lay.addStretch(1)
        open_btn = IconButton(G.OPEN, 30, 12, "打开 config.json")
        open_btn.clicked.connect(lambda: self._open_config())
        close_btn = IconButton(G.CLOSE, 30, 11, "关闭")
        close_btn.clicked.connect(self.request_close)
        lay.addWidget(open_btn)
        lay.addWidget(close_btn)
        return w

    def _make_sidebar(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setFixedWidth(SIDE_W)
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 8)
        lay.setSpacing(2)
        for key, title, glyph in PAGES:
            item = NavItem(glyph, title)
            item.clicked.connect(lambda k=key: self.show_page(k))
            self._navs[key] = item
            lay.addWidget(item)
        lay.addStretch(1)
        return w

    def _make_footer(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setFixedHeight(FOOTER_H)
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(24, 0, 20, 10)
        lay.setSpacing(8)
        self.dot = Dot()
        self.status = TLabel("", 11.5, W.Normal, "hint")
        lay.addWidget(self.dot)
        lay.addWidget(self.status)
        lay.addStretch(1)
        cancel = Button("取消")
        cancel.clicked.connect(self.request_close)
        self.save_btn = Button("保存并应用", G.CHECK, variant="primary")
        self.save_btn.clicked.connect(self.save)
        lay.addWidget(cancel)
        lay.addWidget(self.save_btn)
        return w

    def _build_pages(self):
        builders = {
            "look": self._page_look,
            "hotkeys": self._page_hotkeys,
            "media": self._page_media,
            "notify": self._page_notify,
            "stocks": self._page_stocks,
            "weather": self._page_weather,
            "translate": self._page_translate,
            "live": self._page_live,
            "launcher": self._page_launcher,
            "system": self._page_system,
            "advanced": self._page_advanced,
            "about": self._page_about,
        }
        descs = {
            "look": "位置上贴哪条边、展开方式、主题与收起态内容。拖动灵动岛也会自动记下位置。",
            "hotkeys": "在任何窗口下都能触发的快捷键。修改后立即重新注册。",
            "media": "正在播放的识别方式、自动切换、歌词源与滚动歌词。",
            "notify": "消息页显示的模块、提醒正文、桌面弹窗，以及邮件未读检查。",
            "stocks": "自选股、行情刷新间隔与涨跌幅提醒。",
            "weather": "天气数据源、默认城市与多城市列表。",
            "translate": "DeepSeek 翻译 Key，以及查词后加入 Anki 的方式。",
            "live": "系统声音 → 日语识别 → 中文翻译的实时字幕参数。修改后重新开启实时字幕生效。",
            "launcher": "剪贴板保留条数与主页底部的应用启动器。",
            "system": "Clash Verge 与亮度控制（Twinkle）的本地路径。",
            "advanced": "配置文件的位置、导入导出，以及恢复默认设置。",
            "about": "应用信息、版本更新与开源说明。",
        }
        for key, title, _glyph in PAGES:
            form = Form(self, key, title, descs.get(key, ""))
            builders[key](form)
            form.finish()
            form.outer.addStretch(1)
            self._forms[key] = form
            self.stack.addWidget(form.scroll)

    def show_page(self, key: str, scroll_top: bool = False):
        if key not in self._forms:
            return
        self._current = key
        self._last_page = key
        self.stack.setCurrentWidget(self._forms[key].scroll)
        for k, item in self._navs.items():
            item.set_active(k == key)
        if scroll_top:
            self._forms[key].scroll.verticalScrollBar().setValue(0)

    def resizeEvent(self, e):
        if self._root is not None:
            self._root.setGeometry(self.rect())
            self._fit_sidebar()
        super().resizeEvent(e)

    def _fit_sidebar(self):
        """窗口很矮时压缩导航项高度，保证 12 个分类（含关于）都能显示出来。"""
        n = len(self._navs)
        if not n:
            return
        avail = self.height() - HEADER_H - FOOTER_H - 14 - 2 * (n - 1)
        h = max(26, min(38, avail // n))
        for item in self._navs.values():
            item.setFixedHeight(h)

    # ================================================================ 绑定 / 校验
    def register(self, page, row, control, key, getter, setter, default, validate=None):
        b = Binding(page, row, control, key, getter, setter, default, validate)
        self._bindings.append(b)
        if hasattr(control, "changed"):
            control.changed.connect(lambda r=row: self._on_control_changed(r))
        return b

    def _on_control_changed(self, row: Row):
        row.clear_error()
        self._sticky = ""
        self._refresh_dirty()

    def _snapshot(self):
        for b in self._bindings:
            b.original = _jsonable(self._safe_get(b))
        self._sticky = ""

    def _safe_get(self, b: Binding):
        try:
            return b.get()
        except Exception:  # noqa: BLE001
            return None

    def _changed(self) -> list[Binding]:
        out = []
        for b in self._bindings:
            if _jsonable(self._safe_get(b)) != b.original:
                out.append(b)
        return out

    def reset_page(self, page: str):
        for b in self._bindings:
            if b.page == page:
                b.set(b.default)
                b.row.clear_error()
        self._refresh_dirty()

    def _refresh_dirty(self, clear_message: bool = True):
        if clear_message:
            self._sticky = ""
        n = len(self._changed())
        if self._sticky:
            return
        if n:
            self._set_status(f"{n} 项修改未保存", "accent")
        else:
            self._set_status("修改后点「保存并应用」生效", "hint")

    def _set_status(self, text: str, role: str, sticky: bool = False):
        self.status.setText(text)
        self.status.set_role(role)
        self.dot.set_color(_role_color(role))
        if sticky:
            self._sticky = text

    def _validate(self) -> list[tuple[Binding, str]]:
        errs: list[tuple[Binding, str]] = []
        for b in self._bindings:
            if b.validate is None:
                continue
            try:
                msg = b.validate(b.get())
            except Exception as e:  # noqa: BLE001
                msg = f"读取失败：{e}"
            if msg:
                errs.append((b, msg))
        # 热键不能重复
        seen: dict[str, list[Binding]] = {}
        for b in self._bindings:
            if not b.key.startswith("hotkeys."):
                continue
            spec = normalize_hotkey(str(self._safe_get(b) or ""))
            if spec:
                seen.setdefault(spec.lower(), []).append(b)
        marked = {id(b) for b in (e[0] for e in errs)}
        for spec, group in seen.items():
            if len(group) < 2:
                continue
            for b in group:
                if id(b) not in marked:
                    errs.append((b, f"热键 {spec} 和另一项重复"))
                    marked.add(id(b))
        return errs

    def _show_errors(self, errs: list[tuple[Binding, str]]):
        for b in self._bindings:
            b.row.clear_error()
        for b, msg in errs:
            b.row.set_error(msg)
        first = errs[0]
        self._forms[first[0].page].reveal(first[0].row)
        more = f"，另外还有 {len(errs) - 1} 项" if len(errs) > 1 else ""
        self._set_status(f"{first[1]}{more}", "danger", sticky=True)

    # ================================================================ 保存 / 应用
    def _collect_data(self, changed: list["Binding"] | None = None) -> dict:
        """当前配置 + 表单里改动过的项；没改过的键保持磁盘上 / 别处写入的值。"""
        data = copy.deepcopy(cfg.data)
        for b in (self._changed() if changed is None else changed):
            _set_path(data, b.key, self._safe_get(b))
        return data

    def save(self) -> bool:
        errs = self._validate()
        if errs:
            self._show_errors(errs)
            return False
        changed = self._changed()
        if not changed:
            self._set_status("没有需要保存的修改", "hint", sticky=True)
            return True
        data = self._collect_data(changed)
        prev = copy.deepcopy(cfg.data)
        with cfg._lock:  # noqa: SLF001 - 与 Config.save() 共用同一把锁
            cfg.data = data
        cfg.save()
        self._snapshot()
        self._refresh_dirty(clear_message=True)
        err = self._apply(prev)
        if err is not None:
            self._set_status(f"配置已保存，但立即生效时出错：{err}", "danger", sticky=True)
            return False
        self._set_status("已保存并应用 · " + time.strftime("%H:%M:%S"), "ok", sticky=True)
        return True

    def _apply(self, prev: dict) -> Exception | None:
        if self._on_apply is None:
            return None
        try:
            self._on_apply(prev)
            return None
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return e

    def request_close(self):
        if self._closing:
            return
        n = len(self._changed())
        if n and not Confirm.ask(self, "放弃未保存的修改？",
                                 f"还有 {n} 项修改没有保存，关闭后这些修改会丢失。",
                                 ok_text="放弃修改", danger=True):
            return
        self.close()

    def closeEvent(self, e):
        self._closing = True
        self._shutdown_update_panel()
        self.closed.emit()
        super().closeEvent(e)
        self.deleteLater()          # 关闭后释放，避免反复打开累积窗口

    # ================================================================ 显示
    def showEvent(self, e):
        super().showEvent(e)
        if not self._positioned:
            self._positioned = True
            scr = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) or QtGui.QGuiApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                w = min(WIN_W, max(760, g.width() - 80))
                h = min(WIN_H, max(520, g.height() - 80))
                self.resize(w, h)
                self.move(g.center() - QtCore.QPoint(self.width() // 2, self.height() // 2))
        self.raise_()
        self.activateWindow()
        QtCore.QTimer.singleShot(0, self._force_foreground)

    def _force_foreground(self):
        try:
            ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
        except Exception:  # noqa: BLE001
            pass

    def mousePressEvent(self, e):
        """拖动标题栏移动窗口。"""
        if (e.button() == Qt.MouseButton.LeftButton and self._root is not None
                and e.position().y() <= HEADER_H):
            wh = self.windowHandle()
            if wh is not None:
                wh.startSystemMove()
                e.accept()
                return
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.request_close()
            return
        if (e.key() == Qt.Key.Key_S and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.save()              # Ctrl+S：保存并应用
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, RADIUS, RADIUS)
        p.fillPath(path, panel_color())
        p.save()
        p.setClipPath(path)
        # 角落一两团极淡的强调色光晕，避免整块纯色显得平
        accent = theme.on_color()
        c = QtGui.QColor(accent)
        c.setAlpha(44)
        g = QtGui.QRadialGradient(QtCore.QPointF(r.width() * 0.08, -r.height() * 0.15), r.height() * 1.25)
        g.setColorAt(0.0, c)
        g.setColorAt(1.0, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(r, g)
        c2 = QtGui.QColor(accent)
        c2.setAlpha(22)
        g2 = QtGui.QRadialGradient(QtCore.QPointF(r.width() * 1.02, r.height() * 1.05), r.height() * 0.9)
        g2.setColorAt(0.0, c2)
        g2.setColorAt(1.0, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(r, g2)
        p.restore()
        p.setPen(QtGui.QPen(theme.text(0.14), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, RADIUS, RADIUS)

    # ================================================================ 页：外观
    def _page_look(self, f: Form):
        f.section("位置与屏幕")
        f.choice("island.edge", "吸附位置",
                 "灵动岛贴在屏幕的哪一条边；贴左 / 右时收起态会自动改成竖排。也可以直接拖它。",
                 [("top", "顶部"), ("bottom", "底部"), ("left", "左侧"), ("right", "右侧")], width=150)
        f.percent("island.pos", "沿边位置", "0% = 最左（上），100% = 最右（下）。拖动灵动岛会自动保存。")
        screens = QtGui.QGuiApplication.screens()
        primary = QtGui.QGuiApplication.primaryScreen()
        opts = [(i, f"显示器 {i + 1}" + ("（主屏）" if s is primary else "")) for i, s in enumerate(screens)]
        cur = _as_int(cfg.get("island.screen", 0), 0)
        if cur not in [i for i, _ in opts]:
            cur = 0
        ctrl = Select(opts, cur, 170)
        row = f._add_row(Row("显示器", "多显示器时选择灵动岛所在的那一块。", ctrl))
        f._bind(row, ctrl, "island.screen", ctrl.value, ctrl.set_value, 0)
        f.switch("island.keep_on_top", "开启保持置顶",
                 "开启后灵动岛始终显示在其他应用窗口前面，悬停展开时也不会被盖住；关闭后可以被其他窗口盖住。")

        f.section("主题与效果")
        f.choice("island.theme", "主题", "跟随背景自动切换，或固定一套配色。",
                 [(k, label) for k, label in THEME_NAMES], width=180)
        f.font_picker("island.font_family", "字体",
               "读取本机已安装的字体；选「默认字体」恢复内置字体栈（HarmonyOS Sans → MiSans → 微软雅黑 UI）。"
               "字体缺中文字形时，中文会自动回退到系统字体。")
        f.switch("island.exclude_from_capture", "出现在截图和录屏里",
                 "关闭时灵动岛不会出现在截图 / 录屏中；取毛玻璃背景时会临时排除自己。", invert=True)
        f.number("island.blur_fps", "背景刷新率", "毛玻璃跟随身后画面的速度。越高越跟手，越费 CPU。",
                 2, 30, suffix="帧/秒")

        f.section("展开与收起")
        f.choice("island.expand_mode", "展开方式",
                 "悬停 = 鼠标移上去展开（关闭「悬停默认展开」后改为单击展开）；常驻 = 一直展开；"
                 "长关 = 悬停 / 单击都不展开，双击灵动岛可以切回来。",
                 [("hover", "悬停展开"), ("always", "常驻展开"), ("never", "长关")], width=160)
        f.switch("island.hover_expand", "悬停默认展开",
                 "开启后鼠标移到灵动岛就自动展开；关闭后单击灵动岛展开，长按或拖拽不算点击。")
        f.number("island.hover_expand_ms", "悬停多少毫秒后展开",
                 "仅在开启「悬停默认展开」时生效，防止鼠标路过时误展开。",
                 0, 1000, step=10, suffix="毫秒")
        f.number("island.collapse_delay_ms", "移开后多少毫秒收起", "鼠标离开后等待的时间。",
                 0, 2000, step=20, suffix="毫秒")
        f.switch("island.passthrough", "鼠标穿透", "鼠标直接穿过灵动岛，只响应右键。")
        f.switch("island.hide_fullscreen", "前台全屏时隐藏", "检测到游戏 / 全屏视频时自动收起并隐藏。")

        f.section("收起态")
        f.choice("island.compact_view", "默认内容",
                 "滚轮可以在几种视图之间切换。",
                 [("clock", "时间天气"), ("pet", "小形象"), ("music", "正在播放")], width=160)
        f.number("island.auto_cycle_s", "自动轮换间隔",
                 "时间天气与小形象每隔多久自动切换一次；0 = 不轮换。",
                 0, 600, step=10, suffix="秒")

        f.section("板块")
        f.sections()

    # ================================================================ 页：热键
    def _page_hotkeys(self, f: Form):
        f.section("快捷键")
        f.hotkey("hotkeys.toggle", "展开 / 收起", "例如 Ctrl+Alt+I")
        f.hotkey("hotkeys.translate", "翻译剪贴板", "读取剪贴板文字并翻译")
        f.hotkey("hotkeys.clipboard", "打开剪贴板", "展开灵动岛并切到剪贴板页")
        f.note("需要至少一个 Ctrl / Alt / Shift / Win 修饰键（F1–F24 可以单独使用）。"
               "留空表示不启用；热键被其他软件占用时会注册失败，换一个组合即可。")

    # ================================================================ 页：媒体
    def _page_media(self, f: Form):
        f.section("播放")
        f.switch("media.auto_show", "开始播放时自动显示正在播放",
                 "暂停一段时间后会自动切回收起前的视图。")
        f.switch("media.lyrics", "显示滚动歌词",
                 "默认先查网易云，失败回退 LRCLIB / 自定义源；都没有时只显示标题。")
        f.switch("media.netease_log", "读取网易云本地播放日志",
                 "网易云默认不接入系统媒体控件，打开它才能显示网易云的播放状态与进度。")
        f.section("歌词源")
        f.text("media.lyrics_sources", "回退顺序",
               "用英文逗号分隔，按顺序尝试：netease（网易云）、lrclib、custom（自定义 API）。",
               placeholder="netease,lrclib,custom", wide=True)
        f.text("media.lyrics_custom_url", "自定义歌词 API",
               "可选。URL 支持 {title} {artist} {album} {duration} {song_id} 占位符；"
               "返回 LRC 文本，或 JSON 里的 lyrics / lrc / syncedLyrics 字段。",
               placeholder="https://…?title={title}&artist={artist}", wide=True)
        f.note("LRCLIB 免费、无需 Key。默认网易云 → LRCLIB → 自定义 API；"
               "在回退顺序里删掉某个源即可关闭它。")
        f.section("播放器识别")
        f.custom("media.priority", "识别优先级",
                 "同时有多个播放器时优先显示靠前的；可以填进程名或应用标识。",
                 StringList(cfg.get("media.priority") or [], placeholder="例如 cloudmusic",
                            add_label="添加", reorderable=True),
                 validate=_validate_priority)

    # ================================================================ 页：消息
    def _page_notify(self, f: Form):
        f.section("消息模块")
        f.multi("notify.modules", "启用的模块",
                "消息页显示的模块；也可以在消息页点 + 添加。", KIND_OPTIONS)
        f.multi("notify.body_preview", "显示正文预览",
                "飞书 / 钉钉可以从右下角通知浮窗读取发送人与正文；其他应用只有写进 Windows 通知中心才有正文。",
                BODY_KIND_OPTIONS)

        f.section("DeepSeek 桌面版")
        f.switch("dsh.enabled", "读取 DeepSeek 桌面版完成通知",
                 "直接读 DSH 的会话日志（%USERPROFILE%\\.dsh\\sessions），一轮回答结束、失败或被截断时提醒；不依赖 dsh-pet 的系统通知。")
        f.switch("dsh.include_subagents", "同时提醒子任务",
                 "子代理会话（delegationDepth > 0）也提醒；默认只看主会话。")
        f.switch("dsh.skip_when_active", "正在看 DSH 时不提醒",
                 "DeepSeek 窗口在前台时不弹提示框，避免和屏幕上的回答重复。")
        f.number("dsh.body_chars", "正文预览长度",
                 "从会话日志里取回答开头多少个字显示在弹窗里。", 40, 240, step=10, suffix="字")
        f.path("dsh.sessions_dir", "会话目录",
               "留空 = %USERPROFILE%\\.dsh\\sessions；一般不用改。", kind="dir",
               placeholder="%USERPROFILE%\\.dsh\\sessions")
        f.note("只读会话日志，不会修改 dsh-pet 或任何其它应用的设置；"
               "如果同时装了 dsh-pet 并开着系统通知，两边会各提醒一次。")

        f.section("桌面弹窗")
        f.switch("notify.popup.enabled", "新消息弹出提示框",
                 "在屏幕指定位置显示圆角提示框，淡入淡出；每条消息独立显示 3 秒，多条会按位置堆叠。")
        f.choice("notify.popup.position", "弹出位置",
                 "多条消息会从边缘向中间排列。",
                 POPUP_POSITIONS, width=165)
        f.number("notify.popup.size", "弹窗大小",
                 "100% 为当前默认尺寸（约 360 × 84）；可调 70%–160%。", 70, 160, step=5, suffix="%")

        f.section("打开应用")
        for kind, label in KIND_OPTIONS:
            f.path(f"notify.open.{kind}", label, "", kind="file",
                   placeholder="程序路径（留空按消息来源打开）")
        f.note("每次打开 wisland 时都会自动读取本机常见程序路径，填入仍为空的项；已经填好的路径不会被覆盖。"
               "关闭应用再打开、重启电脑后自启时都会重新检测。"
               "留空时优先按最近一条消息的来源应用打开（例如 Outlook / Thunderbird）；填了路径则优先启动它。")

        f.section("邮件检查")
        f.number("mail.interval_s", "检查间隔", "每隔多久查一次 IMAP 收件箱的未读数。",
                 30, 3600, step=30, suffix="秒")
        f.custom("mail.imap", "IMAP 账户",
                 "首次运行会自动从 Thunderbird 读取服务器与用户名，填入授权码 / 应用专用密码后才会检查。"
                 "留空密码表示暂不检查该账户。",
                 RecordEditor(
                     cfg.get("mail.imap") or [],
                     [{"key": "host", "placeholder": "服务器，如 imap.qq.com", "stretch": 2},
                      {"key": "port", "kind": "int", "default": 993, "lo": 1, "hi": 65535, "width": 78},
                      {"key": "user", "placeholder": "用户名 / 邮箱", "stretch": 2},
                      {"key": "password", "kind": "secret", "placeholder": "授权码", "stretch": 2},
                      {"key": "enabled", "kind": "switch"}],
                     {"host": "", "port": 993, "user": "", "password": "", "enabled": True},
                     add_label="添加账户", empty_hint="还没有 IMAP 账户。"),
                 validate=_validate_imap)
        f.note("Windows「邮件」/ 新版 Outlook 的提醒不需要填账户：在系统设置里允许该应用通知即可。")

    # ================================================================ 页：股票
    def _page_stocks(self, f: Form):
        f.section("自选")
        f.custom("stocks.watchlist", "自选股",
                 "A 股 sh / sz / bj + 6 位；港股 hk + 5 位；美股 us + 代码。也可以在股票页点 + 搜索添加。",
                 StringList(cfg.get("stocks.watchlist") or [], placeholder="例如 sh600519",
                            add_label="添加"),
                 validate=_validate_watchlist)
        f.section("刷新与提醒")
        f.number("stocks.interval", "行情刷新间隔", "交易时段内的轮询间隔；非交易时段会自动放慢。",
                 2, 60, suffix="秒")
        f.number("stocks.pct_alert", "涨跌幅提醒",
                 "单只股票涨跌幅超过它时弹出提醒（每只每天每个方向一次）；0 = 关闭。",
                 0, 20, step=0.5, decimals=1, suffix="%")
        f.note("到价提醒（stocks.alerts）和自定义提醒文案目前只能在 config.json 里编辑："
               "高级 → 打开 config.json。")

    # ================================================================ 页：天气
    def _page_weather(self, f: Form):
        f.section("数据源")
        f.text("weather.api_key", "OpenWeather API Key",
               "留空则使用免费、无需 Key 的 Open-Meteo。", secret=True, width=250)
        f.choice("weather.units", "温度单位", "只在使用 OpenWeather 时生效；Open-Meteo 固定返回摄氏度。",
                 [("metric", "摄氏度 °C"), ("imperial", "华氏度 °F")], width=150)
        f.number("weather.interval_min", "刷新间隔", "天气变化慢，不建议少于 5 分钟。",
                 5, 180, step=5, suffix="分钟")
        f.section("城市")
        f.text("weather.city", "默认城市",
               "IP 定位失败时使用；定位成功时默认城市跟随公网 IP（天气页右键可绑定固定城市）。",
               width=230)
        f.custom("weather.cities", "城市列表",
                 "这里是你自己添加的城市；默认城市始终显示在首位和收起态，绑定时当前 IP 城市仍作为"
                 "第二项显示。坐标留空时按城市名自动查询；也可以在天气页点 + 搜索添加。",
                 RecordEditor(
                     cfg.get("weather.cities") or [],
                     [{"key": "name", "placeholder": "城市名，如 上海", "width": 170},
                      {"key": "lat", "kind": "number", "placeholder": "纬度（可留空）", "width": 130},
                      {"key": "lon", "kind": "number", "placeholder": "经度（可留空）", "width": 130}],
                     {"name": "", "lat": None, "lon": None},
                     add_label="添加城市"),
                 validate=_validate_cities)

    # ================================================================ 页：翻译
    def _page_translate(self, f: Form):
        f.section("DeepSeek 翻译")
        f.text("translate.api_key", "API Key", "翻译与实时字幕的中文翻译都使用它。", secret=True, width=250)
        f.text("translate.base_url", "接口地址", "一般不用改；兼容 OpenAI 协议的服务也可以填。",
               placeholder="https://api.deepseek.com", width=250, validate=_url_problem)
        f.text("translate.model", "模型", "例如 deepseek-flash。", placeholder="deepseek-flash", width=200)
        f.path("translate.import_from", "从其他配置读取 Key",
               "可选：另一个程序的 config.json，启动时从它的 translate.api_key 读取。", kind="file")
        f.section("单词本 / Anki")
        f.text("learn.deck", "Anki 牌组", "加入单词本后推送到这个牌组。", placeholder="日语::生词", width=200)
        f.text("learn.ankiconnect_url", "AnkiConnect 地址",
               "留空 = 本机 127.0.0.1:8765；AnkiDroid 填「手机IP:8765」。",
               placeholder="127.0.0.1:8765", width=220)
        f.switch("learn.auto_sync", "自动同步到 Anki", "加词后自动推送；Anki 没开时静默跳过。")
        f.switch("learn.screenshot", "截图作为卡片配图", "从播放器窗口截一张画面存进卡片。")
        f.text("learn.player_title", "播放器窗口标题", "截图时用来定位播放器窗口，例如 Kazumi。",
               width=220)

    # ================================================================ 页：实时字幕
    def _page_live(self, f: Form):
        f.section("声音来源")
        f.choice("live.audio.source", "采集来源",
                 "系统声音 = 采集扬声器输出；麦克风 = 采集环境声。",
                 [("loopback", "系统声音"), ("mic", "麦克风")], width=150)
        f.text("live.audio.device", "设备名", "留空使用默认设备；也可以填 SoundCard 里的完整设备名。",
               width=280)
        f.number("live.vad.threshold", "静音检测阈值",
                 "0.1 ~ 0.9，越大越难把声音判定为说话。", 0.1, 0.9, step=0.05, decimals=2)
        f.number("live.vad.min_silence_ms", "最短静音", "静音超过这个长度就断句。",
                 100, 1500, step=50, suffix="毫秒")
        f.number("live.vad.min_speech_ms", "最短语音", "短于这个长度的声音忽略。",
                 100, 1000, step=50, suffix="毫秒")
        f.section("日语识别")
        f.path("live.models_dir", "模型目录",
               "parakeet-ja 与 silero_vad.onnx 所在目录；相对路径以 wisland 目录为准。", kind="dir")
        f.switch("live.asr.partial", "实时显示正在识别的内容", "关闭后只显示整句校对结果。")
        f.number("live.asr.draft_interval_ms", "草稿刷新间隔", "越小出字越快，也越占 CPU。",
                 200, 2000, step=50, suffix="毫秒")
        f.number("live.asr.final_threads", "识别线程数", "越大越快，也越占 CPU。", 1, 16)
        f.number("live.asr.max_segment_s", "单句最长时长", "超过就强制断句。", 3, 30, suffix="秒")
        f.switch("live.asr.context_prompt", "用上一句作为识别提示", "连续说话时更准。")
        f.section("字幕翻译")
        f.switch("live.translate.enabled", "翻译成中文", "关闭后只显示日语识别结果。")
        f.switch("live.translate.live", "边说边译", "只翻译还在滚动的草稿；关闭后等整句出来再翻。")
        f.number("live.translate.live_min_interval_ms", "最小翻译间隔", "限制翻译请求频率。",
                 300, 3000, step=100, suffix="毫秒")
        f.number("live.translate.live_min_new_chars", "最少新增字数", "新增不足这么多字就暂不翻译。", 1, 20)
        f.number("live.translate.context_lines", "上下文行数", "带上前面几句作为翻译上下文。", 0, 10)

    # ================================================================ 页：启动器
    def _page_launcher(self, f: Form):
        f.section("剪贴板")
        f.number("clipboard.max", "保留条数", "只保留最近这么多条；置顶的不计入。", 1, 100)
        f.section("启动器")
        f.custom("launcher", "应用或文件启动器",
                 "主页底部的图标，默认只有文件资源管理器；可以添加任意文件（文档 / 快捷方式 / exe 等）。"
                 "路径可以写环境变量；args 等额外字段会原样保留。",
                 RecordEditor(
                     cfg.get("launcher") or [],
                     [{"key": "name", "placeholder": "名称，如 文件资源管理器", "width": 190},
                      {"key": "path", "kind": "path", "browse": "any",
                       "placeholder": "程序或文件路径", "stretch": 1}],
                     {"name": "", "path": ""},
                     add_label="添加应用或文件", empty_hint="还没有添加应用或文件。"),
                 validate=_validate_launcher)
        f.note("也可以直接在主页启动器点 + 添加应用或任意文件；右键图标可重命名或移除。")

    # ================================================================ 页：系统
    def _page_system(self, f: Form):
        f.section("Clash Verge")
        f.path("clash.config_dir", "配置目录",
               "从这里的 config.yaml 读取控制器地址与 secret；支持 %APPDATA% 这类环境变量。", kind="dir")
        f.number("clash.proxy_port", "混合代理端口", "切换系统代理 / TUN 时写进系统设置。", 1, 65535)
        f.path("clash.app", "Clash Verge 程序", "「打开 Clash Verge」按钮使用；留空按常见安装位置查找。",
               kind="file")
        f.section("亮度")
        f.path("brightness.twinkle", "Twinkle 便携版路径",
               "留空直接使用 DDC/CI / WMI 调亮度；便携版 Twinkle 每次调用会弹「安装中」，建议留空。",
               kind="file")

    # ================================================================ 页：高级
    def _page_advanced(self, f: Form):
        f.section("配置文件")
        f.action("config.json", str(CONFIG_PATH),
                 [("打开", self._open_config), ("在文件夹中显示", self._reveal_config)])
        f.action("重新加载", "从磁盘重新读取 config.json 并应用到灵动岛；未保存的修改会丢失。",
                 [("重新加载", self._reload_from_disk)])
        f.section("导入 / 导出")
        f.action("导出配置", "把当前所有设置（含尚未保存的修改）写成一份 JSON 备份。",
                 [("导出…", self._export)])
        f.action("导入配置", "用一份 JSON 配置覆盖当前设置；缺的项用默认值补齐。",
                 [("导入…", self._import)])
        f.section("恢复默认")
        f.action("恢复所有默认值",
                 "主题、热键、自选股、启动器、API Key 等全部恢复为出厂默认，然后立即保存。",
                 [("恢复默认…", self._restore_defaults, "danger")])
        f.note("少数高级字段（notify.toast_apps、notify.flash_processes、apps.restore_hotkey、"
               "stocks.alerts）仍然需要在 config.json 里编辑，可以点上面的「打开」。")
        f.note(f"数据目录：{DATA_DIR}")

    # ================================================================ 页：关于
    def _page_about(self, f: Form):
        f.outer.addSpacing(8)
        hero = Card(radius=18)
        hero.setObjectName("aboutHero")
        hero.setMinimumHeight(88)
        h = QtWidgets.QHBoxLayout(hero)
        h.setContentsMargins(18, 10, 18, 10)
        h.setSpacing(16)
        h.addWidget(Logo(size=64), 0, Qt.AlignmentFlag.AlignVCenter)

        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(2)
        name = TLabel(APP_NAME, 24, W.DemiBold, "title")
        name.setObjectName("aboutAppName")
        titles.addWidget(name)
        titles.addWidget(TLabel(APP_NAME_CN, 13, W.Medium, "desc"))
        titles.addWidget(TLabel(APP_TAGLINE, 11.5, W.Normal, "hint"))
        h.addLayout(titles, 1)
        version_tag = Tag(f"v{APP_VERSION}")
        version_tag.setObjectName("aboutVersionTag")
        h.addWidget(version_tag, 0, Qt.AlignmentFlag.AlignTop)
        f.outer.addWidget(hero)

        f.section("版本更新")
        panel = UpdatePanel(self)
        panel.setObjectName("updatePanel")
        self._update_panel = panel
        f._section.add(panel)

        f.section("二创信息")
        author_tag = Tag(AUTHOR_NAME, url=AUTHOR_URL)
        author_tag.setObjectName("authorTag")
        author_tag.setToolTip("在浏览器中打开白木的主页")
        author_tag.clicked.connect(lambda: _open_url(AUTHOR_URL))
        author_row = TagRow("作者", author_tag)
        author_row.setObjectName("authorRow")
        f._add_row(author_row)

        open_btn = Button("点击打开", G.OPEN)
        open_btn.setObjectName("openRepoButton")
        open_btn.setToolTip(REPO_URL)
        open_btn.clicked.connect(lambda: _open_url(REPO_URL))
        f._add_row(Row("应用开源", "Ciallo～(∠・ω< )⌒★", open_btn))

        license_tag = Tag(LICENSE_NAME, url=LICENSE_URL)
        license_tag.setObjectName("licenseTag")
        license_tag.setToolTip("在浏览器中打开 MIT License")
        license_tag.clicked.connect(lambda: _open_url(LICENSE_URL))
        license_row = TagRow("开源协议", license_tag)
        license_row.setObjectName("licenseRow")
        f._add_row(license_row)

        f.section("原创信息")
        original_author_tag = Tag(ORIGINAL_AUTHOR_NAME)
        original_author_tag.setObjectName("originalAuthorTag")
        original_author_row = TagRow("作者", original_author_tag)
        original_author_row.setObjectName("originalAuthorRow")
        f._add_row(original_author_row)

        original_open_btn = Button("点击打开", G.OPEN)
        original_open_btn.setObjectName("originalOpenRepoButton")
        original_open_btn.setToolTip(ORIGINAL_REPO_URL)
        original_open_btn.clicked.connect(lambda: _open_url(ORIGINAL_REPO_URL))
        f._add_row(Row("应用开源", "原创项目托管在 GitHub，点击按钮在浏览器打开。",
                       original_open_btn))

        original_license_tag = Tag(ORIGINAL_LICENSE_NAME, url=ORIGINAL_LICENSE_URL)
        original_license_tag.setObjectName("originalLicenseTag")
        original_license_tag.setToolTip("在浏览器中打开 MIT License")
        original_license_tag.clicked.connect(lambda: _open_url(ORIGINAL_LICENSE_URL))
        original_license_row = TagRow("开源协议", original_license_tag)
        original_license_row.setObjectName("originalLicenseRow")
        f._add_row(original_license_row)

    # ================================================================ 高级操作
    def _open_config(self):
        _open_path(CONFIG_PATH)

    def _reveal_config(self):
        _reveal(CONFIG_PATH)

    def _reload_from_disk(self):
        n = len(self._changed())
        if n and not Confirm.ask(self, "放弃未保存的修改？",
                                 f"重新加载会用磁盘上的 config.json 覆盖 {n} 项未保存的修改。",
                                 ok_text="重新加载", danger=True):
            return
        prev = copy.deepcopy(cfg.data)
        cfg.load()
        err = self._apply(prev)
        if err is not None:
            self._set_status(f"应用失败：{err}", "danger", sticky=True)
            return
        self._reload()
        self._set_status("已从磁盘重新加载", "ok", sticky=True)

    def _export(self):
        errs = self._validate()
        if errs:
            self._show_errors(errs)
            return
        start = str(ROOT / "wisland-settings.json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出配置", start, "JSON 文件 (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._collect_data(), ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as e:
            self._set_status(f"导出失败：{e}", "danger", sticky=True)
            return
        self._set_status(f"已导出到 {Path(path).name}", "ok", sticky=True)

    def _import(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "导入配置", str(ROOT), "JSON 文件 (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._set_status(f"读取失败：{e}", "danger", sticky=True)
            return
        if not isinstance(data, dict):
            self._set_status("这个文件不是一份 wisland 配置（顶层需要是 JSON 对象）", "danger", sticky=True)
            return
        if not Confirm.ask(self, "导入配置", f"用 {Path(path).name} 覆盖当前配置？", ok_text="导入"):
            return
        prev = copy.deepcopy(cfg.data)
        with cfg._lock:  # noqa: SLF001
            cfg.data = _merge(DEFAULTS, data)
        cfg.save()
        err = self._apply(prev)
        if err is not None:
            self._set_status(f"已导入，但立即生效时出错：{err}", "danger", sticky=True)
            return
        self._reload()
        self._set_status(f"已导入 {Path(path).name}", "ok", sticky=True)

    def _restore_defaults(self):
        if not Confirm.ask(self, "恢复所有默认值？",
                           "主题、热键、自选股、启动器、API Key 等都会清空并恢复为出厂设置。",
                           ok_text="恢复默认", danger=True):
            return
        prev = copy.deepcopy(cfg.data)
        with cfg._lock:  # noqa: SLF001
            cfg.data = copy.deepcopy(DEFAULTS)
        cfg.save()
        err = self._apply(prev)
        if err is not None:
            self._set_status(f"已恢复默认，但立即生效时出错：{err}", "danger", sticky=True)
            return
        self._reload()
        self._set_status("已恢复所有默认值", "ok", sticky=True)

    def _reload(self):
        self._last_page = self._current
        self._shutdown_update_panel()
        old = self._root
        self._root = None
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        self._build()
        self._snapshot()
        self._refresh_dirty(clear_message=True)


# ==================================================================== 校验函数
def _validate_priority(values) -> str | None:
    if not isinstance(values, list):
        return "播放器优先级格式错误"
    seen = set()
    for v in values:
        s = str(v or "").strip()
        if not s:
            return "播放器列表里有空白项，请填写或删除"
        if s.lower() in seen:
            return f"播放器重复：{s}"
        seen.add(s.lower())
    return None


def _validate_watchlist(values) -> str | None:
    if not isinstance(values, list):
        return "自选股格式错误"
    seen = set()
    for code in values:
        c = str(code or "").strip()
        if not c:
            return "自选股里有空白项，请填写或删除"
        problem = _stock_code_problem(c)
        if problem:
            return problem
        if c.lower() in seen:
            return f"自选股重复：{c}"
        seen.add(c.lower())
    return None


def _validate_imap(accounts) -> str | None:
    if not isinstance(accounts, list):
        return "IMAP 账户格式错误"
    for i, a in enumerate(accounts, 1):
        if not isinstance(a, dict):
            return f"第 {i} 个账户格式错误"
        host = str(a.get("host") or "").strip()
        user = str(a.get("user") or "").strip()
        if not host or not user:
            return f"第 {i} 个账户缺少服务器或用户名"
        if " " in host:
            return f"第 {i} 个账户的服务器地址有空格"
        port = _as_int(a.get("port"), 0)
        if not 1 <= port <= 65535:
            return f"第 {i} 个账户的端口无效（1 ~ 65535）"
    return None


def _validate_cities(cities) -> str | None:
    if not isinstance(cities, list):
        return "城市列表格式错误"
    for i, c in enumerate(cities, 1):
        if not isinstance(c, dict):
            return f"第 {i} 个城市格式错误"
        name = str(c.get("name") or "").strip()
        if not name:
            return f"第 {i} 个城市缺少名字"
        lat, lon = c.get("lat"), c.get("lon")
        if (lat is None) != (lon is None):
            return f"{name} 的经纬度需要同时填写或同时留空"
        if lat is not None:
            try:
                latf, lonf = float(lat), float(lon)
            except (TypeError, ValueError):
                return f"{name} 的经纬度不是数字"
            if not -90 <= latf <= 90 or not -180 <= lonf <= 180:
                return f"{name} 的经纬度超出范围"
    return None


def _validate_launcher(items) -> str | None:
    if not isinstance(items, list):
        return "启动器格式错误"
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            return f"第 {i} 个启动项格式错误"
        if not str(it.get("name") or "").strip():
            return f"第 {i} 个启动项缺少名称"
        if not str(it.get("path") or "").strip():
            return f"第 {i} 个启动项缺少路径"
    return None


__all__ = ["SettingsWindow", "SECTION_OPTIONS", "Confirm"]
