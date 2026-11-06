# -*- coding: utf-8 -*-
"""剪贴板历史与日语翻译。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..theme import G, W, icon_font, theme, ui_font
from ..widgets import (Segmented, Card, Hoverable, IconButton, Label, LineEdit, PillButton, ScrollArea, TextEdit, hbox, painter,
                       vbox)
from .base import Page

Qt = QtCore.Qt


class ClipRow(Hoverable):
    """一条剪贴板记录：点击复制；右侧图钉（置顶）和 X（删除）。"""
    pin_toggled = QtCore.Signal()
    delete_clicked = QtCore.Signal()

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.flash = 0.0
        th = item.get("thumb")
        self.pm = QtGui.QPixmap.fromImage(th) if isinstance(th, QtGui.QImage) and not th.isNull() else None
        self.setFixedHeight(64 if self.pm is not None else 46)
        self.setMouseTracking(True)
        self._hot = None

    def _pin_rect(self):
        return QtCore.QRectF(self.width() - 62, (self.height() - 26) / 2, 26, 26)

    def _del_rect(self):
        return QtCore.QRectF(self.width() - 32, (self.height() - 26) / 2, 26, 26)

    def mouseMoveEvent(self, e):
        hot = "pin" if self._pin_rect().contains(e.position()) else "del" if self._del_rect().contains(e.position()) else None
        if hot != self._hot:
            self._hot = hot
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._pin_rect().contains(e.position()):
                self.pin_toggled.emit()
                return
            if self._del_rect().contains(e.position()):
                self.delete_clicked.emit()
                return
        super().mouseReleaseEvent(e)

    def mark_copied(self):
        self.flash = 1.0
        self.update()
        def off():
            try:
                self.flash = 0.0
                self.update()
            except RuntimeError:      # 行已被重建删除
                pass
        QtCore.QTimer.singleShot(900, off)

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.active_pill() if self.flash else theme.card(0.7 + 0.8 * self._hover.x))
        p.drawRoundedRect(r, 14, 14)
        x = r.x() + 12
        if self.pm is not None:
            ir = QtCore.QRectF(x, r.y() + 6, 74, r.height() - 12)
            path = QtGui.QPainterPath()
            path.addRoundedRect(ir, 8, 8)
            p.save()
            p.setClipPath(path)
            sc = self.pm.scaled(ir.size().toSize() * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            sc.setDevicePixelRatio(2)
            p.drawPixmap(ir.topLeft(), sc)
            p.restore()
            x = ir.right() + 10
            txt = "图片"
        else:
            txt = " ".join((self.item.get("text") or "").split())
        tw = r.right() - x - 112
        p.setFont(ui_font(12.5))
        p.setPen(theme.text())
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(x, r.y(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   "已复制" if self.flash else fm.elidedText(txt, Qt.TextElideMode.ElideRight, int(tw)))
        p.setFont(ui_font(10.5, num=True))
        p.setPen(theme.text3())
        p.drawText(QtCore.QRectF(r.right() - 112, r.y(), 44, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   time.strftime("%H:%M", time.localtime(self.item.get("time", 0))))
        pinned = self.item.get("pinned")
        for rect, glyph, hot, base in ((self._pin_rect(), G.PIN if pinned else G.UNPIN, self._hot == "pin", 0.9 if pinned else 0.4),
                                       (self._del_rect(), G.CLOSE, self._hot == "del", 0.4)):
            if hot:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.hover())
                p.drawEllipse(rect)
            p.setFont(icon_font(11))
            p.setPen(theme.text(1.0 if hot else base))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)


class ClipboardPage(Page):
    key, glyph, title, height = "clip", G.CLIP, "剪贴板", 330

    def __init__(self, ctx):
        super().__init__(ctx)
        self.search = LineEdit("搜索")
        self.search.focus_wanted.connect(self.want_focus)
        self.search.textChanged.connect(lambda _: self.refresh())
        clear = PillButton("清空", G.TRASH, height=30)
        clear.clicked.connect(lambda: ctx.clipboard.clear())
        self.sc = ScrollArea()
        self.list = QtWidgets.QGridLayout(self.sc.inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(6)
        self.list.setColumnStretch(0, 1)
        self.list.setColumnStretch(1, 1)
        self.setLayout(vbox(hbox((self.search, 1), clear), (self.sc, 1), spacing=8))
        ctx.clipboard.changed.connect(self._changed)
        self._dirty = True
        self._just = None

    def _changed(self):
        self._dirty = True
        if self.active:
            self.refresh()

    def refresh(self):
        self._dirty = False
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        q = self.search.text().strip().lower()
        n = 0
        for item in self.ctx.clipboard.items():
            if q and (item.get("kind") != "text" or q not in (item.get("text") or "").lower()):
                continue
            row = ClipRow(item)
            iid = item["id"]
            row.clicked.connect(lambda iid=iid, row=row: self._copy(iid, row))
            row.pin_toggled.connect(lambda iid=iid, it=item: self.ctx.clipboard.pin(iid, not it.get("pinned")))
            row.delete_clicked.connect(lambda iid=iid: self.ctx.clipboard.remove(iid))
            self.list.addWidget(row, n // 2, n % 2)
            if self._just and self._just[0] == iid and time.monotonic() - self._just[1] < 0.9:
                row.mark_copied()
            n += 1
        if n == 0:
            self.list.addWidget(Label("空" if not q else "无匹配", 12.5, level=3, align=Qt.AlignmentFlag.AlignCenter), 0, 0, 1, 2)
        self.list.setRowStretch(max(1, (n + 1) // 2), 1)

    def _copy(self, iid, row):
        """点击 = 复制到剪贴板（不粘贴）。"""
        self._just = (iid, time.monotonic())
        row.mark_copied()
        self.ctx.clipboard.copy(iid)

    def on_show(self):
        super().on_show()
        if self._dirty:
            self.refresh()


class LiveView(QtWidgets.QWidget):
    """实时字幕：日语小字 + 中文大字，最新的在最下面；正在说的一句（草稿）实时更新。"""
    MAX = 80

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []          # {sid, jp, zh, final}
        self.partial = ""
        self.hint = ""
        theme.changed.connect(self.update)

    def _row(self, sid):
        for r in reversed(self.rows):
            if r["sid"] == sid:
                return r
        r = {"sid": sid, "jp": "", "zh": "", "final": False}
        self.rows.append(r)
        del self.rows[:-self.MAX]
        return r

    def on_segment(self, sid, jp):
        self._row(sid)["jp"] = jp
        self.partial = ""
        self.update()

    def on_live(self, sid, zh):
        r = self._row(sid)
        if not r["final"]:
            r["zh"] = zh
        self.update()

    def on_final(self, sid, zh):
        r = self._row(sid)
        r["zh"], r["final"] = zh, True
        self.update()

    def on_partial(self, jp):
        self.partial = jp
        self.update()

    def clear(self):
        self.rows, self.partial = [], ""
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        w = self.width() - 8
        y = self.height() - 4
        fj, fz = ui_font(12), ui_font(16, W.Medium)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        items = list(self.rows)
        if self.partial:
            items.append({"jp": self.partial, "zh": "", "final": False, "draft": True})
        if not items:
            p.setFont(ui_font(13))
            p.setPen(theme.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.hint)
            return
        for it in reversed(items):
            zh = it.get("zh", "")
            hz = fmz.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, zh).height() if zh else 0
            hj = fmj.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, it.get("jp", "")).height()
            y -= hz
            if zh:
                p.setFont(fz)
                p.setPen(theme.text(1.0 if it.get("final") else 0.8))
                p.drawText(QtCore.QRect(4, y, w, hz), flags, zh)
            y -= hj + 2
            p.setFont(fj)
            p.setPen(theme.text3() if it.get("draft") else theme.text2())
            p.drawText(QtCore.QRect(4, y, w, hj), flags, it.get("jp", ""))
            y -= 12
            if y < 0:
                break


class LevelBar(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.v = 0.0
        self.setFixedSize(46, 8)

    def set_level(self, v):
        self.v = self.v * 0.6 + float(v) * 0.4
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.track())
        p.drawRoundedRect(r, 4, 4)
        p.setBrush(theme.on_color())
        p.drawRoundedRect(QtCore.QRectF(0, 0, max(8, r.width() * min(1.0, self.v * 1.6)), r.height()), 4, 4)


class TranslatePage(Page):
    key, glyph, title, height = "trans", G.TRANSLATE, "翻译", 330

    def __init__(self, ctx):
        super().__init__(ctx)
        self.req = -1
        self.input = TextEdit("日语")
        self.input.setFixedHeight(86)
        self.input.focus_wanted.connect(self.want_focus)
        self.input.submit.connect(self.go)
        go = PillButton("翻译", G.TRANSLATE, height=30, primary=True)
        go.clicked.connect(self.go)
        clipb = PillButton("翻译剪贴板", G.CLIP, height=30)
        clipb.clicked.connect(lambda: ctx.actions.translate_clipboard(show_page=True))
        self.copy = IconButton(G.COPY, 30, 13, "复制译文")
        self.copy.clicked.connect(self._copy)
        self.out_card = Card(radius=16)
        self.out = QtWidgets.QLabel()
        self.out.setWordWrap(True)
        self.out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.out.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.out.setFont(ui_font(15, W.Medium))
        sc = ScrollArea()
        lay = QtWidgets.QVBoxLayout(sc.inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.out)
        lay.addStretch(1)
        self.state = Label("", 11, level=3)
        self.out_card.setLayout(vbox(hbox(self.state, None, self.copy),
                                     (sc, 1), spacing=2, margins=(14, 6, 8, 10)))
        text_w = QtWidgets.QWidget()
        text_w.setLayout(vbox(self.input, hbox(go, clipb, None, spacing=6), (self.out_card, 1), spacing=8))

        # 实时字幕（移植自译声：系统声音 → 日语识别 → 翻译）
        self.live_btn = PillButton("开始聆听", G.LIVE, height=30, primary=True)
        self.live_btn.clicked.connect(lambda: ctx.live.toggle())
        self.live_status = Label("", 11.5, level=3)
        self.level = LevelBar()
        clear = IconButton(G.TRASH, 30, 12, "清空")
        self.view = LiveView()
        self.view.hint = "播放日语视频后点「开始聆听」"
        clear.clicked.connect(self.view.clear)
        live_card = Card(radius=16)
        live_card.setLayout(vbox((self.view, 1), margins=(10, 8, 10, 8)))
        live_w = QtWidgets.QWidget()
        live_w.setLayout(vbox(hbox(self.live_btn, self.level, self.live_status, None, clear, spacing=8), (live_card, 1), spacing=8))

        self.mode = Segmented(["文本", "实时字幕"])
        self.mode.setFixedWidth(170)
        self.modes = QtWidgets.QStackedWidget()
        self.modes.addWidget(text_w)
        self.modes.addWidget(live_w)
        self.mode.changed.connect(self.modes.setCurrentIndex)
        self.setLayout(vbox(hbox(self.mode, None), (self.modes, 1), spacing=8))

        lv = ctx.live
        lv.segment.connect(self.view.on_segment)
        lv.live_zh.connect(self.view.on_live)
        lv.final_zh.connect(self.view.on_final)
        lv.partial.connect(self.view.on_partial)
        lv.level.connect(self.level.set_level)
        lv.status.connect(self.live_status.setText)
        lv.error.connect(lambda m: self.live_status.setText(m))
        lv.running_changed.connect(self._live_state)
        theme.changed.connect(self._style)
        self._style()
        ctx.translate.delta.connect(self._delta)
        ctx.translate.finished.connect(self._done)
        ctx.translate.failed.connect(self._fail)
        self._buf = ""
        h = list(getattr(ctx.translate, "history", []) or [])
        if h:
            last = h[0] if isinstance(h[0], dict) else {}
            self.input.setPlainText(last.get("src", ""))
            self.out.setText(last.get("dst", ""))

    def show_live(self):
        self.mode.set_index(1)
        self.modes.setCurrentIndex(1)

    def _live_state(self, on: bool):
        self.live_btn.set_text("停止聆听" if on else "开始聆听")
        self.live_btn.primary = not on
        self.live_btn.update()
        if on:
            self.live_status.setText("正在聆听系统声音…")
        else:
            self.level.set_level(0)
            self.live_status.setText("")

    def _style(self):
        self.out.setStyleSheet(f"color:{theme.qss_color(theme.text())};background:transparent")

    def go(self):
        txt = self.input.toPlainText().strip()
        if txt:
            self.ctx.actions.translate_text(txt, show_page=True)

    def start(self, text: str, req: int):
        if self.input.toPlainText().strip() != text.strip():
            self.input.setPlainText(text)
        self.req = req
        self._buf = ""
        self.out.setText("")
        self.state.setText("翻译中…")

    def _delta(self, rid, piece):
        if rid == self.req:
            self._buf += piece
            self.out.setText(self._buf)

    def _done(self, rid, text):
        if rid == self.req:
            self.out.setText(text)
            self.state.setText("")

    def _fail(self, rid, err):
        if rid == self.req:
            self.state.setText("")
            self.out.setText(f"翻译失败：{err}")

    def _copy(self):
        QtGui.QGuiApplication.clipboard().setText(self.out.text())
        self.state.setText("已复制")
