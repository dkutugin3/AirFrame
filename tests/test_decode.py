"""Stage-4 decoder integration contract for aiortc 1.15.0.

Deterministic tests for the narrow decoder layer: bounded encoded queue
(count/bytes/age from access-unit enqueue), bounded decoded handoff (not a
latest-only mailbox), controlled IDR recovery on overload, stale-epoch
filtering, close safety, and a real H.264 decode burst.
"""
import asyncio
from dataclasses import dataclass, field
from fractions import Fraction
import threading
import time
import unittest

import av
from aiortc.codecs import depayload, get_decoder
from aiortc.codecs.h264 import H264Encoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import VIDEO_TIME_BASE
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from airframe.decode import (
    DECODED_AGE, PRE_DECODER_AGE, BoundedDecodedQueue, BoundedFrameQueue,
    DecodePipeline, decoded_limits, pre_decoder_limits)
from airframe.playout import Playout

H264_CODEC = RTCRtpCodecParameters(mimeType='video/H264', clockRate=90000, payloadType=96)
LUMA_BASE, LUMA_STEP = 20, 7


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


@dataclass
class Picture:
    pts: int
    width: int = 64
    height: int = 36
    time_base: Fraction = Fraction(1, 90000)
    planes: tuple = ()
    id: int = field(default=0)


class FakeCodec:
    name = 'H264'
    mimeType = 'video/H264'


def encoded(timestamp, data=b'x' * 512):
    return JitterFrame(data=data, timestamp=timestamp)


def keyframe(timestamp):
    return JitterFrame(data=b'\x00\x00\x00\x01\x67\x00\x00\x00\x01\x68\x00\x00\x00\x01\x65p',
                       timestamp=timestamp)


class InstantDecoder:
    """Decoder that returns one Picture per input without touching FFmpeg."""
    def __init__(self, delay=0.0):
        self.delay = delay

    def decode(self, frame):
        if self.delay:
            time.sleep(self.delay)
        return [Picture(frame.timestamp)]


class GateDecoder:
    """Decoder that blocks until released, so the queue can be overloaded."""
    def __init__(self, gate):
        self.gate = gate
        self.started = threading.Event()

    def decode(self, frame):
        self.started.set()
        self.gate.wait(5)
        return [Picture(frame.timestamp)]


async def wait_until(predicate, timeout=5.0, message='condition not met'):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(.005)


def h264_frame(index, width=160, height=90):
    frame = av.VideoFrame(width, height, 'yuv420p')
    luma = min(235, LUMA_BASE + index * LUMA_STEP)
    frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
    for plane in frame.planes[1:]:
        plane.update(bytes([128]) * plane.buffer_size)
    frame.pts = index * 1500
    frame.time_base = VIDEO_TIME_BASE
    return frame


def h264_stream(count, keyframes=(0,), width=160, height=90):
    encoder = H264Encoder()
    stream = []
    for index in range(count):
        payloads, timestamp = encoder.encode(h264_frame(index, width, height),
                                             force_keyframe=index in keyframes)
        data = b''.join(depayload(H264_CODEC, payload) for payload in payloads)
        stream.append(JitterFrame(data=data, timestamp=timestamp))
    return stream


def luma_id(frame):
    plane = bytes(frame.planes[0])
    mean = sum(plane) / len(plane)
    return max(0, round((mean - LUMA_BASE) / LUMA_STEP))


class BoundedQueueTests(unittest.IsolatedAsyncioTestCase):
    def test_encoded_queue_bounds_count_bytes_age_and_closes_cleanly(self):
        clock = FakeClock()
        queue = BoundedFrameQueue(max_frames=3, max_bytes=2500, clock=clock)
        self.assertTrue(queue.put((FakeCodec(), encoded(0, b'x' * 1000))))
        clock.advance(.05)
        self.assertTrue(queue.put((FakeCodec(), encoded(1, b'x' * 1000))))
        self.assertFalse(queue.put((FakeCodec(), encoded(2, b'x' * 1000))))   # bytes
        stats = queue.stats()
        self.assertEqual(stats['frames'], 2)
        self.assertEqual(stats['bytes'], 2000)
        self.assertAlmostEqual(stats['ageMs'], 50, delta=.5)
        self.assertEqual(queue.overflows, 1)
        queue.put(None)                              # completion signal never dropped
        self.assertIsNone(queue.get())               # pending work is drained on close
        self.assertTrue(queue.closed)

    async def test_decoded_handoff_keeps_whole_supported_burst_not_latest_only(self):
        clock = FakeClock()
        queue = BoundedDecodedQueue(max_frames=decoded_limits(60), max_bytes=1 << 26, clock=clock)
        burst = [Picture(pts=index, id=index) for index in range(14)]
        for index, picture in enumerate(burst):
            clock.advance(1 / 60)
            self.assertTrue(queue.put_nowait(picture))
        self.assertEqual(queue.stats()['frames'], 14)
        received = [await queue.get() for _ in range(14)]
        self.assertEqual([picture.id for picture in received], list(range(14)))
        self.assertEqual(queue.overflows, 0)


class DecodePipelineTests(unittest.IsolatedAsyncioTestCase):
    def make_pipeline(self, clock=None, decoder_factory=None, fps=60):
        clock = clock or FakeClock()
        pipeline = DecodePipeline(asyncio.get_running_loop(), clock=clock, fps=fps,
                                  decoder_factory=decoder_factory or (lambda codec: InstantDecoder()))
        pipeline.start()
        return pipeline, clock

    async def test_short_decode_delay_preserves_the_chain(self):
        gate = threading.Event()
        gate.set()
        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: GateDecoder(gate))
        try:
            for index in range(10):
                clock.advance(.001)
                self.assertTrue(pipeline.offer(FakeCodec(), encoded(index * 1500)))
            clock.advance(PRE_DECODER_AGE / 2)
            self.assertFalse(pipeline.poll())
            await wait_until(lambda: pipeline.decoded.frames == 10)
            pictures = [await pipeline.decoded.get() for _ in range(10)]
            self.assertEqual([picture.pts for picture in pictures],
                             [index * 1500 for index in range(10)])
            self.assertEqual(pipeline.decode_recoveries, 0)
            self.assertEqual(pipeline.awaiting_keyframe, False)
        finally:
            pipeline.close()

    async def test_slow_decoder_keeps_queues_bounded_and_requests_idr(self):
        gate = threading.Event()
        decoder = GateDecoder(gate)
        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: decoder)
        max_frames = pre_decoder_limits(60)
        try:
            self.assertTrue(pipeline.offer(FakeCodec(), encoded(0)))
            await asyncio.wait_for(asyncio.to_thread(decoder.started.wait, 3), 4)
            requested = False
            for index in range(1, 40):
                clock.advance(1 / 60)
                pipeline.offer(FakeCodec(), encoded(index * 1500, b'y' * 2048))
                if pipeline.poll():
                    requested = True
                    break
                self.assertLessEqual(pipeline.encoded.frames, max_frames)
                self.assertLessEqual(pipeline.encoded.bytes, pipeline.encoded.max_bytes)
            self.assertTrue(requested, pipeline.stats())
            self.assertEqual(pipeline.decode_recoveries, 1)
            self.assertEqual(pipeline.epoch, 1)
            self.assertTrue(pipeline.awaiting_keyframe)
            self.assertEqual(pipeline.encoded.frames, 0)      # damaged epoch flushed
            self.assertEqual(pipeline.recovery_reasons[-1], 'encoded queue overload')
            # The in-flight decode of the abandoned epoch must be discarded.
            gate.set()
            await wait_until(lambda: pipeline.stale_decode_drops >= 1)
            # A fresh IDR re-opens decoding.
            self.assertTrue(pipeline.offer(FakeCodec(), keyframe(90000)))
            await wait_until(lambda: not pipeline.awaiting_keyframe)
            await wait_until(lambda: pipeline.decoded.frames >= 1)
            self.assertGreaterEqual(pipeline.decoded.frames, 1)
        finally:
            gate.set()
            pipeline.close()

    async def test_queue_age_comes_from_enqueue_time_not_source_pts(self):
        gate = threading.Event()
        decoder = GateDecoder(gate)
        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: decoder)
        try:
            # Very old source timestamps must not matter: only waiting time does.
            self.assertTrue(pipeline.offer(FakeCodec(), encoded(1, b'x' * 128)))
            await asyncio.wait_for(asyncio.to_thread(decoder.started.wait, 3), 4)
            for index in range(2):
                self.assertTrue(pipeline.offer(FakeCodec(), encoded(index, b'y' * 128)))
            clock.advance(PRE_DECODER_AGE - .010)
            self.assertFalse(pipeline.poll())
            clock.advance(.020)
            self.assertTrue(pipeline.poll())
            self.assertEqual(pipeline.recovery_reasons[-1], 'encoded queue overload')
        finally:
            gate.set()
            pipeline.close()

    async def test_decoded_handoff_overflow_resets_epoch_and_requests_idr(self):
        pipeline, clock = self.make_pipeline()
        try:
            limit = decoded_limits(60)
            for index in range(limit + 8):
                clock.advance(1 / 60)
                self.assertTrue(pipeline.offer(FakeCodec(), encoded(index * 1500, b'z' * 512)))
                await asyncio.sleep(.002)      # let the fast decoder drain the input queue
            await wait_until(lambda: pipeline.decoded.overflows >= 1)
            self.assertTrue(pipeline.poll())
            self.assertEqual(pipeline.decode_recoveries, 1)
            self.assertTrue(pipeline.awaiting_keyframe)
            self.assertEqual(pipeline.decoded.frames, 0)      # stale handoff cleared
        finally:
            pipeline.close()

    async def test_close_is_safe_with_full_queues_and_never_loses_the_signal(self):
        gate = threading.Event()
        decoder = GateDecoder(gate)
        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: decoder)
        self.assertTrue(pipeline.offer(FakeCodec(), encoded(0)))
        await asyncio.wait_for(asyncio.to_thread(decoder.started.wait, 3), 4)
        for index in range(1, 40):
            clock.advance(1 / 60)
            pipeline.offer(FakeCodec(), encoded(index * 1500, b'w' * 2048))
        encoded_before = pipeline.encoded.frames
        self.assertGreater(encoded_before, 0)
        started = time.monotonic()
        pipeline.close()
        pipeline.close()                                   # repeated close is safe
        self.assertLess(time.monotonic() - started, 1.0)   # close itself never hangs
        self.assertTrue(pipeline.closed)
        self.assertGreaterEqual(pipeline.encoded.dropped, encoded_before)
        gate.set()
        await asyncio.wait_for(asyncio.to_thread(pipeline.join, 3), 4)
        self.assertFalse(pipeline._thread.is_alive())

    async def test_old_worker_never_publishes_into_a_new_pipeline(self):
        gate = threading.Event()
        decoder = GateDecoder(gate)
        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: decoder)
        self.assertTrue(pipeline.offer(FakeCodec(), encoded(0)))
        await asyncio.wait_for(asyncio.to_thread(decoder.started.wait, 3), 4)
        pipeline.close()                                   # decode still in flight
        replacement, _ = self.make_pipeline()
        try:
            gate.set()
            await asyncio.wait_for(asyncio.to_thread(pipeline.join, 3), 4)
            await wait_until(lambda: pipeline.stale_decode_drops >= 1)
            self.assertEqual(replacement.decoded.frames, 0)
        finally:
            gate.set()
            replacement.close()

    async def test_real_h264_burst_of_14_frames_drains_in_one_turn_and_playout_keeps_all(self):
        pipeline = DecodePipeline(asyncio.get_running_loop(), fps=60,
                                  decoder_factory=get_decoder)
        pipeline.start()
        try:
            stream = h264_stream(14)
            for frame in stream:
                self.assertTrue(pipeline.offer(H264_CODEC, frame))
            await wait_until(lambda: pipeline.decoded.frames == 14)
            # The decoder emitted a 225 ms / 60 FPS burst; the consumer takes it in
            # one event-loop turn (queue already filled, no await suspends it).
            burst = [await pipeline.decoded.get() for _ in range(14)]
            self.assertEqual([luma_id(frame) for frame in burst], list(range(14)))
            playout = Playout('stable', fps=60)
            for frame in burst:
                playout.push(frame, 0.5)
            output = []
            for tick in range(int(1.5 * 2000)):
                now = tick / 2000
                item, _ = playout.take(now)
                if item is not None:
                    output.append(luma_id(item.frame))
            self.assertEqual(output, list(range(14)))
            self.assertEqual(playout.skipped, 0)
        finally:
            pipeline.close()

    async def test_real_h264_decodes_after_controlled_epoch_reset(self):
        gate = threading.Event()
        gate.set()
        factory = get_decoder

        class GatedDecoder:
            def __init__(self):
                self.inner = factory(H264_CODEC)

            def decode(self, frame):
                gate.wait(5)
                return self.inner.decode(frame)

        pipeline, clock = self.make_pipeline(decoder_factory=lambda codec: GatedDecoder())
        try:
            stream = h264_stream(12, keyframes=(0, 9))
            for index in range(6):
                self.assertTrue(pipeline.offer(H264_CODEC, stream[index]))
            await wait_until(lambda: pipeline.decoded.frames >= 6)
            before = [luma_id(await pipeline.decoded.get()) for _ in range(6)]
            self.assertEqual(before, list(range(6)))
            # Overload the waiting work while the decoder is blocked.
            gate.clear()
            for index in range(6, 9):
                self.assertTrue(pipeline.offer(H264_CODEC, stream[index]))
            clock.advance(PRE_DECODER_AGE + .050)
            self.assertTrue(pipeline.poll())
            self.assertTrue(pipeline.awaiting_keyframe)
            gate.set()
            for index in range(9, 12):
                pipeline.offer(H264_CODEC, stream[index])
            await wait_until(lambda: pipeline.decoded.frames >= 3)
            after = [luma_id(await pipeline.decoded.get()) for _ in range(pipeline.decoded.frames)]
            self.assertEqual(after[:3], [9, 10, 11])
        finally:
            gate.set()
            pipeline.close()


if __name__ == '__main__':
    unittest.main()


class SwitchDecoder(InstantDecoder):
    """Instant decoder that can be blocked on demand to create a stall."""

    def __init__(self):
        super().__init__()
        self.block = None
        self.started = threading.Event()

    def decode(self, frame):
        if self.block is not None:
            self.started.set()
            self.block.wait(5)
        return super().decode(frame)


class RecoveryStalenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_does_not_play_seconds_of_stale_video(self):
        """After a decode-epoch reset the playout must stay near real time."""
        decoder = SwitchDecoder()
        clock = FakeClock()
        clock.now = 0.0
        pipeline = DecodePipeline(asyncio.get_running_loop(), clock=clock, fps=60,
                                  decoder_factory=lambda codec: decoder)
        pipeline.start()
        playout = Playout('stable', fps=60)
        outputs = []

        async def drain(now):
            while pipeline.decoded.frames:
                frame = await pipeline.decoded.get()
                playout.push(frame, now)
            item, _ = playout.take(now)
            while item is not None:
                outputs.append((now, item.frame.pts / 90000))
                item, _ = playout.take(now)

        gate = threading.Event()
        try:
            for index in range(36):
                clock.advance(1 / 60)
                self.assertTrue(pipeline.offer(FakeCodec(), encoded(index * 1500, b'q' * 256)))
                await asyncio.sleep(.002)
                await drain(clock.now)
                self.assertFalse(pipeline.poll())
            # The decoder stalls; 1.5 s of waiting work accumulates.
            decoder.block = gate
            for index in range(36, 44):
                clock.advance(1 / 60)
                self.assertTrue(pipeline.offer(FakeCodec(), encoded(index * 1500, b'q' * 256)))
            await asyncio.wait_for(asyncio.to_thread(decoder.started.wait, 3), 4)
            clock.advance(1.5)
            self.assertTrue(pipeline.poll())
            self.assertTrue(pipeline.awaiting_keyframe)
            gate.set()                              # stalled decode finishes (stale)
            decoder.block = None
            # Resume with a fresh epoch at the current clock.
            for index in range(26):
                clock.advance(1 / 60)
                self.assertTrue(pipeline.offer(FakeCodec(), keyframe(2_000_000 + index * 1500)))
                await asyncio.sleep(.002)
                await drain(clock.now)
            await wait_until(lambda: pipeline.awaiting_keyframe is False)
            await asyncio.sleep(.05)
            await drain(clock.now)
            self.assertTrue(outputs, 'playout produced no output')
            for now, media in outputs:
                self.assertGreaterEqual(media, now - (playout.max_delay + .100),
                                        f'stale frame shown: media={media} now={now}')
        finally:
            gate.set()
            pipeline.close()
