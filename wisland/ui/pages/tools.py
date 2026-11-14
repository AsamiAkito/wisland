# -*- coding: utf-8 -*-
"""剪贴板历史与日语翻译。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.config import cfg
from ..anim import clamp
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
        self.setMinimumHeight(150)

    def resizeEvent(self, e):
        # 接近正方形：高度跟随宽度
        h = max(150, round(self.width() * 0.82))
        if self.height() != h:
            self.setFixedHeight(h)
        self.setMouseTracking(True)
        self._hot = None

    def _pin_rect(self):
        return QtCore.QRectF(self.width() - 66, self.height() - 34, 26, 26)

    def _del_rect(self):
        return QtCore.QRectF(self.width() - 36, self.height() - 34, 26, 26)

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
        sc = self.press_scale()
        p.translate(r.center())
        p.scale(sc, sc)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.active_pill() if self.flash else theme.card(0.75 + 0.8 * self._hover.x))
        p.drawRoundedRect(r, 18, 18)
        body = QtCore.QRectF(r.x() + 12, r.y() + 12, r.width() - 24, r.height() - 48)
        if self.flash:
            p.setFont(ui_font(14, W.DemiBold))
            p.setPen(theme.pill_text())
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, "已复制")
        elif self.pm is not None:
            path = QtGui.QPainterPath()
            path.addRoundedRect(body, 10, 10)
            p.save()
            p.setClipPath(path)
            p.setBrush(theme.track())
            p.drawRoundedRect(body, 10, 10)
            img = self.pm.scaled(body.size().toSize() * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            img.setDevicePixelRatio(2)
            off = QtCore.QPointF((body.width() - img.width() / 2) / 2, (body.height() - img.height() / 2) / 2)
            p.drawPixmap(body.topLeft() + off, img)
            p.restore()
        else:
            txt = " ".join((self.item.get("text") or "").split())
            p.setFont(ui_font(12.5))
            p.setPen(theme.text())
            fm = QtGui.QFontMetrics(p.font())
            lines = max(1, int(body.height() // fm.lineSpacing()))
            # 手动按宽度断行，最多 lines 行，末行省略
            out, cur = [], ""
            for ch in txt:
                if fm.horizontalAdvance(cur + ch) > body.width():
                    out.append(cur)
                    cur = ch
                    if len(out) >= lines:
                        break
                else:
                    cur += ch
            if len(out) < lines and cur:
                out.append(cur)
            if len(out) == lines and (len("".join(out)) < len(txt)):
                out[-1] = fm.elidedText(out[-1] + "…", Qt.TextElideMode.ElideRight, int(body.width()))
            for i, line in enumerate(out[:lines]):
                p.drawText(QtCore.QRectF(body.x(), body.y() + i * fm.lineSpacing(), body.width(), fm.lineSpacing()),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
        # 底栏：时间 · 置顶 · 删除
        p.setFont(ui_font(10.5, num=True))
        p.setPen(theme.text3())
        p.drawText(QtCore.QRectF(r.x() + 14, r.bottom() - 34, 80, 30), Qt.AlignmentFlag.AlignVCenter,
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
    key, glyph, title, height = "clip", G.CLIP, "剪贴板", 400

    def __init__(self, ctx):
        super().__init__(ctx)
        clear = PillButton("清空", G.TRASH, height=28)
        self.clear_btn = clear
        clear.clicked.connect(lambda: ctx.clipboard.clear())
        self.sc = ScrollArea()
        self.list = QtWidgets.QGridLayout(self.sc.inner)
        self.list.setContentsMargins(36, 2, 40, 10)     # 与两侧边缘留出更大距离
        self.list.setHorizontalSpacing(22)
        self.list.setVerticalSpacing(20)
        self.list.setColumnStretch(0, 1)
        self.list.setColumnStretch(1, 1)
        self.setLayout(vbox(hbox(None, clear, margins=(0, 0, 14, 0)), (self.sc, 1), spacing=6))
        ctx.clipboard.changed.connect(self._changed)
        self._dirty = True
        self.height = 400 if ctx.clipboard.items() else 150      # 打开时直接用对的高度
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
        q = ""
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
        self.list.setRowStretch(max(1, (n + 1) // 2), 1)
        self.sc.setVisible(n > 0)
        self.clear_btn.setVisible(n > 0)
        self.set_height(400 if n > 0 else 150)        # 没有内容时回到默认高度

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
    """实时字幕：日语小字（可点词查词）+ 中文大字，最新的在最下面；正在说的一句（草稿）实时更新。"""
    MAX = 80
    word_clicked = QtCore.Signal(object, dict)      # (Token, 所在字幕行)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []          # {sid, jp, zh, final}
        self.partial = ""
        self.partial_zh = ""
        self.hint = ""
        self.tokens_fn = None               # text -> list[Token]（学习服务提供）
        self._hits: list[tuple[QtCore.QRectF, object, dict]] = []
        self._hover = None
        self.scroll = 0.0                   # 距离底部（最新一行）的滚动距离，0 = 跟随最新
        self._content_h = 0.0
        self._flow_cache: dict = {}
        self._latest_btn: QtCore.QRectF | None = None
        self.setMouseTracking(True)
        theme.changed.connect(self.update)

    def _find(self, sid):
        for r in reversed(self.rows):
            if r["sid"] == sid:
                return r
        return None

    def _row(self, sid):
        r = self._find(sid)
        if r is not None:
            return r
        r = {"sid": sid, "jp": "", "zh": "", "final": False}
        self.rows.append(r)
        del self.rows[:-self.MAX]
        return r

    def on_segment(self, sid, jp):
        before = self._content_h
        r = self._row(sid)
        r["jp"] = jp
        if not r["zh"] and self.partial_zh:
            r["zh"] = self.partial_zh          # 草稿译文先挂到这一句，等最终译文覆盖
        self.partial, self.partial_zh = "", ""
        self._grow(before)
        self.update()

    def on_live(self, sid, zh):
        r = self._find(sid)
        if r is None:
            self.partial_zh = zh               # 句子还没定稿：译文跟着草稿行显示，不单独成行
        elif not r["final"]:
            r["zh"] = zh
        self.update()

    def on_final(self, sid, zh):
        before = self._content_h
        r = self._row(sid)
        r["zh"], r["final"] = zh, True
        self._grow(before)
        self.update()

    def on_partial(self, jp):
        self.partial = jp
        self.update()

    def clear(self):
        self.rows, self.partial, self.partial_zh = [], "", ""
        self.scroll = 0.0
        self._flow_cache.clear()
        self.update()

    # ---------- 滚动：滚轮上翻看历史；有新内容时停在原处，点「回到最新」或滚到底恢复跟随 ----------
    def _max_scroll(self) -> float:
        return max(0.0, self._content_h - self.height() + 8)

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.pixelDelta().y()
        if not d:
            e.ignore()
            return
        self.scroll = clamp(self.scroll + d / 120.0 * 52.0, 0.0, self._max_scroll())
        self.update()
        e.accept()

    def _grow(self, before: float):
        """内容变高时：如果正在看历史，就把滚动量加上增量，保持画面不动。"""
        if self.scroll > 0:
            self._measure()
            self.scroll = clamp(self.scroll + (self._content_h - before), 0.0, self._max_scroll())

    # ---------- 日语行：按词排版（可点击） ----------
    def _flow(self, text: str, w: float, fm: QtGui.QFontMetrics, clickable: bool):
        """返回 [(x, line_index, width, Token|None, piece)] 与行数（按文本 / 宽度缓存，分词只做一次）。"""
        key = (text, int(w), clickable)
        hit = self._flow_cache.get(key)
        if hit is not None:
            return hit
        res = self._flow_raw(text, w, fm, clickable)
        if len(self._flow_cache) > 400:
            self._flow_cache.clear()
        self._flow_cache[key] = res
        return res

    def _flow_raw(self, text: str, w: float, fm: QtGui.QFontMetrics, clickable: bool):
        toks = self.tokens_fn(text) if (clickable and self.tokens_fn) else []
        pieces = [(t.surface, t) for t in toks] if toks else [(ch, None) for ch in text]
        out, x, line = [], 0.0, 0
        for piece, tok in pieces:
            pw = fm.horizontalAdvance(piece)
            if x > 0 and x + pw > w:
                x, line = 0.0, line + 1
            out.append((x, line, pw, tok, piece))
            x += pw
        return out, line + 1

    def paintEvent(self, e):
        p = painter(self)
        w = self.width() - 8
        self._measure()
        self.scroll = clamp(self.scroll, 0.0, self._max_scroll())
        y = self.height() - 4 + self.scroll
        fj, fz = ui_font(12.5), ui_font(16, W.Medium)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        lh = fmj.height() + 1
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        items = list(self.rows)
        if self.partial or self.partial_zh:
            items.append({"jp": self.partial, "zh": self.partial_zh, "final": False, "draft": True})
        self._hits = []
        if not items:
            p.setFont(ui_font(13))
            p.setPen(theme.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.hint)
            return
        for it in reversed(items):
            zh = it.get("zh", "")
            hz = fmz.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, zh).height() if zh else 0
            draft = bool(it.get("draft"))
            flow, nlines = self._flow(it.get("jp", ""), w, fmj, not draft)
            hj = nlines * lh
            if y - hz - hj - 2 > self.height():          # 整条都在可见区下方（正在看历史）：跳过
                y -= hz + hj + 2 + 12
                continue
            y -= hz
            if zh:
                p.setFont(fz)
                p.setPen(theme.text(1.0 if it.get("final") else 0.8))
                p.drawText(QtCore.QRect(4, int(y), w, hz), flags, zh)
            y -= hj + 2
            p.setFont(fj)
            for x, line, pw, tok, piece in flow:
                r = QtCore.QRectF(4 + x, y + line * lh, pw, lh)
                hot = tok is not None and tok.clickable and self._hover is not None \
                    and self._hover[0] == it.get("sid") and self._hover[1] == (line, round(x))
                if hot:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(theme.hover())
                    p.drawRoundedRect(r.adjusted(-2, 0, 2, 0), 5, 5)
                p.setPen(theme.text3() if draft else (theme.text() if hot else theme.text2()))
                p.drawText(r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, piece)
                if tok is not None and tok.clickable and not draft and r.bottom() > 0:
                    self._hits.append((r, tok, it, (line, round(x))))
            y -= 12
            if y < 0:
                break
        # 正在看历史：右下角「回到最新」
        self._latest_btn = None
        if self.scroll > 1:
            f = ui_font(11.5, W.Medium)
            txt = "↓ 回到最新"
            bw = QtGui.QFontMetrics(f).horizontalAdvance(txt) + 22
            br = QtCore.QRectF(self.width() - bw - 6, self.height() - 30, bw, 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.card(1.8))
            p.drawRoundedRect(br, 12, 12)
            p.setFont(f)
            p.setPen(theme.text())
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, txt)
            self._latest_btn = br

    def _measure(self):
        """全部字幕的总高度（用于滚动范围）。"""
        w = self.width() - 8
        fj, fz = ui_font(12.5), ui_font(16, W.Medium)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        lh = fmj.height() + 1
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        items = list(self.rows)
        if self.partial or self.partial_zh:
            items.append({"jp": self.partial, "zh": self.partial_zh, "draft": True})
        total = 4.0
        for it in items:
            zh = it.get("zh", "")
            hz = fmz.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, zh).height() if zh else 0
            _, nlines = self._flow(it.get("jp", ""), w, fmj, not it.get("draft"))
            total += hz + nlines * lh + 2 + 12
        self._content_h = total

    def _hit(self, pos):
        for r, tok, row, key in self._hits:
            if r.contains(pos):
                return r, tok, row, key
        return None

    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        new = (h[2].get("sid"), h[3]) if h else None
        if new != self._hover:
            self._hover = new
            self.setCursor(Qt.CursorShape.PointingHandCursor if h else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        if self._hover is not None:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, e):
        if self._latest_btn is not None and self._latest_btn.contains(e.position()):
            self.scroll = 0.0
            self.update()
            return
        h = self._hit(e.position())
        if h and e.button() == Qt.MouseButton.LeftButton:
            self.word_clicked.emit(h[1], h[2])


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


class WordRow(Hoverable):
    """单词本的一行：单词【读音】 释义；右侧同步状态与删除。"""
    delete_clicked = QtCore.Signal()

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.card = card
        self.setFixedHeight(50)
        self.setMouseTracking(True)
        self._hot = False

    def _del_rect(self):
        return QtCore.QRectF(self.width() - 32, (self.height() - 26) / 2, 26, 26)

    def mouseMoveEvent(self, e):
        hot = self._del_rect().contains(e.position())
        if hot != self._hot:
            self._hot = hot
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._del_rect().contains(e.position()):
            self.delete_clicked.emit()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.card(0.7 + 0.8 * self._hover.x))
        p.drawRoundedRect(r, 14, 14)
        c = self.card
        x = r.x() + 14
        p.setFont(ui_font(15, W.DemiBold))
        p.setPen(theme.text())
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(x, r.y(), 200, r.height()), Qt.AlignmentFlag.AlignVCenter, c.word)
        x += fm.horizontalAdvance(c.word) + 6
        if c.reading and c.reading != c.word:
            p.setFont(ui_font(12))
            p.setPen(theme.on_color())
            rd = f"【{c.reading}】"
            p.drawText(QtCore.QRectF(x, r.y(), 200, r.height()), Qt.AlignmentFlag.AlignVCenter, rd)
            x += QtGui.QFontMetrics(p.font()).horizontalAdvance(rd) + 8
        status = {2: "Anki ✓", 1: "已导出"}.get(c.exported, "待同步")
        p.setFont(ui_font(11))
        sw = QtGui.QFontMetrics(p.font()).horizontalAdvance(status) + 4
        tw = r.right() - 44 - sw - x
        meaning = (c.meaning or "").splitlines()[0] if c.meaning else ""
        p.setFont(ui_font(12.5))
        p.setPen(theme.text2())
        p.drawText(QtCore.QRectF(x, r.y(), max(0, tw), r.height()), Qt.AlignmentFlag.AlignVCenter,
                   QtGui.QFontMetrics(p.font()).elidedText(meaning, Qt.TextElideMode.ElideRight, int(max(0, tw))))
        p.setFont(ui_font(11))
        p.setPen(theme.on_color() if c.exported >= 2 else theme.text3())
        p.drawText(QtCore.QRectF(r.right() - 40 - sw, r.y(), sw, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, status)
        if self._hover.x > 0.05:
            dr = self._del_rect()
            if self._hot:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.hover())
                p.drawEllipse(dr)
            p.setFont(icon_font(11))
            p.setPen(theme.text(0.9 if self._hot else 0.45))
            p.drawText(dr, Qt.AlignmentFlag.AlignCenter, G.CLOSE)


class WordbookView(QtWidgets.QWidget):
    """单词本：搜索 / 查新词、同步到 Anki、导出 .apkg、自动同步开关。"""
    def __init__(self, ctx, lookup_cb, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lookup_cb = lookup_cb
        self.search = LineEdit("搜索单词本 · 回车查词")
        self.search.textChanged.connect(lambda _: self.refresh())
        self.search.returnPressed.connect(lambda: self.lookup_cb(self.search.text().strip(), {}))
        self.sync_btn = PillButton("同步到 Anki", G.REFRESH, height=30, primary=True)
        self.sync_btn.clicked.connect(self._sync)
        self.export_btn = PillButton("导出 .apkg", G.OPEN, height=30)
        self.export_btn.clicked.connect(self._export)
        self.auto_btn = PillButton("", G.CHECK, height=30)
        self.auto_btn.clicked.connect(self._toggle_auto)
        self.count = Label("", 11.5, level=3)
        self.sc = ScrollArea()
        self.list = QtWidgets.QVBoxLayout(self.sc.inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(6)
        self.setLayout(vbox(hbox((self.search, 1), spacing=6),
                            hbox(self.sync_btn, self.export_btn, self.auto_btn, None, self.count, spacing=6),
                            (self.sc, 1), spacing=8))
        ctx.learn.changed.connect(self._changed)
        self._dirty = True
        self._auto_label()

    def want_focus(self, cb):
        self.search.focus_wanted.connect(cb)

    def _auto_label(self):
        on = bool(cfg.get("learn.auto_sync", False))
        self.auto_btn.set_text("自动同步：开" if on else "自动同步：关")
        self.auto_btn.glyph = G.CHECK if on else G.CLOSE

    def _toggle_auto(self):
        cfg.set("learn.auto_sync", not bool(cfg.get("learn.auto_sync", False)))
        self._auto_label()
        self.ctx.learn.auto_sync()

    def _changed(self):
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def showEvent(self, e):
        if self._dirty:
            self.refresh()

    def refresh(self):
        self._dirty = False
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cards = self.ctx.learn.cards(self.search.text().strip())
        for c in cards[:200]:
            row = WordRow(c)
            row.clicked.connect(lambda c=c: self.lookup_cb(c.word, {"sentence": c.sentence, "sentence_zh": c.sentence_zh}))
            row.delete_clicked.connect(lambda cid=c.id: self.ctx.learn.delete([cid]))
            self.list.addWidget(row)
        if not cards:
            self.list.addWidget(Label("空", 12.5, level=3,
                                      align=Qt.AlignmentFlag.AlignCenter))
        self.list.addStretch(1)
        n_new = sum(1 for c in self.ctx.learn.cards() if c.exported < 2)
        self.count.setText(f"{len(cards)} 个词 · 待同步 {n_new}")

    def _sync(self):
        ids = self.ctx.learn.unsynced_ids()
        if not ids:
            self.count.setText("没有待同步的词")
            return
        self.count.setText(f"同步中（{len(ids)}）…")
        self.ctx.learn.push_cards(ids)

    def _export(self):
        import time as _t
        from pathlib import Path
        default = Path.home() / "Desktop" / f"wisland单词本_{_t.strftime('%Y%m%d_%H%M')}.apkg"
        isl = self.ctx.island
        if isl:
            isl.locks.add("dialog")
        try:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(None, "导出 Anki 卡组", str(default), "Anki 卡组 (*.apkg)")
        finally:
            if isl:
                isl.locks.discard("dialog")
        if not path:
            return
        try:
            n = self.ctx.learn.export_apkg(path)
            self.count.setText(f"已导出 {n} 张")
        except Exception as e:  # noqa: BLE001
            self.count.setText(f"导出失败：{e}")


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
        self.view.hint = "播放日语视频后点「开始聆听」" + (
            "" if ctx.live.available()[0] else "\n（第一次使用会先自动下载约 630MB 的识别模型）")
        clear.clicked.connect(self.view.clear)
        live_card = Card(radius=16)
        live_card.setLayout(vbox((self.view, 1), margins=(10, 8, 10, 8)))
        live_w = QtWidgets.QWidget()
        live_w.setLayout(vbox(hbox(self.live_btn, self.level, self.live_status, None, clear, spacing=8), (live_card, 1), spacing=8))

        # 查词与单词本（Anki）
        from ..lookup import LookupPopup
        self.popup = LookupPopup(ctx.learn)
        self.view.tokens_fn = ctx.learn.tokens
        self.view.word_clicked.connect(self._word_clicked)
        self.book = WordbookView(ctx, self.lookup)
        self.book.want_focus(self.want_focus)

        self.mode = Segmented(["文本", "实时字幕", "单词本"])
        self.mode.setFixedWidth(240)
        self.modes = QtWidgets.QStackedWidget()
        self.modes.addWidget(text_w)
        self.modes.addWidget(live_w)
        self.modes.addWidget(self.book)
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
        lv.downloading_changed.connect(self._download_state)
        lv.download_progress.connect(self._download_progress)
        lv.download_note.connect(self.live_status.setText)
        lv.download_done.connect(self._download_done)
        theme.changed.connect(self._style)
        self._style()
        ctx.translate.delta.connect(self._delta)
        ctx.translate.finished.connect(self._done)
        ctx.translate.failed.connect(self._fail)
        self._buf = ""
        # 输入框右上角的清空按钮（有内容时才出现）
        self.clear_btn = IconButton(G.CLOSE, 24, 10, "清空", self.input)
        self.clear_btn.clicked.connect(self._clear_all)
        self.clear_btn.hide()
        self.input.textChanged.connect(self._sync_clear)
        self.input.installEventFilter(self)

    def lookup(self, word: str, ctx: dict):
        if not word:
            return
        self.popup.island = self.island
        self.popup.show_for(word, sentence=ctx.get("sentence", ""), sentence_zh=ctx.get("sentence_zh", ""))

    def _word_clicked(self, tok, row):
        self.popup.island = self.island
        self.popup.show_for(tok.lemma or tok.surface, surface=tok.surface, reading=tok.reading, pos=tok.pos,
                            sentence=row.get("jp", ""), sentence_zh=row.get("zh", ""),
                            audio=self.ctx.live.clip(row.get("sid", "")))

    def _sync_clear(self):
        self.clear_btn.setVisible(bool(self.input.toPlainText().strip()))
        self._place_clear()

    def _place_clear(self):
        self.clear_btn.move(self.input.width() - self.clear_btn.width() - 8, 6)
        self.clear_btn.raise_()

    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == QtCore.QEvent.Type.Resize:
            self._place_clear()
        return False

    def _clear_all(self):
        self.ctx.translate.cancel() if hasattr(self.ctx.translate, "cancel") else None
        self.req = -1
        self.input.clear()
        self.out.setText("")
        self.state.setText("")

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

    def _download_state(self, on: bool):
        if on:
            self.live_btn.set_text("取消下载")
            self.live_btn.primary = False
            self.live_btn.update()
            self.live_status.setText("准备下载识别模型…")
            self.level.set_level(0)
        elif not self.ctx.live.running:
            self.live_btn.set_text("开始聆听")
            self.live_btn.primary = True
            self.live_btn.update()

    def _download_progress(self, done: int, total: int, label: str):
        frac = done / total if total else 0.0
        self.level.set_level(max(0.0, min(1.0, frac)))
        mb = done / (1024 * 1024)
        if total:
            self.live_status.setText(
                f"{label} {min(100, int(done * 100 / total))}% · {mb:.0f}/{total / (1024 * 1024):.0f} MB")
        else:
            self.live_status.setText(f"{label} · {mb:.0f} MB")

    def _download_done(self, ok: bool, msg: str):
        if ok:
            self.live_status.setText("模型已就绪，正在启动聆听…")
            return
        self.live_btn.set_text("开始聆听")
        self.live_btn.primary = True
        self.live_btn.update()
        self.level.set_level(0)
        self.live_status.setText(msg)

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
