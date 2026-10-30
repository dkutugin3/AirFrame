"""Headless analysis core for the final-acceptance harness (plan §13.B / §13.C).

Two consumers:

* ``scripts/acceptance-60fps.py`` (native, §13.B) decodes an *external* screen/OBS
  recording of the AirFrame OBS window and passes the per-frame records here to prove
  coverage: expected vs observed unique frame IDs, repeats, drops, inter-new-frame
  intervals, image holds and the fail-closed evidence check.
* ``scripts/quality-compare.py`` (§13.C) uses :func:`quality_report` to compare an
  ID-aligned recording against the lossless bench source: PSNR/SSIM, colour/matrix/
  range patch checks with tolerances that are registered *before* the comparison, and
  saved identical crops of fine text / colour bars / gradients / motion.

No Qt and no GPU here, so everything in this module runs in the Docker test suite.
All functions are deterministic: frame selection and subsampling use fixed strides.
"""
import json
import math
from pathlib import Path
import struct
import zlib

import av

from scripts.bench_source import COLOUR_BARS, MOVER_BAND, TEXT_BAND, decode_frame_id

# Tolerances are deliberately defined here, before any comparison is run, and are
# recorded in every quality report (plan §13.C: "Допуски цветового сравнения фиксируй
# до реализации с учётом округления"). Two categories:
#   coding  - lossless source -> H.264 at a pinned bitrate -> decode (no display):
#             only coding/rounding loss is expected, so the tolerances are strict.
#   capture - lossless source -> app render -> OS screen capture (display colour
#             management, scaling, second H.264 encode): looser, and the report says so.
TOLERANCES_VERSION = '2026-10-30.1'
TOLERANCES = {
    'coding': {
        'psnrYMinDb': 32.0,
        'ssimYMin': 0.97,
        'maxPatchRgbDelta': 8,
        'maxNeutralRgbDelta': 6,
        'maxBlackLevelDelta': 4,
        'maxWhiteLevelDelta': 6,
        'blackLevelMaxAbsolute': 32,
        'whiteLevelMinAbsolute': 200,
    },
    'capture': {
        'psnrYMinDb': 20.0,
        'ssimYMin': 0.85,
        'maxPatchRgbDelta': 48,
        'maxNeutralRgbDelta': 28,
        'maxBlackLevelDelta': 20,
        'maxWhiteLevelDelta': 24,
        'blackLevelMaxAbsolute': 64,
        'whiteLevelMinAbsolute': 168,
    },
}

INTERVAL_HOLD_MS = 50.0
CAPTURE_EVIDENCE_MIN_FRAMES = 10

CROP_REGIONS = {
    'fine-text': TEXT_BAND,
    'colour-bars': COLOUR_BARS,
    'gradient': (0.24, 0.56, 0.53, 0.55),
    'motion': MOVER_BAND,
}


def _finite(value):
    """JSON-safe representation: inf -> 'inf', NaN -> None."""
    if value is None:
        return None
    if math.isinf(value):
        return 'inf' if value > 0 else '-inf'
    if math.isnan(value):
        return None
    return value


def percentiles_ms(samples):
    """Nearest-rank percentiles of millisecond samples (None when there are none)."""
    values = sorted(float(sample) for sample in samples)
    empty = {'count': 0, 'p50Ms': None, 'p95Ms': None, 'p99Ms': None,
             'minMs': None, 'maxMs': None, 'meanMs': None}
    if not values:
        return empty

    def nearest_rank(percentile):
        rank = math.ceil(percentile / 100 * len(values)) - 1
        return values[max(0, min(rank, len(values) - 1))]

    return {'count': len(values), 'p50Ms': nearest_rank(50), 'p95Ms': nearest_rank(95),
            'p99Ms': nearest_rank(99), 'minMs': values[0], 'maxMs': values[-1],
            'meanMs': sum(values) / len(values)}


def extract_capture_records(path, max_frames=None):
    """Decode a recorded container and recover (id, pts) for every captured frame."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        codec = stream.codec_context
        codec.thread_type = 'NONE'
        codec.thread_count = 1
        records = []
        state = {'firstPts': None, 'done': False}

        def consume(frames):
            for frame in frames:
                if max_frames is not None and len(records) >= max_frames:
                    state['done'] = True
                    return
                pts_seconds = None
                if frame.pts is not None:
                    pts_seconds = float(frame.pts * frame.time_base)
                    if state['firstPts'] is None:
                        state['firstPts'] = pts_seconds
                    pts_seconds -= state['firstPts']
                try:
                    frame_id = decode_frame_id(frame)
                except Exception:
                    frame_id = None
                records.append({'id': frame_id, 'ptsSeconds': pts_seconds})

        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            try:
                frames = codec.decode(packet)
            except av.FFmpegError:
                continue
            consume(frames)
            if state['done']:
                break
        if not state['done']:
            try:
                consume(codec.decode(None))
            except av.FFmpegError:
                pass
        span = 0.0
        if len(records) >= 2:
            first, last = records[0]['ptsSeconds'], records[-1]['ptsSeconds']
            if first is not None and last is not None:
                span = max(0.0, last - first)
        video = {
            'codec': stream.codec_context.name,
            'width': stream.codec_context.width,
            'height': stream.codec_context.height,
            'averageRate': float(stream.average_rate) if stream.average_rate else None,
            'frames': len(records),
        }
        return {'records': records, 'video': video, 'spanSeconds': span}
    finally:
        container.close()


def summarize_capture(records, source_fps, nominal_seconds=None,
                      hold_ms=INTERVAL_HOLD_MS):
    """Coverage/interval/hold statistics for one externally captured recording."""
    frames = len(records)
    with_id = [record for record in records if record.get('id') is not None]
    ids = [record['id'] for record in with_id]
    unique = len(set(ids))
    points = [record.get('ptsSeconds') for record in records]
    span = None
    if points and points[0] is not None and points[-1] is not None:
        span = max(0.0, points[-1] - points[0])
    if not span:
        span = float(nominal_seconds) if nominal_seconds else 0.0
    span = float(span) if span else 0.0
    measurement_seconds = float(nominal_seconds) if nominal_seconds is not None else span
    expected = int(round(measurement_seconds * source_fps))
    observed_percent = (unique / expected * 100) if expected else None
    repeats = sum(1 for previous, current in zip(records, records[1:])
                  if previous.get('id') is not None
                  and previous.get('id') == current.get('id'))
    drops = 0
    missing = []
    if ids:
        present = set(ids)
        missing = [value for value in range(min(ids), max(ids) + 1)
                   if value not in present]
        drops = len(missing)
    # Intervals between first appearances of each new frame ID.
    seen = set()
    first_times = []
    for record in records:
        frame_id, pts = record.get('id'), record.get('ptsSeconds')
        if frame_id is None or pts is None or frame_id in seen:
            continue
        seen.add(frame_id)
        first_times.append(pts)
    deltas = [(later - earlier) * 1000
              for earlier, later in zip(first_times, first_times[1:]) if later > earlier]
    # Runs of identical consecutive IDs: an image hold is a run lasting > hold_ms.
    holds = []
    run_id, run_start, run_end = None, None, None
    for record in records:
        frame_id, pts = record.get('id'), record.get('ptsSeconds')
        if frame_id is not None and frame_id == run_id and pts is not None:
            run_end = pts
            continue
        if run_id is not None and run_start is not None and run_end is not None:
            duration = (run_end - run_start) * 1000
            if duration > 0:
                holds.append({'id': run_id, 'durationMs': duration})
        run_id, run_start, run_end = frame_id, pts, pts
    if run_id is not None and run_start is not None and run_end is not None:
        duration = (run_end - run_start) * 1000
        if duration > 0:
            holds.append({'id': run_id, 'durationMs': duration})
    over = [hold for hold in holds if hold['durationMs'] > hold_ms]
    return {
        'framesCaptured': frames,
        'framesWithId': len(with_id),
        'framesWithoutId': frames - len(with_id),
        'uniqueIds': unique,
        'expectedUniqueIds': expected,
        'observedUniquePercent': observed_percent,
        'repeats': repeats,
        'dropsWithinObserved': drops,
        'missingIdSample': missing[:20],
        'spanSeconds': span,
        'measurementSeconds': measurement_seconds,
        'sourceFps': source_fps,
        'intervalStatsMs': percentiles_ms(deltas),
        'holds': {
            'count': len(holds),
            'overThreshold': len(over),
            'thresholdMs': hold_ms,
            'maxMs': max((hold['durationMs'] for hold in holds), default=0.0),
            'samples': sorted(over, key=lambda hold: -hold['durationMs'])[:10],
        },
    }


def capture_evidence_error(summary, min_frames=CAPTURE_EVIDENCE_MIN_FRAMES):
    """None when an external recording is usable evidence, otherwise the exact reason."""
    if summary['framesCaptured'] < min_frames:
        return (f'capture too few frames: {summary["framesCaptured"]} < {min_frames} '
                f'(recorder produced no usable video)')
    if summary['framesWithId'] == 0:
        return ('capture has no decodable frame IDs: the recorded window content is '
                'black/blank/covered or the bench source was not displayed')
    if summary['uniqueIds'] < 2:
        return (f'capture has only {summary["uniqueIds"]} unique frame ID(s): the window '
                f'showed a still or the recorder captured a frozen image')
    return None


def resize_luma(luma, width, height, new_width, new_height):
    """Resize a packed Y plane with FFmpeg (swscale), returning a packed Y plane."""
    if len(luma) != width * height:
        raise ValueError('luma size does not match width*height')
    frame = av.VideoFrame(width, height, 'yuv420p')
    frame.planes[0].update(luma)
    for plane in frame.planes[1:]:
        plane.update(bytes([128]) * plane.buffer_size)
    resized = frame.reformat(width=new_width, height=new_height, format='yuv420p')
    return _packed_plane(resized.planes[0], new_width, new_height)


def _packed_plane(plane, width, height):
    raw = bytes(plane)
    stride = plane.line_size
    if stride == width:
        return raw[:width * height]
    return b''.join(raw[row * stride:row * stride + width] for row in range(height))


def psnr(reference, degraded, width, height, peak=255.0, max_samples=200000):
    """Mean-squared-error PSNR of two packed 8-bit planes; inf for identical frames."""
    if len(reference) != width * height:
        raise ValueError('reference size does not match width*height')
    if len(degraded) != width * height:
        raise ValueError('degraded size does not match width*height')
    step = max(1, (width * height) // max_samples)
    squared_error = 0
    count = 0
    for index in range(0, width * height, step):
        difference = reference[index] - degraded[index]
        squared_error += difference * difference
        count += 1
    if not count:
        raise ValueError('no pixels compared')
    mse = squared_error / count
    if mse == 0:
        return math.inf
    return 10 * math.log10(peak * peak / mse)


def ssim(reference, degraded, width, height, window=8, max_samples=200000):
    """Mean SSIM (uniform window) of two packed 8-bit planes."""
    if len(reference) != width * height:
        raise ValueError('reference size does not match width*height')
    if len(degraded) != width * height:
        raise ValueError('degraded size does not match width*height')
    if width < window or height < window:
        raise ValueError('frame is smaller than the SSIM window')
    windows_x, windows_y = width // window, height // window
    step = max(1, int(math.sqrt(windows_x * windows_y / max(1, max_samples))))
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    total = count = 0
    for top in range(0, height - window + 1, window * step):
        for left in range(0, width - window + 1, window * step):
            sum_x = sum_y = 0
            for row in range(window):
                base = (top + row) * width + left
                for column in range(window):
                    sum_x += reference[base + column]
                    sum_y += degraded[base + column]
            pixels = window * window
            mean_x, mean_y = sum_x / pixels, sum_y / pixels
            var_x = var_y = cov = 0.0
            for row in range(window):
                base = (top + row) * width + left
                for column in range(window):
                    x = reference[base + column] - mean_x
                    y = degraded[base + column] - mean_y
                    var_x += x * x
                    var_y += y * y
                    cov += x * y
            var_x /= pixels
            var_y /= pixels
            cov /= pixels
            numerator = (2 * mean_x * mean_y + c1) * (2 * cov + c2)
            denominator = ((mean_x * mean_x + mean_y * mean_y + c1)
                           * (var_x + var_y + c2))
            total += numerator / denominator
            count += 1
    if not count:
        raise ValueError('no SSIM windows compared')
    return total / count


def align_frames_by_id(reference_ids, records):
    """Match captured records to reference frame IDs. Returns (aligned, missing)."""
    by_id = {}
    for record in records:
        frame_id = record.get('id')
        if frame_id is not None and frame_id not in by_id:
            by_id[frame_id] = record
    aligned = {}
    missing = []
    for frame_id in reference_ids:
        if frame_id in by_id:
            aligned[frame_id] = by_id[frame_id]
        else:
            missing.append(frame_id)
    return aligned, missing


def _mean_patches(rgb, width, height):
    top, bottom, left, right = _region(COLOUR_BARS, width, height)
    bar_count = len(COLOUR_BAR_COUNT)
    patches = []
    for index in range(bar_count):
        bar_left = left + (right - left) * index // bar_count
        bar_right = left + (right - left) * (index + 1) // bar_count
        bar_left = min(bar_left, bar_right - 1)
        totals = [0, 0, 0]
        samples = 0
        for row in range(top, bottom, max(1, (bottom - top) // 12)):
            for column in range(bar_left, bar_right, max(1, (bar_right - bar_left) // 12)):
                offset = (row * width + column) * 3
                totals[0] += rgb[offset]
                totals[1] += rgb[offset + 1]
                totals[2] += rgb[offset + 2]
                samples += 1
        if samples:
            patches.append([total / samples for total in totals])
    return patches


COLOUR_BAR_COUNT = tuple(range(8))  # eight bars defined by scripts/bench_source.py


def _region(bounds, width, height):
    y0, y1, x0, x1 = bounds
    return (int(y0 * height), max(int(y0 * height) + 1, int(y1 * height)),
            int(x0 * width), max(int(x0 * width) + 1, int(x1 * width)))


def _patch_luma(patch):
    return 0.299 * patch[0] + 0.587 * patch[1] + 0.114 * patch[2]


def colour_report(reference_rgb, captured_rgb, width, height, tolerances):
    """Colour-patch, matrix (U/V) and range (black/white level) checks."""
    if len(reference_rgb) != width * height * 3:
        raise ValueError('reference RGB size does not match width*height*3')
    if len(captured_rgb) != width * height * 3:
        raise ValueError('captured RGB size does not match width*height*3')
    reference_patches = _mean_patches(reference_rgb, width, height)
    captured_patches = _mean_patches(captured_rgb, width, height)
    patches = []
    max_delta = 0.0
    for index, (reference, captured) in enumerate(
            zip(reference_patches, captured_patches)):
        deltas = [abs(a - b) for a, b in zip(reference, captured)]
        delta = max(deltas)
        max_delta = max(max_delta, delta)
        patches.append({'index': index, 'reference': reference, 'captured': captured,
                        'maxChannelDelta': delta})
    neutral_index = 6
    neutral_delta = (max(abs(a - b) for a, b in zip(reference_patches[neutral_index],
                                                    captured_patches[neutral_index]))
                     if len(patches) > neutral_index else 0.0)
    reference_levels = {'blackLevel': min(_patch_luma(p) for p in reference_patches),
                        'whiteLevel': max(_patch_luma(p) for p in reference_patches)}
    captured_levels = {'blackLevel': min(_patch_luma(p) for p in captured_patches),
                       'whiteLevel': max(_patch_luma(p) for p in captured_patches)}
    # U/V swap: red and blue patches would exchange their R/B balance.
    def balances(patches):
        if len(patches) < 3:
            return False
        red, blue = patches[0], patches[2]
        return (red[0] - red[2] > 10) and (blue[2] - blue[0] > 10)
    swap_suspected = balances(reference_patches) and not balances(captured_patches)
    checks = {
        'patchDelta': max_delta <= tolerances['maxPatchRgbDelta'],
        'neutralDelta': neutral_delta <= tolerances['maxNeutralRgbDelta'],
        'blackLevelDelta': (abs(captured_levels['blackLevel']
                                - reference_levels['blackLevel'])
                            <= tolerances['maxBlackLevelDelta']),
        'whiteLevelDelta': (abs(captured_levels['whiteLevel']
                                - reference_levels['whiteLevel'])
                            <= tolerances['maxWhiteLevelDelta']),
        'blackNotRaised': (captured_levels['blackLevel']
                           <= tolerances['blackLevelMaxAbsolute']),
        'whiteNotCrushed': (captured_levels['whiteLevel']
                            >= tolerances['whiteLevelMinAbsolute']),
        'uvNotSwapped': not swap_suspected,
    }
    return {
        'patches': patches,
        'maxPatchRgbDelta': max_delta,
        'maxNeutralRgbDelta': neutral_delta,
        'blackLevel': captured_levels['blackLevel'],
        'whiteLevel': captured_levels['whiteLevel'],
        'referenceLevels': reference_levels,
        'uvSwapSuspected': swap_suspected,
        'checks': checks,
        'passed': all(checks.values()),
    }


def _png_chunk(kind, data):
    body = kind + data
    return (struct.pack('>I', len(data)) + body
            + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF))


def write_png_gray(path, width, height, luma):
    """Write an 8-bit grayscale PNG without third-party image libraries."""
    if len(luma) != width * height:
        raise ValueError('luma size does not match width*height')
    raw = b''.join(b'\x00' + bytes(luma[row * width:(row + 1) * width])
                   for row in range(height))
    png = (b'\x89PNG\r\n\x1a\n'
           + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0))
           + _png_chunk(b'IDAT', zlib.compress(raw, 6))
           + _png_chunk(b'IEND', b''))
    Path(path).write_bytes(png)


def write_png_rgb(path, width, height, rgb):
    """Write an 8-bit RGB PNG without third-party image libraries."""
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError('RGB size does not match width*height*3')
    raw = b''.join(b'\x00' + bytes(rgb[row * stride:(row + 1) * stride])
                   for row in range(height))
    png = (b'\x89PNG\r\n\x1a\n'
           + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + _png_chunk(b'IDAT', zlib.compress(raw, 6))
           + _png_chunk(b'IEND', b''))
    Path(path).write_bytes(png)


def save_crops(directory, reference_rgb, captured_rgb, width, height):
    """Save the same crop of reference and captured RGB frames; returns crop names."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    saved = set()
    for name, bounds in CROP_REGIONS.items():
        top, bottom, left, right = _region(bounds, width, height)
        for side, rgb in (('reference', reference_rgb), ('captured', captured_rgb)):
            rows = []
            for row in range(top, bottom):
                start = (row * width + left) * 3
                rows.append(rgb[start:start + (right - left) * 3])
            write_png_rgb(directory / f'{name}-{side}.png', right - left,
                          bottom - top, b''.join(rows))
        saved.add(name)
    return saved


def _to_yuv(frame, size):
    if frame.format.name == 'yuv420p' and (frame.width, frame.height) == tuple(size):
        return frame
    return frame.reformat(width=size[0], height=size[1], format='yuv420p')


def _to_rgb(frame, size):
    if frame.format.name == 'rgb24' and (frame.width, frame.height) == tuple(size):
        return frame
    return frame.reformat(width=size[0], height=size[1], format='rgb24')


def quality_report(pairs, category='capture', analysis_size=(320, 180),
                   crops=None):
    """Compare ID-aligned (reference, captured) frame pairs against registered tolerances.

    ``pairs`` is an iterable of ``(frame_id, reference_video_frame, captured_video_frame)``.
    Percentiles/tolerances are fixed in this module before any comparison happens and are
    copied into the report. ``crops`` is an optional output directory for the four
    identical crop regions of the first compared pair.
    """
    if category not in TOLERANCES:
        raise ValueError(f'unknown tolerance category {category}')
    tolerances = dict(TOLERANCES[category])
    width, height = analysis_size
    frames = []
    failures = []
    parsed = list(pairs)
    for frame_id, reference, captured in parsed:
        reference_yuv = _to_yuv(reference, analysis_size)
        captured_yuv = _to_yuv(captured, analysis_size)
        reference_luma = _packed_plane(reference_yuv.planes[0], width, height)
        captured_luma = _packed_plane(captured_yuv.planes[0], width, height)
        psnr_value = psnr(reference_luma, captured_luma, width, height)
        ssim_value = ssim(reference_luma, captured_luma, width, height)
        reference_rgb = _to_rgb(reference, analysis_size)
        captured_rgb = _to_rgb(captured, analysis_size)
        colour = colour_report(bytes(reference_rgb.planes[0]),
                               bytes(captured_rgb.planes[0]), width, height, tolerances)
        frame_result = {'id': frame_id, 'psnrYdB': _finite(psnr_value),
                        'ssimY': ssim_value, 'colour': colour}
        frames.append(frame_result)
        if not math.isinf(psnr_value) and psnr_value < tolerances['psnrYMinDb']:
            failures.append(f'id {frame_id}: PSNR {psnr_value:.2f} dB '
                            f'< {tolerances["psnrYMinDb"]} dB')
        if ssim_value < tolerances['ssimYMin']:
            failures.append(f'id {frame_id}: SSIM {ssim_value:.4f} '
                            f'< {tolerances["ssimYMin"]}')
        if not colour['passed']:
            failed = [name for name, ok in colour['checks'].items() if not ok]
            failures.append(f'id {frame_id}: colour checks failed: {", ".join(failed)}')
    finite_psnr = [frame['psnrYdB'] for frame in frames
                   if isinstance(frame['psnrYdB'], (int, float))]
    infinite_psnr = len(frames) - len(finite_psnr)
    ssim_values = [frame['ssimY'] for frame in frames]
    aggregate = {
        'psnrYMeanDb': ('inf' if infinite_psnr == len(frames) and frames
                        else (sum(finite_psnr) / len(finite_psnr) if finite_psnr else None)),
        'psnrYMinDb': (min(finite_psnr) if finite_psnr else
                       ('inf' if frames else None)),
        'identicalFrames': infinite_psnr,
        'ssimYMean': sum(ssim_values) / len(ssim_values) if ssim_values else None,
        'ssimYMin': min(ssim_values) if ssim_values else None,
        'maxPatchRgbDelta': max((frame['colour']['maxPatchRgbDelta'] for frame in frames),
                                default=None),
        'uvSwapSuspected': any(frame['colour']['uvSwapSuspected'] for frame in frames),
    }
    crops_saved = []
    if crops is not None and parsed:
        _, reference, captured = parsed[0]
        reference_rgb = _to_rgb(reference, analysis_size)
        captured_rgb = _to_rgb(captured, analysis_size)
        names = save_crops(crops, bytes(reference_rgb.planes[0]),
                           bytes(captured_rgb.planes[0]), width, height)
        crops_saved = sorted(names)
    return {
        'tolerancesVersion': TOLERANCES_VERSION,
        'tolerancesDefinedBeforeComparison': True,
        'tolerances': tolerances,
        'category': category,
        'analysisSize': list(analysis_size),
        'framesCompared': len(frames),
        'frames': frames,
        'aggregate': aggregate,
        'failures': failures[:20],
        'passed': not failures,
        'cropsSaved': crops_saved,
    }
