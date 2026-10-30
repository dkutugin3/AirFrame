"""Bounded interval metrics and optional diagnostic event stream for the bench stand.

Plan §3 asks for limited interval aggregation through the observable pipeline
``assembly -> decode start -> decode end -> ready for output -> handoff to renderer
-> render complete`` (only stages the running integration can actually observe) and,
in diagnostics mode only, a detailed event stream written to a file.

Design constraints
------------------
* Bounded memory: each interval keeps a fixed-size window of the most recent
  samples (percentiles describe that retained window, not the whole history),
  pending per-frame state is capped and evicted oldest-first, and counters are
  plain integers.
* Deterministic tests: the clock is injected (``clock.time()``), so tests use a
  virtual clock and never depend on OS timer accuracy.
* Nothing is invented: missing stages are counted as ``missing`` at frame
  completion, out-of-order marking is supported (interval = end_ts - start_ts),
  backwards time is counted as ``negativeIntervals`` and is not aggregated.
* Invalid data is rejected: unknown event names and NaN/Infinity timestamps are
  counted and never enter the samples.
* File output: ``snapshot()``/``write_json()`` for the bounded aggregate, and
  ``DiagnosticEventWriter`` for the opt-in detailed JSONL stream (itself bounded by
  ``max_events`` so a stuck run cannot fill the disk).

What this does NOT measure
--------------------------
Timestamps come from whichever clock is injected. The stand can prove pipeline
interval behaviour on that clock; it cannot prove physical display presentation or
OBS capture, and it must not subtract unsynchronised phone/PC absolute clocks.
See docs/MEASUREMENT.md.
"""
from collections import deque
import json
import math
from pathlib import Path
import time

EVENT_NAMES = ('assembly', 'decode_start', 'decode_end',
               'output_ready', 'renderer_handoff', 'render_complete')
INTERVALS = {
    f'{start}_to_{end}': (start, end)
    for start, end in zip(EVENT_NAMES, EVENT_NAMES[1:])
}
TERMINAL_EVENT = EVENT_NAMES[-1]

_COUNTER_KEYS = ('invalidTimestamps', 'unknownEvents', 'negativeIntervals',
                 'missingIntervals', 'pendingEvicted')


def _nearest_rank(sorted_samples, percentile):
    """Nearest-rank percentile over an already sorted list; None when empty."""
    if not sorted_samples:
        return None
    rank = math.ceil(percentile / 100 * len(sorted_samples)) - 1
    return sorted_samples[max(0, min(rank, len(sorted_samples) - 1))]


class _MonotonicClock:
    def time(self):
        return time.monotonic()


class DiagnosticEventWriter:
    """Opt-in JSONL event stream. Writes are bounded by ``max_events`` if set."""

    def __init__(self, path, max_events=None):
        if max_events is not None and (type(max_events) is not int or max_events < 1):
            raise ValueError('max_events must be a positive int or None')
        self.path = Path(path)
        self.max_events = max_events
        self._file = self.path.open('w', encoding='utf-8')
        self._written = self._dropped = self._sequence = 0
        self._closed = False

    def write(self, name, ts, frame=None, pts=None):
        if self._closed:
            raise ValueError('diagnostic writer is closed')
        if self.max_events is not None and self._written >= self.max_events:
            self._dropped += 1
            return False
        record = {'seq': self._sequence, 'name': name, 'ts': float(ts),
                  'frame': frame, 'pts': pts}
        self._file.write(json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n')
        self._file.flush()
        self._sequence += 1
        self._written += 1
        return True

    def close(self):
        if not self._closed:
            self._file.close()
            self._closed = True
        return {'written': self._written, 'dropped': self._dropped, 'path': str(self.path)}


class IntervalMetrics:
    """Bounded interval aggregation with an injectable monotonic clock."""

    def __init__(self, clock=None, max_samples=4096, max_pending_frames=1024,
                 event_writer=None):
        if type(max_samples) is not int or max_samples < 1:
            raise ValueError('max_samples must be a positive int')
        if type(max_pending_frames) is not int or max_pending_frames < 1:
            raise ValueError('max_pending_frames must be a positive int')
        self._clock = clock if clock is not None else _MonotonicClock()
        self.max_samples = max_samples
        self.max_pending_frames = max_pending_frames
        self.event_writer = event_writer
        self._session = 1
        self._clear_state()

    def _clear_state(self):
        self._events = 0
        self._events_by_name = {}
        self._pending = {}          # frame -> {event name: ts}
        self._pending_order = deque()  # frames in arrival order for oldest-first eviction
        self._emitted = {}          # frame -> set(interval names already consumed)
        self._samples = {name: deque(maxlen=self.max_samples) for name in INTERVALS}
        self._counts = {name: 0 for name in INTERVALS}
        self._counters = {key: 0 for key in _COUNTER_KEYS}
        self._missing_by_interval = {}

    # -- recording -------------------------------------------------------
    def mark(self, name, frame=None, pts=None):
        """Record one pipeline event. Returns False when the event was rejected."""
        if name not in EVENT_NAMES:
            self._counters['unknownEvents'] += 1
            return False
        timestamp = self._clock.time()
        if (type(timestamp) not in (int, float)) or not math.isfinite(timestamp):
            self._counters['invalidTimestamps'] += 1
            return False
        timestamp = float(timestamp)
        self._events += 1
        self._events_by_name[name] = self._events_by_name.get(name, 0) + 1
        if self.event_writer is not None:
            self.event_writer.write(name, timestamp, frame=frame, pts=pts)
        if frame is None:
            return True
        entry = self._pending.get(frame)
        if entry is None:
            entry = {}
            self._pending[frame] = entry
            self._pending_order.append(frame)
            self._emitted[frame] = set()
            while len(self._pending) > self.max_pending_frames:
                self._evict_oldest()
        entry[name] = timestamp
        self._resolve(frame)
        return True

    def _evict_oldest(self):
        while self._pending_order:
            oldest = self._pending_order.popleft()
            if oldest in self._pending:
                self._pending.pop(oldest, None)
                self._emitted.pop(oldest, None)
                self._counters['pendingEvicted'] += 1
                return

    def _resolve(self, frame):
        entry = self._pending.get(frame)
        if entry is None:
            return
        emitted = self._emitted[frame]
        for interval_name, (start, end) in INTERVALS.items():
            if interval_name in emitted or start not in entry or end not in entry:
                continue
            delta = entry[end] - entry[start]
            emitted.add(interval_name)
            if delta < 0:
                self._counters['negativeIntervals'] += 1
                continue
            self._samples[interval_name].append(delta)
            self._counts[interval_name] += 1
        if TERMINAL_EVENT in entry:
            for interval_name, (start, end) in INTERVALS.items():
                if interval_name in emitted:
                    continue
                if (start in entry) != (end in entry):
                    self._counters['missingIntervals'] += 1
                    self._missing_by_interval[interval_name] = (
                        self._missing_by_interval.get(interval_name, 0) + 1)
            self._pending.pop(frame, None)
            self._emitted.pop(frame, None)

    # -- reporting -------------------------------------------------------
    def snapshot(self):
        """Bounded aggregate; percentiles are over the retained sample window."""
        intervals = {}
        for interval_name in INTERVALS:
            samples = sorted(self._samples[interval_name])
            retained = len(samples)
            count = self._counts[interval_name]
            if samples:
                p50 = _nearest_rank(samples, 50)
                p95 = _nearest_rank(samples, 95)
                p99 = _nearest_rank(samples, 99)
                intervals[interval_name] = {
                    'count': count,
                    'retained': retained,
                    'p50Ms': p50 * 1000,
                    'p95Ms': p95 * 1000,
                    'p99Ms': p99 * 1000,
                    'minMs': samples[0] * 1000,
                    'maxMs': samples[-1] * 1000,
                    'meanMs': sum(samples) / retained * 1000,
                }
            else:
                intervals[interval_name] = {
                    'count': count, 'retained': 0, 'p50Ms': None, 'p95Ms': None,
                    'p99Ms': None, 'minMs': None, 'maxMs': None, 'meanMs': None,
                }
        counters = dict(self._counters)
        counters['missingByInterval'] = dict(self._missing_by_interval)
        return {
            'session': self._session,
            'events': self._events,
            'eventsByName': dict(self._events_by_name),
            'intervals': intervals,
            'counters': counters,
            'pendingFrames': len(self._pending),
        }

    def reset(self):
        """Start a new measurement session; counters and bounded state are cleared."""
        self._session += 1
        self._clear_state()

    def write_json(self, path):
        Path(path).write_text(json.dumps(self.snapshot(), indent=2, allow_nan=False),
                              encoding='utf-8')
