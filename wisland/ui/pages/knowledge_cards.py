# -*- coding: utf-8 -*-
"""知识卡片：沿用剪贴板的绘制、比例和底栏操作。"""

from __future__ import annotations

import json
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...knowledge.catalog import labels
from ..theme import G, icon_font, theme, ui_font
from ..widgets import Hoverable, painter

Qt = QtCore.Qt
EDIT = "\ue70f"
MORE = "\ue712"


def read_thumbnail(path: str) -> QtGui.QImage:
    reader = QtGui.QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(560, 420, Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


class KnowledgeCard(Hoverable):
    edit_requested = QtCore.Signal()
    remove_requested = QtCore.Signal()
    menu_requested = QtCore.Signal()
    save_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()

    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.row = {}
        self.pm = None
        self.status = ""
        self.progress = 0
        self._hot = None
        self.editor = None
        self.setAccessibleDescription("回车打开操作菜单；E 编辑；Delete 移到回收站。")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self._busy = QtCore.QTimer(self, interval=33, timeout=self.update)
        self.set_row(row)

    def set_row(self, row: dict, *, force: bool = False) -> None:
        """保留卡片、缩略图和编辑器，只在展示内容变化时重新解析。"""
        previous = self.row
        self.row = row
        unchanged = all(
            row.get(field) == previous.get(field)
            for field in (
                "name",
                "body",
                "result",
                "edited_body",
                "kind",
                "user_tags",
                "instructions",
                "note_path",
            )
        )
        if self.editor or (unchanged and not force):
            return
        result = json.loads(row["result"]) if row["result"] else {}
        self.title = result.get("title") or row["name"]
        self.text = row["body"] or result.get("summary") or self.title
        if row["kind"] == "image":
            self.text = "正在加载图片预览…"
        if result.get("title") and result.get("summary"):
            self.text = result["title"] + "\n\n" + result["summary"]
        if row.get("edited_body") is not None and row["kind"] != "image":
            self.text = row["edited_body"]
        meta = labels(row)
        tags = list(dict.fromkeys(meta["mine"] + meta["ai"]))
        self.caption = meta["kind"] + (" · " + " / ".join(tags[:2]) if tags else "")
        self.setToolTip(
            self.title
            + "\n我的标签："
            + "、".join(meta["mine"])
            + "\nAI 主题："
            + "、".join(meta["ai"])
            + "\n位置："
            + meta["folder"]
            + ("\n说明：" + row["instructions"] if row["instructions"] else "")
        )
        self._details_tooltip = self.toolTip()
        self.setAccessibleName(self.title + " · " + self.caption)
        self.update()

    def resizeEvent(self, e):
        height = max(150, round(self.width() * 0.82))
        if self.height() != height:
            self.setFixedHeight(height)
        if self.editor:
            self.editor.setGeometry(self.rect().adjusted(12, 12, -12, -44))

    def set_editor(self, editor):
        self._busy.stop()
        if self.editor:
            self.editor.finish()
            self.editor.hide()
            self.editor.deleteLater()
        self.editor = editor
        if editor is None:
            self.set_row(self.row, force=True)
        self.setToolTip("" if editor else self._details_tooltip)
        if editor:
            QtWidgets.QToolTip.hideText()
            editor.setParent(self)
            editor.setGeometry(self.rect().adjusted(12, 12, -12, -44))
            editor.show()
        self.update()

    def set_thumbnail(self, image):
        if not image.isNull():
            self.pm = QtGui.QPixmap.fromImage(image)
        else:
            self.text = "图片暂不可预览"
        self.update()

    def set_progress(self, text, value):
        if self.status != text or self.progress != value:
            self.status, self.progress = text, value
            self.update()
        if value < 0 and not self.visibleRegion().isEmpty() and not self.editor:
            if not self._busy.isActive():
                self._busy.start()
        else:
            self._busy.stop()

    def showEvent(self, e):
        super().showEvent(e)
        if self.progress < 0 and not self.editor:
            self._busy.start()

    def hideEvent(self, e):
        self._busy.stop()
        super().hideEvent(e)

    def _action_rects(self):
        if self.editor:
            return {
                name: QtCore.QRectF(self.width() - offset, self.height() - 34, 26, 26)
                for name, offset in (("save", 64), ("cancel", 36))
            }
        return {
            name: QtCore.QRectF(self.width() - offset, self.height() - 34, 26, 26)
            for name, offset in (("edit", 92), ("menu", 64), ("remove", 36))
            if name != "edit" or self.row["disposition"] != "trash"
        }

    def keyPressEvent(self, e):
        if self.editor:
            if e.key() == Qt.Key.Key_Escape:
                self.cancel_requested.emit()
            elif (
                e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and e.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.save_requested.emit()
            else:
                super().keyPressEvent(e)
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space, Qt.Key.Key_Menu):
            self.menu_requested.emit()
        elif e.key() == Qt.Key.Key_E and self.row["disposition"] != "trash":
            self.edit_requested.emit()
        elif e.key() == Qt.Key.Key_Delete and self.row["disposition"] != "trash":
            self.remove_requested.emit()
        else:
            super().keyPressEvent(e)
            return
        e.accept()

    def mouseMoveEvent(self, e):
        hot = None
        for name, rect in self._action_rects().items():
            if rect.contains(e.position()):
                hot = name
                break
        if hot != self._hot:
            self._hot = hot
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            for name, rect in self._action_rects().items():
                if rect.contains(e.position()):
                    if name == "save":
                        self.save_requested.emit()
                    elif name == "cancel":
                        self.cancel_requested.emit()
                    elif name == "edit":
                        self.edit_requested.emit()
                    elif name == "remove":
                        self.remove_requested.emit()
                    else:
                        self.menu_requested.emit()
                    return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if self.editor:
            e.ignore()
            return
        self.menu_requested.emit()
        e.accept()

    def paintEvent(self, e):
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        scale = self.press_scale()
        p.translate(r.center())
        p.scale(scale, scale)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.card(0.75 + 0.8 * self._hover.x))
        p.drawRoundedRect(r, 18, 18)
        if self.editor:
            p.setFont(ui_font(10.5))
            p.setPen(theme.text3())
            status = "本地修改" if self.row["sent"] else "已暂停上传"
            p.drawText(
                QtCore.QRectF(14, r.bottom() - 34, r.width() - 95, 30), Qt.AlignmentFlag.AlignVCenter, status
            )
            for name, glyph in (("save", G.CHECK), ("cancel", G.CLOSE)):
                p.setFont(icon_font(12))
                p.setPen(theme.text())
                p.drawText(self._action_rects()[name], Qt.AlignmentFlag.AlignCenter, glyph)
            return
        body = QtCore.QRectF(r.x() + 12, r.y() + 12, r.width() - 24, r.height() - 76)
        p.save()
        path = QtGui.QPainterPath()
        path.addRoundedRect(body, 10, 10)
        p.setClipPath(path)
        if self.pm is not None:
            p.setBrush(theme.track())
            p.drawRoundedRect(body, 10, 10)
            image = self.pm.scaled(
                body.size().toSize() * 2,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            image.setDevicePixelRatio(2)
            offset = QtCore.QPointF(
                (body.width() - image.width() / 2) / 2, (body.height() - image.height() / 2) / 2
            )
            p.drawPixmap(body.topLeft() + offset, image)
        else:
            p.setFont(ui_font(12.5))
            p.setPen(theme.text())
            p.drawText(body, int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignTop), self.text[:1400])
        p.restore()
        p.setFont(ui_font(10.5))
        p.setPen(theme.text2())
        fm = QtGui.QFontMetrics(p.font())
        caption = fm.elidedText(self.caption, Qt.TextElideMode.ElideRight, int(r.width() - 24))
        p.drawText(
            QtCore.QRectF(r.x() + 12, r.bottom() - 59, r.width() - 24, 19),
            Qt.AlignmentFlag.AlignVCenter,
            caption,
        )
        if self.progress != 0:
            track = QtCore.QRectF(r.x() + 12, r.bottom() - 38, r.width() - 24, 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.track())
            p.drawRoundedRect(track, 1, 1)
            fill = QtCore.QRectF(track)
            if self.progress < 0:
                fill.setWidth(track.width() * 0.24)
                fill.moveLeft(
                    track.left() + (track.width() - fill.width()) * ((time.monotonic() % 1.5) / 1.5)
                )
            else:
                fill.setWidth(track.width() * self.progress / 100)
            p.setBrush(theme.accent)
            p.drawRoundedRect(fill, 1, 1)
        p.setFont(ui_font(10.5))
        p.setPen(theme.text3())
        status = self.status.replace("已本地保存，待配置服务器", "已保存 · 待连接").replace(
            "仅保留本机", "仅本地"
        )
        footer = QtCore.QRectF(r.x() + 14, r.bottom() - 34, max(1, r.width() - 112), 30)
        p.drawText(
            footer,
            Qt.AlignmentFlag.AlignVCenter,
            fm.elidedText(status, Qt.TextElideMode.ElideRight, int(footer.width())),
        )
        for name, glyph in (
            ("edit", EDIT),
            ("menu", MORE),
            ("remove", G.REFRESH if self.row["disposition"] == "trash" else G.TRASH),
        ):
            if name not in self._action_rects():
                continue
            rect = self._action_rects()[name]
            if self._hot == name:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.hover())
                p.drawEllipse(rect)
            p.setFont(icon_font(11))
            p.setPen(theme.text(1.0 if self._hot == name else 0.5))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
