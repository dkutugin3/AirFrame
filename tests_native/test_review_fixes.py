"""Fix-pass contracts from the branch review (C1, I2, I6, I13).

Run offscreen with:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

Each test here was written first and watched fail against the reviewed code
(see the RED log in the native-gpu report); the production fixes are minimal.
"""
import threading
import unittest

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


class GatedRenderer(render.SoftwareVideoRenderer):
    """Renderer whose ``prepare`` can block, to reproduce the C1 wake-up race."""

    def __init__(self):
        super().__init__()
        self.set_active(True)
        self.block_serial = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def prepare(self, frame, *, serial):
        if serial == self.block_serial:
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("gated prepare was never released")
        return super().prepare(frame, serial=serial)


class RecordingRenderer(render.SoftwareVideoRenderer):
    def __init__(self):
        super().__init__()
        self.set_active(True)
        self.presented = []
        self.prepared = []

    def prepare(self, frame, *, serial):
        prepared = super().prepare(frame, serial=serial)
        self.prepared.append(prepared)
        return prepared

    def present(self, prepared):
        result = super().present(prepared)
        if result:
            self.presented.append(prepared.serial)
        return result


class ExplodingRenderer(render.SoftwareVideoRenderer):
    def __init__(self):
        super().__init__()
        self.set_active(True)

    def prepare(self, frame, *, serial):
        raise RuntimeError("simulated prepare failure")


class RotatingRenderer(render.SoftwareVideoRenderer):
    """Software renderer that reports a 90-degree display rotation."""

    def __init__(self):
        super().__init__()
        self.set_active(True)

    def prepare(self, frame, *, serial):
        prepared = super().prepare(frame, serial=serial)
        return render.PreparedFrame(
            serial=prepared.serial,
            colour=prepared.colour,
            width=prepared.width,
            height=prepared.height,
            rotation=90,
            image=prepared.image,
        )


def _frame(series=1, value=100):
    return fx.solid_frame(32, 18, value, 128, 128)


class LostWakeupRaceTests(unittest.TestCase):
    """C1: a dispatch during the expensive prepare must not lose the newest frame."""

    def setUp(self):
        fx.qapp()

    def test_prepare_slower_than_dispatch_still_presents_the_newest_frame(self):
        frames = Frames()
        gated = GatedRenderer()
        frames.attach_renderer("preview", gated)
        presented, notifications = [], []

        def consume():
            notifications.append(1)
            ready = frames.take_ready()
            presented.extend(prepared.serial for prepared in ready.values())

        frames.notice.changed.connect(consume)

        frames.put(_frame(1, 100))
        gated.block_serial = 2
        worker = threading.Thread(target=lambda: frames.put(_frame(2, 200)), daemon=True)
        worker.start()
        self.assertTrue(gated.entered.wait(5), "worker never reached the gated prepare")
        # Dispatch the already queued notification for frame 1 while frame 2 is
        # still being prepared. This is the window that loses the wake-up.
        fx.pump(0.08)
        self.assertEqual(presented, [1], "the first frame was not presented")
        gated.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "worker did not finish")
        fx.pump(0.08)
        self.assertEqual(presented, [1, 2],
                         "lost wake-up: the newest prepared frame was never presented")
        self.assertEqual(len(notifications), 2, "the second batch produced no notification")


class RotationAwareExactSizeTests(unittest.TestCase):
    """I2: rotated streams must claim 1:1 only when the OBS window really is 1:1."""

    def setUp(self):
        qt_harness.qt_app()
        self.server = qt_harness.StubServer()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            qt_harness.close_window(self.window)
            self.window = None

    def test_rotated_90_stream_sizes_the_exact_window_from_the_output_size(self):
        self.window = Desktop(self.server, renderer_factory=RotatingRenderer)
        self.window.show()
        # Offscreen DPR is 1; pin the reviewed 2x display case explicitly.
        self.window._device_pixel_ratio = lambda: 2.0
        qt_harness.pump_for(0.05)
        self.server.frames.put(fx.solid_frame(640, 360, 100, 128, 128))
        qt_harness.pump_for(0.08)
        self.window.on_stats({"fps": 60.0, "bitrate": 1.0, "jitter": 0.0, "loss": 0.0})
        # 640x360 rotated 90/270 presents as 360x640 physical -> 180x320 logical at 2x.
        self.assertEqual((self.window.obs.width(), self.window.obs.height()), (180, 320),
                         "exact OBS window ignored the display rotation")
        self.assertEqual(
            render.physical_size(self.window.obs.size(), 2.0), QSize(360, 640),
            "the OBS window is not 1:1 physical pixels for the rotated output",
        )
        text = self.window.output_diag.text()
        self.assertIn("360×640", text, text)
        self.assertIn("1.00", text, text)


class PrepareFailureVisibilityTests(unittest.TestCase):
    """I6: a renderer prepare failure must be visible and must not stop the other pane."""

    def setUp(self):
        qt_harness.qt_app()
        self.server = qt_harness.StubServer()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            qt_harness.close_window(self.window)
            self.window = None

    def _window(self):
        created = []

        def factory():
            renderer = ExplodingRenderer() if not created else render.SoftwareVideoRenderer()
            created.append(renderer)
            return renderer

        self.window = Desktop(self.server, renderer_factory=factory)
        self.window.show()
        qt_harness.pump_for(0.05)
        return self.window

    def test_prepare_failure_is_counted_and_surfaced_while_the_other_window_works(self):
        window = self._window()
        self.server.frames.put(_frame(1, 100))
        qt_harness.pump_for(0.08)
        preview = window.surface.renderer
        obs = window.obs_pane.renderer
        self.assertEqual(obs.stats()["framesPresented"], 1,
                         "the healthy window stopped presenting after the other one failed")
        self.assertEqual(preview.stats()["framesPresented"], 0)
        self.assertEqual(preview.stats()["framesPrepareErrors"], 1)
        text = window.output_diag.text()
        self.assertIn("Ошибки подготовки", text, text)
        self.assertIn("simulated prepare failure", text, text)
        # The failure stays counted and visible for later frames too.
        self.server.frames.put(_frame(2, 150))
        qt_harness.pump_for(0.08)
        self.assertEqual(preview.stats()["framesPrepareErrors"], 2)
        self.assertEqual(obs.stats()["framesPresented"], 2)


class ProductionFramesLifetimeTests(unittest.TestCase):
    """I13: the real desktop.Frames mailbox owns the lifetime/pinning contract."""

    def setUp(self):
        fx.qapp()

    def test_replacement_and_session_end_do_not_free_a_held_source_frame(self):
        frames = Frames()
        recorder = RecordingRenderer()
        frames.attach_renderer("preview", recorder)
        source = fx.wramp_frame(64, 36)
        before = [fx.plane_rows(source, index) for index in range(3)]
        frames.put(source)
        prepared = frames.take_ready()["preview"]
        self.assertIs(prepared.source, source, "desktop.Frames must pin the prepared source")
        # Mailbox replacement and the session-end signal must not free the held frame.
        frames.put(fx.solid_frame(64, 36, 200, 128, 128))
        frames.take_ready()
        frames.put(None)
        frames.take_ready()
        self.assertEqual([fx.plane_rows(source, index) for index in range(3)], before)
        recorder.widget().resize(64, 36)
        recorder.widget().show()
        fx.pump(0.02)
        self.assertTrue(recorder.present(prepared), "the held frame can no longer be presented")
        self.assertEqual(recorder.presented, [1])

    def test_two_panes_share_one_decoded_frame_and_keep_it_pinned(self):
        server = qt_harness.StubServer()
        window = Desktop(server, renderer_factory=render.SoftwareVideoRenderer)
        try:
            window.show()
            window.open_obs()
            qt_harness.pump_for(0.06)
            source = fx.wramp_frame(64, 36)
            before = [fx.plane_rows(source, index) for index in range(3)]
            server.frames.put(source)
            qt_harness.pump_for(0.08)
            self.assertEqual(window.surface.renderer.stats()["framesPresented"], 1)
            self.assertEqual(window.obs_pane.renderer.stats()["framesPresented"], 1)
            server.frames.put(None)  # session ends while both panes hold the frame
            qt_harness.pump_for(0.05)
            self.assertEqual([fx.plane_rows(source, index) for index in range(3)], before,
                             "closing the session freed a decoded frame that was in use")
            # The pinned source is still usable for a later conversion.
            image = render.software_image_from_av(source)
            self.assertEqual(image.size(), QSize(64, 36))
        finally:
            qt_harness.close_window(window)


if __name__ == "__main__":
    unittest.main()
