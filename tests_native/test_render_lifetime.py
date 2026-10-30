"""Stage 6 lifetime/selection contracts: mailbox, GPU lifetime, two windows, hidden preview, fallback.

Run offscreen with:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

The GPU frame-lifetime rules are proven here against the pinned PyAV buffers:
a prepared frame must keep its source alive until the GPU use finished, and a
mailbox replacement / close / session switch must not free a frame in use.
Whether the path is accelerated is NOT proven here; the real-window run is
scripts/verify-render-native.py.
"""
import unittest

from av.video.reformatter import ColorRange, Colorspace

try:
    import render_fixtures as fx
except ImportError:  # pragma: no cover
    from tests_native import render_fixtures as fx

from PySide6.QtCore import QSize
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

import airframe.render as render


def _software_renderer():
    return render.SoftwareVideoRenderer()


def _gpu_renderer():
    return render.QtVideoRenderer()


def _show(renderer, width, height):
    renderer.widget().resize(width, height)
    renderer.widget().show()
    fx.pump(0.02)


def _rgb_centre(renderer):
    fx.pump(0.02)
    image = renderer.widget().grab().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2).getRgb()[:3]


class MailboxTests(unittest.TestCase):
    def setUp(self):
        fx.qapp()

    def test_latest_wins_and_notifications_are_coalesced(self):
        mailbox = render.FrameMailbox()
        app = fx.qapp()
        calls = []
        mailbox.changed.connect(lambda: calls.append(mailbox.latest()[0]))
        frames = [fx.solid_frame(8, 8, 16 + i, 128, 128) for i in range(5)]
        for i, frame in enumerate(frames, start=1):
            self.assertEqual(mailbox.put(frame), i)
        self.assertEqual(mailbox.latest()[0], 5)
        self.assertTrue(mailbox.notification_pending)
        fx.pump(0.05)
        self.assertFalse(mailbox.notification_pending)
        self.assertEqual(calls, [5], "five puts must coalesce into one GUI notification")
        mailbox.put(fx.solid_frame(8, 8, 30, 128, 128))
        fx.pump(0.05)
        self.assertEqual(calls, [5, 6])

    def test_put_after_close_is_ignored_and_close_is_idempotent(self):
        mailbox = render.FrameMailbox()
        mailbox.put(fx.solid_frame(8, 8, 20, 128, 128))
        mailbox.close()
        mailbox.close()
        self.assertTrue(mailbox.closed)
        self.assertIsNone(mailbox.put(fx.solid_frame(8, 8, 40, 128, 128)))
        self.assertIsNotNone(mailbox.latest())
        mailbox.clear()
        self.assertIsNone(mailbox.latest())

    def test_mailbox_replacement_and_close_do_not_free_a_presented_source(self):
        """A frame handed to the renderer stays valid across replacement/close."""
        mailbox = render.FrameMailbox()
        source = fx.wramp_frame(64, 36)
        before = fx.plane_rows(source, 0)
        mailbox.put(source)
        serial, handed = mailbox.latest()
        renderer = _gpu_renderer()
        prepared = renderer.prepare(handed, serial=serial)
        # Replace the mailbox and close the session while the frame is prepared.
        mailbox.put(fx.solid_frame(64, 36, 30, 128, 128))
        mailbox.close()
        # The pinned source is intact and can still be presented after close.
        self.assertEqual(fx.plane_rows(source, 0), before)
        _show(renderer, 640, 360)
        renderer.present(prepared)
        self.assertEqual(fx.plane_rows(source, 0), before, "present touched/freed the source early")
        renderer.close()

    def test_session_switch_replaces_frames_without_affecting_held_ones(self):
        mailbox = render.FrameMailbox()
        first = fx.solid_frame(32, 18, 16, 128, 128)
        second = fx.solid_frame(32, 18, 235, 128, 128)
        mailbox.put(first)
        _, held = mailbox.latest()
        mailbox.clear()  # session switch
        mailbox.put(second)
        self.assertIs(mailbox.latest()[1], second)
        self.assertEqual(fx.plane_rows(held, 0)[0][0], 16)
        mailbox.close()


class FrameLifetimeTests(unittest.TestCase):
    def setUp(self):
        fx.qapp()

    def test_gpu_path_does_not_need_the_pyav_buffer_after_present(self):
        renderer = _gpu_renderer()
        _show(renderer, 320, 180)
        frame = fx.wramp_frame(64, 36)
        expected = fx.plane_rows(frame, 0)
        prepared = renderer.prepare(frame, serial=1)
        self.assertIs(prepared.source, frame, "GPU preparation must pin its source frame")
        del frame
        # Only the prepared frame keeps the source: the sink must stay valid.
        renderer.present(prepared)
        self.assertEqual(fx.plane_rows(prepared.source, 0), expected)
        sink_frame = renderer.video_sink().videoFrame()
        self.assertTrue(sink_frame.isValid(), "sink did not keep a valid frame for GPU use")
        self.assertEqual(sink_frame.size(), QSize(64, 36))
        renderer.close()

    def test_software_route_of_gpu_renderer_pins_source_until_present(self):
        """Unverified colour combos go through the software conversion; pin survives replacement."""
        renderer = _gpu_renderer()
        _show(renderer, 320, 180)
        frame = fx.solid_frame(32, 18, 30, 255, 128, colorspace=Colorspace.ITU601,
                               video_range=ColorRange.JPEG)  # BT.601 full: not GPU-verified
        prepared = renderer.prepare(frame, serial=1)
        del frame
        renderer.present(prepared)
        stats = renderer.stats()
        self.assertEqual(stats["framesSoftwareRoute"], 1)
        self.assertIn("colour", (renderer.fallback_reason or "").lower())
        got = _rgb_centre(renderer)
        expected = fx.reference_rgb(30, 255, 128, render.MATRIX_BT601, video_range=False)
        self.assertTrue(fx.rgb_close(got, expected, fx.SOFTWARE_TOLERANCE),
                        f"software-routed frame colour {got} != {expected}")
        renderer.close()


class TwoWindowTests(unittest.TestCase):
    def setUp(self):
        fx.qapp()

    def test_two_consumers_share_one_decoded_frame(self):
        # Solid colour so the expected pixel is independent of chroma upsampling.
        frame = fx.solid_frame(64, 36, 100, 150, 200)
        before = [fx.plane_rows(frame, i) for i in range(3)]
        first = _software_renderer()
        second = _software_renderer()
        _show(first, 64, 36)
        _show(second, 64, 36)
        expected = fx.reference_rgb(100, 150, 200, render.MATRIX_BT601, video_range=True)
        for renderer in (first, second):
            prepared = renderer.prepare(frame, serial=1)
            renderer.present(prepared)
        self.assertEqual([fx.plane_rows(frame, i) for i in range(3)], before,
                         "presenting to one consumer modified the shared decoded frame")
        for renderer in (first, second):
            fx.pump(0.02)
            self.assertEqual(renderer.stats()["framesPresented"], 1)
            got = _rgb_centre(renderer)
            self.assertTrue(fx.rgb_close(got, expected, fx.SOFTWARE_TOLERANCE), f"{got} != {expected}")
        first.close()
        second.close()

    def test_hidden_preview_does_no_work(self):
        renderer = _software_renderer()
        frame = fx.solid_frame(32, 18, 235, 128, 128)
        prepared = renderer.prepare(frame, serial=1)
        self.assertFalse(renderer.wants_frame(), "a hidden renderer must not request frames")
        renderer.present(prepared)
        self.assertEqual(renderer.stats()["framesPresented"], 0)
        self.assertEqual(renderer.stats()["framesSkippedHidden"], 1)
        # Becoming visible activates the renderer for the decode thread (VideoPane does this).
        renderer.set_active(True)
        _show(renderer, 32, 18)
        self.assertTrue(renderer.wants_frame())
        renderer.refresh()
        self.assertEqual(renderer.stats()["framesPresented"], 1)
        renderer.close()

    def test_gpu_renderer_hidden_does_not_upload(self):
        renderer = _gpu_renderer()
        prepared = renderer.prepare(fx.solid_frame(32, 18, 235, 128, 128), serial=1)
        renderer.present(prepared)
        self.assertFalse(renderer.video_sink().videoFrame().isValid(),
                         "hidden GPU renderer uploaded a frame")
        _show(renderer, 32, 18)
        renderer.refresh()
        self.assertTrue(renderer.video_sink().videoFrame().isValid())
        renderer.close()

    def test_close_releases_and_ignores_later_presents(self):
        renderer = _software_renderer()
        _show(renderer, 32, 18)
        renderer.present(renderer.prepare(fx.solid_frame(32, 18, 200, 128, 128), serial=1))
        renderer.close()
        renderer.close()  # idempotent
        self.assertFalse(renderer.wants_frame())
        renderer.present(renderer.prepare(fx.solid_frame(32, 18, 30, 128, 128), serial=2))
        self.assertEqual(renderer.stats()["framesPresented"], 1)


class SelectionAndFallbackTests(unittest.TestCase):
    def setUp(self):
        fx.qapp()

    def test_auto_offscreen_falls_back_to_software_with_a_reason(self):
        renderer = render.create_renderer("auto")
        self.assertEqual(renderer.requested_mode, "auto")
        self.assertEqual(renderer.mode, "software")
        self.assertTrue(renderer.fallback_reason, "a fallback must show its reason")
        self.assertIn("offscreen", renderer.fallback_reason.lower())
        self.assertIn("software", renderer.output_path())
        renderer.close()

    def test_forced_gpu_unavailable_reports_impossibility_honestly(self):
        renderer = render.create_renderer("gpu", probe=lambda: (False, "no RHI backend"))
        self.assertEqual(renderer.requested_mode, "gpu")
        self.assertEqual(renderer.mode, "software", "software must not be labelled hardware")
        self.assertIn("no RHI backend", renderer.fallback_reason)
        renderer.close()

    def test_forced_software_has_no_fallback_reason(self):
        renderer = render.create_renderer("software")
        self.assertEqual(renderer.mode, "software")
        self.assertIsNone(renderer.fallback_reason)
        renderer.close()

    def test_auto_with_gpu_probe_uses_gpu_renderer(self):
        renderer = render.create_renderer("auto", probe=lambda: (True, ""))
        self.assertEqual(renderer.mode, "gpu")
        self.assertIsInstance(renderer, render.QtVideoRenderer)
        renderer.close()

    def test_gpu_renderer_reports_actual_rhi_state_not_the_requested_one(self):
        renderer = _gpu_renderer()
        _show(renderer, 320, 180)
        renderer.present(renderer.prepare(fx.solid_frame(32, 18, 235, 128, 128), serial=1))
        path = renderer.output_path()
        if renderer.rhi_backend() is None:
            self.assertIn("no RHI", path, "offscreen has no RHI backend; diagnostics must say so")
        else:  # pragma: no cover - only on a real display
            self.assertIn("rhi", path.lower())
        renderer.close()

    def test_software_fallback_keeps_showing_video(self):
        renderer = render.create_renderer("auto")  # offscreen => software fallback
        _show(renderer, 32, 18)
        expected = fx.reference_rgb(235, 128, 128, render.MATRIX_BT601, video_range=True)
        renderer.present(renderer.prepare(
            fx.solid_frame(32, 18, 235, 128, 128, colorspace=Colorspace.ITU709,
                           video_range=ColorRange.MPEG), serial=1))
        got = _rgb_centre(renderer)
        self.assertTrue(fx.rgb_close(got, expected, fx.SOFTWARE_TOLERANCE), f"{got} != {expected}")
        renderer.close()


if __name__ == "__main__":
    unittest.main()
