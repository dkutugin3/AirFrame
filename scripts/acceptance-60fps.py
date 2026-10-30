#!/usr/bin/env python3
"""Final-acceptance harness §13.B: externally captured 60 FPS evidence.

Runs the REAL native AirFrame pipeline (aiohttp signaling + WebRTC + Qt windows, as
`scripts/smoke-motion.py` does), feeds it the machine-readable `BenchVideoTrack` source
at the configured FPS, opens the clean `AirFrame — OBS` window, warms up, then records
that window with an EXTERNAL screen recorder and proves from the recording itself which
frame IDs were physically shown.

External capture is mandatory and fail-closed
---------------------------------------------
The recorder is not the app: it captures the composited screen pixels of the OBS window.
The following methods are probed in order and every outcome is recorded verbatim:

1. OBS automation (CLI/websocket)   -> only usable when OBS is already configured for a
                                       non-interactive AirFrame scene; a disabled
                                       websocket / missing dedicated scene is recorded
                                       as "unavailable" (never modified: the user's OBS
                                       config is not ours to change).
2. macOS ``screencapture -l`` video -> probed; failure (macOS may refuse screen video
                                       recording even when screenshots work) recorded.
3. ``ffmpeg -f avfoundation``       -> generic OS screen capture, cropped to the OBS
                                       window's content rectangle; used when available.

Then a short PROBE recording of the OBS window is decoded and the bench frame ID must be
readable from the captured pixels. If no recorder can produce decodable IDs, the harness
writes a report that says exactly why and exits non-zero (code 2). It NEVER substitutes
`painted_frames`, Qt notifications or app statistics for physical capture evidence --
those are reported separately as supplementary app-side telemetry only.

Reported (plan §13.B)
---------------------
* external recording real path/sha256/size/duration; expected vs observed unique frame
  IDs (numerator/denominator and %), repeats, drops;
* p50/p95/p99/max intervals between new frame IDs, unexplained image holds > 50 ms;
* app playout underflows, queue depths/ages and gaps from receiver stats;
* app CPU/RSS and recorder CPU; actual codec, decoder, frame size and receive bitrate;
* the plan's initial thresholds evaluated explicitly (>= 99.5 % unique IDs,
  p99 interval <= 2 source periods, no unexplained holds > 50 ms, zero underflows).

Exit codes: 0 report written and capture evidence valid; 1 only with --require-pass when
thresholds fail; 2 capture evidence unavailable/invalid (fail closed).

Run (native host, see docs/ACCEPTANCE.md):
    PYTHONPATH="$PWD/.superpowers/native-deps" .venv/bin/python scripts/acceptance-60fps.py \
        --duration 600 --warmup 10 --fps 60 --width 1920 --height 1080
Dry run: add ``--duration 15``.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.acceptance_metrics import (  # noqa: E402
    CAPTURE_EVIDENCE_MIN_FRAMES,
    capture_evidence_error,
    extract_capture_records,
    summarize_capture,
)

OBS_APP = Path('/Applications/OBS.app')
SCREENCAPTURE = Path('/usr/sbin/screencapture')
SOURCE_PERIOD_MS = None  # set from --fps


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def log(message):
    print(f'[{time.strftime("%H:%M:%S")}] {message}', flush=True)


def sha256_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def git_state():
    """Exact revision/dirty state under test (the branch is shared and keeps moving)."""
    state = {'head': None, 'dirty': None, 'branch': None}
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                              capture_output=True, text=True, timeout=10)
        state['head'] = head.stdout.strip() or None
        branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        state['branch'] = branch.stdout.strip() or None
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        lines = [line for line in status.stdout.splitlines() if line.strip()]
        state['dirty'] = len(lines)
        state['dirtyFiles'] = [line[3:] for line in lines[:20]]
    except Exception as error:  # pragma: no cover - diagnostics only
        state['error'] = repr(error)
    return state


_CG = None
_CF = None


def _core_graphics():
    """Load CoreGraphics/CoreFoundation helpers for on-screen window enumeration."""
    global _CG, _CF
    if _CG is not None:
        return _CG, _CF
    import ctypes
    import ctypes.util
    cg = ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
    cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                             ctypes.c_uint32]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFNumberGetValue.restype = ctypes.c_ubyte
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_ubyte
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _CG, _CF = cg, cf
    return _CG, _CF


def onscreen_windows():
    """Front-to-back list of on-screen windows (owner name, title, bounds in points)."""
    cg, cf = _core_graphics()
    import ctypes
    utf8 = 0x08000100

    def key(name):
        return cf.CFStringCreateWithCString(None, name.encode(), utf8)

    def number(reference):
        if not reference:
            return None
        value = ctypes.c_double()
        return value.value if cf.CFNumberGetValue(reference, 13,
                                                  ctypes.byref(value)) else None

    def text(reference):
        if not reference:
            return None
        buffer = ctypes.create_string_buffer(1024)
        if cf.CFStringGetCString(reference, buffer, 1024, utf8):
            return buffer.value.decode('utf-8', 'replace')
        return None

    keys = {name: key(name) for name in ('kCGWindowOwnerPID', 'kCGWindowNumber',
                                         'kCGWindowLayer', 'kCGWindowBounds',
                                         'kCGWindowOwnerName', 'kCGWindowName')}
    array = cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # on-screen, no desktop elements
    windows = []
    for index in range(cf.CFArrayGetCount(array)):
        info = cf.CFArrayGetValueAtIndex(array, index)
        bounds_ref = cf.CFDictionaryGetValue(info, keys['kCGWindowBounds'])
        bounds = None
        if bounds_ref:
            bounds = [number(cf.CFDictionaryGetValue(bounds_ref, key(axis)))
                      for axis in ('X', 'Y', 'Width', 'Height')]
        windows.append({
            'pid': int(number(cf.CFDictionaryGetValue(info, keys['kCGWindowOwnerPID'])) or 0),
            'id': int(number(cf.CFDictionaryGetValue(info, keys['kCGWindowNumber'])) or 0),
            'layer': int(number(cf.CFDictionaryGetValue(info, keys['kCGWindowLayer'])) or 0),
            'owner': text(cf.CFDictionaryGetValue(info, keys['kCGWindowOwnerName'])),
            'name': text(cf.CFDictionaryGetValue(info, keys['kCGWindowName'])),
            'bounds': [round(value, 1) for value in bounds] if bounds else None,
        })
    return windows


def display_preflight(obs):
    """Verify the OBS window is on the currently visible display Space.

    A full-screen app (another Space) makes `CGWindowListOptionOnScreenOnly` hide every
    desktop window, so an external recorder would capture that app instead of AirFrame.
    Returns (available, detail) and never mutates other applications.
    """
    geometry = obs.frameGeometry()
    expected = [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
    windows = onscreen_windows()
    own = [window for window in windows
           if window['pid'] == os.getpid() and window['layer'] == 0]
    matched = None
    for window in own:
        bounds = window['bounds']
        if bounds and abs(bounds[0] - expected[0]) <= 4 and abs(bounds[1] - expected[1]) <= 4 \
                and abs(bounds[2] - expected[2]) <= 4 and abs(bounds[3] - expected[3]) <= 4:
            matched = window
            break
    frontmost = [window for window in windows if window['layer'] == 0][:6]
    detail = {'obsWindow': matched, 'ownOnscreenWindows': own,
              'frontmostWindows': frontmost, 'expectedBounds': expected,
              'screen': [obs.screen().geometry().width(), obs.screen().geometry().height()]
                        if obs.screen() else None}
    if matched is not None:
        return True, detail
    reasons = []
    for window in frontmost:
        if window['bounds'] and detail['screen']:
            bounds = window['bounds']
            if (abs(bounds[2] - detail['screen'][0]) <= 4
                    and abs(bounds[3] - detail['screen'][1]) <= 4):
                reasons.append(f"full-screen window from '{window['owner']}'"
                               f" ({window['name'] or 'untitled'}) occupies the display")
    reason = ('OBS window is not on the active display Space: ' + '; '.join(reasons)) \
        if reasons else ('OBS window is not on the active display Space (another app/space '
                         'is in front)')
    detail['reason'] = reason
    return False, detail


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, default=str, allow_nan=False),
                          encoding='utf-8')


def run_command(args, timeout=30):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'command': args, 'exitCode': done.returncode,
                'stdout': done.stdout[-2000:], 'stderr': done.stderr[-2000:]}
    except Exception as error:
        return {'command': args, 'error': repr(error)}


# ---------------------------------------------------------------------------
# Capture backends
# ---------------------------------------------------------------------------
def probe_obs_automation():
    """OBS automation is only accepted if it needs no manual setup; never edit user config."""
    info = {'available': False, 'app': str(OBS_APP) if OBS_APP.exists() else None,
            'reason': None, 'websocketEnabled': None, 'sceneCollections': []}
    if not OBS_APP.exists():
        info['reason'] = 'OBS.app not installed'
        return info
    config = Path.home() / 'Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json'
    if config.is_file():
        try:
            payload = json.loads(config.read_text())
            info['websocketEnabled'] = bool(payload.get('server_enabled'))
        except Exception as error:
            info['websocketEnabled'] = None
            info['configError'] = repr(error)
    scenes_dir = Path.home() / 'Library/Application Support/obs-studio/basic/scenes'
    if scenes_dir.is_dir():
        info['sceneCollections'] = sorted(path.name for path in scenes_dir.glob('*.json'))
    reasons = []
    if not info['websocketEnabled']:
        reasons.append('obs-websocket server is disabled in the user config '
                       '(enabling it would modify the user\'s OBS setup)')
    dedicated = [name for name in info['sceneCollections'] if 'airframe' in name.lower()]
    if not dedicated:
        reasons.append('no dedicated AirFrame scene collection exists '
                       '(creating one requires OBS UI/automation setup)')
    info['reason'] = '; '.join(reasons) if reasons else None
    info['available'] = not reasons
    return info


def probe_screencapture_video(out_dir):
    """Probe macOS screencapture window/none video recording; record the exact outcome."""
    if not SCREENCAPTURE.is_file():
        return {'available': False, 'reason': f'{SCREENCAPTURE} not found'}
    target = out_dir / 'probe-screencapture.mov'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    result = run_command([str(SCREENCAPTURE), '-x', '-v', '-V', '1', str(target)], timeout=25)
    result['fileExists'] = target.exists()
    result['bytes'] = target.stat().st_size if target.exists() else 0
    result['available'] = (result.get('exitCode') == 0 and result['bytes'] > 0)
    if not result['available']:
        result['reason'] = (f'screencapture video recording failed '
                            f'(exit {result.get("exitCode")}): '
                            f'{(result.get("stderr") or result.get("error") or "")[-300:]}')
    return result


def find_recorder(explicit):
    if explicit:
        path = Path(explicit)
        return str(path) if path.is_file() else None
    for candidate in (shutil.which('ffmpeg'), '/opt/homebrew/bin/ffmpeg',
                      '/usr/local/bin/ffmpeg'):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def probe_ffmpeg(recorder):
    if not recorder:
        return {'available': False, 'reason': 'ffmpeg not found on PATH'}
    result = run_command([recorder, '-hide_banner', '-version'], timeout=15)
    version = (result.get('stdout') or '').splitlines()[:1]
    devices = run_command([recorder, '-hide_banner', '-f', 'avfoundation',
                           '-list_devices', 'true', '-i', ''], timeout=15)
    text = (devices.get('stderr') or '') + (devices.get('stdout') or '')
    has_screen = 'Capture screen' in text
    return {'available': result.get('exitCode') == 0 and has_screen,
            'path': recorder, 'version': version,
            'avfoundationScreenDevice': has_screen,
            'reason': None if has_screen else 'avfoundation does not list a screen capture device',
            'deviceList': text[-1500:]}


def resolve_capture_backend(out_dir, recorder):
    """Probe all external-capture methods; pick the first usable one or fail closed."""
    backend = {'probes': {}, 'selected': None, 'failClosedReason': None}
    backend['probes']['obsAutomation'] = probe_obs_automation()
    backend['probes']['screencapture'] = probe_screencapture_video(out_dir)
    backend['probes']['ffmpeg'] = probe_ffmpeg(recorder)
    if backend['probes']['screencapture']['available']:
        backend['selected'] = 'screencapture'
    elif backend['probes']['ffmpeg']['available']:
        backend['selected'] = 'ffmpeg-avfoundation'
    else:
        reasons = []
        for name, probe in backend['probes'].items():
            if not probe.get('available'):
                reasons.append(f'{name}: {probe.get("reason") or "unavailable"}')
        backend['failClosedReason'] = ('no external capture backend available: '
                                       + ' | '.join(reasons))
    return backend


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------
# `ps` is not permitted in this sandbox, so CPU/RSS come from in-process APIs:
# resource.getrusage() for CPU time and libproc for the resident size.
import ctypes
import resource
import struct as _struct

_LIBPROC = None
_PROC_PIDTASKINFO = 4


def resident_bytes(pid=None):
    """Current resident set size via libproc (bytes), or None when unavailable."""
    global _LIBPROC
    try:
        if _LIBPROC is None:
            _LIBPROC = ctypes.CDLL('/usr/lib/libproc.dylib')
            _LIBPROC.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int,
                                              ctypes.c_uint64, ctypes.c_void_p,
                                              ctypes.c_int]
            _LIBPROC.proc_pidinfo.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(96)
        target = os.getpid() if pid is None else int(pid)
        written = _LIBPROC.proc_pidinfo(target, _PROC_PIDTASKINFO, 0, buffer, 96)
        if written <= 0:
            return None
        _, resident, _, _ = _struct.unpack_from('QQQQ', buffer.raw)
        return int(resident)
    except Exception:
        return None


def cpu_seconds():
    """Cumulative user+system CPU seconds of this process."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def process_usage(pid=None):
    """A single usage sample for this process: wall time, CPU seconds, RSS bytes."""
    target = os.getpid() if pid is None else pid
    return {'at': time.monotonic(), 'cpuSeconds': cpu_seconds(),
            'rssBytes': resident_bytes(None if pid is None else target)}


def summarize_usage(samples):
    """Turn raw samples into CPU% and RSS statistics (CPU% for the self process)."""
    if not samples:
        return None
    cpu_percent = []
    sample_list = [sample for sample in samples if sample]
    for previous, current in zip(sample_list, sample_list[1:]):
        wall = current['at'] - previous['at']
        if wall > 0 and previous.get('cpuSeconds') is not None:
            cpu_percent.append((current['cpuSeconds'] - previous['cpuSeconds'])
                               / wall * 100)
    rss = [sample['rssBytes'] for sample in sample_list if sample.get('rssBytes')]
    return {'samples': len(sample_list),
            'cpuPercentMean': sum(cpu_percent) / len(cpu_percent) if cpu_percent else None,
            'cpuPercentMax': max(cpu_percent) if cpu_percent else None,
            'rssMiBMean': sum(rss) / len(rss) / 1048576 if rss else None,
            'rssMiBMax': max(rss) / 1048576 if rss else None}


def process_cpu_percent(samples):
    """CPU% from two cumulative-time samples (helper for short intervals)."""
    if len(samples) < 2:
        return None
    wall = samples[-1]['at'] - samples[0]['at']
    cpu = samples[-1]['cpuSeconds'] - samples[0]['cpuSeconds']
    return cpu / wall * 100 if wall > 0 else None


def recorder_cpu_percent(samples):
    """CPU% for a child process sampled by pid via libproc cumulative times."""
    if len(samples) < 2:
        return None
    wall = samples[-1]['at'] - samples[0]['at']
    cpu = (samples[-1]['cpuSeconds'] or 0) - (samples[0]['cpuSeconds'] or 0)
    return cpu / wall * 100 if wall > 0 else None


def latest_metric(stats, key, aggregate='max'):
    values = [sample[key] for sample in stats if isinstance(sample.get(key), (int, float))]
    if not values:
        return None
    return max(values) if aggregate == 'max' else sum(values) / len(values)


# ---------------------------------------------------------------------------
# Live run: real app + synthetic source + external capture
# ---------------------------------------------------------------------------
def build_app(args, events, frames, stats_sink):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication
    from airframe.desktop import Desktop, LocalServer

    port = args.port
    if not port:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
    server = LocalServer(port, events, frames)
    server.start()
    server.room.settings = {'fps': args.fps, 'bitrate': args.bitrate,
                            'maxSize': args.max_size}
    window = Desktop(server)
    window.show()
    events.stats.connect(lambda stats: stats_sink.append(
        {'at': time.monotonic(), **(stats or {})}) if stats else None)
    window.playout_mode.setCurrentIndex(0)  # stable: the OBS buffer mode
    window.open_obs()
    window.hide()  # keep only the clean AirFrame — OBS window on screen
    obs = window.obs
    obs.setWindowFlags(obs.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    obs.resize(args.window_width, args.window_height)
    obs.show()
    obs.move(args.window_x, args.window_y)
    obs.raise_()
    application = QApplication.instance()
    for _ in range(40):
        application.processEvents()
        time.sleep(.02)
    return {'app': application, 'server': server, 'window': window,
            'obs': obs, 'port': port}


def start_sender_process(args, server, status_path, status):
    """Start the remote synthetic source as its own process (the "phone")."""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        [str(ROOT)] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    env['AIRFRAME_ACCEPTANCE_TOKEN'] = server.room.qr_token
    command = [sys.executable, str(ROOT / 'scripts/acceptance-sender.py'),
               '--port', str(server.port), '--session', 'acceptance',
               '--width', str(args.width), '--height', str(args.height),
               '--fps', str(args.fps), '--status-file', str(status_path)]
    log('starting remote source process: ' + ' '.join(command))
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                               stderr=open(status_path.with_suffix('.log'), 'w'))
    return process


def stop_sender_process(process, status_path, status):
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        status.update(json.loads(Path(status_path).read_text()))
    except Exception as error:
        status['statusFileError'] = repr(error)
    return process.returncode


def wait_for_source(process, status_path, status, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(Path(status_path).read_text())
            status.update(payload)
            if payload.get('streaming'):
                return True
            if payload.get('error'):
                return False
        except Exception:
            pass
        if process.poll() is not None:
            return False
        time.sleep(.05)
    return False


def window_content_rect(obs, app):
    """OBS window content rectangle in physical screen pixels (for ffmpeg crop)."""
    from PySide6.QtCore import QPoint
    content = obs.centralWidget()
    global_point = content.mapToGlobal(QPoint(0, 0))
    screen = obs.screen() or app.primaryScreen()
    ratio = screen.devicePixelRatio()
    return {'x': int(global_point.x() * ratio), 'y': int(global_point.y() * ratio),
            'width': int(content.width() * ratio) & ~1,
            'height': int(content.height() * ratio) & ~1,
            'devicePixelRatio': ratio,
            'screenWidth': int(screen.geometry().width() * ratio),
            'screenHeight': int(screen.geometry().height() * ratio)}


def capture_command(args, recorder, rect, seconds, movie):
    scale = f',scale={args.capture_width}:{args.capture_height}:flags=fast_bilinear' \
        if args.capture_width and args.capture_height else ''
    filter_graph = f'crop={rect["width"]}:{rect["height"]}:{rect["x"]}:{rect["y"]}{scale}'
    return [recorder, '-hide_banner', '-nostats', '-loglevel', 'warning', '-y',
            '-f', 'avfoundation', '-framerate', str(args.fps),
            '-i', args.avfoundation_device,
            '-t', str(seconds), '-an', '-vf', filter_graph,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', str(args.crf),
            '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough',
            '-movflags', '+faststart', str(movie)]


def record_window(args, recorder, rect, seconds, movie, log_path, app, sample_usage,
                  recorder_pid_sink):
    command = capture_command(args, recorder, rect, seconds, movie)
    log('recording: ' + ' '.join(command))
    start_wall = time.monotonic()
    with open(log_path, 'w', encoding='utf-8') as stderr_handle:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                   stderr=stderr_handle)
        recorder_pid_sink['pid'] = process.pid
        children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        samples, recorder_samples = [], []
        last_sample = 0.0
        # The Qt event loop must run close to real time: the OBS window is a Qt widget
        # and only repaints when its event loop is pumped. Sampling bookkeeping runs at
        # 4 Hz, but event pumping happens every few milliseconds.
        while process.poll() is None:
            app.processEvents()
            now = time.monotonic()
            if now - last_sample >= .25:
                last_sample = now
                sample = sample_usage()
                if sample:
                    samples.append(sample)
                recorder_sample = process_usage(process.pid)
                if recorder_sample:
                    recorder_samples.append(recorder_sample)
            time.sleep(.004)
    children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    recorder_cpu = ((children_after.ru_utime - children_before.ru_utime)
                    + (children_after.ru_stime - children_before.ru_stime))
    elapsed = max(1e-9, time.monotonic() - start_wall)
    return {'exitCode': process.returncode, 'command': command,
            'usageSamples': samples, 'usage': summarize_usage(samples),
            'recorderUsage': {'cpuSeconds': recorder_cpu,
                              'averageCpuPercent': recorder_cpu / elapsed * 100,
                              'wallSeconds': elapsed}}


def wait_for_frames(app, frames, server, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if frames.get()[1] is not None and getattr(server.receiver, 'frame_count', 0) > 0:
            return True
        time.sleep(.01)
    return False


def pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)


def run_live(args):
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {'schema': 'airframe-acceptance-60fps/1', 'mode': 'live',
              'startedAt': now_iso(), 'command': ' '.join(sys.argv),
              'host': {'platform': sys.platform, 'python': sys.version.split()[0],
                       'git': git_state()},
              'config': vars(args)}
    # Native dependencies needed by the app are installed outside .venv by
    # scripts/setup-native-tests.sh and injected via PYTHONPATH (documented).
    native_deps = ROOT / '.superpowers/native-deps'
    if native_deps.is_dir() and str(native_deps) not in sys.path:
        sys.path.insert(0, str(native_deps))
    try:
        import qrcode  # noqa: F401  (real runtime dependency of airframe.desktop)
        from PySide6.QtWidgets import QApplication
    except Exception as error:
        report['status'] = 'capture-unavailable'
        report['failClosedReason'] = (
            f'native app dependencies unavailable ({error}); run '
            f'sh scripts/setup-native-tests.sh and PYTHONPATH=.superpowers/native-deps')
        write_json(out_dir / 'acceptance-60fps-report.json', report)
        log('FAIL CLOSED: ' + report['failClosedReason'])
        return 2
    app = QApplication.instance() or QApplication([])
    if not app.screens():
        report['status'] = 'capture-unavailable'
        report['failClosedReason'] = 'no GUI display session: cannot show/capture the OBS window'
        write_json(out_dir / 'acceptance-60fps-report.json', report)
        log('FAIL CLOSED: ' + report['failClosedReason'])
        return 2
    recorder = find_recorder(args.recorder)
    backend = resolve_capture_backend(out_dir / 'probe', recorder)
    report['capture'] = {'backend': backend, 'recorder': recorder,
                         'window': None, 'recording': None, 'probe': None}
    if not backend['selected']:
        report['status'] = 'capture-unavailable'
        report['failClosedReason'] = backend['failClosedReason']
        write_json(out_dir / 'acceptance-60fps-report.json', report)
        log('FAIL CLOSED: ' + backend['failClosedReason'])
        return 2
    log(f'external capture backend: {backend["selected"]}')

    from airframe.desktop import Events, Frames
    events, frames = Events(), Frames()
    stats = []
    app_state = None
    sender_process = None
    sender_status_path = out_dir / 'sender-status.json'
    report['sender'] = {}
    children_baseline = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        app_state = build_app(args, events, frames, stats)
        server, obs = app_state['server'], app_state['obs']
        sender_process = start_sender_process(args, server, sender_status_path,
                                             report['sender'])
        if (not wait_for_source(sender_process, sender_status_path, report['sender'],
                                args.startup_timeout)
                or not wait_for_frames(app, frames, server, args.startup_timeout)):
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = (
                'the native pipeline produced no decoded frames within '
                f'{args.startup_timeout}s (source: {report["sender"].get("error")})')
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
        log(f'pipeline live; warming up {args.warmup}s')
        pump(app, args.warmup)
        stats.clear()  # capture-window statistics only
        rect = window_content_rect(obs, app_state['app'])
        report['capture']['window'] = rect
        display_ok, display_detail = display_preflight(obs)
        report['capture']['display'] = display_detail
        if not display_ok:
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = display_detail['reason']
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
        if (rect['width'] <= 0 or rect['height'] <= 0
                or rect['x'] < 0 or rect['y'] < 0
                or rect['x'] + rect['width'] > rect['screenWidth']
                or rect['y'] + rect['height'] > rect['screenHeight']):
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = (
                f'OBS window content rectangle {rect} is outside the screen; cannot '
                'capture the window externally')
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
        probe_movie = out_dir / 'probe-window.mp4'
        probe_result = record_window(args, recorder, rect, args.probe_seconds,
                                     probe_movie, out_dir / 'probe-ffmpeg.log',
                                     app_state['app'],
                                     lambda: process_usage(os.getpid()), {})
        probe_analysis = extract_capture_records(probe_movie)
        probe_summary = summarize_capture(
            [{'id': record['id'], 'ptsSeconds': record['ptsSeconds']}
             for record in probe_analysis['records']],
            source_fps=args.fps, nominal_seconds=args.probe_seconds)
        probe_error = capture_evidence_error(probe_summary, min_frames=5)
        report['capture']['probe'] = {'recording': str(probe_movie),
                                      'analysis': probe_analysis['video'],
                                      'summary': probe_summary,
                                      'error': probe_error}
        if probe_error:
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = (
                'external capture probe did not contain decodable bench frame IDs: '
                + probe_error)
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
        log('capture probe verified: external recording contains decodable frame IDs')

        display_ok, display_detail = display_preflight(obs)
        report['capture']['displayBeforeCapture'] = display_detail
        if not display_ok:
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = display_detail['reason']
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
        movie = out_dir / 'acceptance-60fps-capture.mp4'
        recorder_state = {}
        recording = record_window(args, recorder, rect, args.duration, movie,
                                  out_dir / 'ffmpeg.log', app_state['app'],
                                  lambda: process_usage(os.getpid()), recorder_state)
        app_usage = recording.pop('usageSamples', [])
        recorder_usage = recording.pop('recorderUsage', None)
        report['capture']['recording'] = {'path': str(movie),
                                          'bytes': movie.stat().st_size if movie.exists() else 0,
                                          'sha256': sha256_file(movie) if movie.exists() else None,
                                          'requestedSeconds': args.duration,
                                          'recorder': recording}
        if not movie.exists() or movie.stat().st_size == 0 or recording['exitCode'] != 0:
            report['status'] = 'capture-unavailable'
            report['failClosedReason'] = (
                f'external recorder failed: exit {recording["exitCode"]}')
            write_json(out_dir / 'acceptance-60fps-report.json', report)
            log('FAIL CLOSED: ' + report['failClosedReason'])
            return 2
    finally:
        if sender_process is not None:
            stop_sender_process(sender_process, sender_status_path, report['sender'])
        if app_state is not None:
            try:
                for key in ('obs', 'window'):
                    widget = app_state.get(key)
                    if widget is not None:
                        widget.close()
                        widget.deleteLater()
                app_state['obs'] = app_state['window'] = None
                app_state['server'].close()
                app.processEvents()
            except Exception as error:  # pragma: no cover - shutdown diagnostics
                log(f'cleanup warning: {error!r}')
            del app_state
    # ----- actual output path / renderer / decoder observability -----
    output_path_info = {}
    try:
        window = app_state['window']
        obs_renderer = window.obs_pane.renderer
        preview_renderer = window.surface.renderer
        decoder_status = {}
        try:
            receiver = app_state['server'].receiver
            pipeline = getattr(receiver, 'decode_pipeline', None)
            if pipeline is not None:
                decoder_status['pipelineStats'] = pipeline.stats()
            if hasattr(pipeline, 'decoder'):
                decoder_status['decoder'] = repr(pipeline.decoder)
        except Exception as error:  # pragma: no cover - diagnostics only
            decoder_status['error'] = repr(error)
        output_path_info = {
            'obsOutputPath': obs_renderer.output_path(),
            'previewOutputPath': preview_renderer.output_path(),
            'obsRendererStats': obs_renderer.stats(),
            'previewRendererStats': preview_renderer.stats(),
            'obsRhiBackend': getattr(obs_renderer, 'rhi_backend', lambda: None)(),
            'obsFallbackReason': obs_renderer.fallback_reason,
            'decoder': decoder_status,
            'decoderNote': ('default decoder factory is aiortc/FFmpeg software unless a '
                            'hardware factory is wired in; reported from the live pipeline'),
        }
    except Exception as error:  # pragma: no cover - diagnostics only
        output_path_info = {'error': repr(error)}

    # ----- process CPU accounting (ps is blocked in this sandbox) -----
    children_final = resource.getrusage(resource.RUSAGE_CHILDREN)
    children_cpu = ((children_final.ru_utime - children_baseline.ru_utime)
                    + (children_final.ru_stime - children_baseline.ru_stime))
    recorder_cpu = (report['capture']['recording'].get('recorder', {})
                    .get('recorderUsage', {}) or {}).get('cpuSeconds', 0.0)
    app_usage_summary = summarize_usage(app_usage)
    report['processUsage'] = {
        'appProcess': app_usage_summary,
        'remoteSource': {'cpuSeconds': max(0.0, children_cpu - recorder_cpu),
                         'averageCpuPercent': (max(0.0, children_cpu - recorder_cpu)
                                               / max(1e-9, args.warmup + args.duration)
                                               * 100)},
        'externalRecorder': recorder_usage,
        'note': ('appProcess is this harness+app process only; the synthetic source runs '
                 'in its own process like the phone encoder (libx264 ultrafast); the '
                 'external recorder is a third process. CPU% is user+system over wall time.'),
    }
    # ----- analysis -----
    try:
        journal = app_state['window'].journal.toPlainText()
        report['appLog'] = journal.splitlines()[-120:]
    except Exception as error:  # pragma: no cover - diagnostics only
        report['appLogError'] = repr(error)
    analysis = extract_capture_records(out_dir / 'acceptance-60fps-capture.mp4')
    summary = summarize_capture(
        [{'id': record['id'], 'ptsSeconds': record['ptsSeconds']}
         for record in analysis['records']],
        source_fps=args.fps,
        nominal_seconds=analysis['spanSeconds'] if analysis['spanSeconds'] else args.duration)
    summary['captureVideo'] = analysis['video']
    summary['captureSpanSeconds'] = analysis['spanSeconds']
    summary['nominalSeconds'] = args.duration
    report['recordingAnalysis'] = summary
    report['outputPath'] = output_path_info
    without_ids = summary['framesWithoutId']
    contamination = None
    if summary['framesCaptured'] and without_ids > max(2, .02 * summary['framesCaptured']):
        contamination = (
            f'{without_ids}/{summary["framesCaptured"]} captured frames had no decodable '
            f'frame ID: the OBS window was covered, frozen or otherwise contaminated '
            f'(another benchmark window?) - recording must not be used as acceptance evidence')
    report['capture']['contaminationSuspected'] = contamination
    report['appUsage'] = {'self': summarize_usage(app_usage), 'recorder': recorder_usage}
    report['appStats'] = {
        'samples': len(stats),
        'convertedFpsMean': latest_metric(stats, 'convertedFps', 'mean'),
        'convertedFpsMax': latest_metric(stats, 'convertedFps', 'max'),
        'receiveFpsMax': latest_metric(stats, 'fps', 'max'),
        'bitrateMbpsMax': latest_metric(stats, 'bitrate', 'max'),
        'receiveBitrateMbpsMean': latest_metric(stats, 'bitrate', 'mean'),
        'playoutMode': stats[-1].get('playoutMode') if stats else None,
        'playoutTargetMs': latest_metric(stats, 'playoutTargetMs', 'max'),
        'playoutQueuedFramesMax': latest_metric(stats, 'playoutQueuedFrames', 'max'),
        'playoutQueuedMiBMax': latest_metric(stats, 'playoutQueuedMiB', 'max'),
        'playoutQueueMsMax': latest_metric(stats, 'playoutQueueMs', 'max'),
        'playoutUnderflowsMax': latest_metric(stats, 'playoutUnderflows', 'max'),
        'playoutRebuffersMax': latest_metric(stats, 'playoutRebuffers', 'max'),
        'skippedTotalMax': latest_metric(stats, 'skipped', 'max'),
        'outputGapMsMax': latest_metric(stats, 'outputGapMs', 'max'),
        'frameGapMsMax': latest_metric(stats, 'frameGapMs', 'max'),
        'assemblyGapMsMax': latest_metric(stats, 'assemblyGapMs', 'max'),
        'packetGapMsMax': latest_metric(stats, 'packetGapMs', 'max'),
        'conversionMsMax': latest_metric(stats, 'conversionMs', 'max'),
        'lossPercentMax': latest_metric(stats, 'loss', 'max'),
        'queueDepths': {key: stats[-1].get(key) for key in (
            'playoutQueuedFrames', 'playoutQueuedMiB', 'playoutQueueMs',
            'playoutTargetMs')} if stats else None,
    }
    measured_codec = analysis['video']['codec']
    report['actual'] = {
        'source': {'fps': args.fps, 'width': args.width, 'height': args.height,
                   'profileBitrateMbpsCap': args.bitrate},
        'codec': measured_codec,
        'negotiatedCodec': [line for line in report.get('sender', {}).get('negotiated', [])
                            if line.startswith('a=rtpmap')],
        'decoder': 'aiortc 1.15.0 H264Decoder (FFmpeg software; PyAV)',
        'captureVideo': analysis['video'],
        'captureFrameSize': [analysis['video']['width'], analysis['video']['height']],
        'receiveBitrateMbps': report['appStats']['receiveBitrateMbpsMean'],
        'windowContentPixels': [report['capture']['window']['width'],
                                report['capture']['window']['height']],
        'recordingScaledTo': [args.capture_width, args.capture_height],
    }
    # ----- thresholds (plan §13.B initial targets) -----
    source_period_ms = 1000.0 / args.fps
    thresholds = {
        'minUniqueFramePercent': 99.5,
        'maxP99IntervalMs': 2 * source_period_ms,
        'maxUnexplainedHoldMs': 50.0,
        'maxUnderflows': 0,
    }
    intervals = summary['intervalStatsMs']
    underflows = report['appStats']['playoutUnderflowsMax'] or 0
    checks = {
        'captureEvidenceValid': capture_evidence_error(summary) is None,
        'uniqueFramePercent': (summary['observedUniquePercent'] is not None
                               and summary['observedUniquePercent']
                               >= thresholds['minUniqueFramePercent']),
        'p99Interval': (intervals['p99Ms'] is not None
                        and intervals['p99Ms'] <= thresholds['maxP99IntervalMs']),
        'unexplainedHolds': summary['holds']['overThreshold'] == 0,
        'underflows': underflows <= thresholds['maxUnderflows'],
    }
    evidence_error = capture_evidence_error(summary)
    report['thresholds'] = {'thresholds': thresholds, 'checks': checks,
                            'passed': all(checks.values()),
                            'evidenceError': evidence_error}
    report['status'] = ('capture-evidence-valid' if evidence_error is None
                        else 'capture-unavailable')
    report['failClosedReason'] = evidence_error
    report['notProven'] = [
        'recorder pipeline jitter is included in recording intervals; app-side stats '
        'are reported separately (supplementary, never a substitute)',
        'physical display latency vs OBS capture latency is not separated',
        'Android/Wi-Fi path is not exercised: the sender is the local bench source',
    ]
    report['finishedAt'] = now_iso()
    write_json(out_dir / 'acceptance-60fps-report.json', report)
    write_json(out_dir / 'app-stats.json', stats[-200:])
    print(json.dumps({'status': report['status'],
                      'capture': {'backend': backend['selected'],
                                  'recording': report['capture']['recording']},
                      'recordingAnalysis': {key: summary[key] for key in (
                          'framesCaptured', 'framesWithId', 'uniqueIds',
                          'expectedUniqueIds', 'observedUniquePercent', 'repeats',
                          'dropsWithinObserved', 'intervalStatsMs', 'holds')},
                      'appStats': report['appStats'],
                      'thresholds': report['thresholds'],
                      'failClosedReason': report['failClosedReason']},
                     indent=2, default=str, allow_nan=False))
    if evidence_error:
        log('FAIL CLOSED: ' + evidence_error)
        return 2
    if report['thresholds']['passed']:
        log('PASS: external capture proves the acceptance thresholds for this run')
        return 0
    log('thresholds not met (report written; no fabricated pass)')
    return 1 if args.require_pass else 0


def run_recording(args):
    """Analyse an existing OBS/ffmpeg recording without capturing live."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = Path(args.recording)
    report = {'schema': 'airframe-acceptance-60fps/1', 'mode': 'recording',
              'startedAt': now_iso(), 'command': ' '.join(sys.argv),
              'recording': {'path': str(path),
                            'bytes': path.stat().st_size if path.exists() else 0,
                            'sha256': sha256_file(path) if path.exists() else None}}
    if not path.is_file():
        report['status'] = 'capture-unavailable'
        report['failClosedReason'] = f'no such recording: {path}'
        write_json(out_dir / 'acceptance-60fps-report.json', report)
        log('FAIL CLOSED: ' + report['failClosedReason'])
        return 2
    analysis = extract_capture_records(path, max_frames=args.max_frames)
    summary = summarize_capture(
        [{'id': record['id'], 'ptsSeconds': record['ptsSeconds']}
         for record in analysis['records']],
        source_fps=args.fps, nominal_seconds=args.nominal_seconds)
    summary['captureVideo'] = analysis['video']
    summary['captureSpanSeconds'] = analysis['spanSeconds']
    error = capture_evidence_error(summary)
    report.update({'status': 'capture-evidence-valid' if error is None else 'capture-unavailable',
                   'failClosedReason': error, 'recordingAnalysis': summary})
    report['finishedAt'] = now_iso()
    write_json(out_dir / 'acceptance-60fps-report.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'failClosedReason',
                                                   'recordingAnalysis')},
                     indent=2, default=str, allow_nan=False))
    return 0 if error is None else 2


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default='artifacts/2026-10-30-stable60fps/acceptance')
    parser.add_argument('--duration', type=float, default=600.0,
                        help='capture duration seconds (plan §13.B: >=600; dry run shorter)')
    parser.add_argument('--warmup', type=float, default=10.0,
                        help='warm-up seconds before the measured capture')
    parser.add_argument('--probe-seconds', type=float, default=2.0)
    parser.add_argument('--startup-timeout', type=float, default=30.0)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--bitrate', type=int, default=24, help='profile bitrate cap (Mbps)')
    parser.add_argument('--max-size', type=int, default=2560)
    parser.add_argument('--profile-fps', type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--window-width', type=int, default=None,
                        help='OBS window content in points; default = source width/2 so '
                             'the window backing store is 1:1 with the decoded frame')
    parser.add_argument('--window-height', type=int, default=None)
    parser.add_argument('--window-x', type=int, default=0)
    parser.add_argument('--window-y', type=int, default=32)
    parser.add_argument('--capture-width', type=int, default=1280)
    parser.add_argument('--capture-height', type=int, default=720)
    parser.add_argument('--crf', type=int, default=23)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--recorder', default=None, help='ffmpeg binary path override')
    parser.add_argument('--avfoundation-device', default='Capture screen 0')
    parser.add_argument('--recording', default=None,
                        help='analyse an existing recording instead of capturing live')
    parser.add_argument('--nominal-seconds', type=float, default=None,
                        help='expected measurement interval for --recording mode')
    parser.add_argument('--max-frames', type=int, default=None)
    parser.add_argument('--require-pass', action='store_true',
                        help='exit 1 when acceptance thresholds are not met')
    args = parser.parse_args(argv)
    if args.window_width is None:
        args.window_width = max(320, args.width // 2)
    if args.window_height is None:
        args.window_height = max(180, args.height // 2)
    if args.recording:
        return run_recording(args)
    return run_live(args)


if __name__ == '__main__':
    raise SystemExit(main())
