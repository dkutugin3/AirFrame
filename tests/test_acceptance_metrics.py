"""Headless contracts for the final-acceptance harness helpers (plan §13.B/§13.C).

These tests run in the Docker suite (no Qt, no GPU, no screen capture) and pin the
*analysis* half of the acceptance harness: extracting frame IDs from a recorded
container, coverage/interval/hold statistics, the fail-closed evidence check, PSNR/SSIM,
colour-matrix-range patch checks with pre-registered tolerances, and PNG crop output.

The native capture driver (scripts/acceptance-60fps.py) is deliberately not exercised
here; it is documented as native-only in docs/ACCEPTANCE.md because it needs a real
window, Screen Recording permission and a recorder.
"""
import json
import math
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.acceptance_metrics import (
    CAPTURE_EVIDENCE_MIN_FRAMES,
    CROP_REGIONS,
    INTERVAL_HOLD_MS,
    TOLERANCES,
    TOLERANCES_VERSION,
    align_frames_by_id,
    capture_evidence_error,
    colour_report,
    extract_capture_records,
    percentiles_ms,
    psnr,
    quality_report,
    resize_luma,
    save_crops,
    ssim,
    summarize_capture,
    write_png_gray,
    write_png_rgb,
)
from scripts.bench_source import BenchScene, COLOUR_BARS, decode_frame_id

SCENE = BenchScene(640, 360, 60)


def packed_plane(frame, width):
    raw = bytes(frame.planes[0])
    stride = frame.planes[0].line_size
    height = frame.height
    if stride == width:
        return raw[:width * height]
    return b''.join(raw[row * stride:row * stride + width] for row in range(height))


def copy_frame(frame):
    duplicate = av.VideoFrame(frame.width, frame.height, frame.format.name)
    for plane, source in zip(duplicate.planes, frame.planes):
        plane.update(bytes(source))
    return duplicate


def records_for(ids, step=1 / 60):
    return [{'id': frame_id, 'ptsSeconds': index * step}
            for index, frame_id in enumerate(ids)]


def encode_fixture(path, frames, fps=60):
    container = av.open(str(path), 'w', format='mp4')
    try:
        stream = container.add_stream('libx264', rate=fps)
        stream.width, stream.height = frames[0].width, frames[0].height
        stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, 90000)
        stream.options = {'crf': '18', 'preset': 'veryfast', 'bframes': '0',
                          'g': str(fps * 2), 'tune': 'zerolatency'}
        for index, frame in enumerate(frames):
            if frame.pts is None:
                frame.pts, frame.time_base = index * 1500, Fraction(1, 90000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        stats = percentiles_ms(list(range(1, 101)))
        self.assertEqual(stats['count'], 100)
        self.assertEqual(stats['p50Ms'], 50.0)
        self.assertEqual(stats['p95Ms'], 95.0)
        self.assertEqual(stats['p99Ms'], 99.0)
        self.assertEqual(stats['maxMs'], 100.0)
        self.assertEqual(stats['minMs'], 1.0)
        self.assertAlmostEqual(stats['meanMs'], 50.5, places=6)

    def test_empty_percentiles_are_none_not_zero(self):
        stats = percentiles_ms([])
        self.assertEqual(stats['count'], 0)
        for key in ('p50Ms', 'p95Ms', 'p99Ms', 'minMs', 'maxMs', 'meanMs'):
            self.assertIsNone(stats[key])


class CaptureSummaryTests(unittest.TestCase):
    def test_expected_vs_observed_unique_ids(self):
        summary = summarize_capture(records_for(range(120)), source_fps=60,
                                    nominal_seconds=2.0)
        self.assertEqual(summary['framesCaptured'], 120)
        self.assertEqual(summary['framesWithId'], 120)
        self.assertEqual(summary['uniqueIds'], 120)
        self.assertEqual(summary['expectedUniqueIds'], 120)
        self.assertAlmostEqual(summary['observedUniquePercent'], 100.0, places=6)
        self.assertEqual(summary['repeats'], 0)
        self.assertEqual(summary['dropsWithinObserved'], 0)

    def test_repeats_drops_and_frames_without_id_are_counted_separately(self):
        records = records_for([0, 0, 1, 2, 2, 2, 5, 6, None, 7])
        summary = summarize_capture(records, source_fps=60, nominal_seconds=10 / 60)
        self.assertEqual(summary['framesCaptured'], 10)
        self.assertEqual(summary['framesWithoutId'], 1)
        self.assertEqual(summary['uniqueIds'], 6)
        self.assertEqual(summary['repeats'], 3)   # 0==0 and 2==2 twice
        self.assertEqual(summary['dropsWithinObserved'], 2)  # ids 3 and 4 missing
        self.assertEqual(summary['expectedUniqueIds'], 10)
        self.assertAlmostEqual(summary['observedUniquePercent'], 60.0, places=6)

    def test_inter_new_frame_intervals_use_first_appearance_times(self):
        records = [{'id': 0, 'ptsSeconds': 0.0},
                   {'id': 1, 'ptsSeconds': .020},
                   {'id': 1, 'ptsSeconds': .030},
                   {'id': 2, 'ptsSeconds': .050},
                   {'id': 3, 'ptsSeconds': .080}]
        summary = summarize_capture(records, source_fps=60, nominal_seconds=1.0)
        stats = summary['intervalStatsMs']
        self.assertEqual(stats['count'], 3)
        self.assertAlmostEqual(stats['p50Ms'], 30.0, places=6)
        self.assertAlmostEqual(stats['p99Ms'], 30.0, places=6)
        self.assertAlmostEqual(stats['maxMs'], 30.0, places=6)

    def test_unexplained_image_holds_over_threshold(self):
        records = records_for([0, 0, 0, 0, 0, 1, 1])
        summary = summarize_capture(records, source_fps=60, nominal_seconds=6 / 60)
        holds = summary['holds']
        self.assertEqual(holds['count'], 2)       # runs of 0s and 1s
        self.assertEqual(holds['overThreshold'], 1)  # 4 intervals = 66.7 ms > 50 ms
        self.assertAlmostEqual(holds['maxMs'], 4 / 60 * 1000, places=3)
        self.assertGreater(holds['maxMs'], INTERVAL_HOLD_MS)

    def test_identical_hold_exactly_at_threshold_is_not_unexplained(self):
        records = records_for([0, 0, 0, 0, 1])  # 3 intervals = 50.0 ms exactly
        summary = summarize_capture(records, source_fps=60, nominal_seconds=5 / 60)
        self.assertEqual(summary['holds']['overThreshold'], 0)


class FailClosedEvidenceTests(unittest.TestCase):
    def test_empty_or_short_capture_fails_closed(self):
        summary = summarize_capture(records_for(range(3)), source_fps=60, nominal_seconds=.05)
        reason = capture_evidence_error(summary)
        self.assertIsNotNone(reason)
        self.assertIn('too few', reason)

    def test_capture_without_decodable_ids_fails_closed(self):
        summary = summarize_capture([{'id': None, 'ptsSeconds': index / 60}
                                     for index in range(200)], source_fps=60,
                                    nominal_seconds=3.3)
        reason = capture_evidence_error(summary)
        self.assertIsNotNone(reason)
        self.assertIn('no decodable frame IDs', reason)

    def test_valid_capture_has_no_error(self):
        summary = summarize_capture(records_for(range(120)), source_fps=60,
                                    nominal_seconds=2.0)
        self.assertIsNone(capture_evidence_error(summary))


class ExtractFromContainerTests(unittest.TestCase):
    def test_ids_are_extracted_from_a_locally_encoded_mp4_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.mp4'
            encode_fixture(path, [SCENE.render(frame_id) for frame_id in range(20)])
            result = extract_capture_records(path)
            ids = [record['id'] for record in result['records']]
            self.assertEqual(ids, list(range(20)))
            self.assertEqual(result['video']['codec'], 'h264')
            self.assertEqual((result['video']['width'], result['video']['height']), (640, 360))
            self.assertGreater(result['spanSeconds'], .2)

    def test_ids_survive_a_scaled_capture_style_recording(self):
        """OBS/ffmpeg output is usually scaled and re-encoded; IDs must still decode."""
        frames = [SCENE.render(frame_id).reformat(width=960, height=540, format='yuv420p')
                  for frame_id in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scaled.mp4'
            encode_fixture(path, frames)
            result = extract_capture_records(path)
            self.assertEqual([record['id'] for record in result['records']], list(range(10)))

    def test_black_recording_yields_no_ids_and_fails_closed(self):
        frames = []
        for _ in range(20):
            frame = av.VideoFrame(640, 360, 'yuv420p')
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frames.append(frame)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'black.mp4'
            encode_fixture(path, frames)
            result = extract_capture_records(path)
            self.assertTrue(all(record['id'] is None for record in result['records']))
            summary = summarize_capture(
                [{'id': record['id'], 'ptsSeconds': record['ptsSeconds']}
                 for record in result['records']], source_fps=60, nominal_seconds=1)
            self.assertIsNotNone(capture_evidence_error(summary))


class MetricMathTests(unittest.TestCase):
    def frame_luma(self, frame_id=7):
        return SCENE.render_luma(frame_id), 640, 360

    def test_identical_frames_give_infinite_psnr_and_unit_ssim(self):
        luma, width, height = self.frame_luma()
        self.assertEqual(psnr(luma, luma, width, height), math.inf)
        self.assertAlmostEqual(ssim(luma, luma, width, height), 1.0, places=6)

    def test_controlled_noise_lowers_psnr_as_expected(self):
        luma, width, height = self.frame_luma()
        degraded = bytes(min(255, value + 5) for value in luma)
        value = psnr(luma, degraded, width, height)
        self.assertGreater(value, 32)
        self.assertLess(value, 36)
        self.assertLess(ssim(luma, degraded, width, height), 1.0)

    def test_constant_frames_do_not_crash_ssim(self):
        flat = bytes([128]) * (64 * 36)
        self.assertAlmostEqual(ssim(flat, flat, 64, 36), 1.0, places=6)

    def test_different_sizes_are_rejected(self):
        luma, width, height = self.frame_luma()
        with self.assertRaises(ValueError):
            psnr(luma, luma[:len(luma) // 2], width, height)

    def test_resize_luma_keeps_the_machine_readable_id(self):
        luma, width, height = self.frame_luma(37)
        resized = resize_luma(luma, width, height, 320, 180)
        self.assertEqual(len(resized), 320 * 180)
        from scripts.bench_source import decode_luma_frame_id
        self.assertEqual(decode_luma_frame_id(resized, 320, 180), 37)


class ColourAndRangeTests(unittest.TestCase):
    def rgb_frame(self, frame_id=9):
        frame = SCENE.render(frame_id)
        return frame, frame.reformat(format='rgb24'), 640, 360

    def test_identical_frame_passes_colour_checks(self):
        _, rgb, width, height = self.rgb_frame()
        pixels = bytes(rgb.planes[0])
        report = colour_report(pixels, pixels, width, height, TOLERANCES['coding'])
        self.assertTrue(report['passed'])
        self.assertFalse(report['uvSwapSuspected'])
        self.assertEqual(report['maxPatchRgbDelta'], 0)

    def test_swapped_u_v_planes_are_suspected(self):
        frame, rgb, width, height = self.rgb_frame()
        swapped = copy_frame(frame)
        swapped.planes[1].update(bytes(frame.planes[2]))
        swapped.planes[2].update(bytes(frame.planes[1]))
        rgb_swapped = swapped.reformat(format='rgb24')
        report = colour_report(bytes(rgb.planes[0]), bytes(rgb_swapped.planes[0]),
                               width, height, TOLERANCES['coding'])
        self.assertTrue(report['uvSwapSuspected'])
        self.assertFalse(report['passed'])
        self.assertGreater(report['maxPatchRgbDelta'], TOLERANCES['coding']['maxPatchRgbDelta'])

    def test_raised_blacks_and_clipped_whites_are_detected(self):
        frame, rgb, width, height = self.rgb_frame()
        # Simulate a range/matrix mismatch: lift blacks and compress the luma range.
        captured = copy_frame(frame)
        luma = bytearray(bytes(captured.planes[0]))
        for index in range(len(luma)):
            luma[index] = min(255, int(luma[index] * .75 + 40))
        captured.planes[0].update(bytes(luma))
        rgb_lifted = captured.reformat(format='rgb24')
        report = colour_report(bytes(rgb.planes[0]), bytes(rgb_lifted.planes[0]),
                               width, height, TOLERANCES['coding'])
        self.assertFalse(report['passed'])
        self.assertGreater(report['blackLevel'], report['referenceLevels']['blackLevel'])


class PngCropTests(unittest.TestCase):
    def test_gray_png_roundtrips_through_pyav(self):
        width, height = 16, 8
        luma = bytes((x * 16 + y * 2) % 256 for y in range(height) for x in range(width))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gray.png'
            write_png_gray(path, width, height, luma)
            container = av.open(str(path))
            frame = next(container.decode(video=0))
            container.close()
            gray = frame if frame.format.name == 'gray' else frame.reformat(format='gray')
            self.assertEqual((gray.width, gray.height), (width, height))
            self.assertEqual(packed_plane(gray, width), luma)

    def test_rgb_png_roundtrips_through_pyav(self):
        width, height = 8, 4
        rgb = bytes((index * 7) % 256 for index in range(width * height * 3))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgb.png'
            write_png_rgb(path, width, height, rgb)
            container = av.open(str(path))
            frame = next(container.decode(video=0))
            container.close()
            self.assertEqual((frame.width, frame.height), (width, height))
            decoded = frame.reformat(format='rgb24')
            self.assertEqual(packed_plane(decoded, width * 3), rgb)

    def test_identical_crops_are_saved_for_reference_and_capture(self):
        reference = SCENE.render(5).reformat(format='rgb24')
        captured = SCENE.render(6).reformat(format='rgb24')
        with tempfile.TemporaryDirectory() as directory:
            paths = save_crops(directory, bytes(reference.planes[0]),
                               bytes(captured.planes[0]), 640, 360)
            self.assertEqual(set(paths), {'fine-text', 'colour-bars', 'gradient', 'motion'})
            files = list(Path(directory).glob('*.png'))
            self.assertEqual(len(files), 8)  # reference + captured crop for each region
            for name, (y0, y1, x0, x1) in CROP_REGIONS.items():
                for side in ('reference', 'captured'):
                    path = Path(directory) / f'{name}-{side}.png'
                    self.assertTrue(path.is_file(), path)
                    container = av.open(str(path))
                    frame = next(container.decode(video=0))
                    container.close()
                    expected_width = max(1, int(x1 * 640) - int(x0 * 640))
                    expected_height = max(1, int(y1 * 360) - int(y0 * 360))
                    self.assertEqual((frame.width, frame.height),
                                     (expected_width, expected_height))


class QualityReportTests(unittest.TestCase):
    def pairs(self, degraded=False):
        pairs = []
        for frame_id in (3, 4):
            reference = SCENE.render(frame_id)
            captured = SCENE.render(frame_id)
            if degraded:
                broken = av.VideoFrame(640, 360, 'yuv420p')
                for plane in broken.planes:
                    plane.update(bytes(plane.buffer_size))
                captured = broken
            pairs.append((frame_id, reference, captured))
        return pairs

    def test_tolerances_are_registered_before_the_comparison(self):
        report = quality_report(self.pairs(), category='coding', analysis_size=(320, 180))
        self.assertEqual(report['tolerancesVersion'], TOLERANCES_VERSION)
        self.assertEqual(report['tolerances'], TOLERANCES['coding'])
        self.assertTrue(report['tolerancesDefinedBeforeComparison'])
        self.assertEqual(report['framesCompared'], 2)
        self.assertTrue(report['passed'])

    def test_degraded_capture_fails_against_registered_tolerances(self):
        report = quality_report(self.pairs(degraded=True), category='coding',
                                analysis_size=(320, 180))
        self.assertFalse(report['passed'])
        self.assertTrue(report['failures'])
        self.assertLess(report['frames'][0]['psnrYdB'], TOLERANCES['coding']['psnrYMinDb'])

    def test_alignment_by_id_reports_missing_reference_frames(self):
        aligned, missing = align_frames_by_id(['a', 'b', 'c'],
                                              [{'id': 'a'}, {'id': 'c'}, {'id': 'x'}])
        self.assertEqual(sorted(aligned), ['a', 'c'])
        self.assertEqual(missing, ['b'])


if __name__ == '__main__':
    unittest.main()
