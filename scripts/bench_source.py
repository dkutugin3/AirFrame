"""Reproducible H.264-capable moving test source with machine-readable frame IDs.

Stage-0 measurement stand (plan §3). The same scene drives:
  * a real aiortc ``VideoStreamTrack`` (``BenchVideoTrack``) for live pipeline tests,
  * a deterministic renderer (``BenchScene.render``/``render_luma``) for disk artifacts,
  * an extractor (``decode_frame_id``) that recovers the frame number from a recorded
    frame, so an OBS recording can be checked against expected unique IDs.

Why this exists
---------------
The plan forbids treating a repeated still image as 60 FPS. Every rendered frame
therefore carries (a) a 20-bit machine-readable ID band with a parity check and
(b) a human-readable decimal counter, and the rest of the scene contains real
per-frame motion: a scrolling band, a fast mover with a shadow, a moving thin
line, a static gradient/shadow background, thin line grids, a colour scale and
fine text. All geometry is expressed as fractions of the frame size, so the
extractor keeps working on scaled OBS recordings.

Contract (also pinned by tests/test_bench_source.py):
  * ``encode_frame_bits(0..2**20-1)`` -> 6 preamble + 20 data + 4 parity bits.
  * ``decode_frame_id(render(id)) == id`` for yuv420p frames at any even geometry
    (central samples of fixed relative cells; per-frame black/white threshold from
    the preamble; parity mismatch or low contrast raise ``FrameIdDecodeError``).
  * PTS = round(id * 90000 / fps) on a 1/90000 time base, so PTS and ID agree.
  * ``BenchVideoTrack`` paces frames on an injectable clock; ``VirtualClock`` gives
    deterministic virtual time (stages 2-7 reuse both).

This module measures a *synthetic* pipeline. It does not prove physical display
presentation or OBS capture; see docs/MEASUREMENT.md.
"""
import asyncio
from fractions import Fraction
import math

import av
from aiortc.mediastreams import MediaStreamError, VideoStreamTrack

FRAME_ID_BITS = 20
FRAME_ID_MAX = (1 << FRAME_ID_BITS) - 1
PREAMBLE_BITS = (1, 0, 1, 0, 1, 0)
PARITY_BITS = 4
PARITY_GROUP = FRAME_ID_BITS // PARITY_BITS
CELL_COUNT = len(PREAMBLE_BITS) + FRAME_ID_BITS + PARITY_BITS
TIME_BASE = Fraction(1, 90000)

# Relative layout shared by the renderer and the extractor (fractions of W/H).
ID_BAND_HEIGHT = 0.055
ID_BAND_SAMPLE_TOP = 0.25
ID_BAND_SAMPLE_BOTTOM = 0.75
ID_CELL_SAMPLE_LEFT = 0.30
ID_CELL_SAMPLE_RIGHT = 0.70
SCROLL_BAND = (0.10, 0.22)
LINE_GRID = (0.24, 0.56, 0.0, 0.52)
COLOUR_BARS = (0.60, 0.90, 0.56, 0.96)
MOVER_BAND = (0.60, 0.88, 0.0, 0.52)
TEXT_BAND = (0.90, 0.995, 0.0, 0.62)

MIN_ID_CONTRAST = 40.0

# BT.601-ish YUV values for eight colour bars: red, green, blue, cyan, magenta,
# yellow, grey, dark grey.
COLOUR_BAR_VALUES = (
    (81, 90, 240), (145, 54, 34), (41, 240, 110), (170, 166, 16),
    (106, 202, 222), (200, 16, 146), (128, 128, 128), (16, 128, 128),
)

# 5x7 bitmap font for the counter text (no font library is available in the test
# image; digits/letters stay legible after H.264 and OBS scaling).
FONT = {
    '0': ('.###.', '#...#', '#..##', '#.#.#', '##..#', '#...#', '.###.'),
    '1': ('..#..', '.##..', '..#..', '..#..', '..#..', '..#..', '.###.'),
    '2': ('.###.', '#...#', '....#', '...#.', '..#..', '.#...', '#####'),
    '3': ('####.', '....#', '....#', '.###.', '....#', '....#', '####.'),
    '4': ('...#.', '..##.', '.#.#.', '#..#.', '#####', '...#.', '...#.'),
    '5': ('#####', '#....', '####.', '....#', '....#', '#...#', '.###.'),
    '6': ('.###.', '#....', '#....', '####.', '#...#', '#...#', '.###.'),
    '7': ('#####', '....#', '...#.', '..#..', '.#...', '.#...', '.#...'),
    '8': ('.###.', '#...#', '#...#', '.###.', '#...#', '#...#', '.###.'),
    '9': ('.###.', '#...#', '#...#', '.####', '....#', '....#', '.###.'),
    'A': ('.###.', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'),
    'D': ('###..', '#..#.', '#...#', '#...#', '#...#', '#..#.', '###..'),
    'E': ('#####', '#....', '#....', '####.', '#....', '#....', '#####'),
    'F': ('#####', '#....', '#....', '####.', '#....', '#....', '#....'),
    'I': ('#####', '..#..', '..#..', '..#..', '..#..', '..#..', '#####'),
    'M': ('#...#', '##.##', '#.#.#', '#.#.#', '#...#', '#...#', '#...#'),
    'P': ('####.', '#...#', '#...#', '####.', '#....', '#....', '#....'),
    'R': ('####.', '#...#', '#...#', '####.', '#.#..', '#..#.', '#...#'),
    'S': ('.####', '#....', '#....', '.###.', '....#', '....#', '####.'),
    'T': ('#####', '..#..', '..#..', '..#..', '..#..', '..#..', '..#..'),
    '=': ('.....', '.....', '#####', '.....', '#####', '.....', '.....'),
    ' ': ('.....', '.....', '.....', '.....', '.....', '.....', '.....'),
}


class FrameIdDecodeError(ValueError):
    """The frame ID band could not be decoded reliably."""


def _check_frame_id(frame_id):
    if type(frame_id) is not int or not 0 <= frame_id <= FRAME_ID_MAX:
        raise ValueError(f'frame id must be an int in [0, {FRAME_ID_MAX}]')
    return frame_id


def _parity_bits(frame_id):
    data_bits = tuple((frame_id >> (FRAME_ID_BITS - 1 - index)) & 1
                      for index in range(FRAME_ID_BITS))
    parity = []
    for group in range(PARITY_BITS):
        value = 0
        for bit in data_bits[group * PARITY_GROUP:(group + 1) * PARITY_GROUP]:
            value ^= bit
        parity.append(value)
    return tuple(parity)


def encode_frame_bits(frame_id):
    """Return the 30 band bits for ``frame_id``: preamble + data (MSB first) + parity."""
    _check_frame_id(frame_id)
    data_bits = tuple((frame_id >> (FRAME_ID_BITS - 1 - index)) & 1
                      for index in range(FRAME_ID_BITS))
    return tuple(PREAMBLE_BITS) + data_bits + _parity_bits(frame_id)


def expected_pts(frame_id, fps=60):
    """PTS on the 1/90000 video time base; ID and PTS therefore always agree."""
    _check_frame_id(frame_id)
    if type(fps) is not int or fps <= 0 or fps > 90000:
        raise ValueError('fps must be a positive int <= 90000')
    return int(round(Fraction(frame_id * 90000, fps)))


def _sample_cell(luma, width, height, stride, index):
    cell_width = width / CELL_COUNT
    left = int((index + ID_CELL_SAMPLE_LEFT) * cell_width)
    right = max(left + 1, int((index + ID_CELL_SAMPLE_RIGHT) * cell_width))
    top = int(ID_BAND_SAMPLE_TOP * ID_BAND_HEIGHT * height)
    bottom = max(top + 1, int(ID_BAND_SAMPLE_BOTTOM * ID_BAND_HEIGHT * height))
    step = max(1, int(math.sqrt(max(1, (right - left) * (bottom - top) / 400))))
    total = count = 0
    for row in range(top, bottom, step):
        offset = row * stride
        for column in range(left, right, step):
            total += luma[offset + column]
            count += 1
    if not count:  # pragma: no cover - geometry validation makes this unreachable
        raise FrameIdDecodeError('empty frame id sample')
    return total / count


def _decode_luma(luma, width, height, stride, verify):
    cells = [_sample_cell(luma, width, height, stride, index) for index in range(CELL_COUNT)]
    white = sum(cells[index] for index in (0, 2, 4)) / 3
    black = sum(cells[index] for index in (1, 3, 5)) / 3
    contrast = white - black
    if contrast < MIN_ID_CONTRAST:
        raise FrameIdDecodeError(f'frame id contrast too low ({contrast:.1f})')
    threshold = (white + black) / 2
    bits = tuple(1 if value > threshold else 0 for value in cells)
    data_bits = bits[len(PREAMBLE_BITS):len(PREAMBLE_BITS) + FRAME_ID_BITS]
    frame_id = 0
    for bit in data_bits:
        frame_id = (frame_id << 1) | bit
    if verify:
        parity = bits[len(PREAMBLE_BITS) + FRAME_ID_BITS:]
        if parity != _parity_bits(frame_id):
            raise FrameIdDecodeError(
                f'frame id parity mismatch (decoded {frame_id}, parity {parity})')
    return frame_id


def decode_luma_frame_id(luma, width, height, verify=True):
    """Decode the ID band from a raw yuv420p Y plane (stride == width)."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('width/height must be positive ints')
    if len(luma) < width * height:
        raise ValueError('luma plane is shorter than width*height')
    return _decode_luma(luma, width, height, width, verify)


def decode_frame_id(frame, verify=True):
    """Decode the ID band from an av.VideoFrame (handles decoder line stride)."""
    if not isinstance(frame, av.VideoFrame):
        raise TypeError('decode_frame_id expects an av.VideoFrame')
    if frame.format.name not in ('yuv420p', 'yuvj420p'):
        raise ValueError(f'unsupported frame format {frame.format.name}')
    plane = frame.planes[0]
    return _decode_luma(bytes(plane), frame.width, frame.height, plane.line_size, verify)


def _fill_rect(plane, width, top, bottom, left, right, value):
    value = max(0, min(255, int(value)))
    row = bytes([value]) * (right - left)
    for y in range(top, bottom):
        start = y * width + left
        plane[start:start + (right - left)] = row


def _draw_text(plane, width, height, text, left, top, scale, value=255):
    scale = max(1, int(scale))
    cursor = int(left)
    for character in text:
        glyph = FONT.get(character.upper(), FONT[' '])
        for row_index, row in enumerate(glyph):
            for column_index, pixel in enumerate(row):
                if pixel == '#':
                    x = cursor + column_index * scale
                    y = int(top) + row_index * scale
                    _fill_rect(plane, width, y, y + scale, x, x + scale, value)
        cursor += (len(glyph[0]) + 1) * scale


class VirtualClock:
    """Deterministic clock for track/pipeline tests: sleep() advances virtual time."""

    def __init__(self, start=0.0):
        self._now = float(start)

    def time(self):
        return self._now

    async def sleep(self, seconds):
        if seconds > 0:
            self._now += float(seconds)
        await asyncio.sleep(0)


class _LoopClock:
    def time(self):
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds):
        if seconds > 0:
            await asyncio.sleep(seconds)


class BenchScene:
    """Deterministic moving scene; geometry is relative so any even size works."""

    def __init__(self, width=1920, height=1080, fps=60):
        if type(width) is not int or type(height) is not int:
            raise ValueError('width/height must be ints')
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError('width/height must be positive even numbers (yuv420p)')
        if type(fps) is not int or fps <= 0 or fps > 90000:
            raise ValueError('fps must be a positive int <= 90000')
        self.width, self.height, self.fps = width, height, fps
        self.time_base = TIME_BASE
        self._text_scale = max(1, height // 180)
        self._fine_scale = max(1, height // 480)
        self._build_backgrounds()

    # -- geometry helpers -------------------------------------------------
    def _region(self, bounds):
        y0, y1, x0, x1 = bounds
        return (int(y0 * self.height), max(int(y0 * self.height) + 1, int(y1 * self.height)),
                int(x0 * self.width), max(int(x0 * self.width) + 1, int(x1 * self.width)))

    def _build_backgrounds(self):
        width, height = self.width, self.height
        y_plane = bytearray(width * height)
        for row in range(height):
            value = 32 + int(168 * row / max(1, height - 1))
            y_plane[row * width:(row + 1) * width] = bytes([value]) * width
        # Thin line grid: 1 px dark horizontal and vertical lines.
        top, bottom, left, right = self._region(LINE_GRID)
        for row in range(top, bottom, max(4, height // 90)):
            _fill_rect(y_plane, width, row, row + 1, left, right, 20)
        for column in range(left, right, max(5, width // 100)):
            _fill_rect(y_plane, width, top, bottom, column, column + 1, 20)
        # Colour scale: eight vertical bars with distinct luma + chroma.
        u_plane = bytearray([128]) * ((width // 2) * (height // 2))
        v_plane = bytearray([128]) * ((width // 2) * (height // 2))
        top, bottom, left, right = self._region(COLOUR_BARS)
        bar_count = len(COLOUR_BAR_VALUES)
        for index, (luma, u, v) in enumerate(COLOUR_BAR_VALUES):
            bar_left = left + (right - left) * index // bar_count
            bar_right = left + (right - left) * (index + 1) // bar_count
            if bar_right <= bar_left:  # pragma: no cover - degenerate small sizes
                continue
            for row in range(top, bottom):
                start = row * width
                y_plane[start + bar_left:start + bar_right] = bytes([luma]) * (bar_right - bar_left)
            for row in range(top // 2, max(top // 2 + 1, bottom // 2)):
                start = row * (width // 2)
                u_plane[start + bar_left // 2:start + bar_right // 2] = bytes([u]) * (bar_right // 2 - bar_left // 2)
                v_plane[start + bar_left // 2:start + bar_right // 2] = bytes([v]) * (bar_right // 2 - bar_left // 2)
        # Static striped pattern for the scrolling band.
        stripe = max(8, width // 40)
        self._scroll_strip = bytes(
            (230 if (column // stripe) % 2 else 40) for column in range(width))
        self._scroll_step = max(1, width // 160)
        self._y_background = bytes(y_plane)
        self._u_background = bytes(u_plane)
        self._v_background = bytes(v_plane)

    # -- per-frame rendering ---------------------------------------------
    def expected_pts(self, frame_id):
        return expected_pts(frame_id, self.fps)

    def _overlay_scrolling_band(self, y_plane, frame_id):
        width = self.width
        top, bottom = self._region(SCROLL_BAND + (0.0, 1.0))[:2]
        offset = (frame_id * self._scroll_step) % width
        row = self._scroll_strip[width - offset:] + self._scroll_strip[:width - offset]
        for y in range(top, bottom):
            start = y * width
            y_plane[start:start + width] = row

    def _overlay_mover(self, y_plane, frame_id):
        width, height = self.width, self.height
        top, bottom, left, right = self._region(MOVER_BAND)
        size = max(12, width // 14)
        span_x = max(1, right - left - size)
        span_y = max(1, bottom - top - size)
        step_x = max(2, width // 120)
        step_y = max(1, height // 240)
        position = (frame_id * step_x) % (2 * span_x)
        x = left + (position if position <= span_x else 2 * span_x - position)
        y = top + (frame_id * step_y) % span_y
        shadow = max(3, size // 3)
        _fill_rect(y_plane, width, y + shadow, y + shadow + size,
                   x + shadow, x + shadow + size, 24)
        _fill_rect(y_plane, width, y, y + size, x, x + size, 235)
        inset = max(2, size // 6)
        _fill_rect(y_plane, width, y + inset, y + size - inset,
                   x + inset, x + size - inset, 60)

    def _overlay_moving_line(self, y_plane, frame_id):
        width = self.width
        top, bottom, left, right = self._region(LINE_GRID)
        span = max(1, right - left)
        step = max(1, span // 60)
        x = left + (frame_id * step) % span
        _fill_rect(y_plane, width, top, bottom, x, x + 1, 16)

    def _overlay_id_band(self, y_plane, frame_id):
        width, height = self.width, self.height
        band_height = max(3, int(ID_BAND_HEIGHT * height))
        for y in range(band_height):
            start = y * width
            y_plane[start:start + width] = bytes(width)
        bits = encode_frame_bits(frame_id)
        cell_width = width / CELL_COUNT
        for index, bit in enumerate(bits):
            left = int(index * cell_width) + 2
            right = max(left + 1, int((index + 1) * cell_width) - 2)
            _fill_rect(y_plane, width, 2, max(3, band_height - 2), left, right,
                       255 if bit else 0)

    def _overlay_counter_text(self, y_plane, frame_id):
        width, height = self.width, self.height
        milliseconds = int(round(Fraction(frame_id * 1000, self.fps)))
        _draw_text(y_plane, width, height, f'FRAME {frame_id:06d}',
                   .02 * width, .905 * height, self._text_scale)
        _draw_text(y_plane, width, height, f'PTS {milliseconds:06d} MS',
                   .02 * width, .905 * height + 9 * self._text_scale, self._text_scale)
        _draw_text(y_plane, width, height, f'ID={frame_id:06d} PTS={milliseconds:06d}',
                   .52 * width, .905 * height, self._fine_scale)

    def _build(self, frame_id):
        _check_frame_id(frame_id)
        y_plane = bytearray(self._y_background)
        self._overlay_scrolling_band(y_plane, frame_id)
        self._overlay_mover(y_plane, frame_id)
        self._overlay_moving_line(y_plane, frame_id)
        self._overlay_id_band(y_plane, frame_id)
        self._overlay_counter_text(y_plane, frame_id)
        return bytes(y_plane), self._u_background, self._v_background

    def render_luma(self, frame_id):
        """Return the rendered yuv420p Y plane as bytes."""
        return self._build(frame_id)[0]

    def render(self, frame_id):
        """Return a fully populated av.VideoFrame with ID-derived PTS."""
        luma, u_plane, v_plane = self._build(frame_id)
        frame = av.VideoFrame(self.width, self.height, 'yuv420p')
        frame.planes[0].update(luma)
        frame.planes[1].update(u_plane)
        frame.planes[2].update(v_plane)
        frame.pts = self.expected_pts(frame_id)
        frame.time_base = self.time_base
        return frame


class BenchVideoTrack(VideoStreamTrack):
    """aiortc source for stages 2-7; pacing clock is injectable (VirtualClock)."""

    def __init__(self, scene=None, clock=None, start_id=0):
        super().__init__()
        _check_frame_id(start_id)
        self.scene = scene if scene is not None else BenchScene()
        self.clock = clock
        self.frame_id = start_id
        self._next_time = None

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        clock = self.clock if self.clock is not None else _LoopClock()
        if self._next_time is None:
            self._next_time = clock.time()
        wait = self._next_time - clock.time()
        if wait > 0:
            await clock.sleep(wait)
        frame = self.scene.render(self.frame_id)
        self.frame_id += 1
        period = 1 / self.scene.fps
        self._next_time = max(self._next_time + period, clock.time() + period / 2)
        return frame
