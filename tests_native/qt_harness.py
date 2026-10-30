"""Shared harness for the native Qt suite (tests_native/).

This suite is intentionally separate from tests/: the Docker test image has no
PySide6, so every module under tests/ must stay headless. Run the native suite
with a Qt-capable Python plus the pinned Python dependencies (qrcode) installed
into a workspace-local dir, leaving .venv untouched:

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

"Offscreen" proves the UI/profile contract, not GPU acceleration, a real
display, QR scanning, OBS capture, or an Android device. Those are separate
native runs.
"""
import asyncio
import socket
import threading
import time

import shiboken6
from aiohttp import WSMsgType
from PySide6.QtWidgets import QApplication

from airframe.desktop import Desktop, Events, Frames, LocalServer
from airframe.server import Room




_app = None


def qt_app():
    """One QApplication per test process."""
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
        _app.setApplicationName("AirFrame native tests")
    return _app


def pump_until(predicate, timeout=8.0, description="condition"):
    """Process Qt events until predicate() is true; fail with a clear message."""
    app = qt_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError(f"Timed out after {timeout:.1f}s waiting for {description}")


def pump_for(seconds=0.2):
    """Process Qt events for a bounded period (used only for negative waits)."""
    app = qt_app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def close_window(window):
    """Close and delete windows while the QApplication still exists (avoids exit segfault)."""
    qt_app()
    window.close()
    qt_app().processEvents()
    shiboken6.delete(window.obs)
    shiboken6.delete(window)


class StubServer:
    """LocalServer stand-in for pure UI tests: records calls, owns no sockets/threads."""

    def __init__(self):
        self.room = Room()
        self.frames = Frames()
        self.events = Events()
        self.port = 9
        self.audio = None
        self.receiver = None
        self.playout_modes = []
        self.profiles = []

    def rotate(self):
        self.room.rotate_code()

    def set_playout(self, mode):
        self.playout_modes.append(mode)

    def configure(self, settings, request_id=""):
        self.profiles.append((dict(settings), request_id))

    def set_volume(self, value):
        pass

    def set_audio(self, enabled):
        pass

    def stop_stream(self):
        pass

    def close(self):
        pass


class FakeSender:
    """A real signaling sender peer with deterministic profile acknowledgements.

    The test drives the same WebSocket messages a phone sends, so profile
    ack handling is exercised through the production signaling path instead of
    being mocked away. `auto_ack`: None -> never ack (timeout), True/False ->
    accept/reject every `configure`, dict -> override applied/settings.
    """

    def __init__(self, port, credential, auto_ack=None):
        self.port, self.credential, self.auto_ack = port, credential, auto_ack
        self.messages = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.failure = None
        self.loop = None
        self.ws = None
        self.thread = threading.Thread(target=self._run, name="airframe-fake-sender", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise AssertionError("Fake sender did not connect to signaling within 5s")
        if self.failure is not None:
            raise AssertionError(f"Fake sender failed: {self.failure!r}")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(self._main())
        except Exception as exc:  # surfaced by start()/close()
            self.failure = self.failure or exc
            self.ready.set()
        finally:
            self.loop.close()

    async def _main(self):
        import aiohttp
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{self.port}/signal") as ws:
                self.ws = ws
                await ws.send_json({"role": "sender", "credential": self.credential})
                self.ready.set()
                async for packet in ws:
                    if packet.type != WSMsgType.TEXT:
                        continue
                    message = packet.json()
                    with self.lock:
                        self.messages.append(message)
                    if message.get("type") == "configure" and self.auto_ack is not None:
                        override = self.auto_ack if isinstance(self.auto_ack, dict) else {}
                        await ws.send_json({
                            "type": "configured",
                            "requestId": message.get("requestId", ""),
                            "applied": bool(override.get("applied", self.auto_ack is not False)),
                            "settings": override.get("settings", message["settings"]),
                        })

    def send(self, message):
        future = asyncio.run_coroutine_threadsafe(self.ws.send_json(message), self.loop)
        future.result(5)

    def received(self, kind):
        with self.lock:
            return [message for message in self.messages if message.get("type") == kind]

    def close(self):
        if self.ws is not None and self.loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop).result(5)
            except Exception:
                pass
        self.thread.join(5)


class ProfileScenario:
    """Fresh real LocalServer + Desktop (+ optional real signaling sender peer)."""

    def __init__(self, sender=False, auto_ack=None):
        qt_app()
        self.events = Events()
        self.frames = Frames()
        self.port = free_port()
        self.server = LocalServer(self.port, self.events, self.frames)
        self.server.start()
        self.window = Desktop(self.server)
        self.window.show()
        self.sender = None
        if sender:
            self.sender = FakeSender(self.port, self.server.room.qr_token, auto_ack)
            self.sender.start()
            pump_until(lambda: "sender" in self.server.room.peers, timeout=5,
                       description="fake sender attached to the session")

    def close(self):
        try:
            if self.sender is not None:
                self.sender.close()
        finally:
            close_window(self.window)
            self.window = None
