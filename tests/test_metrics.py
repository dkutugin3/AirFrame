"""Headless contracts for the bounded interval metrics in scripts/bench_metrics.py.

Plan §3 requires limited interval aggregation (capture -> decode -> ready -> handoff
-> render complete) plus an optional detailed diagnostic event stream written to a
file. These tests pin the behaviour before the stand is used to judge stage work:
virtual clock, first sample, session/counter reset, equal timestamps, missing values,
NaN/Infinity rejection, bounded memory and the diagnostic file.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bench_metrics import (
    EVENT_NAMES,
    INTERVALS,
    DiagnosticEventWriter,
    IntervalMetrics,
)


class FakeClock:
    """Virtual clock: tests set absolute monotonic time; no sleeps, no OS timers."""

    def __init__(self, start=0.0):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def interval(snapshot, name):
    return snapshot['intervals'][name]


def mark_interval(metrics, clock, frame, start, end, milliseconds):
    metrics.mark(start, frame=frame)
    clock.advance(milliseconds / 1000)
    metrics.mark(end, frame=frame)


class FirstSampleTests(unittest.TestCase):
    def test_first_interval_sample_is_reported(self):
        clock, metrics = FakeClock(), None
        metrics = IntervalMetrics(clock=clock)
        metrics.mark('assembly', frame=1)
        clock.advance(.012)
        metrics.mark('decode_start', frame=1)
        snapshot = metrics.snapshot()
        stats = interval(snapshot, 'assembly_to_decode_start')
        self.assertEqual(stats['count'], 1)
        self.assertEqual(stats['retained'], 1)
        self.assertAlmostEqual(stats['p50Ms'], 12.0, places=6)
        self.assertAlmostEqual(stats['p95Ms'], 12.0, places=6)
        self.assertAlmostEqual(stats['p99Ms'], 12.0, places=6)
        self.assertAlmostEqual(stats['maxMs'], 12.0, places=6)
        self.assertAlmostEqual(stats['minMs'], 12.0, places=6)
        self.assertAlmostEqual(stats['meanMs'], 12.0, places=6)
        # Intervals whose start event never arrived are not invented.
        self.assertEqual(interval(snapshot, 'decode_start_to_decode_end')['count'], 0)
        self.assertIsNone(interval(snapshot, 'decode_start_to_decode_end')['p50Ms'])
        self.assertEqual(snapshot['events'], 2)
        self.assertEqual(snapshot['eventsByName'], {'assembly': 1, 'decode_start': 1})

    def test_percentiles_follow_nearest_rank_over_retained_samples(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock, max_samples=16)
        for frame in range(100):
            mark_interval(metrics, clock, frame, 'assembly', 'decode_start', frame + 1)
        stats = interval(metrics.snapshot(), 'assembly_to_decode_start')
        self.assertEqual(stats['count'], 100)
        self.assertEqual(stats['retained'], 16)
        self.assertAlmostEqual(stats['maxMs'], 100.0, places=6)
        self.assertAlmostEqual(stats['minMs'], 85.0, places=6)
        # Nearest-rank over the retained window (85..100), never a fabricated sample.
        self.assertAlmostEqual(stats['p50Ms'], 92.0, places=6)
        self.assertAlmostEqual(stats['p95Ms'], 100.0, places=6)
        self.assertAlmostEqual(stats['p99Ms'], 100.0, places=6)


class SessionTests(unittest.TestCase):
    def test_reset_starts_a_new_session_and_zeroes_bounded_state(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock)
        mark_interval(metrics, clock, 1, 'assembly', 'decode_start', 5)
        metrics.mark('assembly', frame=2)  # deliberately left pending
        before = metrics.snapshot()
        self.assertEqual(before['session'], 1)
        metrics.reset()
        after = metrics.snapshot()
        self.assertEqual(after['session'], 2)
        self.assertEqual(after['events'], 0)
        self.assertEqual(after['eventsByName'], {})
        self.assertEqual(after['pendingFrames'], 0)
        for name in INTERVALS:
            self.assertEqual(after['intervals'][name]['count'], 0)
            self.assertIsNone(after['intervals'][name]['p50Ms'])
        self.assertEqual(after['counters']['invalidTimestamps'], 0)
        self.assertEqual(after['counters']['unknownEvents'], 0)
        self.assertEqual(after['counters']['negativeIntervals'], 0)
        self.assertEqual(after['counters']['missingIntervals'], 0)
        self.assertEqual(after['counters']['pendingEvicted'], 0)
        self.assertEqual(after['counters']['missingByInterval'], {})
        mark_interval(metrics, clock, 3, 'assembly', 'decode_start', 7)
        stats = interval(metrics.snapshot(), 'assembly_to_decode_start')
        self.assertEqual(stats['count'], 1)
        self.assertAlmostEqual(stats['maxMs'], 7.0, places=6)

    def test_equal_timestamps_record_a_zero_interval(self):
        clock = FakeClock(5.0)
        metrics = IntervalMetrics(clock=clock)
        metrics.mark('assembly', frame=7)
        metrics.mark('decode_start', frame=7)
        stats = interval(metrics.snapshot(), 'assembly_to_decode_start')
        self.assertEqual(stats['count'], 1)
        self.assertAlmostEqual(stats['p50Ms'], 0.0, places=6)


class MissingAndInvalidTests(unittest.TestCase):
    def test_incomplete_frames_are_counted_as_missing_at_completion(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock)
        metrics.mark('assembly', frame=1)
        clock.advance(.010)
        metrics.mark('decode_start', frame=1)
        clock.advance(.040)
        metrics.mark('render_complete', frame=1)
        snapshot = metrics.snapshot()
        self.assertEqual(interval(snapshot, 'assembly_to_decode_start')['count'], 1)
        missing = snapshot['counters']['missingByInterval']
        self.assertEqual(missing['decode_start_to_decode_end'], 1)
        self.assertEqual(missing['renderer_handoff_to_render_complete'], 1)
        self.assertEqual(snapshot['counters']['missingIntervals'], 2)
        self.assertEqual(snapshot['pendingFrames'], 0)

    def test_nan_infinity_and_unknown_events_are_rejected(self):
        class BadClock(FakeClock):
            def __init__(self):
                super().__init__()
                self.values = [float('nan'), float('inf'), float('-inf'), 1.0]

            def time(self):
                return self.values.pop(0)

        metrics = IntervalMetrics(clock=BadClock())
        self.assertFalse(metrics.mark('assembly', frame=1))
        self.assertFalse(metrics.mark('assembly', frame=1))
        self.assertFalse(metrics.mark('assembly', frame=1))
        self.assertFalse(metrics.mark('not_a_stage', frame=1))
        self.assertTrue(metrics.mark('assembly', frame=1))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['events'], 1)
        self.assertEqual(snapshot['counters']['invalidTimestamps'], 3)
        self.assertEqual(snapshot['counters']['unknownEvents'], 1)

    def test_going_backwards_in_time_is_counted_not_aggregated(self):
        clock = FakeClock(5.0)
        metrics = IntervalMetrics(clock=clock)
        metrics.mark('assembly', frame=1)
        clock.now = 4.0
        metrics.mark('decode_start', frame=1)
        snapshot = metrics.snapshot()
        self.assertEqual(interval(snapshot, 'assembly_to_decode_start')['count'], 0)
        self.assertEqual(snapshot['counters']['negativeIntervals'], 1)

    def test_interval_pairing_does_not_depend_on_mark_order(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock)
        clock.now = .010
        metrics.mark('decode_start', frame=3)
        clock.now = 0.0
        metrics.mark('assembly', frame=3)
        stats = interval(metrics.snapshot(), 'assembly_to_decode_start')
        self.assertEqual(stats['count'], 1)
        self.assertAlmostEqual(stats['p50Ms'], 10.0, places=6)


class BoundedMemoryTests(unittest.TestCase):
    def test_pending_frames_are_bounded_when_later_stages_never_arrive(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock, max_pending_frames=8)
        for frame in range(100):
            metrics.mark('assembly', frame=frame)
        snapshot = metrics.snapshot()
        self.assertLessEqual(snapshot['pendingFrames'], 8)
        self.assertGreaterEqual(snapshot['counters']['pendingEvicted'], 92)
        self.assertEqual(snapshot['events'], 100)

    def test_stale_samples_do_not_grow_memory(self):
        clock = FakeClock()
        metrics = IntervalMetrics(clock=clock, max_samples=32)
        for frame in range(5000):
            mark_interval(metrics, clock, frame, 'assembly', 'decode_start', 1)
        snapshot = metrics.snapshot()
        self.assertEqual(interval(snapshot, 'assembly_to_decode_start')['count'], 5000)
        self.assertLessEqual(interval(snapshot, 'assembly_to_decode_start')['retained'], 32)


class FileOutputTests(unittest.TestCase):
    def test_diagnostic_event_stream_is_opt_in_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            clock = FakeClock()
            writer = DiagnosticEventWriter(path, max_events=5)
            metrics = IntervalMetrics(clock=clock, event_writer=writer)
            for frame in range(8):
                metrics.mark('assembly', frame=frame, pts=frame * 1500)
            writer.close()
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 5)
            for index, line in enumerate(lines):
                record = json.loads(line)
                self.assertEqual(record['seq'], index)
                self.assertEqual(record['name'], 'assembly')
                self.assertEqual(record['frame'], index)
                self.assertEqual(record['pts'], index * 1500)
            self.assertEqual(writer.close()['written'], 5)
            self.assertEqual(writer.close()['dropped'], 3)

    def test_snapshot_file_output_is_valid_json_without_non_finite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'metrics.json'
            clock = FakeClock()
            metrics = IntervalMetrics(clock=clock)
            metrics.mark('decode_start', frame=1)
            clock.advance(.025)
            metrics.mark('decode_end', frame=1)
            metrics.write_json(path)
            text = path.read_text()
            self.assertNotIn('NaN', text)
            self.assertNotIn('Infinity', text)
            restored = json.loads(text)
            self.assertEqual(restored['session'], 1)
            self.assertEqual(restored['intervals']['decode_start_to_decode_end']['count'], 1)
            self.assertEqual(restored['intervals']['decode_start_to_decode_end']['maxMs'], 25.0)
            self.assertEqual(set(restored['intervals']), set(INTERVALS))

    def test_event_names_and_intervals_match_the_plan_chain(self):
        self.assertEqual(EVENT_NAMES, ('assembly', 'decode_start', 'decode_end',
                                       'output_ready', 'renderer_handoff', 'render_complete'))
        pairs = [(start, end) for start, end in INTERVALS.values()]
        self.assertEqual(pairs, list(zip(EVENT_NAMES, EVENT_NAMES[1:])))


if __name__ == '__main__':
    unittest.main()
