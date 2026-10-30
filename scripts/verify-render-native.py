#!/usr/bin/env python
"""Stage 6 native verification: real window, GPU path, colour and CPU comparison.

Offscreen unit tests prove the contract; this script produces the evidence that
cannot be produced offscreen:

* the real window is drawn through Qt's RHI (Metal on macOS) with the YUV
  triplanar shader - read from QVideoSink.rhi() and Qt's own log messages;
* known YUV patches captured from the real window match the ITU reference
  tables within the tolerance fixed in tests_native/render_fixtures.py;
* BT.601+full (measured off on the RHI shader) is routed through the software
  conversion and stays colour-correct;
* the per-frame CPU cost of the old `frame.reformat(format="bgra")` path is
  compared with the new YUV copy on the same 2560x1440 frame, and both paths
  are driven for 120 frames at 60 Hz in a real window to compare intervals and
  process CPU time;
* an exact-size window (physical pixels equal to the frame) is captured and a
  1:1 checkerboard must arrive without a hidden downscale.

Run (needs a real display; macOS asks for Screen Recording permission for
capturing the window, and the window is briefly raised above other windows):

    QT_LOGGING_RULES="qt.multimedia.video.texturehelper=true" \
        .venv/bin/python scripts/verify-render-native.py

Results: artifacts/2026-10-30-stable60fps/native/render-real-*.json/.png
Declared vs actual: the JSON records `requested_mode`, `actual_output_path`,
`rhi_backend`, `rhi_device` and `texture_helper_messages`.
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QElapsedTimer, QPoint, QSize, Qt, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests_native"))

import render_fixtures as fx  # noqa: E402

import airframe.render as render  # noqa: E402

OUT_DIR = ROOT / "artifacts" / "2026-10-30-stable60fps" / "native"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_qt_messages = []


def _message_handler(mode, context, message):
    _qt_messages.append(message)


def _pump(seconds=0.05):
    app = QApplication.instance()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)


def _solid_frame(width, height, y, u, v, colorspace, video_range):
    return fx.solid_frame(width, height, y, u, v, colorspace=colorspace, video_range=video_range)


def _capture_window(widget):
    """Screen-grab the widget content (logical coords in, physical pixels out).

    Uses the content top-left (not frameGeometry) so a title bar can never be
    mistaken for video content, and retries until two consecutive grabs agree:
    the macOS window server can serve a one-frame-stale image right after a
    new QVideoFrame was submitted.
    """
    widget.raise_()
    previous = None
    for _ in range(8):
        _pump(0.15)
        origin = widget.mapToGlobal(QPoint(0, 0))
        shot = widget.screen().grabWindow(
            0, origin.x(), origin.y(), widget.width(), widget.height()
        ).toImage()
        if previous is not None and shot == previous:
            return shot
        previous = shot
    return previous


def _capture_stable(widget):
    return _capture_window(widget)


def _rhi_state(renderer):
    backend = renderer.rhi_backend()
    device = None
    try:
        rhi = renderer.video_sink().rhi()
        if rhi is not None:
            info = rhi.driverInfo()
            if info is not None:
                device = bytes(info.deviceName).decode("utf-8", "replace")
                backend = backend or rhi.backendName()
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"backend": backend, "device": device, "error": str(exc)}
    return {"backend": backend, "device": device}


def colour_evidence(window, renderer):
    """Capture known patches through the real window and compare with ITU tables."""
    results = []
    patches = [(16, 128, 128), (235, 128, 128), (63, 102, 240), (145, 54, 34),
               (81, 90, 240), (128, 255, 128), (128, 128, 255), (128, 128, 128)]
    tile = 128
    grid = 2
    frame_w, frame_h = tile * grid, tile * grid
    combos = [
        (render.MATRIX_BT601, render.RANGE_LIMITED, True),
        (render.MATRIX_BT709, render.RANGE_LIMITED, True),
        (render.MATRIX_BT709, render.RANGE_FULL, True),
    ]
    for matrix, spec_range, gpu_expected in combos:
        for half in (0, 1):
            group = patches[half * grid * grid:(half + 1) * grid * grid]
            from av.video.reformatter import ColorRange, Colorspace

            fmt = QVideoFrameFormat(QSize(frame_w, frame_h), QVideoFrameFormat.PixelFormat.Format_YUV420P)
            fmt.setColorSpace(
                QVideoFrameFormat.ColorSpace.ColorSpace_BT709
                if matrix == render.MATRIX_BT709
                else QVideoFrameFormat.ColorSpace.ColorSpace_BT601
            )
            fmt.setColorRange(
                QVideoFrameFormat.ColorRange.ColorRange_Full
                if spec_range == render.RANGE_FULL
                else QVideoFrameFormat.ColorRange.ColorRange_Video
            )
            qframe = QVideoFrame(fmt)
            assert qframe.map(QVideoFrame.MapMode.WriteOnly)
            for index in range(qframe.planeCount()):
                dst = qframe.bits(index)
                stride = qframe.bytesPerLine(index)
                tw = tile if index == 0 else tile // 2
                for patch_index, patch in enumerate(group):
                    tx, ty = patch_index % grid, patch_index // grid
                    value = patch[index] if index < 3 else 128
                    for row in range(tw):
                        offset = (ty * tw + row) * stride + tx * tw
                        dst[offset:offset + tw] = bytes([value]) * tw
            qframe.unmap()
            sink = renderer.video_sink()
            sink.setVideoFrame(qframe)
            window.raise_()
            _pump(0.12)
            shot = _capture_stable(window)
            # The video is letterboxed inside the window: map frame pixels through the
            # same fitted rect the renderer uses, then to device pixels.
            target = render.fitted_rect(QSize(frame_w, frame_h), window.size())
            for patch_index, patch in enumerate(group):
                tx, ty = patch_index % grid, patch_index // grid
                px = int((target.x() + tx * tile + tile / 2) * window.devicePixelRatioF())
                py = int((target.y() + ty * tile + tile / 2) * window.devicePixelRatioF())
                got = shot.pixelColor(min(px, shot.width() - 1), min(py, shot.height() - 1)).getRgb()[:3]
                expected = fx.reference_rgb(
                    patch[0], patch[1], patch[2], matrix, video_range=(spec_range == render.RANGE_LIMITED)
                )
                diff = max(abs(a - b) for a, b in zip(got, expected))
                results.append({
                    "matrix": matrix,
                    "range": spec_range,
                    "gpu_expected": gpu_expected,
                    "yuv": list(patch),
                    "expected": list(expected),
                    "captured": list(got),
                    "max_abs_diff": diff,
                    "within_tolerance": diff <= fx.GPU_CAPTURE_TOLERANCE,
                })
    return results


class LegacyBgraSurface(QWidget):
    """Faithful copy of the pre-stage-6 desktop.py painting path (old comparison base)."""

    def __init__(self):
        super().__init__()
        self.image = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def put(self, frame):
        converted = frame.reformat(format="bgra")
        plane = converted.planes[0]
        image = QImage(memoryview(plane), converted.width, converted.height, plane.line_size,
                       QImage.Format.Format_ARGB32)
        image._frame_owner = converted
        self.image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self.image is not None:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = self.image.rect()
            target.setSize(size)
            target.moveCenter(self.rect().center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self.image)
        painter.end()


def _drive_frames(app, deliver, count=120, period=1.0 / 60.0):
    """Drive `deliver` at a 60 Hz target and return intervals + CPU time per frame."""
    started = time.monotonic()
    cpu0 = time.process_time()
    intervals = []
    previous = None
    for index in range(count):
        target = started + (index + 1) * period
        while time.monotonic() < target:
            app.processEvents()
            remaining = target - time.monotonic()
            time.sleep(min(0.002, max(0.0, remaining)))
        deliver(index)
        app.processEvents()
        now = time.monotonic()
        if previous is not None:
            intervals.append((now - previous) * 1000.0)
        previous = now
    cpu = time.process_time() - cpu0
    return {
        "frames": count,
        "intervals_ms": {
            "p50": round(statistics.median(intervals), 3),
            "p95": round(sorted(intervals)[max(0, int(len(intervals) * 0.95) - 1)], 3),
            "p99": round(sorted(intervals)[max(0, int(len(intervals) * 0.99) - 1)], 3),
            "max": round(max(intervals), 3),
        },
        "wall_seconds": round(time.monotonic() - started, 3),
        "process_cpu_ms_total": round(cpu * 1000.0, 2),
        "process_cpu_ms_per_frame": round(cpu * 1000.0 / count, 3),
    }


def cpu_conversion_benchmark(frame, iterations=30):
    """Same decoded 2560x1440 frame: old BGRA reformat vs new YUV bridge copy."""
    def timed(fn):
        samples = []
        for _ in range(iterations):
            begin = QElapsedTimer()
            begin.start()
            fn()
            samples.append(begin.nsecsElapsed() / 1e6)
        return {
            "p50_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[max(0, int(iterations * 0.95) - 1)], 3),
            "max_ms": round(max(samples), 3),
        }

    # Interleave both paths so machine load (other agents run Docker concurrently)
    # hits both equally; keep the same decoded frame for both.
    old_samples = []
    new_samples = []
    for _ in range(iterations):
        begin = QElapsedTimer()
        begin.start()
        frame.reformat(format="bgra")
        old_samples.append(begin.nsecsElapsed() / 1e6)
        begin = QElapsedTimer()
        begin.start()
        render.qvideo_frame_from_av(frame)
        new_samples.append(begin.nsecsElapsed() / 1e6)

    def summarize(samples):
        ordered = sorted(samples)
        return {
            "p50_ms": round(statistics.median(samples), 3),
            "p95_ms": round(ordered[max(0, int(iterations * 0.95) - 1)], 3),
            "max_ms": round(max(samples), 3),
        }

    return {"width": frame.width, "height": frame.height, "iterations": iterations,
            "old_bgra_reformat": summarize(old_samples),
            "new_yuv_bridge_copy": summarize(new_samples)}


def exact_size_evidence(window, renderer):
    """Physical output must be the stream size: 1:1 checkerboard, no hidden downscale."""
    width, height = 1280, 720  # physical pixels; 640x360 logical at 2x on this Mac
    qframe = QVideoFrame(QVideoFrameFormat(QSize(width, height), QVideoFrameFormat.PixelFormat.Format_YUV420P))
    assert qframe.map(QVideoFrame.MapMode.WriteOnly)
    for index in range(qframe.planeCount()):
        dst = qframe.bits(index)
        stride = qframe.bytesPerLine(index)
        for row in range(qframe.mappedBytes(index) // stride):
            line = bytearray([16] * stride)
            for column in range(stride):
                if index == 0:
                    x = column
                    line[column] = 235 if ((x // 4) + (row // 4)) % 2 else 16
                else:
                    line[column] = 128
            dst[row * stride:(row + 1) * stride] = bytes(line)
    qframe.unmap()
    renderer.video_sink().setVideoFrame(qframe)
    window.raise_()
    _pump(0.12)
    shot = _capture_stable(window)
    dpr = window.devicePixelRatioF()
    logical = render.exact_window_size(QSize(width, height), dpr)
    physical = render.physical_size(logical, dpr)
    mismatches = 0
    samples = 0
    # The window is exact physical size: frame pixel (x, y) IS capture pixel (x, y).
    for cell_y in range(0, 8):
        for cell_x in range(0, 8):
            fx_px = 4 * cell_x + 2
            fx_py = 4 * cell_y + 2
            expect_white = (cell_x + cell_y) % 2 == 1
            cap_x = fx_px
            cap_y = fx_py
            if cap_x >= shot.width() or cap_y >= shot.height():
                continue
            got = shot.pixelColor(cap_x, cap_y).getRgb()[:3]
            expected_value = 255 if expect_white else 0
            samples += 1
            if abs(got[0] - expected_value) > 10 or abs(got[1] - expected_value) > 10:
                mismatches += 1
    return {
        "frame_physical": [width, height],
        "window_logical": [logical.width(), logical.height()],
        "window_physical": [physical.width(), physical.height()],
        "capture_size": [shot.width(), shot.height()],
        "device_pixel_ratio": dpr,
        "checker_samples": samples,
        "checker_mismatches": mismatches,
        "one_to_one": shot.width() == width and shot.height() == height and mismatches == 0,
    }


def main():
    qInstallMessageHandler(_message_handler)
    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    report = {
        "platform": app.platformName(),
        "screen": {
            "name": screen.name(),
            "logical": [screen.geometry().width(), screen.geometry().height()],
            "physical": [screen.size().width(), screen.size().height()],
            "device_pixel_ratio": screen.devicePixelRatio(),
            "refresh_rate_hz": screen.refreshRate(),
        },
        "qt": QApplication.instance().applicationVersion() or None,
        "pyside": None,
    }
    try:
        import PySide6

        report["pyside"] = PySide6.__version__
    except Exception:
        pass
    try:
        import av

        report["pyav"] = av.__version__
    except Exception:
        pass

    window = QWidget()
    window.setWindowTitle("AirFrame stage-6 renderer verification")
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.resize(640, 360)
    window.show()
    window.raise_()
    window.activateWindow()
    _pump(0.4)

    renderer = render.create_renderer("auto")
    renderer.widget().setParent(window)
    renderer.widget().resize(window.size())
    renderer.widget().show()
    _pump(0.2)
    report["renderer"] = {
        "requested_mode": renderer.requested_mode,
        "actual_mode": renderer.mode,
        "output_path": renderer.output_path(),
        "fallback_reason": renderer.fallback_reason,
        "type": type(renderer).__name__,
    }
    report["rhi"] = _rhi_state(renderer) if hasattr(renderer, "video_sink") else None

    # --- colour evidence on the real window ---------------------------------
    report["colour"] = colour_evidence(window, renderer)
    report["colour_summary"] = {
        "checks": len(report["colour"]),
        "within_tolerance": sum(1 for row in report["colour"] if row["within_tolerance"]),
        "tolerance": fx.GPU_CAPTURE_TOLERANCE,
        "unsupported_combo_route": renderer.stats().get("framesSoftwareRoute"),
        "unsupported_combo_reason": renderer.fallback_reason,
    }
    capture = _capture_window(window)
    capture.save(str(OUT_DIR / "render-real-colour-capture.png"))

    # BT.601+full is not verified on the RHI shader: the renderer must route it
    # through the software conversion and keep the colour correct.
    from av.video.reformatter import ColorRange as _CR, Colorspace as _CS

    software_checks = []
    for patch in ((63, 102, 240), (145, 54, 34), (16, 128, 128), (235, 128, 128)):
        frame = _solid_frame(256, 256, patch[0], patch[1], patch[2],
                             colorspace=_CS.ITU601, video_range=_CR.JPEG)
        prepared = renderer.prepare(frame, serial=9000 + len(software_checks))
        renderer.present(prepared)
        window.raise_()
        _pump(0.1)
        shot = _capture_stable(window)
        target = render.fitted_rect(QSize(256, 256), window.size())
        px = int((target.x() + target.width() / 2) * window.devicePixelRatioF())
        py = int((target.y() + target.height() / 2) * window.devicePixelRatioF())
        got = shot.pixelColor(min(px, shot.width() - 1), min(py, shot.height() - 1)).getRgb()[:3]
        expected = fx.reference_rgb(patch[0], patch[1], patch[2], render.MATRIX_BT601, video_range=False)
        diff = max(abs(a - b) for a, b in zip(got, expected))
        software_checks.append({
            "yuv": list(patch),
            "expected": list(expected),
            "captured": list(got),
            "max_abs_diff": diff,
            "within_tolerance": diff <= fx.GPU_CAPTURE_TOLERANCE,
        })
    report["software_route"] = {
        "checks": software_checks,
        "fallback_reason": renderer.fallback_reason,
        "stats": renderer.stats(),
        "note": "BT.601+full measured up to 26 codes off on the pinned Qt RHI; "
                "it is deliberately routed through the software conversion.",
    }

    from av.video.reformatter import ColorRange

    # --- CPU conversion comparison on the same 2560x1440 frame --------------
    big = fx.solid_frame(2560, 1440, 63, 102, 240, colorspace=None, video_range=ColorRange.MPEG)
    report["conversion_benchmark"] = cpu_conversion_benchmark(big)
    report["cpu_comparison"] = {
        "old_bgra_ms_per_frame": report["conversion_benchmark"]["old_bgra_reformat"]["p50_ms"],
        "new_yuv_ms_per_frame": report["conversion_benchmark"]["new_yuv_bridge_copy"]["p50_ms"],
    }

    # --- real-window sustained 120 frames at 60 Hz: GPU vs old BGRA path ----
    frames = [
        fx.solid_frame(1280, 720, 16 + (index % 200), 128 + (index % 60), 128, colorspace=None,
                       video_range=ColorRange.MPEG)
        for index in range(24)
    ]
    def deliver_gpu(index):
        frame = frames[index % len(frames)]
        prepared = renderer.prepare(frame, serial=index + 1)
        renderer.present(prepared)

    report["gpu_60hz_run"] = _drive_frames(app, deliver_gpu, count=120)
    report["gpu_stats"] = renderer.stats()

    legacy_window = LegacyBgraSurface()
    legacy_window.setWindowTitle("AirFrame legacy BGRA comparison")
    legacy_window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    legacy_window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    legacy_window.resize(640, 360)
    legacy_window.show()
    legacy_window.raise_()
    _pump(0.3)

    def deliver_legacy(index):
        legacy_window.put(frames[index % len(frames)])

    report["legacy_60hz_run"] = _drive_frames(app, deliver_legacy, count=120)
    legacy_window.close()

    # --- exact physical output size (no silent downscale) -------------------
    report["exact_size"] = exact_size_evidence(window, renderer)
    (OUT_DIR / "render-real-exact-size.json").write_text(json.dumps(report["exact_size"], indent=1))
    shot = _capture_stable(window)
    shot.save(str(OUT_DIR / "render-real-exact-size-capture.png"))

    report["texture_helper_messages"] = [
        message for message in _qt_messages
        if "texturehelper" in message or "HW decoding" in message or "yuv" in message.lower()
    ]
    report["unverified"] = [
        "Windows: no Windows machine on this stand; D3D11VA path and Windows Qt RHI were not run.",
        "OBS Studio recording itself was not automated in this script; the screen capture here "
        "reads the same composited window content OBS window capture would take, but a real OBS "
        "recording is still required for final acceptance (tracked for stage 6b).",
        "This proves content + RHI backend + CPU cost; it is not a display-refresh 60 FPS proof.",
    ]
    (OUT_DIR / "render-real-verification.json").write_text(json.dumps(report, indent=1))

    print(json.dumps({
        "renderer": report["renderer"],
        "rhi": report["rhi"],
        "colour": report["colour_summary"],
        "conversion_benchmark": report["conversion_benchmark"],
        "gpu_60hz_run": report["gpu_60hz_run"],
        "legacy_60hz_run": report["legacy_60hz_run"],
        "exact_size": report["exact_size"],
        "texture_helper_messages": report["texture_helper_messages"],
    }, indent=1))

    renderer.close()
    window.close()
    _pump(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
