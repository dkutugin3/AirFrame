"""Bounded decoded-frame playout. All methods run on the receiver event loop.

PTS controls presentation, arrival time controls buffering. No encoded frame is
removed here. Buffered modes trade a visible, bounded delay for tolerance of bursts.
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


class Playout:
    MAX_BYTES = 128 * 1024 * 1024
    # mode: (base target delay, ceiling after lateness drift, queued frame limit).
    # The frame limit must exceed ceiling*fps with a margin, or steady-state pushes
    # evict the frame that has just become due before take() can show it.
    MODES = {'low': (.0, .0, 1), 'balanced': (.120, .200, 15), 'stable': (.250, .350, 24)}

    def __init__(self, mode='stable', fps=60):
        if mode not in self.MODES:
            raise ValueError('Unknown playout mode')
        self.frames = deque()
        self.bytes = self.skipped = self.underflows = self.rebuffers = 0
        self.mode, self.fps = mode, fps
        self.base_delay, self.max_delay, self.max_frames = self.MODES[mode]
        self.phase = None
        self.last_media = self.last_arrival = None
        self.last_output_media = None
        self.shape = None
        self.target = self.base_delay
        self.starved = False

    def reset(self, mode=None, fps=None):
        if mode is not None and mode not in self.MODES:
            raise ValueError('Unknown playout mode')
        self.frames.clear()
        self.bytes = 0
        self.mode = mode or self.mode
        self.fps = fps or self.fps
        self.base_delay, self.max_delay, self.max_frames = self.MODES[self.mode]
        self.phase = self.last_media = self.last_arrival = self.last_output_media = None
        self.shape = None
        self.target = self.base_delay
        self.starved = False

    def _remove(self):
        item = self.frames.popleft()
        self.bytes -= item.size
        return item

    def push(self, frame, now):
        pts, time_base = getattr(frame, 'pts', None), getattr(frame, 'time_base', None)
        media = float(pts * time_base) if pts is not None and time_base is not None else (
            0.0 if self.last_media is None else self.last_media + 1/self.fps)
        if not math.isfinite(media):
            self.skipped += 1
            return
        shape = (getattr(frame, 'width', 0), getattr(frame, 'height', 0))
        size = sum(getattr(p, 'buffer_size', 0) for p in getattr(frame, 'planes', ()))
        size = size or shape[0]*shape[1]*3//2
        if size > self.MAX_BYTES:
            self.skipped += 1
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
        while len(self.frames) > self.max_frames or self.bytes > self.MAX_BYTES:
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
                'playoutQueueMs': ((self.frames[-1].media-self.frames[0].media+1/self.fps)*1000
                                   if self.frames else 0)}
