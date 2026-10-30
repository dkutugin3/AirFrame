"""Fixtures shared by the native YUV renderer contracts (tests_native/).

Independent of production code on purpose: the colour reference tables are
computed here from the ITU-R BT.601 / BT.709 equations and rounded half away
from zero before the renderer exists, so the tolerance is fixed in advance.

Run with (Docker's tests/ image has no PySide6):

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v
"""
import math
import time

import av
from PySide6.QtWidgets import QApplication

MATRIX_BT601 = "bt601"
MATRIX_BT709 = "bt709"
RANGE_LIMITED = "limited"
RANGE_FULL = "full"

# Tolerances fixed BEFORE the renderer was implemented, from measured rounding
# of swscale/Qt fixed-point conversions against the exact ITU equations:
#   * CPU (software) conversion: observed max |delta| <= 2 code values.
#   * Real-window GPU screen capture, supported combos: observed max |delta| <= 2.
SOFTWARE_TOLERANCE = 3
GPU_CAPTURE_TOLERANCE = 2

# A matrix/range pair counts as GPU-verified only if the pinned Qt 6.12.0 RHI
# conversion matched the ITU reference table within GPU_CAPTURE_TOLERANCE in a
# real-window screen capture. BT.601+full measured up to 26 codes off (Qt RHI
# limitation), so it must be routed to the software conversion instead.
GPU_SUPPORTED_COMBOS = frozenset(
    {(MATRIX_BT601, RANGE_LIMITED), (MATRIX_BT709, RANGE_LIMITED), (MATRIX_BT709, RANGE_FULL)}
)

_app = None


def qapp():
    """One QApplication per test process."""
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
        _app.setApplicationName("AirFrame renderer contracts")
    return _app


def pump(seconds=0.05):
    """Process Qt events for a bounded period (no timing assertions here)."""
    app = qapp()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)


def clamp_round(value):
    return max(0, min(255, int(math.floor(value + 0.5))))


def _coefficients(matrix, full_range):
    """BT.601/BT.709 luma-derived coefficients, limited (video) or full range."""
    if matrix == MATRIX_BT709:
        kr, kb = 0.2126, 0.0722
    else:
        kr, kb = 0.299, 0.114
    kg = 1.0 - kr - kb
    # Digital limited range: Y multiplied by 255/219 and offset by 16; chroma is
    # scaled by 255/224 (half-range reduction) into the same 0..255 codes.
    y_scale = 1.0 if full_range else 255.0 / 219.0
    chroma_scale = 1.0 if full_range else 255.0 / 224.0
    return {
        "scale": y_scale,
        "y_offset": 0.0 if full_range else 16.0,
        "r_v": 2.0 * (1.0 - kr) * chroma_scale,
        "g_u": -2.0 * kb * (1.0 - kb) / kg * chroma_scale,
        "g_v": -2.0 * kr * (1.0 - kr) / kg * chroma_scale,
        "b_u": 2.0 * (1.0 - kb) * chroma_scale,
    }


def reference_rgb(y, u, v, matrix, video_range):
    """Exact ITU reference conversion for one YUV(420) sample triplet."""
    c = _coefficients(matrix, full_range=not video_range)
    yp = (y - c["y_offset"]) * c["scale"]
    up = u - 128.0
    vp = v - 128.0
    r = yp + c["r_v"] * vp
    g = yp + c["g_u"] * up + c["g_v"] * vp
    b = yp + c["b_u"] * up
    return (clamp_round(r), clamp_round(g), clamp_round(b))


def rgb_close(actual, expected, tolerance):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def _fill_plane(plane, value):
    mv = memoryview(plane)
    row = bytes([value]) * plane.line_size
    for y in range(plane.height):
        mv[y * plane.line_size:(y + 1) * plane.line_size] = row


def solid_frame(width, height, y, u, v, pixel_format="yuv420p", colorspace=None, video_range=None):
    """Frame where every luma sample is Y and every chroma sample is U/V."""
    from av.video.reformatter import ColorRange, Colorspace

    frame = av.VideoFrame(width, height, pixel_format)
    if pixel_format == "nv12":
        _fill_plane(frame.planes[0], y)
        uv = bytes([u, v]) * ((frame.planes[1].line_size + 1) // 2)
        mv = memoryview(frame.planes[1])
        for row in range(frame.planes[1].height):
            mv[row * frame.planes[1].line_size:(row + 1) * frame.planes[1].line_size] = uv[:frame.planes[1].line_size]
    else:
        _fill_plane(frame.planes[0], y)
        _fill_plane(frame.planes[1], u)
        _fill_plane(frame.planes[2], v)
    if colorspace is not None:
        frame.colorspace = colorspace
    if video_range is not None:
        frame.color_range = video_range
    return frame


def wramp_frame(width, height, pixel_format="yuv420p"):
    """Frame with unique, position-dependent samples to expose stride/plane bugs.

    Every sample value is a pure function of the source sample coordinates, so
    a test can recompute the expected RGB even after the frame was copied.
    """
    frame = av.VideoFrame(width, height, pixel_format)
    chroma_w = (width + 1) // 2
    chroma_h = (height + 1) // 2
    mv = memoryview(frame.planes[0])
    for row in range(frame.planes[0].height):
        line = bytearray(frame.planes[0].line_size)
        for x in range(frame.planes[0].width):
            line[x] = 20 + ((x * 7 + row * 3) % 200)
        mv[row * frame.planes[0].line_size:(row + 1) * frame.planes[0].line_size] = bytes(line)
    if pixel_format == "nv12":
        mv = memoryview(frame.planes[1])
        for row in range(frame.planes[1].height):
            line = bytearray(frame.planes[1].line_size)
            for cx in range((frame.planes[1].line_size // 2)):
                line[2 * cx] = 30 + ((cx * 5 + row * 11) % 180)
                line[2 * cx + 1] = 40 + ((cx * 13 + row * 7) % 180)
            mv[row * frame.planes[1].line_size:(row + 1) * frame.planes[1].line_size] = bytes(line)
    else:
        mv = memoryview(frame.planes[1])
        for row in range(frame.planes[1].height):
            line = bytearray(frame.planes[1].line_size)
            for cx in range(frame.planes[1].width):
                line[cx] = 30 + ((cx * 5 + row * 11) % 180)
            mv[row * frame.planes[1].line_size:(row + 1) * frame.planes[1].line_size] = bytes(line)
        mv = memoryview(frame.planes[2])
        for row in range(frame.planes[2].height):
            line = bytearray(frame.planes[2].line_size)
            for cx in range(frame.planes[2].width):
                line[cx] = 40 + ((cx * 13 + row * 7) % 180)
            mv[row * frame.planes[2].line_size:(row + 1) * frame.planes[2].line_size] = bytes(line)
    return frame


def expected_rgb_from_frame(frame, x, y, matrix=MATRIX_BT601, video_range=True):
    """Expected RGB of the *source* frame at (x, y) under the old swscale default.

    Deliberately READ-ONLY on the source frame so it also proves the source
    buffer is still intact after a mailbox replacement.
    """
    mv0 = memoryview(frame.planes[0])
    y_sample = mv0[y * frame.planes[0].line_size + x]
    if frame.format.name == "nv12":
        row = frame.planes[1]
        mv1 = memoryview(row)
        offset = (y // 2) * row.line_size + (x // 2) * 2
        u_sample = mv1[offset]
        v_sample = mv1[offset + 1]
    else:
        chroma_row = y // 2
        mv1 = memoryview(frame.planes[1])
        mv2 = memoryview(frame.planes[2])
        u_sample = mv1[chroma_row * frame.planes[1].line_size + (x // 2)]
        v_sample = mv2[chroma_row * frame.planes[2].line_size + (x // 2)]
    return reference_rgb(y_sample, u_sample, v_sample, matrix, video_range)


def source_plane_bytes(frame, index):
    """Bytes per source plane row that carry data (stride padding excluded)."""
    plane = frame.planes[index]
    if frame.format.name == "nv12" and index == 1:
        return 2 * plane.width  # U/V interleaved: two bytes per chroma sample
    return plane.width


def qframe_plane_rows(qframe, index, row_count):
    """Copy Qt YUV frame plane rows through the public map(ReadOnly) API."""
    if not qframe.map(qframe.MapMode.ReadOnly):
        raise AssertionError(f"QVideoFrame plane {index} is not readable")
    try:
        stride = qframe.bytesPerLine(index)
        rows = qframe.mappedBytes(index) // max(1, stride)
        mv = memoryview(qframe.bits(index))
        return [bytes(mv[row * stride:(row + 1) * stride]) for row in range(min(row_count, rows))]
    finally:
        qframe.unmap()


def plane_rows(frame, index):
    """Copy of a plane's bytes (minus stride padding) for intactness checks."""
    plane = frame.planes[index]
    mv = memoryview(plane)
    data_bytes = source_plane_bytes(frame, index)
    return [
        bytes(mv[row * plane.line_size:row * plane.line_size + data_bytes])
        for row in range(plane.height)
    ]
