"""Playout contract: capacity from the real reserve, real decoded-frame bytes,
virtual-clock timing, and explicit refusal of combinations that do not fit.

All timing here runs on a virtual clock (TICK steps); no test depends on the
accuracy of an OS timer.
"""
from dataclasses import dataclass
from fractions import Fraction
import math
from types import SimpleNamespace
import unittest

import av

from airframe.playout import Playout

TICK = 1 / 2000
FPS_59_94 = 60000 / 1001
YUV420_BYTES_2560x1440 = 5529600   # PyAV planes: 3686400 + 921600 + 921600
YUV420_BYTES_1920x2560 = 7372800   # rotated 4:3: 4915200 + 1228800 + 1228800


@dataclass
class Frame:
    """Synthetic frame without planes; size falls back to width*height*1.5."""
    pts: int
    time_base: Fraction = Fraction(1, 60)
    width: int = 1920
    height: int = 886


def video_frame(width, height, pts, time_base=Fraction(1, 60)):
    """A real PyAV YUV420 frame: plane buffer sizes include the actual stride."""
    frame = av.VideoFrame(width, height, 'yuv420p')
    frame.pts = pts
    frame.time_base = time_base
    return frame


def run_playout(buffer, arrivals, duration, stop_after=None):
    """Advance a virtual clock, pushing arrivals and taking frames every TICK.

    `stop_after` ends the run once that many frames were output, so a trailing
    idle tail cannot count as a stream underflow.
    """
    arrivals = sorted(arrivals, key=lambda item: item[0])
    output = []
    index = 0
    for tick in range(int(duration / TICK) + 1):
        now = tick * TICK
        while index < len(arrivals) and arrivals[index][0] <= now:
            buffer.push(arrivals[index][1], now)
            index += 1
        item, _ = buffer.take(now)
        if item is not None:
            output.append((now, item.frame.pts))
            if stop_after is not None and len(output) >= stop_after:
                break
    return output


def continuous_arrivals(fps, count, time_base=None, width=640, height=360, delay=.005):
    time_base = time_base or Fraction(1, fps)
    return [(float(i * time_base) + delay, video_frame(width, height, i, time_base))
            for i in range(count)]


def paused_arrivals(fps, count, pause, start=.6, time_base=None, width=320, height=180):
    """Normal arrivals delayed by `pause` seconds starting at source time `start`."""
    time_base = time_base or Fraction(1, fps)
    arrivals = []
    for i in range(count):
        media = float(i * time_base)
        arrival = media + .005
        if start <= media < start + pause:
            arrival = start + pause + .005
        arrivals.append((arrival, video_frame(width, height, i, time_base)))
    return arrivals


def simulate(mode, fps=60, count=240):
    buffer = Playout(mode, fps=fps)
    time_base = Fraction(1, fps)
    arrivals = []
    for i in range(count):
        source = i / fps
        arrival = source + .005
        for start, end in ((.6, .825), (1.5, 1.65), (2.3, 2.525)):
            if start <= source < end:
                arrival = end + .005
        arrivals.append((arrival, Frame(i, time_base)))
    arrivals.sort(key=lambda item: item[0])
    output = []
    index = 0
    peak = 0
    # Virtual clock: no timing assertions depend on OS scheduling.
    for tick in range(int((count / fps + .6) * 2000)):
        now = tick / 2000
        while index < len(arrivals) and arrivals[index][0] <= now:
            buffer.push(arrivals[index][1], now)
            index += 1
        item, _ = buffer.take(now)
        if item is not None:
            output.append((now, item.frame.pts))
        peak = max(peak, len(buffer.frames))
    return buffer, output, peak


class PlayoutTests(unittest.TestCase):
    def test_225ms_network_pauses_and_catchup_bursts_are_smoothed(self):
        for fps in (30, 60):
            buffer, output, peak = simulate('stable', fps)
            self.assertEqual([i for _, i in output], list(range(240)))
            gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
            self.assertLessEqual(max(gaps), 1 / fps + .001)
            self.assertGreaterEqual(output[0][0], .250)
            self.assertEqual(buffer.skipped, 0)
            self.assertLessEqual(peak, buffer.max_frames)

    def test_stable_continuous_stream_never_evicts_due_frames(self):
        for fps in (30, 60, 120):
            with self.subTest(fps=fps):
                buffer = Playout('stable', fps=fps)
                count = 2 * fps
                arrivals = continuous_arrivals(fps, count)
                output = run_playout(buffer, arrivals, count / fps + .6, stop_after=count)
                self.assertEqual(buffer.max_frames,
                                 math.ceil(round(buffer.max_delay * fps, 6)) + 3)
                self.assertEqual([pts for _, pts in output], list(range(count)),
                                 buffer.metrics())
                self.assertEqual(buffer.skipped, 0, buffer.metrics())
                self.assertEqual(buffer.underflows, 0, buffer.metrics())

    def test_stable_smooths_100_150_225ms_bursts(self):
        for fps in (30, 60, 120):
            for pause in (.100, .150, .225):
                with self.subTest(fps=fps, pause=pause):
                    buffer = Playout('stable', fps=fps)
                    count = 3 * fps
                    arrivals = paused_arrivals(fps, count, pause)
                    output = run_playout(buffer, arrivals, count / fps + .8, stop_after=count)
                    self.assertEqual([pts for _, pts in output], list(range(count)),
                                     buffer.metrics())
                    self.assertEqual(buffer.skipped, 0, buffer.metrics())
                    self.assertEqual(buffer.underflows, 0, buffer.metrics())
                    gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
                    self.assertLessEqual(max(gaps), 1 / fps + 1.5 * TICK)
                    self.assertGreaterEqual(min(gaps), 1 / fps - 1.5 * TICK)

    def test_capacity_accounts_for_actual_frame_bytes(self):
        # Real 2560x1440 YUV420 planes (stride included): 24 frames = 126.6 MiB.
        frame = video_frame(2560, 1440, 0, Fraction(1, 60))
        self.assertEqual(sum(p.buffer_size for p in frame.planes), YUV420_BYTES_2560x1440)
        buffer = Playout('stable', fps=60)
        buffer.push(frame, .005)
        self.assertIsNone(buffer.incompatible)
        self.assertEqual(buffer.max_frames, 24)
        self.assertEqual(buffer.frame_bytes, YUV420_BYTES_2560x1440)
        self.assertEqual(buffer.reserved_bytes, 24 * YUV420_BYTES_2560x1440)
        self.assertLessEqual(buffer.reserved_bytes, buffer.MAX_QUEUE_BYTES)

        # Rotated 1440x2560 has identical plane bytes; the saved 120 FPS
        # combination still fits: 45 frames = 237.3 MiB <= 256 MiB.
        rotated = Playout('stable', fps=120)
        rotated.push(video_frame(1440, 2560, 0, Fraction(1, 120)), .005)
        self.assertIsNone(rotated.incompatible)
        self.assertEqual(rotated.max_frames, 45)
        self.assertEqual(rotated.reserved_bytes, 45 * YUV420_BYTES_2560x1440)
        self.assertLessEqual(rotated.reserved_bytes, rotated.MAX_QUEUE_BYTES)

        # A taller 4:3 rotated capture (1920x2560) would need 316 MiB at 120 FPS:
        # explicit refusal with a safe 60 FPS suggestion instead of silent shrink.
        tall = Playout('stable', fps=120)
        tall_frame = video_frame(1920, 2560, 0, Fraction(1, 120))
        tall.push(tall_frame, .005)
        self.assertIsNotNone(tall.incompatible)
        self.assertEqual(tall.incompatible['suggestionFps'], 60)
        self.assertIn('60 FPS', tall.incompatible['message'])
        self.assertIn('120 FPS', tall.incompatible['message'])
        self.assertEqual(tall.max_frames, 45)      # no silent shrink of the reserve
        self.assertEqual(len(tall.frames), 0)
        self.assertEqual(tall.bytes, 0)
        self.assertGreater(tall.incompatible_skipped, 0)
        tall.push(video_frame(1920, 2560, 1, Fraction(1, 120)), .014)
        self.assertEqual(len(tall.frames), 0)      # still refused, memory bounded
        # A profile change recreates the budget: 60 FPS uses only 169 MiB.
        tall.reset(fps=60)
        self.assertIsNone(tall.incompatible)
        tall.push(tall_frame, 1.0)
        self.assertIsNone(tall.incompatible)
        self.assertEqual(tall.max_frames, 24)
        self.assertEqual(len(tall.frames), 1)

    def test_pauses_beyond_budget_skip_stale_frames_without_fast_playback(self):
        fps = 60
        count = int(3.2 * fps)
        for pause_ms, minimum_skipped in ((400, 1), (1000, 36)):
            with self.subTest(pause_ms=pause_ms):
                buffer = Playout('stable', fps=fps)
                arrivals = paused_arrivals(fps, count, pause_ms / 1000)
                output = run_playout(buffer, arrivals, count / fps + 1.0)
                ids = [pts for _, pts in output]
                self.assertEqual(ids, sorted(set(ids)))       # unique, in order
                self.assertGreaterEqual(buffer.skipped, minimum_skipped,
                                        buffer.metrics())     # stale frames counted
                self.assertLessEqual(len(buffer.frames), buffer.max_frames)
                self.assertLessEqual(buffer.bytes, buffer.MAX_QUEUE_BYTES)
                gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
                self.assertTrue(gaps)
                # Never rush a backlog: no output interval below the source period.
                self.assertGreaterEqual(min(gaps), 1 / fps - 1.5 * TICK)
                # Playback resumes on fresh frames after the pause.
                self.assertGreaterEqual(max(ids), count - int(.5 * fps))

    def test_missing_source_frame_is_skipped_without_rushing(self):
        fps, count, missing = 60, 180, 60
        buffer = Playout('stable', fps=fps)
        time_base = Fraction(1, fps)
        arrivals = [(float(i * time_base) + .005, video_frame(320, 180, i, time_base))
                    for i in range(count) if i != missing]
        output = run_playout(buffer, arrivals, count / fps + .7, stop_after=count - 1)
        ids = [pts for _, pts in output]
        self.assertEqual(ids, [i for i in range(count) if i != missing])
        self.assertEqual(buffer.skipped, 0)
        self.assertLessEqual(buffer.underflows, 1)   # one real stall, not a storm
        gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
        self.assertGreaterEqual(min(gaps), 1 / fps - 1.5 * TICK)
        self.assertLessEqual(max(gaps), 2 / fps + 1.5 * TICK)

    def test_slow_conversion_skips_stale_frames_without_acceleration(self):
        fps, count = 60, 240
        buffer = Playout('stable', fps=fps)
        arrivals = sorted(continuous_arrivals(fps, count, width=320, height=180),
                          key=lambda item: item[0])
        output = []
        index = 0
        for tick in range(int((count / fps + .6) / TICK) + 1):
            now = tick * TICK
            while index < len(arrivals) and arrivals[index][0] <= now:
                buffer.push(arrivals[index][1], now)
                index += 1
            if tick % 80 == 0:                 # conversion blocks for 40 ms
                item, _ = buffer.take(now)
                if item is not None:
                    output.append((now, item.frame.pts))
        self.assertGreater(buffer.skipped, 100)
        ids = [pts for _, pts in output]
        self.assertEqual(ids, sorted(set(ids)))
        gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
        self.assertTrue(gaps)
        self.assertGreaterEqual(min(gaps), .040 - 1.5 * TICK)
        self.assertLessEqual(len(buffer.frames), buffer.max_frames)
        self.assertLessEqual(buffer.bytes, buffer.MAX_QUEUE_BYTES)

    def test_59_94_fps_continuous_stream_keeps_every_frame(self):
        fps = FPS_59_94
        time_base = Fraction(1001, 60000)
        buffer = Playout('stable', fps=fps)
        count = 240
        arrivals = continuous_arrivals(fps, count, time_base=time_base,
                                       width=320, height=180)
        output = run_playout(buffer, arrivals, count / fps + .8, stop_after=count)
        self.assertEqual([pts for _, pts in output], list(range(count)))
        self.assertEqual(buffer.skipped, 0)
        self.assertEqual(buffer.underflows, 0)
        self.assertEqual(buffer.max_frames,
                         math.ceil(round(buffer.max_delay * fps, 6)) + 3)
        gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
        self.assertLessEqual(max(gaps), 1 / fps + 1.5 * TICK)

    def test_fps_and_profile_change_recreate_budget_and_timeline(self):
        buffer = Playout('stable', fps=60)
        self.assertEqual(buffer.max_frames, 24)
        buffer.push(video_frame(320, 180, 0, Fraction(1, 60)), .005)
        self.assertTrue(buffer.frames)
        buffer.reset(fps=120)
        self.assertEqual(buffer.max_frames, 45)
        self.assertFalse(buffer.frames)
        self.assertIsNone(buffer.phase)
        self.assertEqual(buffer.bytes, 0)
        buffer.reset(mode='balanced')
        self.assertEqual(buffer.max_frames, 27)          # ceil(.200*120)+3
        self.assertEqual((buffer.base_delay, buffer.max_delay), (.120, .200))
        buffer.reset(mode='low')
        self.assertEqual(buffer.max_frames, 1)

    def test_byte_budget_bounds_overflow_and_rejects_oversized_frame(self):
        buffer = Playout()                               # stable 60 FPS: 24 frames
        for i in range(40):
            frame = Frame(i)
            frame.planes = [SimpleNamespace(buffer_size=8 * 1024 * 1024)]
            buffer.push(frame, i / 60)
            self.assertLessEqual(buffer.bytes, buffer.MAX_QUEUE_BYTES)
            self.assertLessEqual(len(buffer.frames), buffer.max_frames)
        self.assertGreater(buffer.skipped, 0)            # overflow still bounds memory
        oversized = Frame(40)
        oversized.planes = [SimpleNamespace(buffer_size=buffer.MAX_QUEUE_BYTES + 1)]
        buffer.push(oversized, 1.0)
        self.assertIsNotNone(buffer.incompatible)
        self.assertEqual(len(buffer.frames), 0)
        self.assertEqual(buffer.bytes, 0)

    def test_metrics_expose_queue_memory_and_budget(self):
        buffer = Playout('stable', fps=60)
        for i in range(3):
            buffer.push(video_frame(2560, 1440, i, Fraction(1, 60)), .005 + i / 60)
        metrics = buffer.metrics()
        self.assertEqual(metrics['playoutMaxFrames'], 24)
        self.assertEqual(metrics['playoutBudgetMiB'], 256)
        self.assertAlmostEqual(metrics['playoutReservedMiB'],
                               24 * YUV420_BYTES_2560x1440 / 1048576, places=2)
        self.assertEqual(metrics['playoutQueuedFrames'], 3)
        self.assertAlmostEqual(metrics['playoutQueuedMiB'],
                               3 * YUV420_BYTES_2560x1440 / 1048576, places=2)
        self.assertFalse(metrics['playoutIncompatible'])
        self.assertIsNone(metrics['playoutSuggestionFps'])

    def test_balanced_mode_buffers_half_of_stable(self):
        for fps in (30, 60):
            buffer, output, peak = simulate('balanced', fps)
            frames = [i for _, i in output]
            self.assertEqual(frames, sorted(set(frames)))
            self.assertGreaterEqual(output[0][0], .120)
            self.assertLess(output[0][0], .250)
            self.assertLessEqual(peak, buffer.max_frames)
            self.assertLessEqual(buffer.target, .200)
            # 225 ms pauses exceed the reserve: trimmed, but still visible.
            gaps = [b[0] - a[0] for a, b in zip(output, output[1:])]
            self.assertLessEqual(max(gaps), .150)
            self.assertGreaterEqual(len(frames), 238)

    def test_low_latency_remains_immediate_and_exposes_network_pause(self):
        _, output, peak = simulate('low')
        self.assertLess(output[0][0], .010)
        self.assertLessEqual(peak, 1)
        self.assertGreater(max(b[0] - a[0] for a, b in zip(output, output[1:])), .200)

    def test_slow_consumer_skips_late_frames_without_rushing_a_backlog(self):
        buffer = Playout()
        for i in range(100):
            buffer.push(Frame(i), i / 60)
        self.assertLessEqual(len(buffer.frames), 24)
        self.assertLessEqual(buffer.bytes, buffer.MAX_QUEUE_BYTES)
        item, _ = buffer.take(1.7)
        self.assertIsNotNone(item)
        next_item, delay = buffer.take(1.7)
        self.assertIsNone(next_item)
        self.assertGreater(delay, 0)
        self.assertGreater(buffer.skipped, 70)

    def test_clock_reset_rotation_and_mode_switch_clear_old_frames(self):
        buffer = Playout()
        buffer.push(Frame(100), 0)
        buffer.push(Frame(101), .017)
        buffer.push(Frame(0), .034)
        self.assertEqual([p.frame.pts for p in buffer.frames], [0])
        buffer.push(Frame(1, width=886, height=1920), .05)
        self.assertEqual(len(buffer.frames), 1)
        self.assertEqual(buffer.rebuffers, 2)
        buffer.reset(mode='low')
        self.assertEqual(buffer.bytes, 0)
        buffer.push(Frame(2), .06)
        self.assertIsNotNone(buffer.take(.06)[0])

    def test_underflow_is_counted_once_and_adaptation_has_a_ceiling(self):
        buffer = Playout()
        buffer.push(Frame(0), 0)
        self.assertIsNotNone(buffer.take(.25)[0])
        for i in range(30):
            buffer.take(.3 + i * .01)
        self.assertEqual(buffer.underflows, 1)
        buffer.push(Frame(1), .5)
        self.assertLessEqual(buffer.target, .350)
        self.assertIsNotNone(buffer.take(.6)[0])
        buffer.push(Frame(2), 1)
        self.assertLessEqual(buffer.target, .350)
        self.assertGreater(buffer.rebuffers, 0)

    def test_source_clock_drift_does_not_accumulate_unbounded_delay(self):
        buffer = Playout()
        for i in range(10000):
            now = i / 61
            buffer.push(Frame(i), now)
            buffer.take(now)
            if buffer.frames:
                self.assertLessEqual(buffer.phase + buffer.frames[-1].media - now,
                                     buffer.max_delay + .011)
            self.assertLessEqual(len(buffer.frames), buffer.max_frames)
        self.assertGreater(buffer.rebuffers, 0)


if __name__ == '__main__':
    unittest.main()
