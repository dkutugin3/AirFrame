"""Narrow decoder integration layer for aiortc 1.15.0 (plan stage 4).

aiortc 1.15.0 has two unbounded queues on the receive path:

1. an encoded-frame ``queue.Queue`` in front of its decoder thread, fed by
   ``RTCRtpReceiver._handle_rtp_packet`` from the network event loop;
2. a decoded-frame ``asyncio.Queue`` between that thread and
   ``RemoteStreamTrack.recv()``.

This module replaces both with bounded queues and owns the decoder worker for one
peer connection (per-peer integration, no global codec-factory replacement).
Encoded waiting work is bounded by count, bytes and **150 ms age measured from the
access unit being enqueued** (never from the source PTS or network latency). The
decoded handoff is a bounded queue, not a latest-only mailbox, and holds the
supported 225 ms / 60 FPS burst. Overflow flushes the whole decode epoch, drops
stale in-flight results and asks for a fresh IDR instead of dropping the oldest
dependent encoded frames.

Guarantee boundary: these limits cover *waiting* work, not a native FFmpeg
``decode()`` call that is already executing. A hung native call cannot be
cancelled through asyncio; isolating it would need a separate process. Session
close waits for the in-flight call to return (as aiortc does) and never waits for
queued, not-yet-started work: the completion signal bypasses the bounds and
pending access units are discarded.
"""
import asyncio
import math
import threading
import time
from collections import deque

import aiortc
from aiortc.codecs import get_decoder

from .transport import (H264_NAL_IDR, H265_RANDOM_ACCESS, VIDEO_LOSS_DEADLINE,
                        h264_nal_types, h265_nal_types)

#: Age limit for encoded access units waiting for the decoder (from enqueue time).
PRE_DECODER_AGE = VIDEO_LOSS_DEADLINE
#: Hard byte cap for the pre-decoder queue: multiple multi-MiB 2560px IDRs plus
#: P-frames (~20 x 3 MiB), far above one access unit so a burst fits.
PRE_DECODER_BYTES = 64 * 1024 * 1024
#: Age limit for decoded frames waiting to be consumed by track.recv().
DECODED_AGE = .500
#: Hard byte cap for the decoded handoff. It must hold the supported 225 ms burst
#: even at the saved 120 FPS profile: 31 x 2560x1440 YUV420 (~163 MiB) + margin.
DECODED_BYTES = 192 * 1024 * 1024
#: The playout supports up to 225 ms of burst; the handoff must hold that much.
SUPPORTED_BURST = .225
#: Repeated keyframe requests while a decode epoch waits for an IDR.
KEYFRAME_REPEAT = .500

_REQUIRED = ('_RTCRtpReceiver__decoder_queue', '_RTCRtpReceiver__decoder_thread',
             '_RTCRtpReceiver__stop_decoder')


def pre_decoder_limits(fps):
    """Frame count allowed in front of the decoder for the configured FPS.

    It must hold both the 150 ms age budget and the supported 225 ms burst, so a
    lossless pause that delivers 14 frames at 60 FPS cannot overflow the queue.
    """
    return max(4, math.ceil(fps * PRE_DECODER_AGE) + 4,
               math.ceil(fps * SUPPORTED_BURST) + 4)


def decoded_limits(fps):
    """Decoded-handoff frame count: supported burst plus push/take margin."""
    return max(6, math.ceil(fps * SUPPORTED_BURST) + 4)


def frame_bytes(frame):
    size = sum(getattr(plane, 'buffer_size', 0) for plane in getattr(frame, 'planes', ()))
    if size:
        return size
    return getattr(frame, 'width', 0) * getattr(frame, 'height', 0) * 3 // 2


class BoundedFrameQueue:
    """Thread-safe encoded access-unit queue with hard count/byte bounds.

    ``put`` never blocks the network event loop: when the queue is full the new
    access unit is dropped (memory stays bounded) and an overflow is counted for
    the integration layer to turn into a decode-epoch reset + IDR request.
    ``put(None)`` is the worker completion signal; it is never lost because it
    bypasses the bounds and is delivered even when the queue is full.
    """

    def __init__(self, max_frames, max_bytes, clock=time.monotonic):
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.clock = clock
        self.frames = self.bytes = self.dropped = self.overflows = 0
        self.closed = False
        self._items = deque()
        self._cond = threading.Condition()

    def put(self, item):
        with self._cond:
            if self.closed:
                return False
            if item is None:
                self.closed = True
                self.dropped += len(self._items)
                self._items.clear()
                self.frames = self.bytes = 0
                self._cond.notify_all()
                return True
            codec, encoded = item
            size = len(getattr(encoded, 'data', b'')) or 1
            if len(self._items) >= self.max_frames or self.bytes + size > self.max_bytes:
                self.dropped += 1
                self.overflows += 1
                return False
            self._items.append((self.clock(), size, item))
            self.frames = len(self._items)
            self.bytes += size
            self._cond.notify()
            return True

    def get(self):
        with self._cond:
            while True:
                if self.closed:
                    self.dropped += len(self._items)
                    self._items.clear()
                    self.frames = self.bytes = 0
                    return None
                if self._items:
                    break
                self._cond.wait(.05)
            enqueued, size, item = self._items.popleft()
            self.frames = len(self._items)
            self.bytes -= size
            return enqueued, item

    def flush(self):
        """Drop every queued access unit (used when an epoch is abandoned)."""
        with self._cond:
            dropped = len(self._items)
            self.dropped += dropped
            self._items.clear()
            self.frames = self.bytes = 0
            self._cond.notify_all()
            return dropped

    def close(self):
        with self._cond:
            self.closed = True
            self.dropped += len(self._items)
            self._items.clear()
            self.frames = self.bytes = 0
            self._cond.notify_all()

    def stats(self, now=None):
        with self._cond:
            now = self.clock() if now is None else now
            oldest = max(0.0, now - self._items[0][0]) if self._items else 0.0
            return {'frames': len(self._items), 'bytes': self.bytes,
                    'ageMs': oldest * 1000, 'maxFrames': self.max_frames,
                    'maxBytes': self.max_bytes, 'dropped': self.dropped,
                    'overflows': self.overflows, 'closed': self.closed}


class BoundedDecodedQueue:
    """Bounded async handoff from the decoder worker to ``RemoteStreamTrack``.

    Not a latest-only mailbox: it holds the supported burst so stable playout can
    show every decoded frame evenly. On overflow the epoch is abandoned (the
    queue is flushed) and the pipeline asks for an IDR.
    """

    def __init__(self, max_frames, max_bytes, clock=time.monotonic):
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.clock = clock
        self.frames = self.bytes = self.dropped = self.overflows = 0
        self.closed = False
        self._queue = asyncio.Queue()
        self._meta = deque()

    def put_nowait(self, frame):
        """Called on the event loop by the worker publication callback."""
        if self.closed:
            return False
        size = frame_bytes(frame) or 1
        if self.frames >= self.max_frames or self.bytes + size > self.max_bytes:
            self.overflows += 1
            self._clear()
            return False
        self._queue.put_nowait(frame)
        self._meta.append((self.clock(), size))
        self.frames += 1
        self.bytes += size
        return True

    def _clear(self):
        while not self._queue.empty():
            self._queue.get_nowait()
            self.frames -= 1
            self.dropped += 1
        self._meta.clear()
        self.frames = self.bytes = 0

    def flush(self):
        dropped = self.frames
        self._clear()
        self.dropped += dropped
        return dropped

    async def get(self):
        """Consumer used by ``RemoteStreamTrack.recv``; None = stream closed."""
        if self.closed and self._queue.empty():
            return None
        frame = await self._queue.get()
        if frame is None:
            return None
        enqueued, size = self._meta.popleft()
        self.frames -= 1
        self.bytes -= size
        return frame

    async def put(self, frame):
        """Compatibility hook: ignore aiortc's old decoder-worker end signal.

        ``RTCRtpReceiver._stop_decoder()`` finishes aiortc's own decoder thread with
        ``run_coroutine_threadsafe(output_q.put(None), loop)`` on the track queue it
        captured at start-up. The pipeline owns the decoded handoff now, so that
        legacy end signal is a no-op here: closing the handoff would kill the live
        queue during ``attach()``. Shutdown is signalled by ``close()`` instead.
        """
        return None

    def close(self):
        self.closed = True
        dropped = self.frames
        self._clear()
        self.dropped += dropped
        self._queue.put_nowait(None)

    def stats(self, now=None):
        now = self.clock() if now is None else now
        oldest = max(0.0, now - self._meta[0][0]) if self._meta else 0.0
        return {'frames': self.frames, 'bytes': self.bytes, 'ageMs': oldest * 1000,
                'maxFrames': self.max_frames, 'maxBytes': self.max_bytes,
                'dropped': self.dropped, 'overflows': self.overflows,
                'closed': self.closed}


class DecodePipeline:
    """One bounded decoder worker for one video receiver.

    ``poll()`` is called by the receiver watchdog on the network event loop; it
    returns True when a keyframe (IDR/PLI) should be requested now.
    """

    def __init__(self, loop, request_keyframe=None, fps=60, clock=time.monotonic,
                 decoder_factory=None, pre_max_frames=None, decoded_max_frames=None,
                 pre_max_bytes=PRE_DECODER_BYTES, decoded_max_bytes=DECODED_BYTES):
        self.loop = loop
        self.request_keyframe = request_keyframe
        self.clock = clock
        self.fps = fps
        self.decoder_factory = decoder_factory or get_decoder
        self.encoded = BoundedFrameQueue(pre_max_frames or pre_decoder_limits(fps),
                                         pre_max_bytes, clock)
        self.decoded = BoundedDecodedQueue(decoded_max_frames or decoded_limits(fps),
                                           decoded_max_bytes, clock)
        self.epoch = 0
        self.awaiting_keyframe = False
        self.closed = False
        self.attached = False
        self.receiver = None
        self.decode_recoveries = 0
        self.decode_errors = 0
        self.stale_decode_drops = 0
        self.awaited_keyframe_drops = 0
        self.decoded_count = 0
        self.wait_ms = self.decode_ms = 0.0
        self.recovery_reasons = deque(maxlen=8)
        self._last_recovery_reason = ''
        self._lock = threading.Lock()
        self._needs_request = False
        self._last_request_at = None
        self._seen_encoded_overflows = 0
        self._seen_decoded_overflows = 0
        self._thread = threading.Thread(target=self._run, name='airframe-decoder', daemon=True)

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread.start()

    def attach(self, receiver):
        """Take over the decoder path of a started aiortc RTCRtpReceiver."""
        if self.attached:
            return
        self.attached = True
        self.receiver = receiver
        # aiortc started its own unbounded decoder thread in receive(); it is
        # idle (no access unit was handed over yet), so stopping it is instant.
        stop = getattr(receiver, '_RTCRtpReceiver__stop_decoder')
        stop()
        setattr(receiver, '_RTCRtpReceiver__decoder_queue', self.encoded)
        setattr(receiver, '_RTCRtpReceiver__decoder_thread', self._thread)
        self.start()

    def close(self):
        """Stop the worker; never blocks on queued work and is idempotent."""
        self.closed = True
        self.encoded.close()
        try:
            self.loop.call_soon_threadsafe(self.decoded.close)
        except RuntimeError:
            self.decoded.close()

    def join(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # ---- network-event-loop side ------------------------------------------

    def offer(self, codec, encoded_frame):
        """Hand one access unit to the bounded pre-decoder queue."""
        return self.encoded.put((codec, encoded_frame))

    def poll(self):
        """Advance decode-overload detection; True = request a keyframe now."""
        if self.closed:
            return False
        now = self.clock()
        encoded = self.encoded.stats(now)
        decoded = self.decoded.stats(now)
        with self._lock:
            awaiting = self.awaiting_keyframe
        if not awaiting:
            reason = None
            if self.encoded.overflows > self._seen_encoded_overflows:
                self._seen_encoded_overflows = self.encoded.overflows
                reason = 'encoded queue overflow'
            elif encoded['frames'] and encoded['ageMs'] > PRE_DECODER_AGE * 1000:
                reason = 'encoded queue overload'
            elif self.decoded.overflows > self._seen_decoded_overflows:
                self._seen_decoded_overflows = self.decoded.overflows
                reason = 'decoded handoff overflow'
            elif decoded['frames'] and decoded['ageMs'] > DECODED_AGE * 1000:
                reason = 'decoded handoff overload'
            if reason is not None:
                self._reset_epoch(reason)
                self._last_request_at = now
                self._needs_request = False
                return True
            return False
        if self._needs_request:
            self._needs_request = False
            self._last_request_at = now
            return True
        if self._last_request_at is None:
            self._last_request_at = now
            return True
        if now - self._last_request_at >= KEYFRAME_REPEAT:
            self._last_request_at = now
            return True
        return False

    def _reset_epoch(self, reason):
        with self._lock:
            self.epoch += 1
            self.awaiting_keyframe = True
        self.decode_recoveries += 1
        self.recovery_reasons.append(reason)
        self._last_recovery_reason = reason
        # No drop-oldest of dependent encoded frames: abandon the whole epoch.
        self.encoded.flush()
        self.decoded.flush()

    def stats(self):
        now = self.clock()
        with self._lock:
            awaiting = self.awaiting_keyframe
            epoch = self.epoch
        encoded = self.encoded.stats(now)
        decoded = self.decoded.stats(now)
        return {
            'decodeQueueFrames': encoded['frames'],
            'decodeQueueMiB': encoded['bytes'] / 1048576,
            'decodeQueueAgeMs': encoded['ageMs'],
            'decodeQueueMaxFrames': encoded['maxFrames'],
            'decodeQueueDropped': encoded['dropped'],
            'decodeQueueOverflows': encoded['overflows'],
            'decodedQueueFrames': decoded['frames'],
            'decodedQueueMiB': decoded['bytes'] / 1048576,
            'decodedQueueAgeMs': decoded['ageMs'],
            'decodedQueueMaxFrames': decoded['maxFrames'],
            'decodedQueueOverflows': decoded['overflows'],
            'decodeWaitMs': self.wait_ms,
            'decodeMs': self.decode_ms,
            'decodeRecoveries': self.decode_recoveries,
            'decodeErrors': self.decode_errors,
            'staleDecodeDrops': self.stale_decode_drops,
            'awaitedKeyframeDrops': self.awaited_keyframe_drops,
            'decodeAwaitingKeyframe': awaiting,
            'decodeEpoch': epoch,
            'decodeLastRecoveryReason': self._last_recovery_reason,
        }

    # ---- worker thread -----------------------------------------------------

    def _run(self):
        decoder = None
        decoder_name = None
        try:
            while True:
                item = self.encoded.get()
                if item is None or self.closed:
                    break
                enqueued, (codec, encoded_frame) = item
                with self._lock:
                    epoch = self.epoch
                    awaiting = self.awaiting_keyframe
                if awaiting and not self._is_keyframe(codec, encoded_frame):
                    # Dependent frames of an abandoned epoch are never decoded.
                    self.awaited_keyframe_drops += 1
                    continue
                started = self.clock()
                self.wait_ms = max(0.0, (started - enqueued) * 1000)
                try:
                    name = getattr(codec, 'name', None)
                    if decoder is None or name != decoder_name:
                        decoder = self.decoder_factory(codec)
                        decoder_name = name
                    pictures = decoder.decode(encoded_frame)
                except Exception:
                    self.decode_errors += 1
                    continue
                self.decode_ms = max(0.0, (self.clock() - started) * 1000)
                if awaiting:
                    with self._lock:
                        if epoch == self.epoch:
                            self.awaiting_keyframe = False
                for frame in pictures:
                    try:
                        self.loop.call_soon_threadsafe(self._publish, frame, epoch)
                    except RuntimeError:
                        break
        finally:
            self.closed = True

    def _publish(self, frame, epoch):
        """Event-loop side: filter stale epochs and fill the bounded handoff."""
        if self.closed:
            self.stale_decode_drops += 1
            return
        with self._lock:
            stale = epoch != self.epoch
            awaiting = self.awaiting_keyframe
        if stale or awaiting:
            self.stale_decode_drops += 1
            return
        self.decoded_count += 1
        if not self.decoded.put_nowait(frame):
            # Bounded handoff overflow: abandon the epoch and request an IDR.
            self._reset_epoch('decoded handoff overflow')
            self._needs_request = True

    @staticmethod
    def _is_keyframe(codec, encoded_frame):
        mime = (getattr(codec, 'mimeType', '') or '').lower()
        data = getattr(encoded_frame, 'data', b'')
        if 'h264' in mime:
            return H264_NAL_IDR in h264_nal_types(data)
        if 'h265' in mime:
            return bool(h265_nal_types(data) & H265_RANDOM_ACCESS)
        if 'vp8' in mime:
            return bool(data) and (data[0] & 0x01) == 0
        # Unknown codec: the decoder resumes on the keyframe the PLI requested.
        return True


def install_decoder_pipelines(peer, loop, request_keyframe=None, fps=60,
                              clock=time.monotonic, decoder_factory=None):
    """Install one bounded DecodePipeline per video receiver after validation."""
    if aiortc.__version__ != '1.15.0':
        raise RuntimeError('AirFrame requires aiortc 1.15.0 for its decoder integration')
    pipelines = []
    for receiver in peer.getReceivers():
        track = getattr(receiver, 'track', None)
        if track is None or track.kind != 'video':
            continue
        if not isinstance(getattr(track, '_queue', None), asyncio.Queue):
            raise RuntimeError('Incompatible aiortc track queue; update AirFrame decoder integration')
        missing = object()
        for attribute in _REQUIRED:
            if getattr(receiver, attribute, missing) is missing:
                raise RuntimeError(f'Incompatible aiortc receiver ({attribute}); '
                                   'update AirFrame decoder integration')
        if not callable(getattr(receiver, '_RTCRtpReceiver__stop_decoder')):
            raise RuntimeError('Incompatible aiortc decoder stop hook; update AirFrame integration')
        pipeline = DecodePipeline(loop, request_keyframe=request_keyframe, fps=fps, clock=clock,
                                  decoder_factory=decoder_factory)
        receive = receiver.receive

        async def start_pipeline(parameters, _receive=receive, _pipeline=pipeline):
            await _receive(parameters)
            _pipeline.attach(receiver)

        receiver.receive = start_pipeline
        track._queue = pipeline.decoded
        pipelines.append(pipeline)
    return pipelines
