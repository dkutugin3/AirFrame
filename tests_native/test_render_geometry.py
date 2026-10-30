"""Stage 6 geometry contracts: aspect, portrait/landscape, rotation, resize, HiDPI, exact size.

Run offscreen with:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

The exact-size mode must describe *physical* output pixels: a 2560x1440 stream
must not silently become a 640x360 window that OBS then upscales.
"""
import unittest

try:
    import render_fixtures as fx
except ImportError:  # pragma: no cover
    from tests_native import render_fixtures as fx

from PySide6.QtCore import QRect, QSize

import airframe.render as render


class AspectFitTests(unittest.TestCase):
    def test_landscape_frame_is_letterboxed_without_distortion(self):
        rect = render.fitted_rect(QSize(2560, 1440), QSize(1000, 1000))
        self.assertIsInstance(rect, QRect)
        self.assertLessEqual(rect.width(), 1000)
        self.assertLessEqual(rect.height(), 1000)
        self.assertLessEqual(abs(rect.width() - 1000), 1)
        self.assertLessEqual(abs(rect.height() - 562), 1)  # 1000*1440/2560
        self.assertLessEqual(abs(rect.width() / rect.height() - 2560 / 1440), 0.01)
        self.assertLessEqual(abs(rect.x() + rect.width() // 2 - 500), 1)
        self.assertLessEqual(abs(rect.y() + rect.height() // 2 - 500), 1)

    def test_portrait_frame_is_pillarboxed(self):
        rect = render.fitted_rect(QSize(1080, 2400), QSize(1200, 800))
        self.assertLessEqual(rect.height(), 800)
        self.assertLessEqual(abs(rect.height() - 800), 1)
        self.assertLessEqual(abs(rect.width() - 360), 1)  # 800*1080/2400
        self.assertLessEqual(abs(rect.width() / rect.height() - 1080 / 2400), 0.01)

    def test_rotation_swaps_the_fitting_box(self):
        landscape = render.fitted_rect(QSize(1920, 1080), QSize(600, 600))
        rotated = render.fitted_rect(QSize(1920, 1080), QSize(600, 600), rotation=90)
        self.assertLessEqual(abs(landscape.width() - 600), 1)
        self.assertLessEqual(abs(rotated.width() - 338), 1)  # 600*1080/1920
        self.assertLessEqual(abs(rotated.height() - 600), 1)

    def test_degenerate_sizes_do_not_raise_or_invert(self):
        for frame, viewport in ((QSize(0, 0), QSize(100, 100)), (QSize(100, 50), QSize(0, 0))):
            rect = render.fitted_rect(frame, viewport)
            self.assertGreaterEqual(rect.width(), 0)
            self.assertGreaterEqual(rect.height(), 0)


class ExactOutputSizeTests(unittest.TestCase):
    def test_retina_window_describes_exact_physical_pixels(self):
        # 2560x1440 physical stream on a 2x display: 1280x720 logical window.
        self.assertEqual(render.exact_window_size(QSize(2560, 1440), 2.0), QSize(1280, 720))
        self.assertEqual(render.physical_size(QSize(1280, 720), 2.0), QSize(2560, 1440))
        self.assertEqual(render.physical_size(QSize(1280, 720), 1.0), QSize(1280, 720))

    def test_fractional_scaling_never_silently_downscales(self):
        for dpr in (1.0, 1.25, 1.5, 2.0, 3.0):
            logical = render.exact_window_size(QSize(2560, 1440), dpr)
            physical = render.physical_size(logical, dpr)
            with self.subTest(dpr=dpr):
                self.assertGreaterEqual(physical.width(), 2560, "exact mode must not downscale")
                self.assertGreaterEqual(physical.height(), 1440, "exact mode must not downscale")
                self.assertLessEqual(physical.width() - 2560, max(1, int(dpr)))
                self.assertLessEqual(physical.height() - 1440, max(1, int(dpr)))

    def test_fit_mode_never_enlarges_beyond_the_viewport(self):
        rect = render.fitted_rect(QSize(640, 360), QSize(4000, 3000), exact=False)
        self.assertLessEqual(rect.width(), 4000)
        self.assertLessEqual(rect.height(), 3000)


if __name__ == "__main__":
    unittest.main()
