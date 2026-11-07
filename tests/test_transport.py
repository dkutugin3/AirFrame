"""RTP video transport contract for aiortc 1.15.0.

Stage 2/3 of the stable-60fps plan: packet capacity is a packet limit, not a
playout delay, and an incomplete frame is no longer abandoned only after the
2048-packet capacity overflows. A *proven* sequence gap starts a configurable
deadline (default 150 ms) measured on an injectable monotonic clock; expiry frees
the damaged epoch and waits for a complete H.264 IDR with usable SPS/PPS (or a
VP8 keyframe) instead of forwarding dependent P-frames.
"""
import random
from fractions import Fraction
import unittest

import av
from aiortc.codecs import depayload
from aiortc.codecs.h264 import H264Decoder, H264Encoder
from aiortc.codecs.vpx import Vp8Encoder, vp8_depayload
from aiortc.jitterbuffer import JitterBuffer
from aiortc.mediastreams import VIDEO_TIME_BASE
from aiortc.rtp import RtpPacket
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from airframe.playout import Playout
from airframe.transport import VideoBuffer

CODEC_H264 = RTCRtpCodecParameters(mimeType='video/H264', clockRate=90000, payloadType=96)
H264_CODECS = {96: 'video/H264'}
VP8_CODECS = {96: 'video/VP8'}
LUMA_BASE, LUMA_STEP = 20, 7


class FakeClock:
    """Injectable monotonic clock; deterministic deadline tests never sleep."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def set(self, value):
        self.now = value
        return value

    def advance(self, seconds):
        self.now += seconds
        return self.now


def packet(sequence, timestamp, data):
    result = RtpPacket(sequence_number=sequence % 65536, timestamp=timestamp, payload_type=96)
    result._data = data
    return result


def raw_packet(sequence, timestamp, data=b'x', ssrc=1):
    result = RtpPacket(sequence_number=sequence % 65536, timestamp=timestamp,
                       payload_type=96, ssrc=ssrc)
    result._data = data
    return result


def add_packets(buffer, packets):
    """Add RTP packets in order; returns (assembled frames, pli flags)."""
    frames = []
    plis = 0
    for item in packets:
        pli, frame = buffer.add(item)
        plis += int(pli)
        if frame is not None:
            frames.append(frame)
    return frames, plis


def h264_packets(payloads, timestamp, sequence, ssrc=1):
    """Depayload the way aiortc's RTCRtpReceiver does before the jitter buffer."""
    packets = []
    for payload in payloads:
        item = RtpPacket(sequence_number=sequence % 65536, timestamp=timestamp,
                         payload_type=96, ssrc=ssrc)
        item.payload = payload
        item._data = depayload(CODEC_H264, payload)
        packets.append(item)
        sequence += 1
    return packets, sequence


def h264_stream(count=12, keyframes=(0,), width=320, height=180):
    """Real H.264 bitstream at 60 FPS: [(frame_id, rtp_payloads, rtp_timestamp)]."""
    encoder = H264Encoder()
    stream = []
    for index in range(count):
        frame = av.VideoFrame(width, height, 'yuv420p')
        luma = min(235, LUMA_BASE + index * LUMA_STEP)
        frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
        for plane in frame.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        frame.pts = index * 1500          # 90 kHz / 60 FPS
        frame.time_base = VIDEO_TIME_BASE
        payloads, timestamp = encoder.encode(frame, force_keyframe=index in keyframes)
        stream.append((index, payloads, timestamp))
    return stream


def vp8_stream(count=13, width=160, height=90, keyframe_indices=(0, 11)):
    """Real VP8 bitstream: [(is_keyframe, rtp_payloads, rtp_timestamp)]."""
    codec = av.CodecContext.create('libvpx', 'w')
    codec.width, codec.height = width, height
    codec.pix_fmt = 'yuv420p'
    codec.time_base = Fraction(1, 90000)
    codec.framerate = Fraction(30)
    codec.options = {'lag-in-frames': '0', 'deadline': 'realtime', 'cpu-used': '-6', 'g': '3000'}
    encoded = []
    for index in range(count):
        frame = av.VideoFrame(width, height, 'yuv420p')
        luma = min(235, LUMA_BASE + index * LUMA_STEP)
        frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
        for plane in frame.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        frame.pts, frame.time_base = index, Fraction(1, 90000)
        if index in keyframe_indices:
            frame.pict_type = av.video.frame.PictureType.I
        encoded.extend(codec.encode(frame))
    encoded.extend(codec.encode(None))
    packetizer = Vp8Encoder()
    stream = []
    for index, item in enumerate(encoded):
        item.pts, item.time_base = index, Fraction(1, 90000)
        payloads, timestamp = packetizer.pack(item)
        is_keyframe = (vp8_depayload(payloads[0])[0] & 0x01) == 0
        stream.append((is_keyframe, payloads, timestamp))
    return stream


def frame_luma(frame):
    # The decoder may align rows beyond the visible width, especially on
    # x86. Padding bytes are neither pixels nor part of our frame-ID signal.
    plane = frame.planes[0]
    pixels = memoryview(plane)
    total = sum(sum(pixels[row * plane.line_size:row * plane.line_size + frame.width])
                for row in range(frame.height))
    return total / (frame.width * frame.height)


def large_h264_packet():
    """A real noisy 640x360 keyframe whose H.264 payload needs >128 RTP packets."""
    codec = av.CodecContext.create('libx264', 'w')
    codec.width = 640
    codec.height = 360
    codec.pix_fmt = 'yuv420p'
    codec.time_base = Fraction(1, 90000)
    codec.framerate = Fraction(30)
    codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '23'}
    frame = av.VideoFrame(640, 360, 'yuv420p')
    frame.pts = 0
    frame.planes[0].update(random.Random(7).randbytes(frame.planes[0].buffer_size))
    for plane in frame.planes[1:]:
        plane.update(bytes([128]) * plane.buffer_size)
    return codec, frame, codec.encode(frame)[0]


def luma_id(frame):
    return max(0, round((frame_luma(frame) - LUMA_BASE) / LUMA_STEP))


def h264_nal_types(data):
    types = set()
    index = 0
    while True:
        found = data.find(b'\x00\x00\x01', index)
        if found < 0:
            return types
        header = found + 3
        if header < len(data):
            types.add(data[header] & 0x1f)
        index = header


class VideoBufferTests(unittest.TestCase):
    def test_fixture_frame_id_ignores_luma_stride_padding(self):
        frame = av.VideoFrame(17, 9, 'yuv420p')
        plane = frame.planes[0]
        plane.update(bytes(plane.buffer_size))
        pixels = memoryview(plane)
        for row in range(frame.height):
            start = row * plane.line_size
            pixels[start:start + frame.width] = bytes([LUMA_BASE + 13 * LUMA_STEP]) * frame.width
        self.assertEqual(luma_id(frame), 13)

    def test_large_frame_is_not_truncated_on_lossless_link(self):
        expected=b''.join(bytes([i%256])*1200 for i in range(300))
        buffer=VideoBuffer();frames=[];pli_count=0
        for i in range(301):
            pli,frame=buffer.add(packet(65400+i,100 if i<300 else 200,expected[i*1200:(i+1)*1200] if i<300 else b'next'))
            pli_count+=pli
            if frame is not None:frames.append(frame)
        self.assertEqual(pli_count,0)
        self.assertEqual(len(frames),1)
        self.assertEqual(frames[0].data,expected)
        self.assertEqual(buffer.completed,1)
        self.assertGreater(buffer.interval()['largestFrameKiB'],300)

    def test_reordering_waits_for_missing_packet_without_extra_prefetch(self):
        buffer=VideoBuffer()
        self.assertIsNone(buffer.add(packet(0,100,b'a'))[1])
        self.assertIsNone(buffer.add(packet(2,100,b'c'))[1])
        self.assertIsNone(buffer.add(packet(3,200,b'd'))[1])
        pli,frame=buffer.add(packet(1,100,b'b'))
        self.assertFalse(pli)
        self.assertEqual(frame.data,b'abc')

    def test_bounded_capacity_and_recovery_on_actual_overflow(self):
        buffer=VideoBuffer();pli=False
        for i in range(2050):
            flag,_=buffer.add(packet(i,100,b'x'))
            pli|=flag
        self.assertTrue(pli)
        self.assertEqual(buffer.capacity,2048)
        self.assertEqual(len(buffer._packets),2048)
        self.assertGreater(buffer.interval()['bufferRecoveries'],0)

    def test_real_h264_keyframe_larger_than_128_packets_decodes_intact(self):
        _,_,encoded=large_h264_packet()
        payloads,timestamp=H264Encoder().pack(encoded)
        self.assertGreater(len(payloads),128)
        codec=RTCRtpCodecParameters(mimeType='video/H264',clockRate=90000,payloadType=96)
        buffer=VideoBuffer();old=JitterBuffer(128,is_video=True)
        frames=[];old_frames=[];flags=old_flags=0
        for i in range(len(payloads)+1):
            # The next timestamp completes assembly, exactly as in aiortc's receiver.
            data=depayload(codec,payloads[i]) if i<len(payloads) else b'next'
            p=packet(i,timestamp if i<len(payloads) else timestamp+3000,data)
            pli,frame=buffer.add(p);old_pli,old_frame=old.add(p)
            flags+=pli;old_flags+=old_pli
            if frame is not None:frames.append(frame)
            if old_frame is not None:old_frames.append(old_frame)
        self.assertEqual(flags,0)
        self.assertGreater(old_flags,0)
        self.assertEqual(len(frames),1)
        self.assertNotEqual(old_frames[0].data,frames[0].data)
        decoded=H264Decoder().decode(frames[0])
        self.assertEqual(len(decoded),1)
        self.assertEqual((decoded[0].width,decoded[0].height),(640,360))
        self.assertGreater(len(frames[0].data),160000)


class LossDeadlineTests(unittest.TestCase):
    def test_hevc_recovery_waits_for_random_access_after_loss(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs={96: 'video/H265'})
        dependent = b'\x00\x00\x00\x01\x02\x01p'
        idr = b'\x00\x00\x00\x01\x26\x01i'
        sets = b''.join(b'\x00\x00\x00\x01' + bytes([kind << 1, 1]) + b's'
                        for kind in (32, 33, 34))
        buffer.add(raw_packet(0, 0, sets + idr))
        buffer.add(raw_packet(2, 3000, dependent))  # missing sequence 1
        self.assertTrue(buffer.expire(.16))
        self.assertTrue(buffer.awaiting_keyframe)
        buffer.add(raw_packet(3, 4500, dependent))
        _, frame = buffer.add(raw_packet(4, 6000, dependent))
        self.assertIsNone(frame)
        self.assertTrue(buffer.awaiting_keyframe)
        buffer.add(raw_packet(5, 7500, sets + idr))
        _, frame = buffer.add(raw_packet(6, 9000, dependent))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, sets + idr)
        self.assertFalse(buffer.awaiting_keyframe)

    def test_retransmission_before_deadline_preserves_frame_without_pli(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock)
        clock.set(0.0)
        _, frame = buffer.add(raw_packet(0, 1000, b'a'))
        self.assertIsNone(frame)
        clock.set(0.02)
        # A later packet proves the gap (sequence 1 is missing).
        _, frame = buffer.add(raw_packet(2, 1000, b'c'))
        self.assertIsNone(frame)
        self.assertEqual(buffer.expire(0.10), False)        # 80 ms < 150 ms deadline
        clock.set(0.15)
        pli, frame = buffer.add(raw_packet(1, 1000, b'b'))  # retransmission in time
        self.assertFalse(pli)
        self.assertIsNone(frame)
        # aiortc flushes a frame when the next RTP timestamp arrives.
        _, frame = buffer.add(raw_packet(3, 2000, b'n'))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b'abc')
        self.assertFalse(buffer.expire(0.16))
        self.assertFalse(buffer.expire(0.40))
        self.assertEqual(buffer.pli_requests, 0)
        self.assertEqual(buffer.expiries, 0)
        self.assertFalse(buffer.awaiting_keyframe)

    def test_unrecoverable_loss_expires_without_capacity_overflow(self):
        # Old code abandoned the damaged frame only when the 2048-packet capacity
        # overflowed: 2048 packets / (60 FPS * 10 packets) = 3.413 s
        # (artifacts/2026-10-30-stable60fps/core/stage3-old-code-baseline.txt).
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock)
        self.assertEqual(buffer.capacity, 2048)
        sequence = 0
        # 10 packets per frame at 60 FPS; the second packet of the first frame is lost.
        clock.set(0.0)
        for offset in range(10):
            if offset == 1:
                sequence += 1
                continue
            pli, frame = buffer.add(raw_packet(sequence, 0, b'x' * 16))
            sequence += 1
            self.assertFalse(pli)
            self.assertIsNone(frame)
        frames = []
        for frame_index in range(1, 4):
            clock.set(frame_index / 60)
            for _ in range(10):
                pli, frame = buffer.add(raw_packet(sequence, frame_index * 1500, b'x' * 16))
                sequence += 1
                self.assertFalse(pli)
                if frame is not None:
                    frames.append(frame)
        self.assertEqual(frames, [])
        # The 3rd packet of frame 0 (seq 2) proves the gap at t=0.
        self.assertFalse(buffer.expire(.149))
        self.assertTrue(buffer.expire(.150))
        self.assertTrue(buffer.awaiting_keyframe)
        self.assertEqual(buffer.expiries, 1)
        self.assertEqual(buffer.pli_requests, 1)
        # Recovery came from the deadline, not from the 2048-packet capacity.
        self.assertLess(sequence, 100)
        self.assertLessEqual(sum(1 for item in buffer._packets if item is not None), 1)

    def test_expiry_runs_when_no_new_packets_arrive(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock)
        clock.set(0.0)
        buffer.add(raw_packet(0, 1000, b'a'))
        clock.set(0.02)
        buffer.add(raw_packet(2, 1000, b'c'))          # proven gap at 0.02
        self.assertFalse(buffer.expire(0.10))
        self.assertTrue(buffer.expire(0.20))           # 180 ms > 150 ms, no packets
        self.assertTrue(buffer.awaiting_keyframe)
        self.assertFalse(buffer.expire(0.25))          # PLI rate limit 1/500 ms
        self.assertFalse(buffer.expire(0.69))
        self.assertTrue(buffer.expire(0.71))
        self.assertEqual(buffer.pli_requests, 2)

    def test_later_packets_do_not_extend_a_proven_gap(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock)
        clock.set(0.0)
        buffer.add(raw_packet(0, 1000, b'a'))
        clock.set(0.02)
        buffer.add(raw_packet(2, 1000, b'c'))          # proven gap at 0.02
        sequence = 3
        for index in range(1, 60):
            clock.set(0.02 + index * .010)
            buffer.add(raw_packet(sequence, 2000 + index, b'y' * 4))
            sequence += 1
        self.assertFalse(buffer.expire(0.02 + .110))   # still inside the first deadline
        self.assertTrue(buffer.expire(0.02 + .151))    # later packets did not move it
        self.assertEqual(buffer.expiries, 1)

    def test_pli_repeats_are_rate_limited_and_stop_after_keyframe_recovery(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs=H264_CODECS)
        stream = h264_stream(count=9, keyframes=(0, 6))
        sequence = 0
        clock.set(0.0)
        # First two frames are fine (the first is a full IDR + SPS/PPS).
        for frame_index in (0, 1):
            _, payloads, timestamp = stream[frame_index]
            packets, sequence = h264_packets(payloads, timestamp, sequence)
            add_packets(buffer, packets)
        # Lose the only packet of frame 2, then frame 3's packets prove the gap.
        _, payloads, timestamp = stream[2]
        _, sequence = h264_packets(payloads, timestamp, sequence)
        clock.set(0.05)
        _, payloads, timestamp = stream[3]
        packets, sequence = h264_packets(payloads, timestamp, sequence)
        frames, _ = add_packets(buffer, packets)
        self.assertEqual(frames, [])
        # Expiry after 150 ms of a proven gap: first PLI immediately.
        self.assertFalse(buffer.expire(0.199))
        self.assertTrue(buffer.expire(0.201))
        self.assertTrue(buffer.awaiting_keyframe)
        self.assertEqual(buffer.pli_requests, 1)
        # The sender keeps streaming dependent P-frames: they must not be shown.
        dropped = []
        for frame_index in (4, 5):
            _, payloads, timestamp = stream[frame_index]
            packets, sequence = h264_packets(payloads, timestamp, sequence)
            frames, _ = add_packets(buffer, packets)
            dropped.extend(frames)
        self.assertEqual(dropped, [])
        self.assertGreaterEqual(buffer.recovery_skipped, 1)
        # PLI repeats no faster than one per 500 ms; the pending loss does not extend it.
        clock.set(0.35)
        self.assertFalse(buffer.expire(clock.now))
        clock.set(0.70)
        self.assertFalse(buffer.expire(clock.now))
        clock.set(0.72)
        self.assertTrue(buffer.expire(clock.now))
        self.assertEqual(buffer.pli_requests, 2)
        # A real forced keyframe (SPS/PPS + IDR) re-opens the epoch.
        for frame_index in (6, 7):
            _, payloads, timestamp = stream[frame_index]
            packets, sequence = h264_packets(payloads, timestamp, sequence)
            frames, _ = add_packets(buffer, packets)
        self.assertFalse(buffer.awaiting_keyframe)
        self.assertEqual(buffer.recoveries, 1)
        self.assertTrue(frames)
        accepted_types = h264_nal_types(frames[0].data)
        self.assertIn(5, accepted_types, 'new epoch must start at an IDR')
        self.assertIn(7, accepted_types, 'new epoch must carry SPS')
        self.assertIn(8, accepted_types, 'new epoch must carry PPS')
        decoded = H264Decoder()
        pictures = []
        for frame in frames:
            pictures.extend(decoded.decode(frame))
        self.assertTrue(pictures)
        self.assertGreaterEqual(luma_id(pictures[0]), 6)
        # Repetition stops after recovery.
        clock.set(5.0)
        self.assertFalse(buffer.expire(clock.now))
        self.assertEqual(buffer.pli_requests, 2)

    def test_lossless_225ms_pause_creates_no_false_recovery_or_pli(self):
        # Fully ordered, lossless RTP: frames 9..22 arrive 225 ms late as a burst.
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs=H264_CODECS)
        stream = h264_stream(count=31, keyframes=(0,))
        sequence = 0
        frames, plis = [], 0
        for frame_index, (_, payloads, timestamp) in enumerate(stream):
            arrival = frame_index / 60
            if 9 <= frame_index <= 22:
                arrival = 22 / 60
            clock.set(arrival)
            packets, sequence = h264_packets(payloads, timestamp, sequence)
            new_frames, new_plis = add_packets(buffer, packets)
            frames.extend((clock.now, frame) for frame in new_frames)
            plis += new_plis
            for _ in range(3):       # watchdog ticks, even without packets
                clock.advance(.001)
                self.assertFalse(buffer.expire(clock.now))
        for _ in range(400):
            clock.advance(.001)
            self.assertFalse(buffer.expire(clock.now))
        self.assertEqual(plis, 0)
        self.assertEqual(buffer.expiries, 0)
        self.assertFalse(buffer.awaiting_keyframe)
        self.assertEqual(len(frames), 30)          # the last frame waits for a next timestamp
        decoder = H264Decoder()
        pictures = []
        for arrival, frame in frames:
            for picture in decoder.decode(frame):
                pictures.append((arrival, picture))
        self.assertEqual(len(pictures), 30)
        self.assertEqual([luma_id(picture) for _, picture in pictures], list(range(30)))
        # The stage-2 stable playout keeps all 14 burst frames for even output.
        playout = Playout('stable', fps=60)
        arrivals = pictures
        index = 0
        output = []
        for tick in range(int((max(time for time, _ in arrivals) + 1.0) / (1 / 2000))):
            now = tick / 2000
            while index < len(arrivals) and arrivals[index][0] <= now:
                playout.push(arrivals[index][1], now)
                index += 1
            item, _ = playout.take(now)
            if item is not None:
                output.append(luma_id(item.frame))
        self.assertEqual(output, list(range(30)))
        self.assertEqual(playout.skipped, 0)

    def test_late_old_epoch_packets_are_ignored_after_expiry(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs=H264_CODECS)
        stream = h264_stream(count=8, keyframes=(0, 5))
        sequence = 0
        clock.set(0.0)
        for frame_index in (0, 1):
            _, payloads, timestamp = stream[frame_index]
            packets, sequence = h264_packets(payloads, timestamp, sequence)
            add_packets(buffer, packets)
        # Lose frame 2 entirely; frame 3 proves the gap.
        lost_packets, sequence = h264_packets(stream[2][1], stream[2][2], sequence)
        clock.set(0.05)
        packets, sequence = h264_packets(stream[3][1], stream[3][2], sequence)
        add_packets(buffer, packets)
        self.assertTrue(buffer.expire(0.25))
        floor = buffer.recovery_seq_floor
        self.assertIsNotNone(floor)
        # Late retransmissions of the abandoned epoch are ignored, never decoded.
        stale = [raw_packet(sequence - 1 - index, 2000 - index, b'late')
                 for index in range(1, 6)]
        frames, plis = add_packets(buffer, stale)
        self.assertEqual(frames, [])
        self.assertEqual(plis, 0)
        self.assertGreaterEqual(buffer.stale_skipped, len(stale))
        self.assertEqual(buffer.expiries, 1)
        # The next real keyframe is accepted on a fresh sequence.
        recovery_sequence = sequence + 100
        for frame_index in (5, 6):
            _, payloads, timestamp = stream[frame_index]
            packets, recovery_sequence = h264_packets(payloads, timestamp, recovery_sequence)
            frames, _ = add_packets(buffer, packets)
        self.assertFalse(buffer.awaiting_keyframe)
        self.assertTrue(frames)

    def test_sequence_number_wrap_does_not_break_reassembly(self):
        buffer = VideoBuffer(clock=FakeClock())
        self.assertIsNone(buffer.add(packet(65534, 1000, b'a'))[1])
        self.assertIsNone(buffer.add(packet(65535, 1000, b'b'))[1])
        _, frame = buffer.add(packet(0, 2000, b'c'))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b'ab')
        pli, frame = buffer.add(packet(1, 2000, b'd'))
        self.assertFalse(pli)
        self.assertIsNone(frame)
        self.assertEqual(buffer.expiries, 0)
        # A loss across the wrap expires on the injectable deadline as usual.
        clock = buffer.clock
        self.assertIsNone(buffer.add(packet(3, 3000, b'e'))[1])
        clock.set(1.0)
        self.assertTrue(buffer.expire(clock.now + .20))
        self.assertTrue(buffer.awaiting_keyframe)

    def test_ssrc_change_cancels_old_recovery_state(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs=H264_CODECS)
        clock.set(0.0)
        buffer.add(raw_packet(0, 1000, b'a'))
        clock.set(0.02)
        buffer.add(raw_packet(2, 1000, b'c'))
        self.assertTrue(buffer.expire(0.30))
        self.assertTrue(buffer.awaiting_keyframe)
        self.assertEqual(buffer.pli_requests, 1)
        # A new SSRC (reconnect/new offer) cancels the old timer and PLI state.
        clock.set(0.40)
        buffer.add(raw_packet(100, 5000, b'x' * 8, ssrc=999))
        self.assertFalse(buffer.awaiting_keyframe)
        self.assertGreaterEqual(buffer.ssrc_resets, 1)
        clock.set(5.0)
        self.assertFalse(buffer.expire(clock.now))
        self.assertEqual(buffer.pli_requests, 1)

    def test_vp8_recovery_waits_for_a_keyframe(self):
        clock = FakeClock()
        buffer = VideoBuffer(clock=clock, codecs=VP8_CODECS)
        stream = vp8_stream()
        keyframes = [index for index, item in enumerate(stream) if item[0]]
        self.assertGreaterEqual(len(keyframes), 2)
        sequence = 0

        def feed(frame_index, drop_first=False, ssrc=1):
            nonlocal sequence
            _, payloads, timestamp = stream[frame_index]
            packets, sequence = h264_packets(payloads, timestamp, sequence, ssrc=ssrc)
            for offset, item in enumerate(packets):
                item.payload_type = 96
                item._data = vp8_depayload(payloads[offset])
                if drop_first and offset == 0:
                    continue
                buffer.add(item)

        clock.set(0.0)
        lost = 4
        for frame_index in range(lost):
            feed(frame_index)
        # Lose the first packet of frame 4; frame 5 proves the gap.
        feed(lost, drop_first=True)
        clock.set(0.10)
        feed(lost + 1)
        self.assertTrue(buffer.expire(0.30))
        self.assertTrue(buffer.awaiting_keyframe)
        # Dependent inter frames are not handed to the decoder.
        feed(lost + 2)
        feed(lost + 3)
        self.assertGreaterEqual(buffer.recovery_skipped, 1)
        # The next real VP8 keyframe re-opens the epoch.
        feed(keyframes[1])
        feed(keyframes[1] + 1)
        self.assertFalse(buffer.awaiting_keyframe)
        self.assertGreaterEqual(buffer.recoveries, 1)


if __name__ == '__main__':
    unittest.main()
