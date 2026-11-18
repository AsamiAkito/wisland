"""离屏验证贴边轮廓与空闲后台线程，不启动桌面窗口。"""

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui

from wisland.ui.glass import Backdrop
from wisland.ui.island import Island
from wisland.ui.shape import anchored_path, body_path, edge_corners, neck_path


class RenderingTests(unittest.TestCase):
    def test_animated_background_preserves_upstream_capture_rate(self):
        from unittest.mock import Mock

        screen = Mock()
        screen.geometry.return_value = QtCore.QRect(0, 0, 1920, 1080)
        backdrop = Mock()
        backdrop.covers.return_value = True
        window = SimpleNamespace(
            mode="anchored",
            edge="top",
            expanded=True,
            sw=SimpleNamespace(rest=False),
            sh=SimpleNamespace(rest=False),
            body=QtCore.QRectF(180, 100, 640, 400),
            _win_origin=QtCore.QPoint(),
            path=body_path(QtCore.QRectF(180, 100, 640, 400), (0, 0, 30, 30)),
            scr=screen,
            backdrop=backdrop,
            target_size=lambda: (640, 400),
            _anchor_along=lambda w, h: 500,
            _center_on_edge=lambda edge, along, gap, w, h: QtCore.QPointF(along, 100 + h / 2),
            capture_visible=lambda: False,
        )
        with patch("wisland.ui.island.cfg.get", return_value=8):
            Island._request_backdrop(window)
            self.assertEqual(backdrop.request.call_args.args[2], 1 / 40)
            window.capture_visible = lambda: True
            Island._request_backdrop(window)
            self.assertEqual(backdrop.request.call_args.args[2], 1 / 40)
            window.sw.rest = window.sh.rest = True
            Island._request_backdrop(window)
            self.assertEqual(backdrop.request.call_args.args[2], 1 / 1.5)
            window.mode = "drag"
            Island._request_backdrop(window)
            self.assertEqual(backdrop.request.call_args.args[2], 1 / 40)
            window.mode = "anchored"
            backdrop.covers.return_value = False
            Island._request_backdrop(window)
            self.assertTrue(backdrop.request.call_args.kwargs["urgent"])

    def test_frame_interval_does_not_restart_an_active_timer(self):
        timer = QtCore.QTimer()
        timer.setInterval(16)
        window = SimpleNamespace(timer=timer)
        with patch.object(timer, "setInterval", wraps=timer.setInterval) as set_interval:
            for _ in range(100):
                Island._set_frame_interval(window, 16)
            set_interval.assert_not_called()
            Island._set_frame_interval(window, 33)
            Island._set_frame_interval(window, 33)
            set_interval.assert_called_once_with(33)

    def test_animation_wakes_idle_timer_without_resetting_it_every_frame(self):
        timer = QtCore.QTimer()
        timer.setInterval(33)
        rest = SimpleNamespace(rest=True)
        moving = SimpleNamespace(rest=False)
        window = SimpleNamespace(timer=timer, mode="anchored", sw=moving, sh=rest, poke=rest)
        window._set_frame_interval = lambda interval: Island._set_frame_interval(window, interval)
        with (
            patch.object(timer, "isActive", return_value=True),
            patch.object(timer, "setInterval", wraps=timer.setInterval) as set_interval,
        ):
            for _ in range(20):
                Island._kick(window)
            set_interval.assert_called_once_with(16)

    def test_direct_outline_preserves_all_four_edges(self):
        for edge in ("top", "bottom", "left", "right"):
            for expanded in (False, True):
                with self.subTest(edge=edge, expanded=expanded):
                    width, height = (320, 260) if expanded else (260, 50)
                    if edge in ("left", "right"):
                        width, height = height, width
                    rect = QtCore.QRectF(60, 60, width, height)
                    radius = min(width / 2, height / 2, 30)
                    radii = tuple(0 if near else radius for near in edge_corners(edge))
                    line = {
                        "top": rect.top(),
                        "bottom": rect.bottom(),
                        "left": rect.left(),
                        "right": rect.right(),
                    }[edge]
                    horiz = edge in ("top", "bottom")
                    center = rect.center().x() if horiz else rect.center().y()
                    length = width if horiz else height
                    old = body_path(rect, radii).united(neck_path(edge, line, center, length, 0, 0, 1, 13, 0))
                    new = anchored_path(rect, edge, 13, radius)
                    self.assertEqual(old.boundingRect(), new.boundingRect())
                    self.assertLess(new.elementCount(), old.elementCount())
                    images = []
                    for path in (old, new):
                        image = QtGui.QImage(450, 450, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                        image.fill(0)
                        painter = QtGui.QPainter(image)
                        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                        painter.fillPath(path, QtGui.QColor("white"))
                        painter.end()
                        images.append(bytes(image.constBits()))
                    difference = sum(abs(a - b) for a, b in zip(*images)) / len(images[0])
                    self.assertLess(difference, 0.25)

    def test_backdrop_waits_without_a_capture_request(self):
        backdrop = Backdrop()
        with patch.object(backdrop._wake, "wait", wraps=backdrop._wake.wait) as wait:
            backdrop.start()
            try:
                time.sleep(0.05)
            finally:
                backdrop.stop()
                backdrop._thread.join(timeout=1)
            self.assertFalse(backdrop._thread.is_alive())
            self.assertLessEqual(wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
