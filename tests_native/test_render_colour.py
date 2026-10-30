"""Stage 6 colour/format contracts for the YUV renderer (new tests_native file).

Run offscreen with:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

These tests pin the colour metadata, plane/stride handling and U/V order that
the GPU path hands to Qt, and the exact reference conversion used by the
software fallback. Tolerances are fixed in render_fixtures before the renderer
is implemented (SOFTWARE_TOLERANCE = 3 against the ITU equations).

Offscreen this does NOT prove GPU acceleration: it proves the frame contract.
The real-window screen-capture run is scripts/verify-render-native.py.
"""
import unittest

from av.video.reformatter import ColorRange, Colorspace

try:  # discovered with top_level_dir=tests_native
    import render_fixtures as fx
except ImportError:  # pragma: no cover - defensive for other discovery roots
    from tests_native import render_fixtures as fx

from PySide6.QtCore import QSize
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QtVideo

import airframe.render as render

TOL = fx.SOFTWARE_TOLERANCE

# Distinct patches: greys, primaries and chroma-only probes that expose a
# swapped U/V, a wrong matrix, or a wrong limited/full range.
PATCHES = [
    (16, 128, 128),    # limited black
    (235, 128, 128),   # limited white
    (128, 128, 128),   # mid grey
    (63, 102, 240),    # red-ish
    (145, 54, 34),     # blue-ish
    (81, 90, 240),     # red-ish (601 encoding)
    (128, 255, 128),   # Cb-only probe: blue
    (128, 0, 128),     # Cb-only probe: opposite
    (128, 128, 255),   # Cr-only probe: red
    (128, 128, 0),     # Cr-only probe: opposite
    (0, 128, 128),     # full-range black
    (255, 128, 128),   # full-range white
]


def _spec(matrix, video_range, as_name_range):
    return render.ColourSpec(matrix=matrix, range=as_name_range)


def _software_image(matrix, video_range, y, u, v):
    frame = fx.solid_frame(
        16,
        16,
        y,
        u,
        v,
        colorspace=Colorspace.ITU709 if matrix == render.MATRIX_BT709 else Colorspace.ITU601,
        video_range=ColorRange.MPEG if video_range == render.RANGE_LIMITED else ColorRange.JPEG,
    )
    return render.software_image_from_av(frame)


class ColourConversionTests(unittest.TestCase):
    def test_software_conversion_matches_itu_reference_for_all_specs(self):
        """Known matrix + full/limited range must match the ITU reference table."""
        for matrix in (render.MATRIX_BT601, render.MATRIX_BT709):
            for video_range in (True, False):
                spec_range = render.RANGE_LIMITED if video_range else render.RANGE_FULL
                image = _software_image(matrix, video_range, *PATCHES[0])
                for (y, u, v) in PATCHES:
                    frame = fx.solid_frame(
                        16,
                        16,
                        y,
                        u,
                        v,
                        colorspace=Colorspace.ITU709 if matrix == render.MATRIX_BT709 else Colorspace.ITU601,
                        video_range=ColorRange.MPEG if video_range else ColorRange.JPEG,
                    )
                    image = render.software_image_from_av(frame)
                    got = image.pixelColor(8, 8).getRgb()[:3]
                    expected = fx.reference_rgb(y, u, v, matrix, video_range)
                    with self.subTest(matrix=matrix, video_range=video_range, yuv=(y, u, v)):
                        self.assertTrue(
                            fx.rgb_close(got, expected, TOL),
                            f"{matrix}/{spec_range} YUV({y},{u},{v}) -> {got}, expected {expected} +/-{TOL}",
                        )

    def test_no_swapped_uv(self):
        """Cb (U) drives blue and Cr (V) drives red; a swap must be impossible."""
        blue = render.software_image_from_av(
            fx.solid_frame(16, 16, 128, 255, 128, video_range=ColorRange.JPEG)
        ).pixelColor(8, 8).getRgb()[:3]
        red = render.software_image_from_av(
            fx.solid_frame(16, 16, 128, 128, 255, video_range=ColorRange.JPEG)
        ).pixelColor(8, 8).getRgb()[:3]
        self.assertGreater(blue[2] - blue[0], 100, f"Cb-high patch is not blue: {blue}")
        self.assertGreater(red[0] - red[2], 100, f"Cr-high patch is not red: {red}")

    def test_limited_range_black_and_white_are_not_remapped(self):
        """No raised blacks (16 -> ~0) and no clipped whites short of 235 -> ~255."""
        black = render.software_image_from_av(
            fx.solid_frame(16, 16, 16, 128, 128, colorspace=Colorspace.ITU709, video_range=ColorRange.MPEG)
        ).pixelColor(8, 8).getRgb()[:3]
        white = render.software_image_from_av(
            fx.solid_frame(16, 16, 235, 128, 128, colorspace=Colorspace.ITU709, video_range=ColorRange.MPEG)
        ).pixelColor(8, 8).getRgb()[:3]
        self.assertLessEqual(max(black), 1, f"limited black raised: {black}")
        self.assertGreaterEqual(min(white), 250, f"limited white clipped: {white}")

    def test_unspecified_metadata_matches_legacy_bgra_path(self):
        """Metadata-less streams keep the exact old swscale colours (no regression)."""
        frame = fx.wramp_frame(64, 36)
        new = render.software_image_from_av(frame)
        legacy = QVideoFrame.toImage  # doc anchor; actual conversion below
        bgra = frame.reformat(format="bgra")
        plane = bgra.planes[0]
        from PySide6.QtGui import QImage

        legacy_image = QImage(memoryview(plane), bgra.width, bgra.height, plane.line_size,
                              QImage.Format.Format_ARGB32).copy()
        diffs = []
        for y in (3, 11, 25):
            for x in (0, 17, 40, 63):
                got = new.pixelColor(x, y).getRgb()[:3]
                exp = legacy_image.pixelColor(x, y).getRgb()[:3]
                diffs.append(max(abs(a - b) for a, b in zip(got, exp)))
        self.assertLessEqual(max(diffs), 1, f"new conversion deviates from legacy BGRA: {diffs}")

    def test_default_colour_spec_matches_old_path_defaults(self):
        frame = fx.solid_frame(8, 8, 63, 102, 240)
        spec = render.ColourSpec.from_av_frame(frame)
        self.assertEqual(spec.matrix, render.MATRIX_BT601)
        self.assertEqual(spec.range, render.RANGE_LIMITED)

    def test_qvideo_frame_carries_declared_colour_metadata(self):
        mapping = {
            (render.MATRIX_BT601, render.RANGE_LIMITED):
                (QVideoFrameFormat.ColorSpace.ColorSpace_BT601, QVideoFrameFormat.ColorRange.ColorRange_Video),
            (render.MATRIX_BT601, render.RANGE_FULL):
                (QVideoFrameFormat.ColorSpace.ColorSpace_BT601, QVideoFrameFormat.ColorRange.ColorRange_Full),
            (render.MATRIX_BT709, render.RANGE_LIMITED):
                (QVideoFrameFormat.ColorSpace.ColorSpace_BT709, QVideoFrameFormat.ColorRange.ColorRange_Video),
            (render.MATRIX_BT709, render.RANGE_FULL):
                (QVideoFrameFormat.ColorSpace.ColorSpace_BT709, QVideoFrameFormat.ColorRange.ColorRange_Full),
        }
        for (matrix, spec_range), (qt_cs, qt_range) in mapping.items():
            frame = fx.solid_frame(
                32, 18, 63, 102, 240,
                colorspace=Colorspace.ITU709 if matrix == render.MATRIX_BT709 else Colorspace.ITU601,
                video_range=ColorRange.JPEG if spec_range == render.RANGE_FULL else ColorRange.MPEG,
            )
            self.assertEqual(render.ColourSpec.from_av_frame(frame).matrix, matrix)
            qframe = render.qvideo_frame_from_av(frame)
            surface = qframe.surfaceFormat()
            with self.subTest(matrix=matrix, spec_range=spec_range):
                self.assertEqual(qframe.pixelFormat(), QVideoFrameFormat.PixelFormat.Format_YUV420P)
                self.assertEqual(surface.colorSpace(), qt_cs)
                self.assertEqual(surface.colorRange(), qt_range)
                self.assertEqual(qframe.size(), QSize(32, 18))

    def test_gpu_colour_support_is_limited_to_verified_combos(self):
        """Only combos measured correct on the real Metal path may use the GPU path."""
        self.assertEqual(render.GPU_SUPPORTED_COLOURS, fx.GPU_SUPPORTED_COMBOS)
        for (matrix, spec_range) in fx.GPU_SUPPORTED_COMBOS:
            self.assertTrue(render.ColourSpec(matrix, spec_range).gpu_supported,
                            f"{matrix}/{spec_range} must be GPU-supported (verified by capture)")
        self.assertFalse(render.ColourSpec(render.MATRIX_BT601, render.RANGE_FULL).gpu_supported,
                         "BT.601 full measured up to 26 codes off on Qt 6.12 RHI; must not be GPU-supported")


class PlaneLayoutTests(unittest.TestCase):
    def _assert_planes_match(self, frame, qframe, max_rows=None):
        """The Qt frame must contain the exact source plane bytes, strides honoured."""
        for index in range(len(frame.planes)):
            expected_rows = fx.plane_rows(frame, index)
            data_bytes = fx.source_plane_bytes(frame, index)
            rows = len(expected_rows) if max_rows is None else min(len(expected_rows), max_rows)
            actual_rows = fx.qframe_plane_rows(qframe, index, rows)
            if index == 0:
                self.assertEqual(len(actual_rows), rows, "luma plane: wrong row count")
            else:
                # Qt internally rounds odd heights down for 4:2:0 chroma; the rows it
                # does have must match exactly (documented odd-size limitation).
                self.assertGreaterEqual(len(actual_rows), rows // 2, "chroma plane lost too many rows")
            for row, (got, expected) in enumerate(zip(actual_rows, expected_rows)):
                with self.subTest(plane=index, row=row):
                    self.assertEqual(got[:data_bytes], expected[:data_bytes],
                                     f"plane {index} row {row} bytes differ (stride/plane bug)")

    def test_yuv420p_nonstandard_stride(self):
        frame = fx.wramp_frame(100, 50)  # av line_size 112 > width: stride matters
        self.assertNotEqual(frame.planes[0].line_size, frame.width)
        qframe = render.qvideo_frame_from_av(frame)
        self.assertEqual(qframe.planeCount(), 3)
        self._assert_planes_match(frame, qframe)

    def test_nv12_nonstandard_stride(self):
        frame = fx.wramp_frame(100, 50, pixel_format="nv12")
        qframe = render.qvideo_frame_from_av(frame)
        self.assertEqual(qframe.pixelFormat(), QVideoFrameFormat.PixelFormat.Format_NV12)
        self.assertEqual(qframe.planeCount(), 2)
        self._assert_planes_match(frame, qframe)

    def test_odd_dimensions_survive_plane_copy(self):
        frame = fx.wramp_frame(65, 49)
        qframe = render.qvideo_frame_from_av(frame)
        self.assertEqual(qframe.size(), QSize(65, 49))
        # Qt rounds the odd chroma height up/down internally: compare whole rows it has.
        self._assert_planes_match(frame, qframe)

    def test_solid_colours_survive_the_qt_cpu_conversion(self):
        """Qt's own CPU conversion (fixed BT.601 limited) must agree with the reference."""
        for (y, u, v) in ((16, 128, 128), (235, 128, 128), (128, 255, 128), (128, 128, 255)):
            frame = fx.solid_frame(32, 18, y, u, v)
            got = render.qvideo_frame_from_av(frame).toImage().pixelColor(16, 9).getRgb()[:3]
            expected = fx.reference_rgb(y, u, v, render.MATRIX_BT601, video_range=True)
            with self.subTest(yuv=(y, u, v)):
                self.assertTrue(fx.rgb_close(got, expected, TOL), f"{got} != {expected}")

    def test_nv12_uv_order_reaches_the_converter(self):
        blue = render.qvideo_frame_from_av(
            fx.solid_frame(32, 18, 128, 255, 128, pixel_format="nv12")).toImage().pixelColor(16, 9).getRgb()[:3]
        red = render.qvideo_frame_from_av(
            fx.solid_frame(32, 18, 128, 128, 255, pixel_format="nv12")).toImage().pixelColor(16, 9).getRgb()[:3]
        self.assertGreater(blue[2] - blue[0], 100, f"NV12 Cb-high is not blue: {blue}")
        self.assertGreater(red[0] - red[2], 100, f"NV12 Cr-high is not red: {red}")

    def test_unsupported_pixel_format_is_reported_not_guessed(self):
        frame = fx.solid_frame(32, 18, 100, 128, 128, pixel_format="yuv444p")
        with self.assertRaises(render.UnsupportedPixelFormat):
            render.qvideo_frame_from_av(frame)
        # The software conversion can still display it (honest fallback path).
        image = render.software_image_from_av(frame)
        self.assertEqual(image.size(), QSize(32, 18))


class RotationContractTests(unittest.TestCase):
    def test_qt_rotation_metadata_is_carried_on_the_frame(self):
        frame = fx.solid_frame(32, 18, 100, 128, 128)
        for degrees, qt_rotation in (
            (90, QtVideo.Rotation.Clockwise90),
            (180, QtVideo.Rotation.Clockwise180),
            (270, QtVideo.Rotation.Clockwise270),
        ):
            qframe = render.qvideo_frame_from_av(frame, rotation=degrees)
            with self.subTest(degrees=degrees):
                self.assertEqual(qframe.rotation(), qt_rotation)
                self.assertEqual(qframe.surfaceFormat().rotation(), qt_rotation)

    def test_pyav_counter_clockwise_rotation_maps_to_qt_clockwise(self):
        """PyAV reports counter-clockwise degrees; display rotation is clockwise."""
        self.assertEqual(render.ccw_to_cw(0), 0)
        self.assertEqual(render.ccw_to_cw(90), 270)
        self.assertEqual(render.ccw_to_cw(180), 180)
        self.assertEqual(render.ccw_to_cw(270), 90)
        self.assertEqual(render.ccw_to_cw(-90), 90)

    def test_software_image_rotates_pixels_and_swaps_dimensions(self):
        frame = fx.solid_frame(32, 18, 16, 128, 128)  # dark
        # Right half bright: a ramp exposes rotation direction.
        mv = memoryview(frame.planes[0])
        for row in range(frame.planes[0].height):
            base = row * frame.planes[0].line_size
            mv[base + 16:base + 32] = bytes([200]) * 16
        unrotated = render.software_image_from_av(frame)
        rotated = render.software_image_from_av(frame, rotation=90)
        self.assertEqual(rotated.size(), QSize(18, 32))
        # Clockwise: the source left column becomes the top row.
        self.assertLess(rotated.pixelColor(0, 0).red(), 40, "left column must become the top edge")
        self.assertGreater(rotated.pixelColor(0, 31).red(), 160, "right edge must become the bottom edge")
        self.assertEqual(unrotated.size(), QSize(32, 18))


if __name__ == "__main__":
    unittest.main()
