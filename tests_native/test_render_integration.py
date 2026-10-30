"""Stage 6b integration contracts: desktop.py renders decoded YUV frames, not BGRA.

Run offscreen with:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

This file exercises the real Desktop/Frames wiring with injected renderers: a
QtVideoRenderer for the "GPU path contract" (offscreen it has no RHI, which is
reported honestly) and a SoftwareVideoRenderer for the fallback contract.
Acceleration itself is only proven by scripts/verify-render-native.py.
"""
import unittest

import av

try:
    from tests_native import qt_harness
except ImportError:  # pragma: no cover - discovery from tests_native
    import qt_harness

from PySide6.QtCore import QSize

import airframe.render as render
from airframe.desktop import Desktop, Frames

try:
    import render_fixtures as fx
except ImportError:  # pragma: no cover
    from tests_native import render_fixtures as fx


class ReformatCounter:
    """Counts PyAV BGRA conversions without changing their result."""

    def __init__(self):
        self.calls = 0
        self._original = av.VideoFrame.reformat

    def __enter__(self):
        original = self._original
        counter = self

        def counting(frame_self, *args, **kwargs):
            if kwargs.get("format") == "bgra" or (len(args) == 1 and args[0] == "bgra"):
                counter.calls += 1
            return original(frame_self, *args, **kwargs)

        av.VideoFrame.reformat = counting
        return self

    def __exit__(self, *exc_info):
        av.VideoFrame.reformat = self._original


def _gpu_factory():
    return render.QtVideoRenderer()


def _software_factory():
    renderer = render.SoftwareVideoRenderer()
    renderer.fallback_reason = "GPU path unavailable: test injection"
    return renderer


class DesktopIntegrationTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()
        self.server = qt_harness.StubServer()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            qt_harness.close_window(self.window)
            self.window = None

    def _window(self, factory=_gpu_factory):
        self.window = Desktop(self.server, renderer_factory=factory)
        self.window.show()
        qt_harness.pump_for(0.05)
        return self.window

    def _put(self, frame):
        self.server.frames.put(frame)
        qt_harness.pump_for(0.05)

    # --- mailbox / main path -------------------------------------------------
    def test_main_path_keeps_the_yuv_frame_and_drops_the_mandatory_bgra_conversion(self):
        window = self._window()
        frame = fx.solid_frame(64, 36, 63, 102, 240)
        with ReformatCounter() as counter:
            self._put(frame)
        self.assertEqual(counter.calls, 0, "the main path still converts every frame to BGRA")
        serial, latest = self.server.frames.latest()
        self.assertEqual(serial, 1)
        self.assertIsInstance(latest, av.VideoFrame)
        self.assertEqual((latest.width, latest.height), (64, 36))
        self.assertEqual(window.surface.renderer.stats()["framesPresented"], 1)

    def test_legacy_get_still_returns_a_usable_image_for_existing_smoke_scripts(self):
        self._window()
        frame = fx.solid_frame(32, 18, 235, 128, 128)
        self._put(frame)
        serial, image = self.server.frames.get()
        self.assertEqual(serial, 1)
        self.assertIsNotNone(image)
        self.assertEqual((image.width(), image.height()), (32, 18))

    def test_hidden_obs_window_does_no_prepare_work_and_main_preview_still_renders(self):
        window = self._window()
        with ReformatCounter() as counter:
            for _ in range(5):
                self._put(fx.solid_frame(64, 36, 100, 128, 128))
        self.assertEqual(counter.calls, 0, "GPU-path preparation must not use BGRA conversion")
        self.assertEqual(window.surface.renderer.stats()["framesPrepared"], 5)
        self.assertEqual(window.obs_pane.renderer.stats()["framesPrepared"], 0,
                         "a hidden OBS window prepared frames nobody can see")
        window.open_obs()
        qt_harness.pump_for(0.05)
        # Becoming visible costs exactly one catch-up conversion of the latest frame.
        self.assertEqual(window.obs_pane.renderer.stats()["framesPrepared"], 1)
        self.assertEqual(window.obs_pane.renderer.stats()["framesPresented"], 1)
        before = window.obs_pane.renderer.stats()["framesPrepared"]
        self._put(fx.solid_frame(64, 36, 120, 128, 128))
        self.assertGreater(window.obs_pane.renderer.stats()["framesPrepared"], before)
        self.assertEqual(window.obs_pane.renderer.stats()["framesPresented"], 2)

    def test_two_windows_share_one_decoded_frame(self):
        window = self._window()
        window.open_obs()
        qt_harness.pump_for(0.05)
        bright = fx.solid_frame(64, 36, 100, 150, 200)
        # The preview is throttled while both windows are open (serial % 2), so the
        # second frame is the one both windows present; the source object is shared.
        self.server.frames.put(bright)
        frame = fx.solid_frame(64, 36, 100, 150, 200)
        before = [fx.plane_rows(frame, index) for index in range(3)]
        self.server.frames.put(frame)
        qt_harness.pump_for(0.05)
        self.assertEqual([fx.plane_rows(frame, index) for index in range(3)], before)
        preview = window.surface.renderer
        obs = window.obs_pane.renderer
        self.assertEqual(preview.stats()["framesPresented"], 1)
        self.assertEqual(obs.stats()["framesPresented"], 1)
        expected = fx.reference_rgb(100, 150, 200, render.MATRIX_BT601, video_range=True)
        for pane_renderer in (preview, obs):
            if pane_renderer.mode == "gpu":
                sink = pane_renderer.video_sink().videoFrame()
                self.assertTrue(sink.isValid())
                got = sink.toImage().pixelColor(32, 18).getRgb()[:3]
                self.assertTrue(fx.rgb_close(got, expected, fx.SOFTWARE_TOLERANCE))
        if obs.mode == "software":
            qt_harness.pump_for(0.02)
            shot = window.obs_pane.grab().toImage()
            got = shot.pixelColor(shot.width() // 2, shot.height() // 2).getRgb()[:3]
            self.assertTrue(fx.rgb_close(got, expected, fx.SOFTWARE_TOLERANCE))

    def test_notifications_coalesce_and_do_not_accumulate(self):
        window = self._window()
        seen = []
        self.server.frames.notice.changed.connect(lambda: seen.append(self.server.frames.latest()[0]))
        for _ in range(21):
            self.server.frames.put(fx.solid_frame(64, 36, 100, 128, 128))
        qt_harness.pump_for(0.05)
        self.assertEqual(len(seen), 1, f"notifications accumulated: {seen}")
        self.assertEqual(seen[0], 21)
        self.assertEqual(window.surface.renderer.stats()["framesPresented"], 1,
                         "the GUI must present only the newest frame of a burst")

    def test_disconnect_clears_both_surfaces(self):
        window = self._window()
        window.open_obs()
        self._put(fx.solid_frame(64, 36, 100, 128, 128))
        self.server.frames.put(None)  # receiver completion signal
        qt_harness.pump_for(0.05)
        self.assertIsNone(window.surface.current_image())
        self.assertIsNone(window.obs_pane.current_image())
        self.assertTrue(window.surface.placeholder.isVisible())

    # --- diagnostics / exact output size ------------------------------------
    def test_diagnostics_show_actual_output_path_and_fallback_reason(self):
        window = self._window(factory=_software_factory)
        self._put(fx.solid_frame(64, 36, 100, 128, 128))
        self.window.on_stats({"fps": 60.0, "bitrate": 1.0, "jitter": 0.0, "loss": 0.0})
        text = window.output_diag.text()
        self.assertIn("software", text.lower(), text)
        self.assertIn("GPU path unavailable: test injection", text)

    def test_diagnostics_show_software_conversion_fallback_for_unverified_colour(self):
        window = self._window()
        self._put(fx.solid_frame(64, 36, 30, 255, 128, colorspace=av.video.reformatter.Colorspace.ITU601,
                                 video_range=av.video.reformatter.ColorRange.JPEG))
        self.window.on_stats({"fps": 60.0, "bitrate": 1.0, "jitter": 0.0, "loss": 0.0})
        text = window.output_diag.text()
        self.assertIn("bt601/full", text)
        self.assertIn("software colour conversion", text)

    def test_exact_output_size_describes_physical_pixels(self):
        window = self._window()
        self.assertTrue(window.exact_output.isChecked(), "exact 1:1 output must be the default")
        self._put(fx.solid_frame(2560, 1440, 100, 128, 128))
        dpr = window.obs.devicePixelRatioF()
        expected_logical = render.exact_window_size(QSize(2560, 1440), dpr)
        self.assertEqual(window.obs.width(), expected_logical.width())
        self.assertEqual(window.obs.height(), expected_logical.height())
        self.assertEqual(render.physical_size(window.obs.size(), dpr), QSize(2560, 1440))
        self.window.on_stats({"fps": 60.0, "bitrate": 1.0, "jitter": 0.0, "loss": 0.0})
        self.assertIn("2560", window.output_diag.text())
        self.assertIn("1.00", window.output_diag.text(), "exact mode must report 1:1 scale")

    def test_exact_mode_off_keeps_a_window_that_can_be_moved(self):
        window = self._window()
        window.exact_output.setChecked(False)
        self._put(fx.solid_frame(2560, 1440, 100, 128, 128))
        self.assertEqual(window.obs.width(), 1280)
        self.assertEqual(window.obs.height(), 720)


if __name__ == "__main__":
    unittest.main()
