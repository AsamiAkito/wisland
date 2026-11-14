# -*- coding: utf-8 -*-
"""查词弹窗：点字幕里的日语单词弹出；显示读音与词典释义，可加入单词本 / 同步到 Anki。"""
from __future__ import annotations

import html
import ctypes

from PySide6 import QtCore, QtGui, QtWidgets

from .theme import G, W, theme, ui_font
from .widgets import IconButton, LineEdit, PillButton, hbox, painter, vbox

Qt = QtCore.Qt


def _esc(t: str) -> str:
    return html.escape(t or "", quote=False)


class LookupPopup(QtWidgets.QWidget):
    def __init__(self, learn, island=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.learn = learn
        self.island = island
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(380, 340)
        self._ctx: dict = {}
        self._entries: list = []
        self.header = QtWidgets.QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.edit = LineEdit("查词")
        self.edit.setFixedHeight(30)
        self.edit.focus_wanted.connect(self._want_focus)
        self.edit.returnPressed.connect(self._requery)
        self.body = QtWidgets.QTextBrowser()
        self.body.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.body.setOpenLinks(False)
        self.body.viewport().setAutoFillBackground(False)
        self.btn_add = PillButton("加入单词本", G.ADD, height=30, primary=True)
        self.btn_push = PillButton("加入并同步 Anki", G.REFRESH, height=30)
        self.btn_add.clicked.connect(lambda: self._add(False))
        self.btn_push.clicked.connect(lambda: self._add(True))
        close = IconButton(G.CLOSE, 28, 11, "关闭")
        close.clicked.connect(self.hide)
        self.status = QtWidgets.QLabel()
        self.status.setFont(ui_font(11))
        self.setLayout(vbox(hbox((self.header, 1), close), self.edit, (self.body, 1),
                            hbox(self.btn_add, self.btn_push, None, self.status, spacing=6),
                            spacing=8, margins=(16, 12, 12, 12)))
        self._watch = QtCore.QTimer(self, interval=25, timeout=self._outside_check)
        self._armed = False
        theme.changed.connect(self._style)
        self._style()

    # ---------- 外观 ----------
    def _style(self):
        fg = theme.qss_color(theme.text())
        fg2 = theme.qss_color(theme.text2())
        self.header.setStyleSheet(f"color:{fg};background:transparent")
        self.status.setStyleSheet(f"color:{fg2};background:transparent")
        self.body.setStyleSheet(
            f"QTextBrowser{{background:transparent;color:{fg};border:none;font-family:{theme.font_css()};font-size:13px}}"
            "QScrollBar:vertical{width:6px;background:transparent}"
            f"QScrollBar::handle:vertical{{background:{theme.qss_color(theme.text(0.25))};border-radius:3px}}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0}")
        self.update()

    def refresh_fonts(self):
        self._style()

    def paintEvent(self, e):
        p = painter(self)
        bg, _fg, _sel = theme.menu_qss()
        c = QtGui.QColor(0, 0, 0)
        try:
            parts = bg.replace("rgba(", "").replace(")", "").split(",")
            c = QtGui.QColor(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
        except Exception:  # noqa: BLE001
            pass
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QtGui.QPen(theme.rim_top(), 1))
        p.setBrush(c)
        p.drawRoundedRect(r, 18, 18)

    # ---------- 显示 ----------
    def show_for(self, word: str, *, surface="", reading="", pos="", sentence="", sentence_zh="", audio=None):
        self._ctx = {"word": word, "surface": surface, "reading": reading, "pos": pos,
                     "sentence": sentence, "sentence_zh": sentence_zh, "audio": audio}
        self.edit.setText(word)
        self._lookup(word, surface, reading, pos)
        p = QtGui.QCursor.pos()
        scr = QtGui.QGuiApplication.screenAt(p) or QtGui.QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = min(max(g.left() + 8, p.x() - self.width() // 2), g.right() - self.width() - 8)
        y = p.y() + 18 if p.y() + 18 + self.height() < g.bottom() else p.y() - self.height() - 18
        self.move(x, max(g.top() + 8, y))
        self.show()
        self.raise_()
        self._armed = False
        self._watch.start()
        if self.island is not None:
            self.island.locks.add("lookup")

    def hideEvent(self, e):
        self._watch.stop()
        if self.island is not None:
            self.island.locks.discard("lookup")
        super().hideEvent(e)

    def _want_focus(self):
        hwnd = int(self.winId())
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.activateWindow()

    def _outside_check(self):
        """点到弹窗外面就关闭（灵动岛不抢焦点，Qt 自带的失焦关闭不会触发）。"""
        u = ctypes.windll.user32
        down = any(u.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02))
        if not self._armed:
            self._armed = not down
            return
        if down and not self.geometry().contains(QtGui.QCursor.pos()):
            menus = [w for w in QtWidgets.QApplication.topLevelWidgets()
                     if isinstance(w, QtWidgets.QMenu) and w.isVisible()]
            if not any(w.geometry().contains(QtGui.QCursor.pos()) for w in menus):
                self.hide()

    def _requery(self):
        text = self.edit.text().strip()
        if not text:
            return
        toks = [t for t in self.learn.tokens(text) if t.clickable]
        one = toks[0] if len(toks) == 1 else None
        self._ctx["word"] = text
        self._lookup(text, one.lemma if one else "", one.reading if one else "", one.pos if one else "")

    def _lookup(self, word, surface="", reading="", pos=""):
        from ..learn.tokenizer import kata_to_hira
        entries, err = self.learn.lookup(word, surface, reading)
        self._entries = entries
        head = entries[0].term if entries else word
        head_r = entries[0].reading if entries else reading
        if head_r and kata_to_hira(head) == head_r:
            head_r = ""
        acc = theme.qss_color(theme.on_color())
        sub = []
        if surface and surface != head:
            sub.append(f"原文 {_esc(surface)}")
        if pos:
            sub.append(_esc(pos))
        self.header.setText(
            f'<span style="font-size:22px;font-weight:600">{_esc(head)}</span>'
            + (f'&nbsp;&nbsp;<span style="font-size:15px;color:{acc}">【{_esc(head_r)}】</span>' if head_r else "")
            + (f'<br><span style="font-size:11px;color:{theme.qss_color(theme.text3())}">{" · ".join(sub)}</span>' if sub else ""))
        mute = theme.qss_color(theme.text3())
        if err:
            self.body.setHtml(f"<p style='color:{mute}'>{_esc(err)}</p>")
        elif not entries:
            self.body.setHtml(f"<p style='color:{mute}'>无结果</p>")
        else:
            parts, last = [], None
            for e in entries[:8]:
                title = e.dict_title.split("[")[0].strip()
                if title != last:
                    parts.append(f'<p style="color:{mute};font-size:11px;margin:8px 0 2px">{_esc(title)}</p>')
                    last = title
                rd = f' <span style="color:{acc}">【{_esc(e.reading)}】</span>' if e.reading and e.reading != e.term else ""
                parts.append(f'<p style="margin:2px 0"><b>{_esc(e.term)}</b>{rd}</p>{e.html}')
            self.body.setHtml("".join(parts))
        exists = self.learn.has_word(head)
        self.btn_add.set_text("加入单词本")
        self.btn_add.setEnabled(True)
        self.status.setText("已在单词本" if exists else "")

    def _add(self, push: bool):
        from ..learn.service import meaning_for_card
        e0 = self._entries[0] if self._entries else None
        word = e0.term if e0 else (self._ctx.get("word") or self.edit.text().strip())
        reading = e0.reading if e0 else self._ctx.get("reading", "")
        if not word:
            return
        try:
            self.learn.add_card(word=word, reading=reading, meaning=meaning_for_card(self._entries),
                                sentence=self._ctx.get("sentence", ""), sentence_zh=self._ctx.get("sentence_zh", ""),
                                audio=self._ctx.get("audio"), push=push)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"保存失败：{e}")
            return
        self.btn_add.set_text("✓ 已加入")
        self.btn_add.setEnabled(False)
        self.status.setText("同步中…" if push else "")
        QtCore.QTimer.singleShot(700, self.hide)


_ = W
