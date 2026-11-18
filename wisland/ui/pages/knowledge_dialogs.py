"""独立的编辑与设置窗口，灵动岛收起后仍可使用。"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..theme import theme, ui_font


def make_dialog(title, width=520):
    dialog = QtWidgets.QDialog(None, QtCore.Qt.WindowType.Window | QtCore.Qt.WindowType.WindowStaysOnTopHint)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    dialog.setMinimumWidth(width)
    dialog.setFont(ui_font(13))
    background = QtGui.QColor(theme.tint())
    background.setAlpha(255)
    foreground = theme.qss_color(theme.text())
    dialog.setStyleSheet(
        f"QDialog{{background:{theme.qss_color(background)};color:{foreground}}}"
        f"QLabel,QCheckBox{{color:{foreground};background:transparent}}"
        f"QLineEdit,QSpinBox,QPlainTextEdit,QListWidget{{background:{theme.qss_color(theme.card())};color:{foreground};"
        f"border:1px solid {theme.qss_color(theme.card())};border-radius:5px;padding:5px;selection-background-color:{theme.qss_color(theme.accent)}}}"
        f"QPushButton{{background:{theme.qss_color(theme.card())};color:{foreground};border:none;border-radius:6px;padding:7px 12px}}"
        f"QPushButton:hover{{background:{theme.qss_color(theme.hover())}}}"
    )
    return dialog


def show_dialog(page, dialog):
    if page.island:
        page.island.locks.discard("knowledge-dialog")
        page.island.release_focus()
    dialog.adjustSize()
    screen = page.screen().availableGeometry()
    dialog.setMaximumHeight(max(300, screen.height() - 60))
    dialog.move(screen.center() - dialog.rect().center())
    QtWidgets.QApplication.instance().aboutToQuit.connect(dialog.close)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def open_editor(page, task_id):
    return page.start_edit(task_id)
