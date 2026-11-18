# -*- coding: utf-8 -*-
"""卡片内编辑正文、标签与说明，不创建额外窗口。"""

from __future__ import annotations

import json
import re

from PySide6 import QtCore, QtGui, QtWidgets

from ..widgets import Label, LineEdit, PillButton, ScrollArea, Segmented, TextEdit, vbox


class BodyEdit(TextEdit):
    def keyPressEvent(self, event):
        # 正文中的普通回车保留换行；Ctrl+Enter 由编辑器统一保存。
        QtWidgets.QPlainTextEdit.keyPressEvent(self, event)


class InlineEditor(QtWidgets.QWidget):
    save_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()
    auto_save_requested = QtCore.Signal()

    def __init__(self, row, document, focus, parent=None):
        super().__init__(parent)
        self._closing = False
        self._internal_click = False
        self._blur = QtCore.QTimer(self, singleShot=True, interval=180, timeout=self._save_if_outside)
        tabs = Segmented(["正文", "标签与说明"], height=26)
        self.stack = QtWidgets.QStackedWidget()
        self.content = BodyEdit("填写正文；原始附件保留", px=12)
        self.content.setAccessibleName("笔记正文")
        self.content.setPlainText(document["content"])
        self.tags = LineEdit("标签用逗号分隔，可直接创建")
        self.tags.setPlaceholderText("添加标签，回车确认")
        self.tags.setAccessibleName("我的标签")
        self._tags = list(json.loads(row["user_tags"]))
        tag_page = QtWidgets.QWidget()
        self.chips = ScrollArea()
        self.chip_grid = QtWidgets.QGridLayout(self.chips.inner)
        self.chip_grid.setContentsMargins(0, 0, 0, 0)
        self.chip_grid.setHorizontalSpacing(4)
        self.chip_grid.setVerticalSpacing(4)
        self.chip_grid.setColumnStretch(0, 1)
        self.chip_grid.setColumnStretch(1, 1)
        self.tags.setToolTip("输入后按回车添加；可用逗号分隔。最多 10 个标签，每个 32 字。")
        self.tags.returnPressed.connect(self.add_tags)
        self.tags.editingFinished.connect(self.add_tags)
        self.instructions = BodyEdit("告诉 AI 整理重点，可留空", px=12)
        self.instructions.setAccessibleName("补充整理说明")
        self.instructions.setPlainText(row["instructions"])
        self.chips.setFixedHeight(30)
        self.instructions.setMinimumHeight(32)
        tag_page.setLayout(vbox(self.tags, self.chips, (self.instructions, 1), spacing=5))
        for widget in (self.content, tag_page):
            self.stack.addWidget(widget)
        tabs.changed.connect(self.stack.setCurrentIndex)
        for field in (self.content, self.tags, self.instructions):
            field.focus_wanted.connect(focus)
        self.error = Label("", 10.5, level=3)
        self.error.hide()
        self.setLayout(vbox(tabs, (self.stack, 1), self.error, spacing=4))
        self._render_tags()
        save = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self)
        save.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save.activated.connect(self.save_requested)
        cancel = QtGui.QShortcut(QtGui.QKeySequence("Escape"), self)
        cancel.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        cancel.activated.connect(self.cancel_requested)
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)
        app.applicationStateChanged.connect(self._application_changed)
        app.aboutToQuit.connect(self._save_on_exit)

    def finish(self):
        self._closing = True
        self._blur.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)

    def eventFilter(self, obj, event):
        if (
            not self._closing
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and isinstance(obj, QtWidgets.QWidget)
        ):
            inside = self._inside(obj)
            # Segmented 等无焦点控件会把同一次点击传给父卡片。
            # 按真实点击位置判定区域，不能把冒泡接收者当作点击目标。
            if isinstance(event, QtGui.QMouseEvent) and obj.window() is self.window():
                point = self.mapFromGlobal(event.globalPosition().toPoint())
                inside = inside or self.rect().contains(point)
            self._internal_click = inside
            if self._internal_click:
                self._blur.stop()
            else:
                self._blur.start()
        return False

    def _inside(self, widget):
        return widget is self or (widget is not None and self.isAncestorOf(widget))

    def _focus_changed(self, old, new):
        if self._closing:
            return
        if self._inside(new):
            self._blur.stop()
        elif self._inside(old) or old is self.parentWidget():
            if new is not self.parentWidget():
                self._internal_click = False
            self._blur.start()

    def _application_changed(self, state):
        if not self._closing and state != QtCore.Qt.ApplicationState.ApplicationActive:
            self._blur.start()

    def _save_if_outside(self):
        app = QtWidgets.QApplication.instance()
        if (
            self._internal_click
            and self.isVisible()
            and app.applicationState() == QtCore.Qt.ApplicationState.ApplicationActive
        ):
            return
        if not self._closing and (
            not self.isVisible()
            or not self._inside(app.focusWidget())
            or app.applicationState() != QtCore.Qt.ApplicationState.ApplicationActive
        ):
            self.auto_save_requested.emit()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._closing:
            self._blur.start()

    def _save_on_exit(self):
        if not self._closing:
            self.auto_save_requested.emit()

    def values(self):
        if not self.add_tags():
            raise ValueError("请缩短标签后再保存")
        return self.content.toPlainText(), list(self._tags), self.instructions.toPlainText()

    def add_tags(self):
        values = [t.strip() for t in re.split("[,，;；\n]", self.tags.text()) if t.strip()]
        if not values:
            return True
        combined = list(dict.fromkeys(self._tags + values))
        if len(combined) > 10 or any(len(tag) > 32 for tag in combined):
            self.show_error("最多 10 个标签，每个 32 字")
            return False
        self._tags = combined
        self.tags.clear()
        self.error.hide()
        self._render_tags()
        return True

    def _render_tags(self):
        for row in range(self.chip_grid.rowCount()):
            self.chip_grid.setRowStretch(row, 0)
        while self.chip_grid.count():
            item = self.chip_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, tag in enumerate(self._tags):
            chip = PillButton(tag + " ×", height=24)
            chip.setMinimumWidth(0)
            chip.setMaximumWidth(130)
            chip.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
            chip.setToolTip("移除标签：" + tag)
            chip.setAccessibleName("移除标签：" + tag)
            chip.clicked.connect(lambda tag=tag: self.remove_tag(tag))
            self.chip_grid.addWidget(chip, index // 2, index % 2)
        self.chip_grid.setRowStretch((len(self._tags) + 1) // 2, 1)
        self.chips.setVisible(bool(self._tags))
        self.tags.setPlaceholderText(
            f"添加标签 · 已有 {len(self._tags)} 个" if self._tags else "添加标签，回车确认"
        )

    def remove_tag(self, tag):
        self._tags = [value for value in self._tags if value != tag]
        self._render_tags()

    def show_error(self, text):
        self.error.setText(text)
        self.error.setToolTip(text)
        self.error.show()
