import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtWidgets

from wisland.core.config import cfg
from wisland.ui.island import Island
from wisland.ui.pages.base import Page


class EdgeStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_strip_bounds_and_hover_on_each_edge(self):
        for edge in ("top", "bottom", "left", "right"):
            settings = {"edge": edge, "edge_strip": True, "expand_mode": "hover",
                        "hover_expand_ms": 0, "collapse_delay_ms": 0}
            with self.subTest(edge=edge), patch.dict(cfg.data, island=settings), patch.object(cfg, "save"):
                island = Island([Page()])
                try:
                    bounds = island.path.boundingRect()
                    depth = bounds.height() if edge in ("top", "bottom") else bounds.width()
                    self.assertLessEqual(depth, 4)
                    self.assertGreaterEqual(depth, 3)
                    canvas = QtGui.QImage(island.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                    canvas.fill(QtCore.Qt.GlobalColor.transparent)
                    island.render(canvas)
                    self.assertGreater(canvas.pixelColor(bounds.center().toPoint()).alpha(), 0)
                    outside = bounds.center().toPoint()
                    if edge in ("top", "bottom"):
                        outside.setY(round(bounds.bottom() + 1 if edge == "top" else bounds.top() - 1))
                    else:
                        outside.setX(round(bounds.right() + 1 if edge == "left" else bounds.left() - 1))
                    self.assertEqual(canvas.pixelColor(outside).alpha(), 0)
                    self.assertFalse(island._inside(outside + island._win_origin))
                    center = bounds.center().toPoint() + island._win_origin
                    with patch.object(QtGui.QCursor, "pos", return_value=center), \
                            patch("wisland.ui.island.user32.GetAsyncKeyState", return_value=0):
                        island._poll_hover()
                        island._poll_hover()
                    self.assertTrue(island.expanded)
                    with patch.object(QtGui.QCursor, "pos", return_value=center + QtCore.QPoint(900, 680)):
                        island._poll_hover()
                        island._poll_hover()
                    self.assertFalse(island.expanded)
                    self.assertEqual(island.target_size(), (bounds.width(), bounds.height()))
                    island.set_edge_strip(False)
                    self.assertEqual(island.target_size(), island.compact.size_hint())
                finally:
                    island.timer.stop()
                    island.deleteLater()
