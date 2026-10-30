"""Settings remain readable and reachable in compact windows and larger text."""
import unittest

from av import VideoFrame

try:
    from tests_native import qt_harness
    from tests_native.test_telemetry_ui import TELEMETRY, legacy_stats
except ImportError:
    import qt_harness
    from test_telemetry_ui import TELEMETRY, legacy_stats

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QScrollArea, QSlider, QWidget


class DesktopLayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = qt_harness.qt_app()
        self.window = qt_harness.Desktop(qt_harness.StubServer())
        self.window.show()
        self.settle()

    def tearDown(self):
        qt_harness.close_window(self.window)

    def settle(self):
        # Wrapping changes the page's height; Qt needs a subsequent layout pass
        # after the viewport decides whether a vertical scrollbar is necessary.
        for _ in range(5):
            self.app.processEvents()

    def scrolls(self):
        return (self.window.connection_scroll, self.window.video_scroll,
                self.window.output_scroll)

    def activate_control(self, control):
        tabs = self.window.settings_tabs
        for index in range(tabs.count()):
            page = tabs.widget(index)
            if page is control or page.isAncestorOf(control):
                tabs.setCurrentIndex(index)
                self.settle()
                break
        else:
            self.fail(f"Settings control has no settings tab: {control}")
        parent = control.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        self.assertIsNotNone(parent, "Every settings page needs a vertical scroll area")
        parent.ensureWidgetVisible(control, 0, 0)
        self.settle()
        return parent

    def assert_no_horizontal_overflow(self):
        tabs = self.window.settings_tabs
        for index in range(tabs.count()):
            tabs.setCurrentIndex(index)
            self.settle()
            scroll = self.scrolls()[index]
            with self.subTest(tab=tabs.tabText(index)):
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
                for child in scroll.widget().findChildren(QWidget):
                    if not isinstance(child, (QLabel, QAbstractButton, QComboBox, QSlider)):
                        continue
                    if not child.isVisibleTo(scroll.widget()):
                        continue
                    origin = child.mapTo(scroll.widget(), QPoint())
                    self.assertGreaterEqual(origin.x(), 0, child.objectName() or str(child))
                    self.assertLessEqual(origin.x() + child.width(), scroll.widget().width(),
                                         child.objectName() or str(child))
                    if isinstance(child, QLabel) and child.text():
                        if child.wordWrap():
                            self.assertGreaterEqual(child.height(), child.heightForWidth(child.width()),
                                                    child.text())
                        else:
                            widest_line = max(child.fontMetrics().horizontalAdvance(line)
                                              for line in child.text().splitlines())
                            self.assertLessEqual(widest_line, child.contentsRect().width(),
                                                 child.text())

    def test_compact_and_regular_windows_have_no_horizontal_settings_overflow(self):
        for width, height in ((880, 640), (950, 780), (1200, 900)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.settle()
                self.assertEqual(self.window.width(), width,
                                 "A child minimum width must not force the window wider")
                self.assertEqual(self.window.height(), height,
                                 "Settings must scroll rather than force a taller window")
                self.assert_no_horizontal_overflow()

    def test_larger_text_does_not_force_horizontal_settings_scrolling(self):
        # Explicitly override the stylesheet font, as QApplication.setFont is
        # superseded by QWidget's CSS font-size in the application theme.
        self.window.setStyleSheet(self.window.styleSheet() +
                                 "\nQWidget {font-size:16px;}")
        self.window.resize(950, 780)
        self.settle()
        self.assert_no_horizontal_overflow()

    def test_every_settings_control_is_reachable_by_vertical_scrolling(self):
        self.window.resize(880, 640)
        self.settle()
        controls = (self.window.addresses, self.window.profile, self.window.target_fps,
                    self.window.max_size, self.window.rate, self.window.apply_button,
                    self.window.smoothness, self.window.playout_mode,
                    self.window.exact_output, self.window.phone_volume,
                    self.window.audio_capture)
        for control in controls:
            with self.subTest(control=control.objectName() or type(control).__name__):
                scroll = self.activate_control(control)
                rect = QRect(control.mapTo(scroll.viewport(), QPoint()), control.size())
                self.assertTrue(scroll.viewport().rect().contains(rect),
                                f"Control remains clipped after scrolling: {rect}")

    def test_connection_qr_and_code_fit_their_visible_containers(self):
        self.window.resize(880, 640)
        self.settle()
        scroll = self.activate_control(self.window.qr)
        qr = self.window.qr
        pixmap = qr.pixmap()
        self.assertIsNotNone(pixmap)
        self.assertGreaterEqual(pixmap.width(), 180, "QR should retain readable module sizes")
        logical = pixmap.deviceIndependentSize()
        self.assertLessEqual(logical.width(), qr.contentsRect().width())
        self.assertLessEqual(logical.height(), qr.contentsRect().height())
        self.assertGreaterEqual(qr.mapTo(scroll.viewport(), QPoint()).x(), 0)
        self.activate_control(self.window.code)
        code = self.window.code
        self.assertEqual(len(code.text().replace(" ", "")), 8)
        self.assertLessEqual(code.fontMetrics().horizontalAdvance(code.text()),
                             code.contentsRect().width(), "Pairing code must remain fully readable")

    def test_live_statistics_preserve_the_preview_and_readable_summary(self):
        # Resolution in the compact summary describes an actual decoded frame,
        # rather than the size merely advertised by the sender telemetry.
        self.window.server.frames.put(VideoFrame(2560, 1440, "yuv420p"))
        for width, height, larger_text in ((880, 640, False), (950, 780, True)):
            with self.subTest(size=(width, height), larger_text=larger_text):
                if larger_text:
                    self.window.setStyleSheet(self.window.styleSheet() +
                                             "\nQWidget {font-size:16px;}")
                self.window.resize(width, height)
                self.window.server.events.stats.emit(legacy_stats(**TELEMETRY))
                self.window.server.events.stats.emit(legacy_stats(**{
                    **TELEMETRY, "framesEncoded":12060, "framesSent":12010,
                    "sampledMonotonicMs":124456,
                }))
                self.settle()
                self.assertEqual((self.window.width(), self.window.height()), (width, height))
                summary = self.window.loss
                self.assertGreaterEqual(summary.height(), summary.heightForWidth(summary.width()))
                self.assertIn("Потери: 0.0%", summary.text())
                self.assertIn("2560", summary.text())
                self.assertNotIn("Телеметрия", summary.text())
                self.assertGreaterEqual(self.window.surface.width(), self.window.surface.minimumWidth())
                self.assertGreaterEqual(self.window.surface.height(), self.window.surface.minimumHeight())
                self.assertGreaterEqual(self.window.surface.height(), 180,
                                        "Live telemetry must not squeeze the preview away")
                details = self.window.stream_diag.toPlainText()
                for expected in ("Телеметрия", "захват 59.5", "H264", "42e01f"):
                    self.assertIn(expected, details)
                self.assertTrue(self.window.stream_diag.isReadOnly())


if __name__ == "__main__":
    unittest.main()
