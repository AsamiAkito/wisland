import unittest
from unittest.mock import Mock, patch

from PySide6 import QtCore, QtGui, QtWidgets

from wisland.core.config import cfg
from wisland.ui.island import Island
from wisland.ui.pages.base import Page


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        for override in (patch.dict(cfg.data, island={}), patch.object(cfg, "save")):
            override.start()
            self.addCleanup(override.stop)

    def make_island(self):
        pages = []
        for key in ("home", "stock", "weather", "trans"):
            page = Page()
            page.key = page.title = key
            pages.append(page)
        island = Island(pages)
        self.addCleanup(island.deleteLater)
        self.addCleanup(island.timer.stop)
        return island

    def test_filtered_navigation_selects_the_matching_stack_widget(self):
        cfg.set("island.last_tab", "stock")
        cfg.set("island.hidden_pages", ["stock"])
        island = self.make_island()
        self.assertEqual(island.nav.keys(), ["home", "weather", "trans"])
        self.assertEqual(island.stack.currentWidget().key, "home")

        island.nav.set_current("trans", emit=True)
        self.assertEqual(island.stack.currentWidget().key, "trans")
        island.toggle("stock")
        self.assertFalse(island.expanded)
        self.assertEqual(island.stack.currentWidget().key, "trans")

        cfg.set("island.hidden_pages", ["stock", "trans"])
        island.refresh_pages()
        self.assertEqual(island.nav.current(), "home")
        self.assertEqual(island.stack.currentWidget().key, "home")
        cfg.set("island.hidden_pages", [])
        island.refresh_pages()
        island.nav.set_current("stock", emit=True)
        self.assertEqual(island.stack.currentWidget().key, "stock")

    def test_drag_to_left_monitor_and_restore_after_display_reorder(self):
        island = self.make_island()
        primary = QtGui.QGuiApplication.primaryScreen()
        secondary = Mock(spec=QtGui.QScreen)
        secondary.name.return_value = "test-secondary"
        secondary.geometry.return_value = QtCore.QRect(-2560, 0, 1707, 960)
        secondary.availableGeometry.return_value = QtCore.QRect(-2560, 0, 1707, 930)
        secondary.devicePixelRatio.return_value = 1.5
        cursor = QtCore.QPoint(-1900, 9)
        with patch.object(QtGui.QCursor, "pos", return_value=cursor), \
                patch.object(QtGui.QGuiApplication, "screenAt", return_value=secondary), \
                patch.object(QtGui.QGuiApplication, "screens", return_value=[primary, secondary]):
            start = island.body.center().toPoint() + island._win_origin
            island.begin_drag(start)
            target = island._drag_target()
            self.assertIs(island.scr, secondary)
            self.assertEqual(island.fx.x, target.x())
            island._end_drag()
            self.assertEqual(island.edge, "top")
            island.along.snap(island.along.target)
            island.gap.snap(0)
            island._tick()
            self.assertEqual(island.mode, "anchored")
            self.assertEqual(cfg.get("island.screen_name"), "test-secondary")
            self.assertEqual(cfg.get("island.screen"), 1)
            self.assertAlmostEqual(cfg.get("island.pos"), (target.x() + 2560) / 1707, places=3)

        with patch.object(QtGui.QGuiApplication, "screens", return_value=[secondary, primary]):
            self.assertIs(island._pick_screen(), secondary)
        island._screen_removed(secondary)
        self.assertIs(island.scr, primary)
        self.assertIs(island._pick_screen(), primary)
