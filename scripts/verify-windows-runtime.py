"""Native Windows portable check; requires an interactive Windows desktop.

Invoked by verify-windows.ps1 with the BUNDLED interpreter. It refuses macOS,
Linux, offscreen Qt and imported packages outside the selected bundle. Captures
only the known test patches in its own OBS test window; no full-desktop image is
saved. This checks startup/rendering/signaling, not phone/Wi-Fi/60-FPS performance.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import socket
import struct
import sys
import threading
import time
import traceback


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    report = {
        "status": "failed", "platform": sys.platform,
        "system": platform.platform(), "machine": platform.machine(),
        "python": sys.version, "executable": sys.executable,
        "pointerBits": struct.calcsize("P") * 8,
        "bundle": str(args.bundle.resolve()), "checks": [],
        "scope": "Native Windows startup, window pixels, controls and local H.264 WebRTC; no real phone, Wi-Fi or OBS recording/performance claim.",
    }
    app = window = server = None
    qt_message_api = None
    previous_qt_handler = None
    sender = None
    sender_stop, sender_done = threading.Event(), threading.Event()
    sender_errors = []

    def passed(name, **details):
        report["checks"].append({"check": name, "status": "passed", **details})
        print("PASS: " + name, flush=True)

    try:
        require(sys.platform == "win32", "This verification must run on real Windows; a Mac/Linux run cannot pass.")
        require(report["pointerBits"] == 64, "The portable package requires a 64-bit Windows interpreter.")
        require(os.environ.get("QT_QPA_PLATFORM", "windows").split(":")[0] == "windows",
                "Unset QT_QPA_PLATFORM: offscreen/minimal Qt is not a native Windows GUI check.")
        media = {}
        for name in ("mf.dll", "mfplat.dll", "mfreadwrite.dll"):
            try:
                ctypes.WinDLL(name, winmode=0x00000800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
                media[name] = "available"
            except OSError as exc:
                media[name] = str(exc)
        report["mediaFoundation"] = media
        require(all(value == "available" for value in media.values()),
                "Windows Media Foundation is unavailable. Windows N needs the Media Feature Pack; see mediaFoundation in this report.")
        passed("Windows x64 and Media Foundation prerequisites")

        os.environ["QT_LOGGING_RULES"] = os.environ.get("QT_LOGGING_RULES", "") + "\nqt.multimedia.plugin=true"
        qt_message_api = importlib.import_module("PySide6.QtCore")
        report["qtMessages"] = []

        def qt_message(kind, context, message):
            category = getattr(context, "category", "")
            level = getattr(kind, "name", str(kind))
            if len(report["qtMessages"]) < 400:
                report["qtMessages"].append({"level": level, "category": category, "message": message[:1600]})
                # Keep native diagnostics visible while bounding repeated logs.
                print(f"Qt [{level}] [{category}]: {message[:1600]}", file=sys.stderr, flush=True)
            else:
                report["qtMessagesDropped"] = report.get("qtMessagesDropped", 0) + 1
                if report["qtMessagesDropped"] == 1:
                    print("Qt message limit reached; further messages counted in the JSON report.", file=sys.stderr, flush=True)

        previous_qt_handler = qt_message_api.qInstallMessageHandler(qt_message)
        modules = ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
                   "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
                   "PySide6.QtSvg", "PySide6.QtTest", "av", "aiortc", "qrcode", "airframe.desktop")
        locations = {}
        bundle = args.bundle.resolve()
        for name in modules:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            require(path.is_relative_to(bundle), f"{name} imported outside the portable bundle: {path}")
            locations[name] = str(path)
        report["imports"] = locations
        report["versions"] = {name: importlib.metadata.version(name)
                              for name in ("airframe", "PySide6", "av", "aiortc", "qrcode")}
        passed("All native DLL-backed imports from the selected portable bundle")

        import shiboken6
        from av import VideoFrame
        from aiohttp import ClientSession
        from aiortc import (RTCConfiguration, RTCPeerConnection, RTCRtpSender,
                            RTCSessionDescription, VideoStreamTrack)
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QIcon, QWheelEvent
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QComboBox, QSlider
        from airframe import render
        from airframe.desktop import AudioOut, Desktop, Events, Frames, LocalServer
        import airframe

        app = QApplication([])
        app.setApplicationName("AirFrame Windows verification")
        app.setStyle("Fusion")
        require(app.platformName() == "windows", f"Expected native Windows Qt, got {app.platformName()}")
        require(app.primaryScreen() is not None, "An interactive Windows desktop is required.")
        assets = Path(airframe.__file__).parent / "assets"
        for name in ("chevron.svg", "check.svg"):
            require(QSvgRenderer(str(assets / name)).isValid(), f"SVG asset failed to load: {name}")
        icon = QIcon(str(assets / "airframe.png"))
        require(not icon.isNull(), "The bundled application icon failed to load.")
        app.setWindowIcon(icon)
        passed("Native Qt Windows platform, SVG assets and application icon")

        events, frames = Events(), Frames()
        server = LocalServer(free_port(), events, frames, AudioOut())
        server.start()
        window = Desktop(server, renderer_factory=lambda: render.create_renderer("auto"))
        window.show()
        require(QTest.qWaitForWindowExposed(window, 10000), "The native desktop window was not exposed.")

        def pump(seconds=0.08):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.003)

        def until(predicate, description, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                require(not sender_errors, "Synthetic sender failed: " + str(sender_errors))
                if predicate():
                    return
                time.sleep(0.004)
            raise RuntimeError("Timed out: " + description)

        original_style = window.styleSheet()
        areas = (window.connection_scroll, window.video_scroll, window.output_scroll)
        geometries = []
        for width, height, larger_text in ((880, 640, False), (1200, 900, False), (950, 780, True)):
            window.setStyleSheet(original_style + ("\nQWidget {font-size:16px;}" if larger_text else ""))
            window.resize(width, height)
            pump()
            require((window.width(), window.height()) == (width, height), "A layout minimum forced the window outside the requested size.")
            for index, area in enumerate(areas):
                window.settings_tabs.setCurrentIndex(index)
                pump()
                require(area.horizontalScrollBar().maximum() == 0, "Horizontal overflow: " + window.settings_tabs.tabText(index))
                require(area.widget().width() <= area.viewport().width(), "Settings content is wider than its viewport.")
                geometries.append({"window": [width, height], "font16": larger_text,
                                   "tab": window.settings_tabs.tabText(index),
                                   "horizontalRange": area.horizontalScrollBar().maximum()})
                if (width, height) == (1200, 900):
                    require(window.grab().save(str(args.out / f"desktop-tab-{index}.png")), "Cannot save widget screenshot.")
        report["layout"] = geometries
        passed("All settings tabs at compact/regular sizes and larger text")
        window.setStyleSheet(original_style)
        window.resize(950, 780)
        window.settings_tabs.setCurrentWidget(window.video_scroll)
        pump()

        wheel_checks = []
        for control in [*window.findChildren(QComboBox), *window.findChildren(QSlider)]:
            enabled = control.isEnabled()
            control.setEnabled(True)
            control.setFocus()
            before = control.currentIndex() if isinstance(control, QComboBox) else control.value()
            for angle, pixels in ((120, 0), (-120, 0), (0, 30), (0, -30)):
                center = control.rect().center()
                event = QWheelEvent(QPointF(center), QPointF(control.mapToGlobal(center)),
                                    QPoint(0, pixels), QPoint(0, angle),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.ScrollUpdate, False)
                QApplication.sendEvent(control, event)
                after = control.currentIndex() if isinstance(control, QComboBox) else control.value()
                require(after == before, f"Mouse wheel changed a closed control: {control.objectName() or type(control).__name__}")
                require(not event.isAccepted(), "The control swallowed the settings scroll gesture.")
            control.setEnabled(enabled)
            wheel_checks.append(type(control).__name__)
        combo = window.target_fps
        window.video_scroll.ensureWidgetVisible(combo)
        pump()
        old_index = combo.currentIndex()
        combo.showPopup()
        pump()
        view = combo.view()
        target = combo.model().index((old_index + 1) % combo.count(), 0)
        require(view.isVisible(), "The native dropdown did not open.")
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(target).center())
        pump()
        require(combo.currentIndex() == target.row() and not view.isVisible(), "Click selection in the open dropdown failed.")
        window.sync_profile_controls()
        passed("Closed controls reject wheel changes; open dropdown selects normally", controls=wheel_checks)

        # Two known frames: compare composited screen pixels, never QVideoWidget.grab().
        # BT.601 limited-range patches have independent, fixed RGB references.
        yuv = ((81, 90, 240), (145, 54, 34), (41, 240, 110), (235, 128, 128))
        expected = ((254, 0, 0), (0, 255, 1), (0, 0, 255), (255, 255, 255))

        def test_frame(scene=0):
            frame = VideoFrame(640, 360, "yuv420p")
            order = (0, 1, 2, 3) if scene == 0 else (1, 0, 3, 2)
            for plane_index, plane in enumerate(frame.planes):
                data = bytearray(plane.buffer_size)
                half_width, half_height = plane.width // 2, plane.height // 2
                for row in range(plane.height):
                    pair = 0 if row < half_height else 2
                    left, right = yuv[order[pair]][plane_index], yuv[order[pair + 1]][plane_index]
                    start = row * plane.line_size
                    data[start:start + plane.width] = bytes([left]) * half_width + bytes([right]) * (plane.width - half_width)
                plane.update(data)
            return frame

        screen_rows = []
        for scene in (0, 1):
            serial = frames.put(test_frame(scene))
            until(lambda: window.obs_pane.renderer.presented_serial == serial, "Frame presentation in the OBS test pane")
            if not window.obs.isVisible():
                window.open_obs()
            screen = window.obs.screen()
            geometry = screen.availableGeometry()
            window.obs.move(geometry.center() - window.obs.rect().center())
            window.obs.raise_()
            window.obs.activateWindow()
            pump(0.3)
            require(geometry.contains(window.obs.geometry()), "The OBS test window must fit on one visible screen.")
            captured = None
            samples = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pump(0.12)
                origin = window.obs.mapToGlobal(QPoint(0, 0))
                shot = screen.grabWindow(0, origin.x(), origin.y(), window.obs.width(), window.obs.height()).toImage()
                require(not shot.isNull(), "Windows screen capture is unavailable; unlock the interactive desktop.")
                order = (0, 1, 2, 3) if scene == 0 else (1, 0, 3, 2)
                samples = []
                for quadrant, patch in enumerate(order):
                    x = int(shot.width() * (0.25 if quadrant % 2 == 0 else 0.75))
                    y = int(shot.height() * (0.25 if quadrant < 2 else 0.75))
                    actual = shot.pixelColor(x, y).getRgb()[:3]
                    difference = max(abs(a - b) for a, b in zip(actual, expected[patch]))
                    samples.append({"expected": expected[patch], "actual": actual, "maxDifference": difference})
                if all(sample["maxDifference"] <= 12 for sample in samples):
                    captured = shot
                    break
            screen_rows.append({"scene": scene, "verified": captured is not None,
                                "size": [shot.width(), shot.height()], "patches": samples})
            report["screenPixelChecks"] = screen_rows
            require(captured is not None,
                    "The composited OBS test pixels did not match the known color patches. Keep its test window visible and unobscured; see screenPixelChecks.")
            require(captured.save(str(args.out / f"obs-composited-patches-{scene}.png")), "Cannot save verified test-window image.")
        dpr = window.obs.devicePixelRatioF()
        physical = render.physical_size(window.obs.size(), dpr)
        require(abs(physical.width() - 640) <= 1 and abs(physical.height() - 360) <= 1,
                "Exact OBS sizing does not match the test frame's physical pixels.")
        window.exact_output.setChecked(False)
        pump()
        require(window.obs.maximumWidth() > 640, "Disabling exact OBS size did not release its size constraint.")
        window.exact_output.setChecked(True)
        pump()
        report["renderer"] = {"requested": "auto", "mode": window.obs_pane.renderer.mode,
                              "outputPath": window.obs_pane.renderer.output_path(),
                              "rhiBackend": window.obs_pane.renderer.rhi_backend(),
                              "fallbackReason": window.obs_pane.renderer.fallback_reason,
                              "dpr": dpr}
        passed("Default auto renderer: two distinct composited YUV scenes and exact OBS size")
        window.open_obs()  # Hide the test output before checking the settings.

        class SyntheticTrack(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.index = 0

            async def recv(self):
                pts, time_base = await self.next_timestamp()
                frame = test_frame((self.index // 15) % 2)
                frame.pts, frame.time_base = pts, time_base
                self.index += 1
                return frame

        async def phone():
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peer.addTrack(SyntheticTrack())
            codecs = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == "video/H264"]
            require(codecs, "No H.264 sender codec available in the portable runtime.")
            peer.getTransceivers()[0].setCodecPreferences(codecs)
            try:
                async with ClientSession() as client:
                    async with client.ws_connect(f"http://127.0.0.1:{server.port}/signal") as ws:
                        await ws.send_json({"role": "sender", "credential": server.room.qr_token})
                        auth = await ws.receive_json(timeout=10)
                        require(auth.get("type") == "authenticated", "QR-token signaling authentication failed.")
                        await peer.setLocalDescription(await peer.createOffer())
                        await ws.send_json({"type": "offer", "session": "windows-native-verification", "sdp": peer.localDescription.sdp})
                        while True:
                            answer = await ws.receive_json(timeout=15)
                            if answer.get("type") == "answer":
                                break
                        await peer.setRemoteDescription(RTCSessionDescription(type="answer", sdp=answer["sdp"]))
                        while not sender_stop.is_set():
                            try:
                                message = await ws.receive_json(timeout=0.25)
                            except asyncio.TimeoutError:
                                continue
                            if message.get("type") == "configure":
                                await ws.send_json({"type": "configured", "requestId": message["requestId"],
                                                    "settings": message["settings"], "applied": True})
            finally:
                await peer.close()

        def run_phone():
            try:
                asyncio.run(phone())
            except Exception:
                sender_errors.append(traceback.format_exc())
            finally:
                sender_done.set()

        before = frames.serial
        sender = threading.Thread(target=run_phone, name="AirFrame-Windows-test-sender", daemon=True)
        sender.start()
        until(lambda: frames.serial >= before + 6 and window.surface.renderer.presented_serial > before,
              "Six actual H.264 frames over localhost WebRTC", seconds=30)
        received = frames.latest()[1]
        require(received is not None and (received.width, received.height) == (640, 360), "Decoded WebRTC frame dimensions are wrong.")
        window.settings_tabs.setCurrentWidget(window.video_scroll)
        window.apply_button.click()
        until(lambda: window.profile_ack_state == "applied", "Profile acknowledgement over signaling")
        report["webrtc"] = {"codec": "H264", "source": "synthetic-localhost", "decodedFrames": frames.serial - before,
                            "profileAckState": window.profile_ack_state}
        passed("Local H.264 encode/transport/decode, QR authentication and profile acknowledgement")
        sender_stop.set()
        until(sender_done.is_set, "Synthetic sender shutdown", seconds=10)
        until(lambda: "sender" not in server.room.peers, "Sender disconnect cleanup")
        report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        sender_stop.set()
        if sender is not None:
            sender.join(timeout=2)
        if window is not None:
            try:
                window.close()
                app.processEvents()
                shiboken6.delete(window.obs)
                shiboken6.delete(window)
            except Exception:
                report["cleanupError"] = traceback.format_exc()
                report["status"] = "failed"
        if server is not None:
            server.close()
        if qt_message_api is not None:
            qt_message_api.qInstallMessageHandler(previous_qt_handler)
        (args.out / "runtime-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
