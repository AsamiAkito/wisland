"""The only adapter to upstream Wisland. No changes to island.py are required."""

from PySide6 import QtCore, QtWidgets

from ..ui.theme import G, theme, ui_font
from .service import KnowledgeService


def prepare(ctx, pages):
    from ..ui.pages.knowledge import KnowledgePage

    ctx.knowledge = KnowledgeService()
    pages.append(KnowledgePage(ctx))


class DropAdapter(QtCore.QObject):
    def __init__(self, island, service):
        super().__init__(island)
        self.island, self.service = island, service
        self.active = False
        self.overlay = QtWidgets.QLabel("松手收集", island)
        self.overlay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.overlay.setFont(ui_font(14))
        self.overlay.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.hide()
        self.timer = QtCore.QTimer(self, interval=33, timeout=self.position)
        island.setAcceptDrops(True)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def position(self):
        rect = self.island.body.toAlignedRect().adjusted(5, 3, -5, -3)
        self.overlay.setGeometry(rect)
        self.overlay.setStyleSheet(
            f"QLabel{{background:{theme.qss_color(theme.tint())};color:{theme.qss_color(theme.text())};border:1px solid {theme.qss_color(theme.accent)};border-radius:14px}}"
        )
        self.overlay.raise_()

    def finish(self):
        self.active = False
        self.island.locks.discard("knowledge-drop")
        self.overlay.hide()
        self.timer.stop()

    def eventFilter(self, obj, event):
        if self.active and obj is self.island.poll and event.type() == QtCore.QEvent.Type.Timer:
            return True
        if not isinstance(obj, QtWidgets.QWidget) or obj.window() is not self.island:
            return False
        kind = event.type()
        if kind in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove):
            mime = event.mimeData()
            if not (mime.hasUrls() or mime.hasText() or mime.hasImage()) or self.island.passthrough():
                return False
            self.active = True
            self.island.locks.add("knowledge-drop")
            self.overlay.show()
            self.position()
            self.timer.start()
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
            return True
        if kind == QtCore.QEvent.Type.Drop:
            if self.island.passthrough():
                self.finish()
                return False
            items = self.service.payload(event.mimeData())
            self.finish()
            if items:
                event.setDropAction(QtCore.Qt.DropAction.CopyAction)
                event.accept()
                self.service.capture(items)
                return True
        elif kind == QtCore.QEvent.Type.DragLeave:
            self.finish()
        return False


def attach(app, ctx, island):
    service = ctx.knowledge
    ctx.knowledge_adapter = DropAdapter(island, service)
    service.notice.connect(lambda text: island.activity({"glyph": G.CLIP, "title": text}, 3.0))
    island.hotkey.connect(lambda name: service.capture_clipboard() if name == "knowledge_capture" else None)
    app.aboutToQuit.connect(service.stop)
    service.start()
