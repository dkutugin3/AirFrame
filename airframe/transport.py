"""Bounded video packet reassembly for aiortc 1.15.0.

The upstream 128-packet video buffer can discard the beginning of a large
H.264 keyframe even on a lossless link. Capacity is a packet limit, not an
extra playout delay: prefetch remains zero.

aiortc has no public jitter-buffer configuration API. Keep this one per-receiver
hook isolated, pin the dependency, and fail explicitly on incompatible internals.
"""
import time

import aiortc
from aiortc.jitterbuffer import JitterBuffer

VIDEO_PACKET_CAPACITY = 2048
_BUFFER_ATTRIBUTE = '_RTCRtpReceiver__jitter_buffer'


class VideoBuffer(JitterBuffer):
    def __init__(self):
        super().__init__(capacity=VIDEO_PACKET_CAPACITY, prefetch=0, is_video=True)
        self.completed = 0
        self.last_packet_at = self.last_frame_at = 0.0
        self.packet_gap = self.frame_gap = 0.0
        self.max_bytes = self.recoveries = 0

    def add(self, packet):
        now = time.monotonic()
        if self.last_packet_at:
            self.packet_gap = max(self.packet_gap, now - self.last_packet_at)
        self.last_packet_at = now
        pli, frame = super().add(packet)
        self.recoveries += int(pli)
        if frame is not None:
            self.completed += 1
            self.max_bytes = max(self.max_bytes, len(frame.data))
            if self.last_frame_at:
                self.frame_gap = max(self.frame_gap, now - self.last_frame_at)
            self.last_frame_at = now
        return pli, frame

    def interval(self):
        now = time.monotonic()
        metrics = {
            'packetGapMs': max(self.packet_gap, now-self.last_packet_at if self.last_packet_at else 0)*1000,
            'assemblyGapMs': max(self.frame_gap, now-self.last_frame_at if self.last_frame_at else 0)*1000,
            'largestFrameKiB': self.max_bytes/1024,
            'bufferRecoveries': self.recoveries,
        }
        self.packet_gap = self.frame_gap = 0.0
        self.max_bytes = self.recoveries = 0
        return metrics


def install_video_buffers(peer):
    if aiortc.__version__ != '1.15.0':
        raise RuntimeError('AirFrame requires aiortc 1.15.0 for its video packet buffer')
    buffers = []
    for receiver in peer.getReceivers():
        if receiver.track is None or receiver.track.kind != 'video':
            continue
        old = getattr(receiver, _BUFFER_ATTRIBUTE, None)
        if type(old) is not JitterBuffer or old.capacity != 128:
            raise RuntimeError('Incompatible aiortc video buffer; update AirFrame transport integration')
        buffer = VideoBuffer()
        setattr(receiver, _BUFFER_ATTRIBUTE, buffer)
        buffers.append(buffer)
    return buffers
