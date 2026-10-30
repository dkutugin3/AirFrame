"""Bounded video packet reassembly for aiortc 1.15.0.

The upstream 128-packet video buffer can discard the beginning of a large
H.264 keyframe even on a lossless link. Capacity is a packet limit, not an
extra playout delay: prefetch remains zero.

Bounded loss wait (plan stage 3): a *proven* sequence gap (a later packet
arrived) starts a deadline measured on an injectable monotonic clock. It is
independent of the 2048-packet capacity and of the playout queue; later packets
do not extend it. A retransmission before the deadline preserves the frame. On
expiry the damaged epoch is freed and only a complete H.264 IDR with usable
SPS/PPS (or a VP8 keyframe) re-opens the stream - arbitrary P-frames are never
forwarded as if nothing happened. The first PLI is sent immediately after
expiry, repeats are limited to one per 500 ms, and repeats stop after recovery.
A lossless RTP pause proves no gap and therefore never triggers a false PLI.

aiortc has no public jitter-buffer configuration API. Keep this one per-receiver
hook isolated, pin the dependency, and fail explicitly on incompatible internals.
"""
import time

import aiortc
from aiortc.jitterbuffer import JitterBuffer
from aiortc.utils import uint16_add, uint16_gt

VIDEO_PACKET_CAPACITY = 2048
VIDEO_LOSS_DEADLINE = .150
PLI_REPEAT_INTERVAL = .500
_BUFFER_ATTRIBUTE = '_RTCRtpReceiver__jitter_buffer'
_CODECS_ATTRIBUTE = '_RTCRtpReceiver__codecs'
_NACK_ATTRIBUTE = '_RTCRtpReceiver__nack_generator'

H264_NAL_IDR = 5
H264_NAL_SPS = 7
H264_NAL_PPS = 8


def h264_nal_types(data):
    """NAL unit types of an Annex-B access unit (aiortc depayload output)."""
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


class VideoBuffer(JitterBuffer):
    def __init__(self, capacity=VIDEO_PACKET_CAPACITY, clock=time.monotonic,
                 deadline=VIDEO_LOSS_DEADLINE, pli_interval=PLI_REPEAT_INTERVAL,
                 codecs=None):
        super().__init__(capacity=capacity, prefetch=0, is_video=True)
        self.clock = clock
        self.deadline = deadline
        self.pli_interval = pli_interval
        self.codecs = dict(codecs or {})
        # Interval metrics carried over from the original transport hook.
        self.completed = 0
        self.last_packet_at = self.last_frame_at = 0.0
        self.packet_gap = self.frame_gap = 0.0
        self.max_bytes = 0
        self.overflow_recoveries = 0
        # Bounded-loss-wait state.
        self.last_ssrc = None
        self.awaiting_keyframe = False
        self.expiries = 0
        self.pli_requests = 0
        self.recoveries = 0
        self.recovery_skipped = 0
        self.stale_skipped = 0
        self.ssrc_resets = 0
        self.recovery_epoch = 0
        self._max_sequence = None
        self._missing = set()
        self._blocked_since = None
        self._last_pli_at = None
        self._recovery_seq_floor = None
        self._codec_family = None
        self._sps_seen = self._pps_seen = False
        self._events = {'expiries': 0, 'recoveries': 0, 'ssrc': 0}
        self._metrics = {'expiries': 0, 'recoveries': 0, 'ssrc': 0}

    # ---- bounded loss wait -------------------------------------------------

    def expire(self, now=None):
        """Advance recovery timers without blocking; True = send a PLI now.

        Must be called from the network event loop (receiver watchdog) so the
        deadline also fires while no RTP packet arrives.
        """
        now = self.clock() if now is None else now
        if self.awaiting_keyframe:
            return self._next_pli(now)
        if self._blocked_since is not None and now - self._blocked_since >= self.deadline:
            self._expire_epoch()
            self._events['expiries'] += 1
            self._metrics['expiries'] += 1
            return self._next_pli(now)
        return False

    def take_events(self):
        """Recovery events since the last call (receiver logging/metrics)."""
        events, self._events = self._events, {'expiries': 0, 'recoveries': 0, 'ssrc': 0}
        return events

    def _next_pli(self, now):
        if self._last_pli_at is not None and now - self._last_pli_at < self.pli_interval:
            return False
        self._last_pli_at = now
        self.pli_requests += 1
        return True

    @property
    def recovery_seq_floor(self):
        """Sequence floor of the epoch being recovered (older packets are stale)."""
        return self._recovery_seq_floor

    def _purge(self):
        if self._origin is not None:
            self.remove(self.capacity)
        # A fresh assembly timeline: the abandoned epoch is not referenced again.
        self._origin = None

    def _expire_epoch(self):
        self.expiries += 1
        self.recovery_epoch += 1
        self._purge()
        self.awaiting_keyframe = True
        self._recovery_seq_floor = self._max_sequence
        self._blocked_since = None
        self._missing.clear()

    def _note_gap(self, sequence, now):
        """Track proven sequence gaps; the first one starts the age clock."""
        if self._max_sequence is None:
            self._max_sequence = sequence
            return
        if uint16_gt(sequence, self._max_sequence):
            expected = uint16_add(self._max_sequence, 1)
            if uint16_gt(sequence, expected):
                distance = uint16_add(sequence, -self._max_sequence)
                if distance > self.capacity:
                    # A jump larger than the buffer: a restart/new epoch, not a
                    # per-packet gap. Let aiortc reseed its origin.
                    self._missing.clear()
                    self._max_sequence = sequence
                    self._blocked_since = None
                    return
                missing = expected
                while uint16_gt(sequence, missing) and len(self._missing) < self.capacity:
                    self._missing.add(missing)
                    missing = uint16_add(missing, 1)
                if self._blocked_since is None and not self.awaiting_keyframe:
                    self._blocked_since = now
            self._max_sequence = sequence
        else:
            self._missing.discard(sequence)
            if not self._missing and self._blocked_since is not None:
                # The retransmission healed the only proven gap.
                self._blocked_since = None

    def _reset_epoch_state(self):
        self.recovery_epoch += 1
        self.awaiting_keyframe = False
        self._blocked_since = None
        self._missing.clear()
        self._max_sequence = None
        self._recovery_seq_floor = None
        self._last_pli_at = None
        self._purge()

    def _codec_map_from_receiver(self):
        """aiortc fills its codec table when the DTLS transport connects."""
        receiver = getattr(self, 'receiver', None)
        codecs = getattr(receiver, _CODECS_ATTRIBUTE, None) if receiver else None
        if not isinstance(codecs, dict):
            return {}
        return {payload_type: codec.mimeType
                for payload_type, codec in codecs.items()
                if getattr(codec, 'mimeType', None)}

    def _family(self, mime_type):
        mime = (mime_type or '').lower()
        if 'h264' in mime:
            return 'h264'
        if 'vp8' in mime:
            return 'vp8'
        return mime or None

    def _accepts_as_keyframe(self, frame):
        """A new epoch starts at a complete IDR with usable SPS/PPS (H.264)."""
        family = self._codec_family
        if family == 'h264':
            types = h264_nal_types(frame.data)
            if H264_NAL_SPS in types:
                self._sps_seen = True
            if H264_NAL_PPS in types:
                self._pps_seen = True
            return (H264_NAL_IDR in types) and self._sps_seen and self._pps_seen
        if family == 'vp8':
            return bool(frame.data) and (frame.data[0] & 0x01) == 0
        # Unknown codec: no silent P-frame gating; the decoder/higher layer
        # resumes on the keyframe requested by the PLI.
        return True

    def _note_parameter_sets(self, frame):
        if self._codec_family != 'h264':
            return
        types = h264_nal_types(frame.data)
        if H264_NAL_SPS in types:
            self._sps_seen = True
        if H264_NAL_PPS in types:
            self._pps_seen = True

    # ---- jitter buffer hook ------------------------------------------------

    def add(self, packet):
        now = self.clock()
        if self.last_ssrc is not None and packet.ssrc != self.last_ssrc:
            # Reconnect / new offer / SSRC change cancels old timers, NACK and PLI.
            self.ssrc_resets += 1
            self._events['ssrc'] += 1
            self._metrics['ssrc'] += 1
            self._reset_epoch_state()
        self.last_ssrc = packet.ssrc
        if not self.codecs:
            self.codecs = self._codec_map_from_receiver()
        if self.codecs:
            family = self._family(self.codecs.get(packet.payload_type))
            if family is not None:
                self._codec_family = family

        if (self.awaiting_keyframe and self._recovery_seq_floor is not None
                and not uint16_gt(packet.sequence_number, self._recovery_seq_floor)):
            # A late packet of the abandoned epoch must not seed a new timeline.
            self.stale_skipped += 1
            return False, None

        if self.last_packet_at:
            self.packet_gap = max(self.packet_gap, now - self.last_packet_at)
        self.last_packet_at = now
        self._note_gap(packet.sequence_number, now)

        pli, frame = super().add(packet)

        if pli:
            self.overflow_recoveries += 1
        if frame is not None:
            self.completed += 1
            self.max_bytes = max(self.max_bytes, len(frame.data))
            if self.last_frame_at:
                self.frame_gap = max(self.frame_gap, now - self.last_frame_at)
            self.last_frame_at = now
            if self.awaiting_keyframe:
                if self._accepts_as_keyframe(frame):
                    self.awaiting_keyframe = False
                    self.recoveries += 1
                    self._events['recoveries'] += 1
                    self._metrics['recoveries'] += 1
                    self.recovery_epoch += 1
                    self._blocked_since = None
                    self._missing.clear()
                    self._recovery_seq_floor = None
                else:
                    # Dependent frames of a damaged epoch are never forwarded.
                    self.recovery_skipped += 1
                    frame = None
            else:
                self._note_parameter_sets(frame)

        if pli:
            # Keep aiortc's immediate NACK/PLI behaviour but never allow a PLI storm.
            if self._last_pli_at is None or now - self._last_pli_at >= self.pli_interval:
                self._last_pli_at = now
                self.pli_requests += 1
            else:
                pli = False
        return pli, frame

    def interval(self):
        now = self.clock()
        metrics = {
            'packetGapMs': max(self.packet_gap, now-self.last_packet_at if self.last_packet_at else 0)*1000,
            'assemblyGapMs': max(self.frame_gap, now-self.last_frame_at if self.last_frame_at else 0)*1000,
            'largestFrameKiB': self.max_bytes/1024,
            'bufferRecoveries': self.overflow_recoveries,
            'deadlineExpiries': self._metrics['expiries'],
            'pliRequests': self.pli_requests,
            'keyframeRecoveries': self._metrics['recoveries'],
            'recoverySkipped': self.recovery_skipped,
            'stalePacketSkips': self.stale_skipped,
            'ssrcResets': self._metrics['ssrc'],
            'awaitingKeyframe': self.awaiting_keyframe,
            'lossDeadlineMs': self.deadline*1000,
            'blockedMs': (max(0.0, now-self._blocked_since)*1000
                          if self._blocked_since is not None else 0.0),
        }
        self.packet_gap = self.frame_gap = 0.0
        self.max_bytes = self.overflow_recoveries = 0
        self._metrics = {'expiries': 0, 'recoveries': 0, 'ssrc': 0}
        return metrics


def install_video_buffers(peer, clock=time.monotonic):
    """Install one VideoBuffer per video receiver, after structural validation."""
    if aiortc.__version__ != '1.15.0':
        raise RuntimeError('AirFrame requires aiortc 1.15.0 for its video packet buffer')
    buffers = []
    for receiver in peer.getReceivers():
        if receiver.track is None or receiver.track.kind != 'video':
            continue
        old = getattr(receiver, _BUFFER_ATTRIBUTE, None)
        if type(old) is not JitterBuffer or old.capacity != 128:
            raise RuntimeError('Incompatible aiortc video buffer; update AirFrame transport integration')
        if not isinstance(getattr(receiver, _CODECS_ATTRIBUTE, None), dict):
            raise RuntimeError('Incompatible aiortc receiver codecs; update AirFrame transport integration')
        if not callable(getattr(receiver, '_send_rtcp_pli', None)):
            raise RuntimeError('Incompatible aiortc receiver PLI hook; update AirFrame transport integration')
        nack = getattr(receiver, _NACK_ATTRIBUTE, None)
        if nack is None or not hasattr(nack, 'missing') or not hasattr(nack, 'max_seq'):
            raise RuntimeError('Incompatible aiortc NACK state; update AirFrame transport integration')
        buffer = VideoBuffer(clock=clock)
        buffer.receiver = receiver
        setattr(receiver, _BUFFER_ATTRIBUTE, buffer)
        buffers.append(buffer)
    return buffers
