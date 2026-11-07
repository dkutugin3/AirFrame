"""Per-output preparation must not duplicate copies or run conversions on the GUI thread."""
import unittest
from unittest.mock import patch
import av
from tests_native import qt_harness, render_fixtures as fx
from airframe.desktop import Frames
from airframe import render

class PreparationTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()

    def test_equivalent_outputs_share_one_owned_yuv_buffer(self):
        frames = Frames()
        first, second = render.QtVideoRenderer(), render.QtVideoRenderer()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        for key, renderer in [('preview', first), ('obs', second)]:
            renderer.set_active(True)
            frames.attach_renderer(key, renderer)
        frame = fx.solid_frame(64, 36, 100, 150, 200)
        frames.put(frame)
        ready = frames.take_ready()
        self.assertIs(ready['preview'].qframe, ready['obs'].qframe)
        self.assertTrue(ready['obs'].qframe.isValid())
        self.assertEqual(first.stats()['framesPrepared'], 1)
        self.assertEqual(second.stats()['framesPrepared'], 1)
        # A new source gets a new buffer; old consumers retain their frame.
        frames.put(fx.solid_frame(64, 36, 200, 128, 128))
        newer = frames.take_ready()
        self.assertIsNot(newer['obs'].qframe, ready['obs'].qframe)
        self.assertTrue(ready['obs'].qframe.isValid())

    def test_colour_fallback_is_converted_during_prepare(self):
        renderer = render.QtVideoRenderer()
        self.addCleanup(renderer.close)
        frame = fx.solid_frame(64, 36, 30, 255, 128,
                              colorspace=av.video.reformatter.Colorspace.ITU601,
                              video_range=av.video.reformatter.ColorRange.JPEG)
        prepared = renderer.prepare(frame, serial=1)
        self.assertIsNone(prepared.qframe)
        self.assertIsNotNone(prepared.image, 'conversion must finish before GUI presentation')
        with patch.object(render, 'software_image_from_av', side_effect=AssertionError('GUI conversion')):
            self.assertTrue(renderer.present(prepared, hidden_ok=True))
        self.assertFalse(renderer.last_image().isNull())

if __name__ == '__main__':
    unittest.main()
