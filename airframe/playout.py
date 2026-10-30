"""Bounded decoded-frame playout. All methods run on the receiver event loop.

PTS controls presentation, arrival time controls buffering. No encoded frame is
removed here. Buffered modes trade a visible, bounded delay for tolerance of bursts.

Capacity contract (plan stage 2):
- the frame reserve is computed from the mode's maximum temporal reserve and the
  session FPS: ``ceil(max_delay * fps) + 3`` (the +3 covers the push/take edge), so
  the saved 120 FPS profile gets 45 frames instead of the former fixed 24;
- the byte budget is checked against the real decoded plane buffers (stride
  included), not only ``width*height*1.5``;
- a combination that cannot fit the documented hard limit is refused explicitly with
  a safe 60 FPS suggestion instead of silently shrinking the reserve. Overflow while
  running still bounds memory.
"""
from collections import deque
from dataclasses import dataclass
import math


@dataclass
class PendingFrame:
    frame: object
    media: float
    arrived: float
    size: int


def decoded_frame_bytes(frame):
    """Bytes of a decoded frame with real strides, falling back to YUV420 size."""
    size = sum(getattr(plane, 'buffer_size', 0) for plane in getattr(frame, 'planes', ()))
    if size:
        return size
    return getattr(frame, 'width', 0) * getattr(frame, 'height', 0) * 3 // 2


class Playout:
    # Documented hard limit for the decoded-frame playout queue ONLY. Real
    # 2560x1440 YUV420 planes are 5,529,600 bytes each; the stable reserve at
    # 120 FPS is 45 frames = 237 MiB, so 256 MiB is the smallest limit that keeps
    # the saved 120 FPS profile playable. Every other queue (encoded packets,
    # decoded handoff, renderer mailbox) has its own separate budget.
    MAX_QUEUE_BYTES = 256 * 1024 * 1024
    # mode: (base target delay, ceiling after lateness drift). The frame reserve is
    # derived from the ceiling; mode 'low' presents immediately and holds at most 1.
    MODES = {'low': (0.0, 0.0), 'balanced': (.120, .200), 'stable': (.250, .350)}

    def __init__(self, mode='stable', fps=60):
        if mode not in self.MODES:
            raise ValueError('Unknown playout mode')
        self.frames = deque()
        self.bytes = self.skipped = self.underflows = self.rebuffers = 0
        self.incompatible = None
        self.incompatible_skipped = 0
        self.frame_bytes = self.reserved_bytes = 0
        self.mode, self.fps = mode, fps
        self._configure()
        self.phase = None
        self.last_media = self.last_arrival = None
        self.last_output_media = None
        self.shape = None
        self.target = self.base_delay
        self.starved = False

    def _configure(self):
        """Recreate the frame reserve and byte reservation for mode/fps."""
        self.base_delay, self.max_delay = self.MODES[self.mode]
        self.max_frames = (1 if self.mode == 'low'
                           else math.ceil(round(self.max_delay * self.fps, 6)) + 3)
        self.frame_bytes = 0
        self.reserved_bytes = 0

    def reset(self, mode=None, fps=None):
        if mode is not None and mode not in self.MODES:
            raise ValueError('Unknown playout mode')
        self.frames.clear()
        self.bytes = 0
        self.mode = mode or self.mode
        self.fps = fps or self.fps
        # A profile/resolution change explicitly recreates the budget and timeline;
        # counters stay cumulative for the session metrics.
        self._configure()
        self.incompatible = None
        self.incompatible_skipped = 0
        self.phase = self.last_media = self.last_arrival = self.last_output_media = None
        self.shape = None
        self.target = self.base_delay
        self.starved = False

    def _remove(self):
        item = self.frames.popleft()
        self.bytes -= item.size
        return item

    def _reserve(self, size):
        frames = 1 if self.mode == 'low' else self.max_frames
        return frames * size

    def _refuse(self, size):
        """Mark the combination incompatible: explicit state, safe FPS suggestion."""
        reserved = self._reserve(size)
        suggested = self._suggested_fps(size)
        self.incompatible = {
            'mode': self.mode,
            'fps': self.fps,
            'frameBytes': size,
            'reservedBytes': reserved,
            'limitBytes': self.MAX_QUEUE_BYTES,
            'suggestionFps': suggested,
            'message': (f"Буфер вывода несовместим: {self.fps:g} FPS требует "
                        f"{reserved / 1048576:.0f} МиБ ({self.max_frames} кадров), "
                        f"лимит {self.MAX_QUEUE_BYTES / 1048576:.0f} МиБ. "
                        f"Выберите {suggested} FPS."),
        }
        self.frames.clear()
        self.bytes = 0
        # The refused timeline cannot be scheduled; drop it without touching the
        # cumulative session counters. frame_bytes/reserved_bytes stay visible so
        # the required memory is not hidden from statistics.
        self.phase = self.last_media = self.last_arrival = self.last_output_media = None
        self.shape = None
        self.target = self.base_delay
        self.starved = False

    def _suggested_fps(self, size):
        for fps in (60, 30):
            frames = math.ceil(round(self.max_delay * fps, 6)) + 3
            if frames * size <= self.MAX_QUEUE_BYTES:
                return fps
        return 30

    def push(self, frame, now):
        pts, time_base = getattr(frame, 'pts', None), getattr(frame, 'time_base', None)
        media = float(pts * time_base) if pts is not None and time_base is not None else (
            0.0 if self.last_media is None else self.last_media + 1/self.fps)
        if not math.isfinite(media):
            self.skipped += 1
            return
        shape = (getattr(frame, 'width', 0), getattr(frame, 'height', 0))
        size = decoded_frame_bytes(frame)
        if size:
            self.frame_bytes = max(self.frame_bytes, size)
            self.reserved_bytes = self._reserve(self.frame_bytes)
            if self.reserved_bytes > self.MAX_QUEUE_BYTES:
                # Not enough memory for the full reserve: refuse the combination
                # explicitly instead of evicting every frame before output.
                if self.incompatible is None:
                    self._refuse(size)
                self.incompatible_skipped += 1
                return
        # A changed capture size, clock reset or discontinuity starts a new timeline.
        discontinuity = (self.last_media is not None and (
            media < self.last_media or media-self.last_media > 2
            or abs((now-self.last_arrival)-(media-self.last_media)) > 2))
        if self.phase is not None and (discontinuity or shape != self.shape):
            self.skipped += len(self.frames)
            self.reset()
            self.rebuffers += 1
        if self.last_media is not None and media == self.last_media:
            self.skipped += 1
            return
        if self.phase is None:
            self.phase = now + self.target - media
        elif self.mode != 'low' and self.phase + media > now + self.max_delay + .010:
            # A source clock running faster than wall time must not create an
            # ever-growing future schedule. Due frames will be skipped, not rushed.
            self.phase = now + self.target - media
            self.rebuffers += 1
        elif self.mode != 'low' and self.phase + media < now - .010:
            late = now - (self.phase + media)
            increase = min(late + .040, self.max_delay - self.target)
            if increase > 0:
                self.phase += increase
                self.target = min(self.max_delay, self.target+increase)
            elif late > self.max_delay:
                # Do not play seconds of stale video after a sustained interruption.
                self.skipped += len(self.frames)
                self.reset()
                self.rebuffers += 1
                self.phase = now + self.target - media
        self.shape = shape
        self.last_media, self.last_arrival = media, now
        self.frames.append(PendingFrame(frame, media, now, size))
        self.bytes += size
        while len(self.frames) > self.max_frames or self.bytes > self.MAX_QUEUE_BYTES:
            self._remove()
            self.skipped += 1

    def take(self, now):
        """Return (due frame, seconds until next deadline). Never rush a backlog."""
        if not self.frames:
            if (self.mode != 'low' and self.last_output_media is not None
                    and now > self.phase+self.last_output_media+1.5/self.fps
                    and not self.starved):
                self.underflows += 1
                self.starved = True
            return None, .020
        due = now if self.mode == 'low' else self.phase+self.frames[0].media
        if due > now + .000001:
            return None, due-now
        item = self._remove()
        # If conversion or the event loop was slow, select the most recent due frame.
        while self.frames and self.phase+self.frames[0].media <= now + .000001:
            item = self._remove()
            self.skipped += 1
        self.last_output_media = item.media
        self.starved = False
        return item, 0.0

    def metrics(self):
        return {'playoutMode': self.mode, 'playoutTargetMs': self.target*1000,
                'playoutQueuedFrames': len(self.frames), 'playoutQueuedMiB': self.bytes/1048576,
                'playoutUnderflows': self.underflows, 'playoutRebuffers': self.rebuffers,
                'playoutMaxFrames': self.max_frames,
                'playoutBudgetMiB': self.MAX_QUEUE_BYTES/1048576,
                'playoutReservedMiB': self.reserved_bytes/1048576,
                'playoutIncompatible': self.incompatible is not None,
                'playoutIncompatibleSkipped': self.incompatible_skipped,
                'playoutSuggestionFps': (self.incompatible['suggestionFps']
                                         if self.incompatible else None),
                'playoutQueueMs': ((self.frames[-1].media-self.frames[0].media+1/self.fps)*1000
                                   if self.frames else 0)}
