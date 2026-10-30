#!/usr/bin/env python3
"""Final-acceptance harness §13.C: quality comparison against the lossless bench source.

Compares an ID-aligned recording (external capture or an OBS recording) with the lossless
synthetic source, and separately with a *pinned-bitrate* encode of that same source so
coding loss and decode/render-path loss are reported apart instead of blended:

* ``coding``  : lossless source -> libx264 at the pinned bitrate/resolution/fps -> decode.
                Only coding + rounding loss is expected here; strict tolerances.
* ``capture`` : lossless source -> app render -> external screen capture (display colour
                management, possible scaling, second H.264 encode). Looser tolerances.

Per ID it reports PSNR (Y) and SSIM (Y), colour-patch deltas, black/white levels, U/V-swap
and range checks, and saves identical crops of fine text, colour bars, gradients and the
motion region for visual review. Tolerances are defined in ``scripts/acceptance_metrics.py``
BEFORE any comparison and are copied into the report (plan §13.C requirement).
Frames are aligned by the machine-readable bench frame ID, never by position.

Run (headless; can run on the host or in the Docker test container):
    .venv/bin/python scripts/quality-compare.py \
        --capture artifacts/2026-10-30-stable60fps/acceptance/dry-run/acceptance-60fps-capture.mp4 \
        --out artifacts/2026-10-30-stable60fps/acceptance/quality-dry-run
"""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys

import av

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.acceptance_metrics import (  # noqa: E402
    TOLERANCES,
    TOLERANCES_VERSION,
    align_frames_by_id,
    quality_report,
)
from scripts.bench_source import BenchScene, decode_frame_id  # noqa: E402


def now_iso():
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, default=str, allow_nan=False),
                          encoding='utf-8')


def decode_recording_ids(path, wanted, max_frames=None):
    """Decode a recording, keep only frames whose bench ID is in ``wanted``."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        stream.codec_context.thread_type = 'NONE'
        frames = {}
        seen_ids = []
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            try:
                decoded = stream.codec_context.decode(packet)
            except av.FFmpegError:
                continue
            for frame in decoded:
                try:
                    frame_id = decode_frame_id(frame)
                except Exception:
                    continue
                seen_ids.append(frame_id)
                if frame_id in wanted and frame_id not in frames:
                    frames[frame_id] = frame
            if max_frames is not None and len(frames) >= max_frames:
                break
        return frames, seen_ids
    finally:
        container.close()


def encode_pinned(frames, path, fps, bitrate_kbps):
    """Encode reference frames at a pinned resolution/fps/bitrate (the coding baseline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), 'w', format='mp4')
    try:
        stream = container.add_stream('libx264', rate=fps)
        stream.width, stream.height = frames[0].width, frames[0].height
        stream.pix_fmt = 'yuv420p'
        stream.bit_rate = bitrate_kbps * 1000
        stream.time_base = Fraction(1, 90000)
        maxrate = bitrate_kbps * 1000
        stream.options = {'preset': 'veryfast', 'bframes': '0', 'tune': 'zerolatency',
                          'maxrate': str(maxrate), 'bufsize': str(maxrate * 2)}
        for frame in frames:
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def decode_all(path):
    """Decode a container into {bench_id: frame} for ID alignment."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        stream.codec_context.thread_type = 'NONE'
        frames = {}
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            try:
                decoded = stream.codec_context.decode(packet)
            except av.FFmpegError:
                continue
            for frame in decoded:
                try:
                    frames.setdefault(decode_frame_id(frame), frame)
                except Exception:
                    continue
        return frames
    finally:
        container.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default='artifacts/2026-10-30-stable60fps/acceptance/quality')
    parser.add_argument('--capture', required=True,
                        help='recording to grade against the lossless bench source')
    parser.add_argument('--baseline', default=None,
                        help='optional second recording (old version) to compare pairwise')
    parser.add_argument('--scene-width', type=int, default=1920)
    parser.add_argument('--scene-height', type=int, default=1080)
    parser.add_argument('--scene-fps', type=int, default=60)
    parser.add_argument('--sample-every', type=int, default=15,
                        help='compare every Nth captured frame ID')
    parser.add_argument('--max-frames', type=int, default=40)
    parser.add_argument('--analysis-size', default='640x360',
                        help='WxH used for PSNR/SSIM/patches (documented in the report)')
    parser.add_argument('--bitrate-kbps', type=int, default=8000,
                        help='pinned coding-loss bitrate for the reference encode')
    parser.add_argument('--no-coding-baseline', action='store_true')
    parser.add_argument('--category', default='capture', choices=sorted(TOLERANCES))
    parser.add_argument('--require-pass', action='store_true',
                        help='exit 1 when the registered tolerances are not met')
    args = parser.parse_args(argv)
    analysis_width, analysis_height = (int(part) for part in args.analysis_size.lower().split('x'))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        'schema': 'airframe-quality-compare/1',
        'startedAt': now_iso(),
        'command': ' '.join(sys.argv),
        'source': {'scene': [args.scene_width, args.scene_height], 'fps': args.scene_fps,
                   'type': 'lossless BenchScene frames (yuv420p, no compression before app)',
                   'analysisSize': [analysis_width, analysis_height]},
        'capture': {'path': str(args.capture),
                    'bytes': Path(args.capture).stat().st_size if Path(args.capture).exists() else 0},
        'tolerancesVersion': TOLERANCES_VERSION,
        'tolerancesDefinedBeforeComparison': True,
        'tolerances': {category: dict(values) for category, values in TOLERANCES.items()},
        'codingBaseline': {'enabled': not args.no_coding_baseline,
                           'pinnedBitrateKbps': args.bitrate_kbps},
    }
    if not Path(args.capture).is_file():
        report['status'] = 'failed-evidence'
        report['reason'] = f'no such recording: {args.capture}'
        write_json(out_dir / 'quality-report.json', report)
        print(report['reason'])
        return 2
    # 1) Discover captured IDs and pick a deterministic sample.
    capture_frames = {}
    container = av.open(str(args.capture))
    try:
        stream = container.streams.video[0]
        stream.codec_context.thread_type = 'NONE'
        available = []
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            try:
                decoded = stream.codec_context.decode(packet)
            except av.FFmpegError:
                continue
            for frame in decoded:
                try:
                    available.append(decode_frame_id(frame))
                except Exception:
                    continue
        report['capture']['decodedIds'] = len(available)
        report['capture']['uniqueIds'] = len(set(available))
        report['capture']['video'] = {
            'codec': stream.codec_context.name,
            'width': stream.codec_context.width,
            'height': stream.codec_context.height,
            'averageRate': float(stream.average_rate) if stream.average_rate else None,
        }
        scaler = []
        if stream.codec_context.width and stream.codec_context.height:
            if (stream.codec_context.width, stream.codec_context.height) != (
                    args.scene_width, args.scene_height):
                scaler.append(
                    'recording resolution differs from the source resolution: '
                    f'{stream.codec_context.width}x{stream.codec_context.height} vs '
                    f'{args.scene_width}x{args.scene_height}; this scaling is part of the '
                    'measured capture/render-path loss, not coding loss')
        report['capture']['scalingNotes'] = scaler
        ids = sorted(set(available))
        if not ids:
            report['status'] = 'failed-evidence'
            report['reason'] = ('capture has no decodable bench frame IDs; quality cannot '
                                'be compared without ID alignment')
            write_json(out_dir / 'quality-report.json', report)
            print(report['reason'])
            return 2
        selected = ids[::max(1, args.sample_every)][:args.max_frames]
        report['selectedIds'] = selected
    finally:
        container.close()
    capture_frames, _ = decode_recording_ids(args.capture, set(selected),
                                             max_frames=len(selected))
    scene = BenchScene(args.scene_width, args.scene_height, args.scene_fps)
    reference_frames = {frame_id: scene.render(frame_id) for frame_id in selected}
    missing_capture = [frame_id for frame_id in selected if frame_id not in capture_frames]
    pairs = [(frame_id, reference_frames[frame_id], capture_frames[frame_id])
             for frame_id in selected if frame_id in capture_frames]
    report['alignedFrames'] = len(pairs)
    report['missingCaptureIds'] = missing_capture[:20]
    report['status'] = 'compared' if pairs else 'failed-evidence'
    if not pairs:
        report['reason'] = 'no aligned frames between capture and reference'
        write_json(out_dir / 'quality-report.json', report)
        print(report['reason'])
        return 2
    # 3) Coding-loss baseline: lossless source -> pinned-bitrate H.264 -> decode.
    if not args.no_coding_baseline:
        coding_path = out_dir / 'coding-baseline.mp4'
        encode_pinned([reference_frames[frame_id] for frame_id in selected],
                      coding_path, args.scene_fps, args.bitrate_kbps)
        coding_frames = decode_all(coding_path)
        coding_pairs = [(frame_id, reference_frames[frame_id], coding_frames[frame_id])
                        for frame_id in selected if frame_id in coding_frames]
        report['coding'] = quality_report(
            coding_pairs, category='coding', analysis_size=(analysis_width, analysis_height),
            crops=out_dir / 'crops-coding' if coding_pairs else None)
        report['coding']['baselineRecording'] = {
            'path': str(coding_path), 'bitrateKbps': args.bitrate_kbps,
            'alignedFrames': len(coding_pairs),
            'note': ('isolates coding + rounding loss at the pinned bitrate; this is the '
                     'reference floor for the app path, not the app result')}
    # 4) Capture/render-path comparison against the same lossless source.
    report['capturePath'] = quality_report(
        pairs, category=args.category, analysis_size=(analysis_width, analysis_height),
        crops=out_dir / 'crops-capture')
    report['capturePath']['recording'] = {
        'path': str(args.capture),
        'note': ('includes display colour management, window scaling and the recorder '
                 'encoder; compare with the coding baseline to separate coding loss from '
                 'capture/render-path loss')}
    # 5) Optional old-vs-new recording comparison, ID aligned.
    if args.baseline:
        baseline_frames = decode_all(args.baseline)
        baseline_ids = set(baseline_frames)
        common = [frame_id for frame_id in selected
                  if frame_id in baseline_ids and frame_id in capture_frames]
        baseline_pairs = [(frame_id, baseline_frames[frame_id], capture_frames[frame_id])
                          for frame_id in common]
        report['baselineVsNew'] = quality_report(
            baseline_pairs, category=args.category,
            analysis_size=(analysis_width, analysis_height))
        report['baselineVsNew']['recordingA'] = str(args.baseline)
        report['baselineVsNew']['recordingB'] = str(args.capture)
        report['baselineVsNew']['note'] = (
            'pairwise old-vs-new on ID-aligned frames; tolerances are the capture category')
    report['verdict'] = {
        'capturePathPassed': report['capturePath']['passed'],
        'codingPassed': report.get('coding', {}).get('passed'),
        'failures': report['capturePath']['failures'][:10],
    }
    report['notProven'] = [
        'physical display colour accuracy beyond the recorded pixels',
        'GPU/OBS colour pipeline behaviour of a different machine',
        'subjective quality: PSNR/SSIM must be read together with the saved crops',
    ]
    report['finishedAt'] = now_iso()
    write_json(out_dir / 'quality-report.json', report)
    summary = {'status': report['status'], 'alignedFrames': report['alignedFrames'],
               'capturePath': {key: report['capturePath'][key] for key in (
                   'passed', 'aggregate', 'failures')},
               'coding': ({key: report['coding'][key] for key in ('passed', 'aggregate')}
                          if 'coding' in report else None),
               'crops': {'coding': report.get('coding', {}).get('cropsSaved'),
                         'capture': report['capturePath'].get('cropsSaved')},
               'tolerancesVersion': TOLERANCES_VERSION}
    print(json.dumps(summary, indent=2, default=str, allow_nan=False))
    if args.require_pass and not report['capturePath']['passed']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
