"""Headless contracts for the reproducible bench source in scripts/bench_source.py.

The stage-0 stand must be trustworthy before it is used to grade later stages, so
these tests pin: the machine-readable frame ID roundtrip (including through real
H.264), real frame-to-frame motion (not repeated stills), PTS mapping, the scene
features the plan requires (fine text, thin lines, colour scale, gradients,
scrolling, fast motion), and reusability as an aiortc VideoStreamTrack source.

Everything here is deterministic and Docker-safe: no PySide6, no OS-timer timing
asserts. Timing comes from an injected virtual clock.
"""
import asyncio
from fractions import Fraction
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiortc.codecs import depayload
from aiortc.codecs.h264 import H264Decoder, H264Encoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from scripts.bench_source import (
    COLOUR_BARS,
    FRAME_ID_BITS,
    FRAME_ID_MAX,
    ID_BAND_HEIGHT,
    LINE_GRID,
    MOVER_BAND,
    PREAMBLE_BITS,
    SCROLL_BAND,
    TEXT_BAND,
    TIME_BASE,
    BenchScene,
    BenchVideoTrack,
    FrameIdDecodeError,
    VirtualClock,
    decode_frame_id,
    decode_luma_frame_id,
    encode_frame_bits,
    expected_pts,
)


def crop(luma, width, height, bounds):
    """Crop the Y plane for relative bounds (y0, y1, x0, x1)."""
    y0, y1, x0, x1 = bounds
    top, bottom = int(y0 * height), max(int(y0 * height) + 1, int(y1 * height))
    left, right = int(x0 * width), max(int(x0 * width) + 1, int(x1 * width))
    return b''.join(luma[row * width + left:row * width + right] for row in range(top, bottom))


def changed(left, right):
    assert len(left) == len(right)
    return sum(1 for a, b in zip(left, right) if a != b)


def h264_roundtrip(frames):
    """Encode/decode frames through the same aiortc H.264 path the receiver uses."""
    encoder, decoder = H264Encoder(), H264Decoder()
    codec = RTCRtpCodecParameters(mimeType='video/H264', clockRate=90000, payloadType=96)
    decoded = []
    for frame in frames:
        payloads, timestamp = encoder.encode(frame)
        data = b''.join(depayload(codec, payload) for payload in payloads)
        decoded.extend(decoder.decode(JitterFrame(data=data, timestamp=timestamp)))
    return decoded


class FrameIdCodingTests(unittest.TestCase):
    def test_frame_id_roundtrips_for_a_range_of_ids(self):
        scene = BenchScene(640, 360)
        for frame_id in (0, 1, 2, 29, 30, 59, 101, 255, 256, 1023, 65535, FRAME_ID_MAX):
            with self.subTest(frame_id=frame_id):
                frame = scene.render(frame_id)
                self.assertEqual(decode_frame_id(frame), frame_id)
                self.assertEqual(
                    decode_luma_frame_id(scene.render_luma(frame_id), 640, 360), frame_id)

    def test_frame_id_bits_layout_is_machine_readable(self):
        bits = encode_frame_bits(0)
        self.assertEqual(len(bits), len(PREAMBLE_BITS) + FRAME_ID_BITS + 4)
        self.assertEqual(bits[:len(PREAMBLE_BITS)], PREAMBLE_BITS)
        self.assertEqual(encode_frame_bits(1)[len(PREAMBLE_BITS):len(PREAMBLE_BITS) + FRAME_ID_BITS],
                         (0,) * (FRAME_ID_BITS - 1) + (1,))

    def test_frame_id_survives_a_rescaled_frame(self):
        """OBS recordings can be scaled; relative sampling must still decode the ID."""
        scene = BenchScene(640, 360)
        frame = scene.render(7)
        scaled = frame.reformat(width=480, height=270, format='yuv420p')
        self.assertEqual(decode_frame_id(scaled), 7)
        smaller = frame.reformat(width=320, height=180, format='yuv420p')
        self.assertEqual(decode_frame_id(smaller), 7)

    def test_invalid_ids_and_geometry_are_rejected(self):
        with self.assertRaises(ValueError):
            encode_frame_bits(-1)
        with self.assertRaises(ValueError):
            encode_frame_bits(FRAME_ID_MAX + 1)
        scene = BenchScene(640, 360)
        with self.assertRaises(ValueError):
            scene.render(-1)
        with self.assertRaises(ValueError):
            BenchScene(639, 360)
        with self.assertRaises(ValueError):
            BenchScene(640, 359)

    def test_corrupted_or_blank_id_band_is_detected(self):
        scene = BenchScene(640, 360)
        luma = bytearray(scene.render_luma(5))
        # id 5 ends in data bit 1: paint that cell black so the ID reads 4 and the
        # stored parity no longer matches. A corruption of a whole cell must be caught.
        cell = len(PREAMBLE_BITS) + FRAME_ID_BITS - 1
        cell_width = 640 / 30
        left, right = int((cell + .30) * cell_width), int((cell + .70) * cell_width)
        top, bottom = int(.25 * ID_BAND_HEIGHT * 360), int(.75 * ID_BAND_HEIGHT * 360)
        for row in range(top, bottom):
            for column in range(left, right):
                luma[row * 640 + column] = 0
        with self.assertRaises(FrameIdDecodeError):
            decode_luma_frame_id(bytes(luma), 640, 360)
        blank = bytes([128]) * (640 * 360)
        with self.assertRaises(FrameIdDecodeError):
            decode_luma_frame_id(blank, 640, 360)

    def test_rendering_is_reproducible(self):
        first = BenchScene(640, 360)
        second = BenchScene(640, 360)
        self.assertEqual(first.render_luma(42), second.render_luma(42))
        self.assertEqual(first.render_luma(42), first.render_luma(42))
        self.assertNotEqual(first.render_luma(42), first.render_luma(43))


class PtsTests(unittest.TestCase):
    def test_pts_mapping_is_deterministic_and_increasing(self):
        scene = BenchScene(640, 360, fps=60)
        self.assertEqual(expected_pts(0, 60), 0)
        self.assertEqual(expected_pts(1, 60), 1500)
        self.assertEqual(expected_pts(60, 60), 90000)
        self.assertEqual(expected_pts(1, 30), 3000)
        previous = -1
        for frame_id in range(20):
            frame = scene.render(frame_id)
            self.assertEqual(frame.pts, expected_pts(frame_id, 60))
            self.assertEqual(frame.time_base, TIME_BASE)
            self.assertGreater(frame.pts, previous)
            previous = frame.pts


class MotionTests(unittest.TestCase):
    def test_consecutive_frames_are_never_repeated_stills(self):
        scene = BenchScene(640, 360)
        frames = [scene.render_luma(frame_id) for frame_id in range(8)]
        size = len(frames[0])
        for left, right in zip(frames, frames[1:]):
            self.assertNotEqual(left, right)
            fraction = changed(left, right) / size
            self.assertGreater(fraction, .01, 'consecutive frames must show real motion')

    def test_scrolling_band_and_fast_mover_move_every_frame(self):
        scene = BenchScene(640, 360)
        for bounds, minimum in ((SCROLL_BAND + (0, 1), .10), (MOVER_BAND, .005)):
            previous = crop(scene.render_luma(10), 640, 360, bounds)
            current = crop(scene.render_luma(11), 640, 360, bounds)
            fraction = changed(previous, current) / len(previous)
            self.assertGreater(fraction, minimum, str(bounds))

    def test_scene_contains_colour_scale_thin_lines_gradient_and_text(self):
        scene = BenchScene(640, 360)
        frame = scene.render(123)
        luma = scene.render_luma(123)
        # Dark 1 px lines inside the line grid.
        rows = crop(luma, 640, 360, LINE_GRID)
        region_width = max(1, int((LINE_GRID[3] - LINE_GRID[2]) * 640))
        means = [sum(rows[i:i + region_width]) / region_width
                 for i in range(0, len(rows) - region_width + 1, region_width)]
        self.assertGreater(max(means) - min(means), 40, 'thin dark lines must be rendered')
        self.assertGreater(sum(1 for mean in means if mean < sum(means) / len(means) - 30), 2)
        # Smooth vertical gradient in a strip that carries no other element.
        strip = crop(luma, 640, 360, (.24, .56, .53, .55))
        strip_width = max(1, int((.55 - .53) * 640))
        strip_means = [sum(strip[i:i + strip_width]) / strip_width
                       for i in range(0, len(strip) - strip_width + 1, strip_width)]
        self.assertGreater(max(strip_means) - min(strip_means), 40)
        self.assertLessEqual(max(abs(b - a) for a, b in zip(strip_means, strip_means[1:])), 8)
        # Colour scale: at least four distinct chroma pairs in the bar area.
        u = bytes(frame.planes[1])
        v = bytes(frame.planes[2])
        uw, uh = 320, 180
        pairs = set()
        top, bottom = int(COLOUR_BARS[0] * uh), int(COLOUR_BARS[1] * uh)
        left, right = int(COLOUR_BARS[2] * uw), int(COLOUR_BARS[3] * uw)
        for row in range(top, bottom, 5):
            for column in range(left, right, 5):
                pairs.add((u[row * uw + column], v[row * uw + column]))
        self.assertGreaterEqual(len(pairs), 4, 'colour scale must render distinct chroma')
        # Fine text: the decimal counter changes when the ID changes.
        text_123 = crop(luma, 640, 360, TEXT_BAND)
        text_124 = crop(scene.render_luma(124), 640, 360, TEXT_BAND)
        self.assertGreater(changed(text_123, text_124), 20, 'fine decimal counter must change')


class H264Tests(unittest.TestCase):
    def test_h264_roundtrip_preserves_frame_ids(self):
        scene = BenchScene(640, 360)
        frames = [scene.render(frame_id) for frame_id in range(8)]
        decoded = h264_roundtrip(frames)
        self.assertGreaterEqual(len(decoded), 6, 'H.264 pipeline must return frames')
        ids = [decode_frame_id(frame) for frame in decoded]
        self.assertEqual(ids, list(range(len(decoded))))


class TrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_track_produces_sequential_ids_and_pts_on_a_virtual_clock(self):
        scene = BenchScene(640, 360, fps=60)
        clock = VirtualClock()
        track = BenchVideoTrack(scene, clock=clock)
        try:
            frames = [await asyncio.wait_for(track.recv(), 5) for _ in range(5)]
            self.assertEqual([decode_frame_id(frame) for frame in frames], list(range(5)))
            self.assertEqual([frame.pts for frame in frames], [expected_pts(i, 60) for i in range(5)])
            self.assertAlmostEqual(clock.time(), 4 / 60, places=6)
        finally:
            track.stop()

    async def test_track_feeds_a_real_aiortc_h264_pipeline(self):
        """The stand is only reusable if it drives aiortc's own encode/decode path."""
        from aiortc import (
            RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription,
        )
        scene = BenchScene(640, 360, fps=60)
        track = BenchVideoTrack(scene, clock=VirtualClock())
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        received, done = [], asyncio.Event()

        @receiver.on('track')
        def on_track(remote):
            async def read():
                while True:
                    received.append(await remote.recv())
                    if len(received) >= 4:
                        done.set()
                        return
            asyncio.ensure_future(read())

        try:
            sender.addTrack(track)
            capabilities = [c for c in RTCRtpSender.getCapabilities('video').codecs
                            if c.mimeType == 'video/H264']
            self.assertTrue(capabilities)
            sender.getTransceivers()[0].setCodecPreferences(capabilities)
            await sender.setLocalDescription(await sender.createOffer())
            await receiver.setRemoteDescription(sender.localDescription)
            await receiver.setLocalDescription(await receiver.createAnswer())
            await sender.setRemoteDescription(RTCSessionDescription(
                type='answer', sdp=receiver.localDescription.sdp))
            await asyncio.wait_for(done.wait(), 15)
            ids = [decode_frame_id(frame) for frame in received]
            self.assertEqual(ids, list(range(len(ids))))
            self.assertEqual((received[0].width, received[0].height), (640, 360))
        finally:
            track.stop()
            await sender.close()
            await receiver.close()


if __name__ == '__main__':
    unittest.main()
