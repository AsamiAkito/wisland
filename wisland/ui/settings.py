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
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import CONFIG_PATH, DATA_DIR, DEFAULTS, ROOT, cfg, expand
from .brand import asset_path
from .popup import POPUP_POSITIONS
from .theme import G, THEME_NAMES, W, icon_font, theme, ui_font
from .widgets import Hoverable, IconButton, ScrollArea, Themed, painter

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
        "accent": theme.on_color(),
        "danger": theme.up(),
        "ok": theme.down(),
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
        self.clicked.connect(lambda: self.changed.emit())

    def _fit(self) -> int:
        fm = QtGui.QFontMetrics(ui_font(12.5, W.Medium))
        return fm.horizontalAdvance(self.text) + 26 + (20 if self.glyph else 0)

    def set_text(self, t: str):
        self.text = t
        self.setMinimumWidth(self._fit())
        self.update()

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
            p.setBrush(theme.on_color())
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
        p.setPen(fg)
        x = r.x() + 13
        if self.glyph:
            p.setFont(icon_font(12))
            p.drawText(QtCore.QRectF(x, r.y(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
            x += 19
        p.setFont(ui_font(12.5, W.Medium))
        p.drawText(QtCore.QRectF(x, r.y(), r.right() - x - 10, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)


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
        self.resize(400, 196)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(8)
        lay.addWidget(TLabel(title, 16, W.DemiBold, "title", wrap=True))
        body = TLabel(text, 12, W.Normal, "desc", wrap=True)
        body.setMinimumHeight(40)
        lay.addWidget(body, 1)
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        cancel = Button("取消")
        cancel.clicked.connect(self.reject)
        ok = Button(ok_text, variant="danger" if danger else "primary")
        ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        path = asset_path("logo.png")
        self._pm = QtGui.QPixmap(str(path)) if path is not None else QtGui.QPixmap()
        self._hook_theme()

    def paintEvent(self, _e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if not self._pm.isNull():
            path = QtGui.QPainterPath()
            path.addRoundedRect(r, 11, 11)
            p.save()
            p.setClipPath(path)
            p.drawPixmap(r, self._pm, QtCore.QRectF(self._pm.rect()))
            p.restore()
            p.setPen(QtGui.QPen(theme.text(0.14), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 11, 11)
            return
        c = theme.on_color()
        fill = QtGui.QColor(c)
        fill.setAlpha(38)
        p.setBrush(fill)
        p.setPen(QtGui.QPen(QtGui.QColor(c.red(), c.green(), c.blue(), 110), 1))
        p.drawRoundedRect(r, 12, 12)
        p.setFont(icon_font(19))
        p.setPen(c)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, G.WINDOWS)


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
        self._sticky = ""
        self._positioned = False
        self._closing = False
        self._build()
        self._snapshot()
        self._refresh_dirty(clear_message=True)
        theme.changed.connect(self.update)

    # ================================================================ 结构
    def _build(self):
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
        """窗口很矮时压缩导航项高度，保证 11 个分类都能显示出来。"""
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
        f.switch("island.exclude_from_capture", "出现在截图和录屏里",
                 "关闭时灵动岛不会出现在截图 / 录屏中；取毛玻璃背景时会临时排除自己。", invert=True)
        f.number("island.blur_fps", "背景刷新率", "毛玻璃跟随身后画面的速度。越高越跟手，越费 CPU。",
                 2, 30, suffix="帧/秒")

        f.section("展开与收起")
        f.choice("island.expand_mode", "展开方式",
                 "悬停 = 鼠标移上去展开；常驻 = 一直展开；长关 = 悬停 / 单击都不展开，双击灵动岛可以切回来。",
                 [("hover", "悬停展开"), ("always", "常驻展开"), ("never", "长关")], width=160)
        f.number("island.hover_expand_ms", "悬停多少毫秒后展开", "防止鼠标路过时误展开。",
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
                KIND_OPTIONS)

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
        f.note("首次启动会自动读取本机常见程序路径并填入；也可以在这里改成其他路径或清空。"
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
        f.text("weather.city", "默认城市", "城市列表为空时使用；支持中文名（Open-Meteo 模式）。", width=230)
        f.custom("weather.cities", "多城市",
                 "第一个显示在收起态。坐标留空时按城市名自动查询；也可以在天气页点 + 搜索添加。",
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
