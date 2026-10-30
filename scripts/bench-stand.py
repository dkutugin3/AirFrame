#!/usr/bin/env python3
"""Reproducible stage-0 measurement stand: generate an H.264 source and verify its IDs.

Two modes:

``stand`` (default)
    Render ``--frames`` deterministic moving frames with ``scripts/bench_source.py``,
    mux them as real H.264 with PyAV, decode the file back, recover the machine-readable
    frame ID from every decoded frame, and write a JSON report with
      * ID coverage: decoded frames, unique IDs, missing, duplicates, order violations,
      * PTS agreement between decoded presentation time and the ID-derived 1/90000 PTS,
      * frame-to-frame motion: identical consecutive frames are counted (a repeated
        still must never pass as motion),
      * bounded interval metrics of the stages this headless stand can observe,
      * raw file size / sha256.
    With ``--diagnostics`` it also writes the detailed JSONL event stream.

``verify --verify PATH``
    Extract frame IDs from an existing recording (for example an OBS capture of the
    live ``BenchVideoTrack`` source) and report the same coverage/motion numbers.
    This is the tool later stages use to check OBS output; it cannot prove physical
    presentation on its own.

What the stand observes, and what it does not
---------------------------------------------
``assembly -> decode_start -> decode_end -> output_ready -> renderer_handoff ->
render_complete`` are recorded around real operations of this process: packet demux,
FFmpeg decode and ID extraction. ``renderer_handoff``/``render_complete`` refer to a
headless consumer that only counts frames — they are NOT the Qt renderer, OBS capture
or a physical display. See docs/MEASUREMENT.md.

Run (inside the prepared Docker test container):
    docker exec -w /app af-test python scripts/bench-stand.py \
        --frames 240 --fps 60 --width 1920 --height 1080 \
        --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

import av

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bench_metrics import DiagnosticEventWriter, IntervalMetrics
from scripts.bench_source import BenchScene, FrameIdDecodeError, decode_frame_id

SINK_DESCRIPTION = ('headless stand consumer (counts decoded frames); NOT the Qt '
                    'renderer, OBS capture or a physical display')


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def environment():
    import aiortc
    return {
        'recordedAt': now_iso(),
        'python': sys.version.replace('\n', ' '),
        'platform': platform.platform(),
        'av': av.__version__,
        'aiortc': aiortc.__version__,
        'ffmpeg': {name: '.'.join(str(part) for part in version)
                   for name, version in av.library_versions.items()},
        'hostname': os.environ.get('HOSTNAME', ''),
    }


def write_stand_artifact(scene, frames, path, crf):
    """Render ``frames`` frames and mux them as H.264. Returns encode seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), 'w', format='mp4')
    try:
        stream = container.add_stream('libx264', rate=scene.fps)
        stream.width, stream.height = scene.width, scene.height
        stream.pix_fmt = 'yuv420p'
        stream.time_base = scene.time_base
        stream.options = {'crf': str(crf), 'preset': 'veryfast', 'bframes': '0',
                          'g': str(scene.fps * 2), 'tune': 'zerolatency'}
        container.metadata.update({
            'title': f'AirFrame bench source {scene.width}x{scene.height}@{scene.fps}',
            'comment': json.dumps({'frames': frames, 'crf': crf,
                                   'timeBase': '1/90000',
                                   'id': 'machine-readable 20-bit band + parity'}),
        })
        started = time.monotonic()
        for frame_id in range(frames):
            frame = scene.render(frame_id)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        return time.monotonic() - started
    finally:
        container.close()


def _frame_change_fraction(previous_luma, current_luma, step):
    changed = total = 0
    for index in range(0, len(previous_luma), step):
        total += 1
        if previous_luma[index] != current_luma[index]:
            changed += 1
    return changed / max(1, total)


def decode_and_verify(path, metrics, max_frames=None):
    """Decode a recording, recover IDs/PTS/motion, return the verification report."""
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        codec = stream.codec_context
        codec.thread_type = 'NONE'
        codec.thread_count = 1
        ids, pts_values, change_fractions = [], [], []
        state = {'decoded': 0, 'identical': 0, 'previous': None, 'errors': 0}
        decoded_frames = [0]

        def consume(frames, position):
            for index, frame in enumerate(frames):
                # Packet k decodes to frame k (encoder uses bframes=0), so the same
                # key pairs decode_start -> decode_end. Extra flush frames get a
                # distinct key and are simply not paired.
                key = position if index == 0 else position * 10000 + index
                metrics.mark('decode_end', frame=key)
                state['decoded'] += 1
                try:
                    frame_id = decode_frame_id(frame)
                except FrameIdDecodeError:
                    ids.append(None)
                    state['previous'] = None
                else:
                    ids.append(frame_id)
                    pts_values.append(
                        float(frame.pts * frame.time_base) if frame.pts is not None else None)
                    luma = bytes(frame.planes[0])
                    if state['previous'] is not None and len(state['previous']) == len(luma):
                        fraction = _frame_change_fraction(
                            state['previous'], luma, step=max(1, len(luma) // 20000))
                        change_fractions.append(fraction)
                        if fraction == 0:
                            state['identical'] += 1
                    state['previous'] = luma
                metrics.mark('output_ready', frame=key)
                metrics.mark('renderer_handoff', frame=key)  # SINK_DESCRIPTION
                metrics.mark('render_complete', frame=key)
                decoded_frames[0] += 1

        packet_count = 0
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            if max_frames is not None and decoded_frames[0] >= max_frames:
                break
            position = packet_count
            packet_count += 1
            metrics.mark('assembly', frame=position)
            metrics.mark('decode_start', frame=position)
            try:
                frames = codec.decode(packet)
            except av.FFmpegError:
                state['errors'] += 1
                continue
            consume(frames, position)
        if max_frames is None or decoded_frames[0] < max_frames:
            try:
                consume(codec.decode(None), packet_count)
            except av.FFmpegError:
                state['errors'] += 1
        fps = float(stream.average_rate) if stream.average_rate else None
        report = _verification_report(ids, pts_values, state['identical'],
                                      change_fractions, fps, packet_count)
        report['decodeErrors'] = state['errors']
        report['codec'] = stream.codec_context.name
        report['width'] = stream.codec_context.width
        report['height'] = stream.codec_context.height
        return report
    finally:
        container.close()


def _verification_report(ids, pts_values, identical, change_fractions, fps, packet_count):
    valid = [(position, value) for position, value in enumerate(ids) if value is not None]
    values = [value for _, value in valid]
    duplicates = 0
    order_violations = 0
    previous = None
    seen = set()
    for value in values:
        if value in seen:
            duplicates += 1
        seen.add(value)
        if previous is not None and value != previous + 1:
            order_violations += 1
        previous = value
    missing = sorted(set(range(min(values), max(values) + 1)) - seen) if values else []
    pts = None
    if fps and pts_values:
        errors = [abs(media_seconds - frame_id / fps) * 1000
                  for frame_id, media_seconds in zip(values, pts_values)
                  if media_seconds is not None]
        if errors:
            pts = {'maxMs': max(errors), 'meanMs': sum(errors) / len(errors),
                   'withinOneFrame': all(error <= 1000 / fps for error in errors)}
    changes = sorted(change_fractions)
    return {
        'decodedFrames': len(ids),
        'framesWithoutDecodableId': sum(1 for value in ids if value is None),
        'uniqueIds': len(seen),
        'duplicates': duplicates,
        'missingIds': len(missing),
        'missingIdSample': missing[:20],
        'orderViolations': order_violations,
        'firstId': values[0] if values else None,
        'lastId': values[-1] if values else None,
        'packetsDemuxed': packet_count,
        'pts': pts,
        'motion': {
            'pairsCompared': len(change_fractions),
            'identicalConsecutivePairs': identical,
            'minChangedFraction': changes[0] if changes else None,
            'p50ChangedFraction': changes[len(changes) // 2] if changes else None,
            'maxChangedFraction': changes[-1] if changes else None,
            'note': ('Real motion must change consecutive frames; identical pairs are '
                     'counted separately and never treated as 60 FPS.'),
        },
    }


def run_stand(args):
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    writer = (DiagnosticEventWriter(artifacts / 'diagnostics.jsonl',
                                    max_events=args.max_events)
              if args.diagnostics else None)
    metrics = (IntervalMetrics(event_writer=writer) if writer is not None
               else IntervalMetrics())
    scene = BenchScene(args.width, args.height, args.fps)
    video_path = artifacts / f'bench-source-{args.width}x{args.height}-{args.fps}fps.mp4'
    started_at = now_iso()
    started = time.monotonic()
    try:
        encode_seconds = write_stand_artifact(scene, args.frames, video_path, args.crf)
        verification = decode_and_verify(video_path, metrics)
        elapsed = time.monotonic() - started
        intervals = metrics.snapshot()
    finally:
        diagnostics = writer.close() if writer is not None else None
    report = {
        'schema': 'airframe-bench-stand/1',
        'mode': 'stand',
        'startedAt': started_at,
        'finishedAt': now_iso(),
        'command': ' '.join(sys.argv),
        'environment': environment(),
        'scene': {
            'width': args.width, 'height': args.height, 'fps': args.fps,
            'frames': args.frames,
            'timeBase': '1/90000',
            'features': ['machine-readable 20-bit frame id + parity',
                         'human-readable decimal frame id and PTS',
                         'scrolling band', 'fast mover + shadow', 'moving thin line',
                         'static vertical gradient/shadow background', 'thin line grid',
                         'eight-bar colour scale', 'fine text'],
        },
        'artifact': {
            'path': str(video_path),
            'bytes': video_path.stat().st_size,
            'sha256': sha256_file(video_path),
            'encodeSeconds': encode_seconds,
        },
        'verification': verification,
        'intervals': intervals,
        'diagnostics': diagnostics,
        'sink': SINK_DESCRIPTION,
        'totalSeconds': elapsed,
        'notProven': ['physical display presentation', 'GPU acceleration',
                      'OBS capture correctness on this machine',
                      'real Android network path or absolute phone/PC clock sync'],
    }
    report_path = artifacts / 'stand-report.json'
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    metrics.write_json(artifacts / 'intervals.json')
    print(json.dumps({key: report[key] for key in ('schema', 'scene', 'artifact',
                                                   'verification', 'diagnostics')},
                     indent=2, allow_nan=False))
    print(f'stand report: {report_path}', flush=True)
    ok = (verification['decodedFrames'] == args.frames
          and verification['framesWithoutDecodableId'] == 0
          and verification['missingIds'] == 0
          and verification['duplicates'] == 0
          and verification['orderViolations'] == 0
          and verification['motion']['identicalConsecutivePairs'] == 0
          and (verification['pts'] is None or verification['pts']['withinOneFrame']))
    print('PASS: every rendered frame decoded back with its unique ID, in order, '
          'PTS-aligned and with real motion' if ok else 'FAIL: see stand report',
          flush=True)
    return 0 if ok else 1


def run_verify(args):
    path = Path(args.verify)
    if not path.is_file():
        raise SystemExit(f'no such recording: {path}')
    metrics = IntervalMetrics()
    started = time.monotonic()
    verification = decode_and_verify(path, metrics, max_frames=args.max_frames)
    report = {
        'schema': 'airframe-bench-verify/1',
        'mode': 'verify',
        'recordedAt': now_iso(),
        'command': ' '.join(sys.argv),
        'environment': environment(),
        'recording': {'path': str(path), 'bytes': path.stat().st_size,
                      'sha256': sha256_file(path)},
        'verification': verification,
        'intervals': metrics.snapshot(),
        'decodeSeconds': time.monotonic() - started,
        'notProven': ['physical display presentation', 'GPU acceleration',
                      'OBS encoder settings are not inspected by this tool'],
    }
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False),
                               encoding='utf-8')
        print(f'verify report: {report_path}', flush=True)
    print(json.dumps(report['verification'], indent=2, allow_nan=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verify', help='verify an existing recording instead of generating')
    parser.add_argument('--report', help='write verify-mode report JSON to this path')
    parser.add_argument('--artifacts', default='artifacts/2026-10-30-stable60fps/bench',
                        help='directory for generated artifact/report files')
    parser.add_argument('--frames', type=int, default=240, help='frames to generate')
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--crf', type=int, default=20,
                        help='x264 CRF (lower = better quality)')
    parser.add_argument('--diagnostics', action='store_true',
                        help='write the detailed JSONL event stream')
    parser.add_argument('--max-events', type=int, default=100000,
                        help='bound for the diagnostic event stream')
    parser.add_argument('--max-frames', type=int, default=None,
                        help='stop decoding after this many frames (verify mode)')
    args = parser.parse_args(argv)
    if args.verify:
        return run_verify(args)
    return run_stand(args)


if __name__ == '__main__':
    raise SystemExit(main())
