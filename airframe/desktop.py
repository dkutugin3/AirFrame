"""Native Qt controls, FFmpeg-decoded WebRTC frames, and a clean OBS window."""
import argparse
import asyncio
from collections import deque
from pathlib import Path
import sys
import threading
import time
import secrets

import qrcode
import shiboken6

from aiohttp import web
from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtMultimedia import QAudioFormat, QAudioSink
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLayout, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from . import render as video_render
from .adaptive import AdaptiveQuality
from .receiver import Receiver
from .server import (BASE_PROFILE, Room, TelemetrySampler, create_app,
                     format_telemetry_lines, telemetry_bottleneck, video_settings)
from .widgets import WheelSafeComboBox, WheelSafeSlider

PRESETS = {
    # The experimental 120 fps preset stays selectable but is never a default.
    "Максимум · 120 fps": {"fps": 120, "bitrate": 80, "maxSize": 2560},
    "Стабильность · 30 fps": {"fps": 30, "bitrate": 10, "maxSize": 1920},
    # Lighter 60 fps profile for weaker machines.
    "Плавность · 60 fps": {"fps": 60, "bitrate": 16, "maxSize": 1920},
    # Base profile of a new session, shared with airframe.server.BASE_PROFILE.
    "Качество · 60 fps": dict(BASE_PROFILE),
}
PROFILE_ACK_TIMEOUT_MS = 10000
ACK_PENDING = "pending"
ACK_SAVED = "saved"
ACK_APPLIED = "applied"
ACK_REJECTED = "rejected"
ACK_TIMEOUT = "timeout"
PLAYOUT_LABELS = {"stable": "стабильность для OBS", "balanced": "баланс", "low": "минимальная задержка"}
STYLE = """
QWidget {background:#101416;color:#edf1f2;font-size:13px;}
QLabel {background:transparent;}
QFrame#panel {background:#191f23;border:1px solid #2b343a;border-radius:12px;}
QLabel#brand {font-size:25px;font-weight:650;color:#b6f5a1;}
QLabel#title {font-size:18px;font-weight:600;}
QLabel#muted {color:#9aa7af;}
QLabel#eyebrow {color:#b6f5a1;font-size:12px;font-weight:600;}
QLabel#status {background:#1e3028;color:#b6f5a1;border-radius:15px;padding:8px 12px;}
QLabel#qr {background:white;border:4px solid white;border-radius:10px;}
QLabel#code {color:#b6f5a1;background:#12221c;font-size:28px;font-weight:600;padding:8px;border-radius:8px;}
QLabel#metric {font-size:26px;font-weight:500;}
QPushButton {background:#252f35;border:1px solid #3b484f;border-radius:8px;padding:10px 13px;}
QPushButton:hover {background:#303e43;border-color:#63746c;}
QPushButton:pressed {background:#1a262b;}
QPushButton:focus {border:1px solid #b6f5a1;}
QPushButton#primary {background:#b6f5a1;color:#192a14;font-weight:600;border:1px solid #b6f5a1;}
QPushButton#primary:hover {background:#cbffb9;}
QPushButton:disabled {color:#6c7a81;background:#191f23;border-color:#2b343a;}
QComboBox {background:#222c32;color:#edf1f2;border:1px solid #3b484f;border-radius:8px;padding:9px 30px 9px 11px;min-height:19px;}
QComboBox:hover {border-color:#63746c;}
QComboBox:focus {border-color:#b6f5a1;}
QComboBox::drop-down {subcontrol-origin:padding;subcontrol-position:top right;width:26px;border:0;}
QComboBox::down-arrow {image:url("__ASSETS__/chevron.svg");width:12px;height:12px;}
QComboBox QAbstractItemView {background:#222c32;color:#edf1f2;selection-background-color:#365043;selection-color:#edf1f2;border:1px solid #3b484f;padding:4px;outline:0;}
QSlider {background:transparent;min-height:24px;}
QSlider::groove:horizontal {background:#35443d;height:5px;border-radius:2px;}
QSlider::sub-page:horizontal {background:#85c99a;border-radius:2px;}
QSlider::handle:horizontal {background:#b6f5a1;width:16px;margin:-6px 0;border-radius:8px;}
QSlider::handle:horizontal:disabled {background:#64746b;}
QCheckBox {background:transparent;spacing:9px;min-height:24px;}
QCheckBox::indicator {width:16px;height:16px;border:1px solid #65756e;border-radius:4px;background:#151d20;}
QCheckBox::indicator:hover {border-color:#b6f5a1;}
QCheckBox::indicator:checked {background:#b6f5a1;border-color:#b6f5a1;image:url("__ASSETS__/check.svg");}
QTabWidget::pane {background:#191f23;border:1px solid #2b343a;border-radius:10px;top:-1px;}
QTabBar::tab {background:transparent;color:#9aa7af;border-bottom:2px solid transparent;padding:12px 10px;}
QTabBar::tab:selected {color:#b6f5a1;border-bottom-color:#b6f5a1;}
QTabBar::tab:hover {color:#edf1f2;}
QWidget#settingsPage, QScrollArea#settingsScroll {background:#191f23;border:0;}
QScrollBar:vertical {background:transparent;width:8px;margin:5px 0;}
QScrollBar::handle:vertical {background:#46554f;border-radius:4px;min-height:36px;}
QScrollBar::handle:vertical:hover {background:#658070;}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:transparent;}
QPlainTextEdit {background:#151b1f;border:1px solid #303b42;border-radius:8px;color:#aebbc3;font-family:monospace;font-size:12px;}
""".replace("__ASSETS__", Path(__file__).with_name("assets").as_posix())
APP_ICON = Path(__file__).with_name("assets") / "airframe.png"


class Events(QObject):
    state = Signal(str, str)
    stats = Signal(object)
    log = Signal(str)


class FrameNotice(QObject):
    wake = Signal()
    changed = Signal()

    def __init__(self, frames):
        super().__init__()
        self.frames = frames
        self.wake.connect(self.dispatch, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def dispatch(self):
        with self.frames.lock:
            self.frames.notification_pending = False
        self.changed.emit()


class Frames:
    """Latest-ready-frame mailbox: the decode thread prepares each visible output.

    ``put`` is called from the decoded-frame worker thread (see Receiver). It
    stores the YUV frame and prepares it for every *active* renderer, so the
    per-frame YUV bridge copy happens off the GUI thread. The GUI is notified
    once per batch and presents only the newest prepared frame per window, so
    notifications never accumulate and a hidden window costs nothing.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.serial = 0
        self.frame = None
        self.renderers = {}
        self.ready = {}
        self.preview_stride = 1
        self.notification_pending = False
        self.notice = FrameNotice(self)
        self._legacy_serial = 0
        self._legacy_image = None

    def attach_renderer(self, key, renderer):
        with self.lock:
            self.renderers[key] = renderer

    def detach_renderer(self, key):
        with self.lock:
            self.renderers.pop(key, None)

    def set_preview_stride(self, stride):
        """Skip preview frames on the decode thread while the OBS window is open."""
        with self.lock:
            self.preview_stride = max(1, int(stride))

    def put(self, frame):
        with self.lock:
            self.serial += 1
            serial = self.serial
            self.frame = frame
            renderers = list(self.renderers.items())
            stride = self.preview_stride
            notify = not self.notification_pending
            self.notification_pending = True
        ready = {}
        if frame is not None:
            for key, renderer in renderers:
                try:
                    if not renderer.wants_frame():
                        continue
                    if key == "preview" and stride > 1 and serial % stride == 0:
                        continue
                    ready[key] = renderer.prepare(frame, serial=serial)
                except Exception as error:
                    # One broken output must not stop the other one or the mailbox.
                    renderer.note_prepare_error(error)
                    continue
        with self.lock:
            # Keep the newest prepared frame per output: a dispatch may be
            # pending while this put prepares, and a pane whose frame was
            # skipped by the preview stride must not lose its earlier one.
            for key, prepared in ready.items():
                self.ready[key] = prepared
            # C1: a dispatch may have consumed the previous batch while this
            # put was preparing; re-arm the notification so the newest frame
            # cannot be left unpresented after a slow prepare.
            if not notify and not self.notification_pending:
                notify = True
                self.notification_pending = True
        if notify:
            self.notice.wake.emit()
        return serial

    def take_ready(self):
        with self.lock:
            ready, self.ready = self.ready, {}
            return ready

    def latest(self):
        with self.lock:
            if self.serial == 0:
                return None
            return self.serial, self.frame

    def get(self):
        """Legacy helper for the native smoke scripts: ``(serial, QImage)``.

        The main path never calls this; it is the old conversion kept only so
        scripts/smoke-*.py keep checking the session end state.
        """
        with self.lock:
            serial, frame = self.serial, self.frame
        if serial != self._legacy_serial:
            self._legacy_serial = serial
            self._legacy_image = None if frame is None else video_render.software_image_from_av(frame)
        return serial, None if frame is None else self._legacy_image

    def clear(self):
        with self.lock:
            self.frame = None
            self.ready = {}


class AudioOut(QObject):
    """Plays WebRTC phone audio on the default output; OBS captures it as desktop audio."""
    pushed = Signal(bytes)

    def __init__(self):
        super().__init__()
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.sink = QAudioSink(fmt)
        self.sink.setBufferSize(16384)
        self.io = None
        self.pushed.connect(self.write, Qt.ConnectionType.QueuedConnection)

    def put(self, data):
        self.pushed.emit(data)

    @Slot(bytes)
    def write(self, data):
        if self.io is None:
            self.io = self.sink.start()
        self.io.write(data)

    def stop(self):
        self.pushed.disconnect(self.write)
        if self.io is not None:
            self.sink.stop()
            self.io = None


class LocalServer:
    def __init__(self, port, events, frames, audio=None):
        self.port, self.events, self.frames, self.audio = port, events, frames, audio
        self.room = Room()
        self.receiver = None
        self.started = threading.Event()
        self.error = None
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self.run, name="AirFrame-WebRTC", daemon=True)

    def run(self):
        async def serve():
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            self.receiver = Receiver(self.room, self.frames.put, self.events.state.emit,
                                     self.events.stats.emit, self.events.log.emit,
                                     self.audio.put if self.audio else None)
            self.room.peers["receiver"] = self.receiver
            runner = web.AppRunner(create_app(self.room), access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, "0.0.0.0", self.port).start()
                self.started.set()
                await self.stop_event.wait()
            except Exception as exc:
                self.error = exc
                self.started.set()
            finally:
                await runner.cleanup()
        asyncio.run(serve())

    def start(self):
        self.thread.start()
        if not self.started.wait(10):
            raise RuntimeError("Сервер подключения не запустился за 10 секунд")
        if self.error:
            raise RuntimeError(f"Не удалось открыть порт {self.port}: {self.error}")

    def submit(self, coroutine):
        if self.loop is None or self.loop.is_closed():
            # Late GUI/queued events must not touch a stopped server loop.
            coroutine.close()
            return None
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        def completed(f):
            try:
                f.result()
            except Exception as exc:
                self.events.log.emit(str(exc))
        future.add_done_callback(completed)
        return future

    def configure(self, settings, request_id=""):
        async def apply():
            requested = video_settings(settings)
            sender_connected = "sender" in self.room.peers
            # The wish is saved for the next connection immediately; while a
            # phone is connected it becomes effective only after its ack.
            wish = self.room.request_profile(requested, request_id)
            if not sender_connected:
                self.events.stats.emit({"configured": {"requestId": request_id, "saved": True,
                                                       "settings": wish,
                                                       "effective": dict(self.room.effective_profile)}})
                return
            await self.room.send("sender", {"type": "configure", "settings": wish,
                                            "requestId": request_id})
        self.submit(apply())

    def set_playout(self, mode):
        async def change():
            self.receiver.set_playout(mode)
        self.submit(change())

    def set_volume(self, value):
        async def change():
            await self.room.send("sender", {"type": "volume", "value": value})
        self.submit(change())

    def set_audio(self, enabled):
        async def change():
            await self.room.send("sender", {"type": "audio", "enabled": enabled})
        self.submit(change())

    def stop_stream(self):
        async def stop():
            await self.room.send("sender", {"type": "stop"})
            await self.receiver.close_peer()
            self.events.state.emit("idle", "Трансляция завершена")
        self.submit(stop())

    def rotate(self):
        self.loop.call_soon_threadsafe(self.room.rotate_code)

    def close(self):
        if self.loop and self.stop_event and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(timeout=5)


class VideoPane(QWidget):
    """One video output surface: the renderer widget plus the idle preview placeholder."""

    def __init__(self, renderer_factory, preview=False, parent=None, always_active=False):
        super().__init__(parent)
        self.preview = preview
        self.renderer = renderer_factory()
        self._last_metric_at = time.monotonic()
        self._previous_presented = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.renderer.widget())
        self.placeholder = QLabel(
            "Экран телефона появится здесь\n\nПодключите Android во вкладке «Подключение»",
            self,
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.placeholder.setStyleSheet("QLabel {background:#131b1a;color:#a8bdb0;font-size:16px;padding:24px;}")
        self.placeholder.setVisible(bool(preview))
        # The OBS output pane keeps preparing and holding the newest frame even
        # while its window is closed: opening the window must show it at once,
        # and a broken preview must not stop the recording output.
        self.always_active = always_active
        self.renderer.set_active(always_active)

    def set_active(self, active):
        if not self.always_active:
            self.renderer.set_active(active)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.placeholder.setGeometry(self.rect())

    def showEvent(self, event):
        super().showEvent(event)
        self.renderer.set_active(True)
        self.placeholder.setGeometry(self.rect())

    def hideEvent(self, event):
        super().hideEvent(event)
        self.renderer.set_active(False)

    def present(self, prepared):
        if self.renderer.present(prepared, hidden_ok=self.always_active):
            if self.preview:
                self.placeholder.hide()

    def refresh_latest(self, frames):
        """Show the newest mailbox frame after becoming visible (one conversion)."""
        latest = frames.latest()
        if latest is None or latest[1] is None:
            return
        serial, frame = latest
        if self.renderer.presented_serial == serial:
            return
        self.present(self.renderer.prepare(frame, serial=serial))

    def clear(self):
        self.renderer.clear()
        if self.preview:
            self.placeholder.show()
            self.placeholder.setGeometry(self.rect())

    def current_image(self):
        return self.renderer.last_image()

    @property
    def painted_frames(self):
        return self.renderer.stats()["framesPresented"]

    def presentation_fps(self):
        now = time.monotonic()
        presented = self.painted_frames
        fps = (presented - self._previous_presented) / max(.001, now - self._last_metric_at)
        self._previous_presented, self._last_metric_at = presented, now
        return fps


def text(value, kind="", wrap=False):
    item = QLabel(value)
    item.setObjectName(kind)
    item.setWordWrap(wrap)
    if wrap:
        item.setMinimumWidth(0)
        item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return item


def panel():
    widget = QFrame()
    widget.setObjectName("panel")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(18,18,18,18)
    layout.setSpacing(12)
    return widget, layout


def settings_page():
    """A width-constrained settings page with vertical scrolling only."""
    area = QScrollArea()
    area.setObjectName("settingsScroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    page = QWidget()
    page.setObjectName("settingsPage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(18,18,18,18)
    layout.setSpacing(12)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
    area.setWidget(page)
    return area, layout


class Desktop(QMainWindow):
    def __init__(self, server, renderer_factory=None):
        super().__init__()
        self.server = server
        self.renderer_factory = renderer_factory or (lambda: video_render.create_renderer("auto"))
        self.setWindowTitle("AirFrame")
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1220,820)
        self.setMinimumSize(880,640)
        self.setStyleSheet(STYLE)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24,20,24,18)
        layout.setSpacing(18)
        header = QHBoxLayout()
        header.addWidget(text("◉  airframe", "brand"))
        header.addStretch()
        self.status = text("●  Ожидание телефона", "status")
        header.addWidget(self.status)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(12)
        preview_header = QHBoxLayout()
        preview_header.addWidget(text("Предпросмотр", "title"))
        preview_header.addStretch()
        preview_header.addWidget(text("ANDROID → КОМПЬЮТЕР", "muted"))
        left.addLayout(preview_header)
        self.surface = VideoPane(self.renderer_factory, preview=True)
        self.surface.setMinimumSize(240,180)
        left.addWidget(self.surface,1)
        self.detail = text("Готов к подключению по Wi-Fi", "muted", True)
        left.addWidget(self.detail)
        metrics, metrics_layout = panel()
        row = QHBoxLayout()
        self.fps = text("—", "metric")
        self.bitrate = text("—", "metric")
        self.jitter = text("—", "metric")
        for title, value in (("FPS",self.fps),("Мбит/с",self.bitrate),("Джиттер, мс",self.jitter)):
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(value)
            column.addWidget(text(title,"muted"))
            row.addLayout(column,1)
        metrics_layout.addLayout(row)
        self.loss = text("Потери: — · размер: —", "muted", True)
        metrics_layout.addWidget(self.loss)
        left.addWidget(metrics)
        self.obs_button = QPushButton("Открыть окно для OBS  ↗")
        self.obs_button.setObjectName("primary")
        self.obs_button.clicked.connect(self.open_obs)
        left.addWidget(self.obs_button)
        self.stop = QPushButton("Завершить трансляцию")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(server.stop_stream)
        left.addWidget(self.stop)
        left.addWidget(text("Для плавной передачи: Wi-Fi 5/6 на 5 или 6 ГГц. "
                            "Компьютер лучше подключить по Ethernet.","muted",True))
        body.addLayout(left,1)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.setMinimumWidth(360)
        self.settings_tabs.setMaximumWidth(400)
        self.settings_tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.settings_tabs.setDocumentMode(True)
        self.settings_tabs.tabBar().setDrawBase(False)
        self.connection_scroll, cl = settings_page()
        self.settings_scroll = self.connection_scroll
        self.video_scroll, ql = settings_page()
        self.output_scroll, ol = settings_page()
        self.settings_tabs.addTab(self.connection_scroll, "Подключение")
        self.settings_tabs.addTab(self.video_scroll, "Видео")
        self.settings_tabs.addTab(self.output_scroll, "Звук и OBS")

        cl.addWidget(text("Сканируйте QR в AirFrame на Android, затем разрешите захват экрана.","muted",True))
        self.qr = QLabel()
        self.qr.setObjectName("qr")
        self.qr.setFixedSize(232,232)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.qr,0,Qt.AlignmentFlag.AlignHCenter)
        self.qr_payload = None
        cl.addWidget(text("Код для ручного подключения","muted"))
        self.code = text("", "code")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.code)
        code_actions = QHBoxLayout()
        self.expires = text("Код действует 10 минут","muted",True)
        code_actions.addWidget(self.expires,1)
        rotate = QPushButton("Обновить код")
        rotate.clicked.connect(server.rotate)
        code_actions.addWidget(rotate)
        cl.addLayout(code_actions)
        cl.addSpacing(4)
        cl.addWidget(text("Адрес компьютера","muted"))
        self.addresses = WheelSafeComboBox()
        self.refresh_addresses()
        cl.addWidget(self.addresses)
        self.addresses.currentTextChanged.connect(lambda _: self.refresh_code())
        cl.addWidget(text("Телефон и компьютер должны быть в одной локальной сети.","muted",True))
        cl.addStretch()

        ql.addWidget(text("Качество трансляции", "title"))
        ql.addWidget(text("Профиль","muted"))
        self.profile = WheelSafeComboBox()
        self.profile.addItems([*PRESETS,"Свой профиль"])
        ql.addWidget(self.profile)
        row = QHBoxLayout()
        fps_column = QVBoxLayout()
        fps_column.setSpacing(6)
        fps_column.addWidget(text("Частота кадров","muted"))
        self.target_fps = WheelSafeComboBox()
        for rate in (30,60,120):
            self.target_fps.addItem(f"{rate} fps",rate)
        fps_column.addWidget(self.target_fps)
        size_column = QVBoxLayout()
        size_column.setSpacing(6)
        size_column.addWidget(text("Разрешение","muted"))
        self.max_size = WheelSafeComboBox()
        for size in (1280,1920,2560):
            self.max_size.addItem(f"{size} px",size)
        size_column.addWidget(self.max_size)
        row.addLayout(fps_column,1)
        row.addLayout(size_column,1)
        ql.addLayout(row)
        ql.addWidget(text("Разрешение ограничивает длинную сторону кадра. Пропорции сохраняются.","muted",True))
        self.rate_label = text("Битрейт: максимум 24 Мбит/с","muted",True)
        ql.addWidget(self.rate_label)
        self.rate = WheelSafeSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(4,80)
        ql.addWidget(self.rate)
        self.hevc = QCheckBox("Предпочесть HEVC/H.265")
        self.hevc.setChecked(bool(self.server.room.saved_profile.get("preferHevc", False)))
        self.hevc.toggled.connect(self.custom_profile)
        ql.addWidget(self.hevc)
        ql.addWidget(text("Лучше качество при том же битрейте; H.264 остаётся запасным кодеком.",
                          "muted", True))
        self.apply_button = apply = QPushButton("Применить профиль")
        apply.setObjectName("primary")
        apply.clicked.connect(self.apply_profile)
        ql.addWidget(apply)
        self.profile_result = text("Профиль ещё не отправлен", "muted", True)
        self.pending_profile = None
        self.profile_ack_state = None
        self.profile_ack_timeout_ms = PROFILE_ACK_TIMEOUT_MS
        self.confirmed_profile = dict(video_settings(self.server.room.settings))
        self.telemetry = TelemetrySampler()
        self.last_bottleneck = None
        self.telemetry_logged_at = 0.0
        self.profile_timeout = QTimer(self)
        self.profile_timeout.setSingleShot(True)
        self.profile_timeout.timeout.connect(self.profile_timed_out)
        ql.addWidget(self.profile_result)
        ql.addSpacing(6)
        self.smoothness = QCheckBox("Приоритет плавности")
        self.smoothness.setChecked(False)
        self.smoothness.toggled.connect(self.smoothness_toggled)
        ql.addWidget(self.smoothness)
        ql.addWidget(text("Если кодировщик не справляется, снижать 2560 → 1920 px.","muted",True))
        self.adaptive = AdaptiveQuality(base=video_settings(dict(self.server.room.saved_profile)),
                                        floor=1920, priority="quality")
        self.adaptive_now = time.monotonic
        ql.addWidget(text("120 fps — экспериментальный режим. Фактическая частота зависит от телефона, сети и компьютера.","muted",True))
        ql.addStretch()

        ol.addWidget(text("Вывод в OBS", "title"))
        ol.addWidget(text("Плавность вывода","muted"))
        self.playout_mode = WheelSafeComboBox()
        self.playout_mode.addItem("Стабильность OBS · 250–350 мс", "stable")
        self.playout_mode.addItem("Баланс · 120–200 мс", "balanced")
        self.playout_mode.addItem("Минимальная задержка · без запаса", "low")
        self.playout_mode.currentIndexChanged.connect(self.playout_selected)
        ol.addWidget(self.playout_mode)
        ol.addWidget(text("Запас кадров сглаживает сетевые паузы и добавляет задержку.","muted",True))
        self.exact_output = QCheckBox("Точный размер окна OBS")
        self.exact_output.setChecked(True)
        self.exact_output.toggled.connect(self.exact_output_toggled)
        ol.addWidget(self.exact_output)
        ol.addWidget(text("Один пиксель кадра соответствует одному физическому пикселю окна.","muted",True))
        ol.addSpacing(8)
        ol.addWidget(text("Звук телефона", "title"))
        self.audio_capture = QCheckBox("Передавать звук телефона")
        self.audio_capture.setChecked(True)
        self.audio_capture.toggled.connect(self.audio_toggled)
        ol.addWidget(self.audio_capture)
        ol.addWidget(text("Android 10+. Звук воспроизводится на компьютере; в OBS добавьте захват звука рабочего стола. "
                          "Некоторые приложения запрещают передачу звука.","muted",True))
        ol.addWidget(text("Громкость мультимедиа","muted"))
        volume_row = QHBoxLayout()
        self.phone_volume = WheelSafeSlider(Qt.Orientation.Horizontal)
        self.phone_volume.setRange(0,100)
        self.phone_volume.setEnabled(False)
        self.volume_value = text("—","muted")
        self.volume_value.setMinimumWidth(40)
        volume_row.addWidget(self.phone_volume,1)
        volume_row.addWidget(self.volume_value)
        ol.addLayout(volume_row)
        self.volume_dragging = False
        self.volume_sent_at = 0.0
        self.phone_volume.sliderPressed.connect(lambda: setattr(self,"volume_dragging",True))
        self.phone_volume.sliderReleased.connect(self.volume_released)
        self.phone_volume.sliderMoved.connect(self.volume_moved)
        ol.addWidget(text("Громкость синхронизируется с телефоном после подключения.","muted",True))
        ol.addStretch()
        for combo in self.settings_tabs.findChildren(QComboBox):
            # A long option must not dictate the page's minimum width.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for combo in (self.target_fps, self.max_size):
            combo.setMinimumContentsLength(4)
        body.addWidget(self.settings_tabs)
        layout.addLayout(body,1)
        footer = QHBoxLayout()
        footer.addWidget(text("AirFrame 0.6.2 · Локальная трансляция","muted"))
        footer.addStretch()
        diagnostics = QPushButton("Диагностика и помощь")
        diagnostics.clicked.connect(self.show_diagnostics)
        footer.addWidget(diagnostics)
        layout.addLayout(footer)
        self.setCentralWidget(root)
        self.obs = QMainWindow()
        self.obs.setWindowTitle("AirFrame — OBS")
        # Frameless: a "window capture" source in OBS gets only the video, not a title bar.
        self.obs.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.obs.resize(1280, 720)
        self.obs_pane = VideoPane(self.renderer_factory, always_active=True)
        self.obs.setCentralWidget(self.obs_pane)
        self.obs.installEventFilter(self)
        self._exact_size = None
        QShortcut(QKeySequence("Escape"), self.obs, activated=self.obs.hide)
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(1000)
        self.diagnostics = QMainWindow(self)
        self.diagnostics.setWindowTitle("AirFrame — Диагностика")
        self.diagnostics.resize(860,700)
        diag_widget = QWidget()
        dl = QVBoxLayout(diag_widget)
        self.output_diag = text("", "muted", True)
        dl.addWidget(self.output_diag)
        dl.addWidget(text("Оба устройства должны быть в одной сети. Разрешите локальную сеть в брандмауэре. "
                          "Для OBS: источник «Захват окна» → «AirFrame — OBS». Окно вывода оставьте открытым. "
                          "На macOS дайте OBS разрешение на запись экрана.\n"
                          "Код действует 10 минут; после блокировки Android нужно заново разрешить захват.","muted",True))
        dl.addWidget(text("Текущая статистика", "eyebrow"))
        self.stream_diag = QPlainTextEdit()
        self.stream_diag.setReadOnly(True)
        self.stream_diag.setPlainText("Статистика появится после подключения телефона.")
        dl.addWidget(self.stream_diag,1)
        dl.addWidget(text("Журнал событий", "eyebrow"))
        dl.addWidget(self.journal,1)
        export = QPushButton("Сохранить журнал…")
        export.clicked.connect(self.export_log)
        dl.addWidget(export)
        self.diagnostics.setCentralWidget(diag_widget)
        server.frames.attach_renderer("preview", self.surface.renderer)
        server.frames.attach_renderer("obs", self.obs_pane.renderer)
        server.frames.notice.changed.connect(self.frames_ready)
        self.sync_profile_controls()
        self.profile.currentTextChanged.connect(self.select_profile)
        self.rate.valueChanged.connect(self.custom_profile)
        self.target_fps.currentIndexChanged.connect(self.custom_profile)
        self.max_size.currentIndexChanged.connect(self.custom_profile)
        server.events.state.connect(self.on_state)
        server.events.stats.connect(self.on_stats)
        server.events.log.connect(self.log)
        self.refresh = QTimer(self)
        self.refresh.setInterval(1000)
        self.refresh.timeout.connect(self.refresh_code)
        self.refresh.start()
        self.refresh_code()
        self.log("Готов к подключению. Медиапоток DTLS-SRTP; сигнализация без TLS, только для доверенной LAN.")

    def refresh_addresses(self):
        addresses = []
        for interface in QNetworkInterface.allInterfaces():
            if interface.flags() & QNetworkInterface.InterfaceFlag.IsLoopBack:
                continue
            if not interface.flags() & QNetworkInterface.InterfaceFlag.IsUp:
                continue
            for entry in interface.addressEntries():
                ip = entry.ip()
                if ip.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not ip.isLoopback():
                    priority = 1 if interface.name().startswith(("utun","tun","bridge","veth","docker")) else 0
                    addresses.append((priority,ip.toString()))
        for _, address in sorted(set(addresses)):
            self.addresses.addItem(f"{address}:{self.server.port}")
        if not addresses:
            self.addresses.addItem("Нет LAN-адреса — подключите Wi-Fi")

    def refresh_code(self):
        address = self.addresses.currentText()
        payload = f"airframe://connect?v=1&host={address.split(':')[0]}&port={self.server.port}&key={self.server.room.qr_token}"
        if payload != self.qr_payload:
            self.qr_payload = payload
            matrix = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
            matrix.add_data(payload)
            matrix.make(fit=True)
            cells = matrix.get_matrix()
            # Integer-sized QR modules stay crisp on high-DPI displays too.
            ratio = self.devicePixelRatioF()
            scale = max(1, int(224 * ratio) // len(cells))
            image = QImage(len(cells)*scale,len(cells)*scale,QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.white)
            painter = QPainter(image)
            for y,row in enumerate(cells):
                for x,dark in enumerate(row):
                    if dark:painter.fillRect(x*scale,y*scale,scale,scale,Qt.GlobalColor.black)
            painter.end()
            from PySide6.QtGui import QPixmap
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(ratio)
            self.qr.setPixmap(pixmap)
        code = self.server.room.code
        self.code.setText(code[:4]+" "+code[4:])
        remaining = self.server.room.code_until-time.monotonic()
        if remaining <= 0:
            self.server.rotate()
        self.expires.setText(f"Код действует ещё {max(0,int(remaining))//60}:{max(0,int(remaining))%60:02d}")

    def sync_profile_controls(self, settings=None):
        """Show a profile in the controls without emitting edit signals.

        Used both for the initial session profile and for the profile the phone
        actually reports after an acknowledgement, so the window always shows
        the effective settings rather than a wish.
        """
        settings = video_settings(self.server.room.settings if settings is None else settings)
        for field in (self.target_fps,self.max_size,self.rate):
            field.blockSignals(True)
        self.target_fps.setCurrentIndex(self.target_fps.findData(settings["fps"]))
        self.max_size.setCurrentIndex(self.max_size.findData(settings["maxSize"]))
        self.rate.setValue(settings["bitrate"])
        self.hevc.blockSignals(True)
        self.hevc.setChecked(bool(settings.get("preferHevc", False)))
        self.hevc.blockSignals(False)
        for field in (self.target_fps,self.max_size,self.rate):
            field.blockSignals(False)
        name = next((name for name, profile in PRESETS.items() if profile == settings), "Свой профиль")
        self.profile.blockSignals(True)
        self.profile.setCurrentText(name)
        self.profile.blockSignals(False)
        self.rate_label.setText(f"Битрейт: максимум {self.rate.value()} Мбит/с")
        return settings

    def select_profile(self, name):
        if name not in PRESETS:
            return
        self.sync_profile_controls(PRESETS[name])

    def custom_profile(self,*args):
        # Editing a field means the session preset no longer describes the profile.
        self.profile.blockSignals(True)
        self.profile.setCurrentText("Свой профиль")
        self.profile.blockSignals(False)
        self.rate_label.setText(f"Битрейт: максимум {self.rate.value()} Мбит/с")

    def select_playout_mode(self, mode):
        """Select a playout mode by its stable value low/balanced/stable, not by index."""
        index = self.playout_mode.findData(mode)
        if index < 0:
            raise ValueError(f"Unknown playout mode: {mode}")
        self.playout_mode.setCurrentIndex(index)
        return mode

    def playout_selected(self, _index):
        mode = self.playout_mode.currentData()
        if mode is not None:
            self.server.set_playout(mode)

    def volume_moved(self, value):
        if time.monotonic() - self.volume_sent_at < .150:
            return
        self.volume_sent_at = time.monotonic()
        self.server.set_volume(value)

    def volume_released(self):
        self.volume_dragging = False
        self.server.set_volume(self.phone_volume.value())

    def audio_toggled(self, enabled):
        self.server.set_audio(enabled)
        self.log("Звук телефона: " + ("захват включён" if enabled else "захват выключен"))

    def smoothness_toggled(self, checked):
        self.adaptive.priority = "smoothness" if checked else "quality"
        self.adaptive.rearm(video_settings(dict(self.server.room.saved_profile)))
        self.log("Приоритет: " + ("плавность (управляемое снижение 2560→1920)" if checked
                                 else "качество (разрешение не снижается)"))

    def adaptive_observe(self, stats):
        """Feed the 1 Hz sender telemetry to the compromise controller."""
        if not stats or "encodedFps" not in stats:
            return
        action = self.adaptive.observe(limited=stats.get("limitation", "unknown"),
                                       encoded_fps=stats.get("encodedFps", 0.0),
                                       now=self.adaptive_now())
        if action is None:
            return
        self.log("Адаптация: " + action["reason"])
        if action["action"] == "configure":
            self.server.configure(action["settings"], "adaptive-" + secrets.token_hex(4))
        else:
            self.profile_result.setText(action["reason"])

    def apply_profile(self):
        settings = video_settings({"fps":self.target_fps.currentData(),
                                   "maxSize":self.max_size.currentData(),
                                   "bitrate":self.rate.value(),
                                   "preferHevc":self.hevc.isChecked()})
        self.adaptive.manual_override(video_settings(settings))
        self.pending_profile = secrets.token_hex(8)
        self.profile_ack_state = ACK_PENDING
        self.profile_result.setText("Ожидаем подтверждение от телефона…")
        self.apply_button.setEnabled(False)
        self.profile_timeout.start(self.profile_ack_timeout_ms)
        self.server.configure(settings, self.pending_profile)
        self.log(f"Профиль: до {settings['fps']} fps, {settings['maxSize']} px, до {settings['bitrate']} Мбит/с")

    def profile_ack(self, result):
        """Reflect the phone's acknowledgement; only the pending request counts.

        saved  - the profile is stored for the next connection, not applied now;
        applied - the phone accepted it (this does not prove 60 FPS, only that
                  setParameters() was accepted);
        rejected - the phone refused it: keep showing the effective profile.
        """
        if not self.pending_profile or result.get("requestId") != self.pending_profile:
            return
        self.profile_timeout.stop()
        self.apply_button.setEnabled(True)
        if result.get("saved") is True:
            self.profile_ack_state = ACK_SAVED
            effective = video_settings(result.get("effective") or result.get("settings")
                                       or self.confirmed_profile)
            self.confirmed_profile = effective
            self.pending_profile = None
            self.sync_profile_controls(effective)
            message = "Сохранено. Применится при подключении телефона."
        elif result.get("applied") is True:
            self.profile_ack_state = ACK_APPLIED
            effective = video_settings(result.get("effective") or result.get("settings")
                                       or self.confirmed_profile)
            self.confirmed_profile = effective
            self.pending_profile = None
            self.sync_profile_controls(effective)
            message = (f"Телефон подтвердил профиль: до {effective['fps']} fps, "
                       f"{effective['maxSize']} px, до {effective['bitrate']} Мбит/с. "
                       "Фактические fps зависят от телефона, сети и компьютера.")
        else:
            # A rejection is never shown as applied; the previous effective
            # profile stays in the session and in the controls.
            self.profile_ack_state = ACK_REJECTED
            effective = video_settings(result.get("effective") or self.confirmed_profile)
            self.pending_profile = None
            self.sync_profile_controls(effective)
            message = (f"Телефон отклонил профиль. Действует прежний профиль: до {effective['fps']} fps, "
                       f"{effective['maxSize']} px, до {effective['bitrate']} Мбит/с. Обновите APK и повторите.")
        self.profile_result.setText(message)
        self.log(message)

    def profile_timed_out(self):
        self.profile_ack_state = ACK_TIMEOUT
        self.apply_button.setEnabled(True)
        seconds = max(1, self.profile_ack_timeout_ms // 1000)
        message = (f"Телефон не подтвердил профиль за {seconds} с. Профиль сохранён и применится "
                   "при следующем подключении; проверьте APK и повторите.")
        self.profile_result.setText(message)
        self.log(message)

    def on_state(self,state,detail):
        if getattr(self, "closing", False):
            return
        names={"idle":"Ожидание телефона","connecting":"Подключение","live":"В эфире","retry":"Восстановление","error":"Нужно внимание"}
        self.status.setText("●  "+names.get(state,state))
        self.status.setStyleSheet("color:"+("#ffb2a2" if state=="error" else "#b6f5a1"))
        self.detail.setText(detail)
        self.stop.setEnabled(state in ("live","connecting","retry"))
        if state == "connecting":
            self.server.set_audio(self.audio_capture.isChecked())
            # New media session: cumulative telemetry counters restart.
            self.telemetry.reset()
            self.last_bottleneck = None
        self.log(detail)

    def on_stats(self,stats):
        if getattr(self, "closing", False):
            return
        if "playoutChanged" in stats:
            self.log("Режим вывода применён: " + PLAYOUT_LABELS.get(stats["playoutChanged"], stats["playoutChanged"]))
            return
        if "configured" in stats:
            self.profile_ack(stats["configured"])
            return
        self.fps.setText(f"{stats['fps']:.0f}" if stats else "—")
        self.bitrate.setText(f"{stats['bitrate']:.1f}" if stats else "—")
        self.jitter.setText(f"{stats['jitter']:.1f}" if stats else "—")
        latest = self.server.frames.latest()
        frame = latest[1] if latest is not None else None
        size = f"{frame.width} × {frame.height}" if frame is not None else "—"
        # Keep the clean output smooth; while both windows are open the preview
        # presents every second frame instead of scaling two large windows at 60 Hz.
        self.server.frames.set_preview_stride(2 if self.obs.isVisible() else 1)
        surface = self.obs_pane if self.obs.isVisible() else self.surface
        paint_fps = surface.presentation_fps()
        pipeline_detail = (f"\nПодготовка: {stats.get('convertedFps',0):.0f} fps · вывод: {paint_fps:.0f} fps"
                           f" · пропущено устаревших: {stats.get('skipped',0)}"
                           f" · преобразование: {stats.get('conversionMs',0):.1f} мс"
                           f" · пауза декодера: {stats.get('frameGapMs',0):.0f} мс") if stats else ""
        if stats:
            pipeline_detail += (f"\nВывод: {PLAYOUT_LABELS.get(stats.get('playoutMode'), stats.get('playoutMode'))}"
                                f" · запас цели {stats.get('playoutTargetMs',0):.0f} мс"
                                f" · в очереди {stats.get('playoutQueuedFrames',0)} кадров"
                                f" · опустошений {stats.get('playoutUnderflows',0)}"
                                f" · сбросов шкалы {stats.get('playoutRebuffers',0)}"
                                f" · пауза вывода {stats.get('outputGapMs',0):.0f} мс")
            pipeline_detail += (f"\nПауза RTP: {stats.get('packetGapMs',0):.0f} мс"
                                f" · сборка: {stats.get('assembledFps',0):.0f} fps, пауза {stats.get('assemblyGapMs',0):.0f} мс"
                                f" · кадр до {stats.get('largestFrameKiB',0):.0f} КиБ"
                                f" · восстановлений буфера: {stats.get('bufferRecoveries',0)}")
        codec_detail = (f" · кодек: {stats['codec']}" +
                        (f" ({stats['codecProfile']})" if stats.get("codecProfile") else "")
                        if "codec" in stats else "")
        sender_detail = (f" · кодировщик: {stats['encodedFps']:.0f} fps, {stats['width']} × {stats['height']}"
                         f"{codec_detail}"
                         f" · ограничение: {stats['limitation']}" if "encodedFps" in stats else "")
        telemetry_detail = ""
        if stats:
            try:
                target_fps = video_settings(self.server.room.settings)["fps"]
            except ValueError:
                target_fps = 60
            rates = self.telemetry.update(stats)
            lines = format_telemetry_lines(stats, rates, received_fps=stats.get("fps"),
                                           target_fps=target_fps)
            telemetry_detail = "\n" + "\n".join(lines)
            bottleneck = telemetry_bottleneck(stats, rates, received_fps=stats.get("fps"),
                                              target_fps=target_fps)
            now = time.monotonic()
            if bottleneck != self.last_bottleneck or now - self.telemetry_logged_at >= 5:
                # Journal export is the diagnostics report; log transitions and a
                # periodic summary instead of one line per statistics tick.
                self.last_bottleneck = bottleneck
                self.telemetry_logged_at = now
                for line in lines:
                    self.log(line)
        volume = stats.get("volume") if stats else None
        if not self.volume_dragging:
            self.phone_volume.setEnabled(volume is not None)
            if volume is not None:
                self.phone_volume.setValue(int(volume))
        self.volume_value.setText(f"{int(volume)}%" if volume is not None else "—")
        self.loss.setText(f"Потери: {stats['loss']:.1f}% · кадр: {size}" if stats else "Потери: — · размер: —")
        self.stream_diag.setPlainText(
            f"Потери: {stats['loss']:.1f}% · {size} · приём включает WebRTC overhead"
            f"{sender_detail}{telemetry_detail}{pipeline_detail}"
            if stats else "Статистика появится после подключения телефона.")
        self.adaptive_observe(stats)
        self._refresh_output_diag()

    def eventFilter(self, obj, event):
        if obj is self.obs:
            if event.type() == QEvent.Type.Show:
                self.obs_pane.set_active(True)
                self.obs_pane.refresh_latest(self.server.frames)
                self.server.frames.set_preview_stride(2)
                self.obs_button.setText("Скрыть окно для OBS  ↙")
                self._refresh_output_diag()
            elif event.type() == QEvent.Type.Hide:
                self.obs_pane.set_active(False)
                self.server.frames.set_preview_stride(1)
                self.obs_button.setText("Открыть окно для OBS  ↗")
                self._refresh_output_diag()
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.surface.set_active(True)
        self.surface.refresh_latest(self.server.frames)
        self._refresh_output_diag()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.surface.set_active(False)

    @Slot()
    def frames_ready(self):
        """GUI side of the mailbox: present the newest prepared frame per window."""
        if getattr(self, "closing", False):
            return
        ready = self.server.frames.take_ready()
        output_prepared = None
        for key, prepared in ready.items():
            pane = self.obs_pane if key == "obs" else self.surface
            pane.present(prepared)
            if key == "obs" or output_prepared is None:
                output_prepared = prepared
        latest = self.server.frames.latest()
        if latest is None or latest[1] is None:
            self.surface.clear()
            self.obs_pane.clear()
        else:
            # The exact output size follows the stream, independent of the
            # preview throttling (the preview may skip this serial entirely).
            self._apply_output_size(output_prepared)
        self._refresh_output_diag()

    def exact_output_toggled(self, checked):
        if not checked and self._exact_size is not None:
            self.obs.setMinimumSize(320, 180)
            self.obs.setMaximumSize(16777215, 16777215)
            self._exact_size = None
            self._refresh_output_diag()
        elif checked:
            self._apply_output_size()

    def _device_pixel_ratio(self):
        """Hook so tests can pin the DPR of the target display explicitly."""
        return self.obs.devicePixelRatioF() or 1.0

    def output_size(self, prepared=None):
        """Rotated physical output size of the current stream."""
        if prepared is not None:
            return prepared.output_size()
        for pane in (self.obs_pane, self.surface):
            held = pane.renderer.last_prepared()
            if held is not None:
                return held.output_size()
        latest = self.server.frames.latest()
        frame = latest[1] if latest is not None else None
        if frame is None:
            return None
        size = QSize(frame.width, frame.height)
        if video_render.display_rotation_from_frame(frame) in (90, 270):
            size.transpose()
        return size

    def _apply_output_size(self, prepared=None):
        """Exact mode: the OBS window's device pixels equal the stream pixels."""
        if not self.exact_output.isChecked():
            return
        physical = self.output_size(prepared)
        if physical is None:
            return
        dpr = self._device_pixel_ratio()
        logical = video_render.exact_window_size(physical, dpr)
        size = (logical.width(), logical.height())
        if size != self._exact_size:
            self.obs.setFixedSize(logical)
            self._exact_size = size

    def _refresh_output_diag(self):
        """Diagnostics: the actual output path/mode and every fallback reason."""
        if not hasattr(self, "output_diag"):
            return
        lines = ["Вывод предпросмотра: " + self.surface.renderer.output_path(),
                 "Вывод окна OBS: " + self.obs_pane.renderer.output_path()]
        output = self.output_size()
        if output is not None:
            dpr = self._device_pixel_ratio()
            physical = video_render.physical_size(self.obs.size(), dpr)
            scale = physical.width() / output.width() if output.width() else 0.0
            exact = " · точно 1:1" if abs(scale - 1.0) < 0.01 else ""
            lines.append(
                f"Кадр (с поворотом): {output.width()} × {output.height()} px · окно OBS "
                f"{self.obs.width()}×{self.obs.height()} лог. = "
                f"{physical.width()}×{physical.height()} физ. px · масштаб {scale:.2f}{exact}"
            )
        for name, renderer in (("предпросмотр", self.surface.renderer), ("OBS", self.obs_pane.renderer)):
            errors = renderer.stats().get("framesPrepareErrors", 0)
            if errors:
                lines.append(f"Ошибки подготовки ({name}): {errors} · последняя: {renderer.last_prepare_error}")
        reasons = []
        for name, renderer in (("предпросмотр", self.surface.renderer), ("OBS", self.obs_pane.renderer)):
            if renderer.fallback_reason:
                reasons.append(f"{name}: {renderer.fallback_reason}")
        if reasons:
            lines.append("Fallback: " + " · ".join(reasons))
        self.output_diag.setText("\n".join(lines))

    def open_obs(self):
        if self.obs.isVisible():
            self.obs.hide()
            return
        self._apply_output_size()
        self.obs.show()
        self.obs.raise_()
        self.obs.activateWindow()

    def show_diagnostics(self):
        self.diagnostics.show()
        self.diagnostics.raise_()

    def log(self,message):
        self.journal.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {message}")

    def export_log(self):
        path,_ = QFileDialog.getSaveFileName(self,"Сохранить журнал","airframe-log.txt","Text (*.txt)")
        if path:
            try:
                Path(path).write_text(self.journal.toPlainText(),encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self,"Журнал",str(exc))

    def closeEvent(self,event):
        self.closing = True
        self.obs.hide()
        self.diagnostics.hide()
        self.server.frames.detach_renderer("preview")
        self.server.frames.detach_renderer("obs")
        self.surface.renderer.close()
        self.obs_pane.renderer.close()
        if self.server.audio is not None:
            self.server.audio.stop()
        self.server.close()
        event.accept()


def main():
    parser=argparse.ArgumentParser(description="AirFrame — native Android receiver")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--renderer",choices=("auto","gpu","software"),default="auto",
                        help="video output path; auto proves the RHI path at runtime")
    args=parser.parse_args()
    app=QApplication(sys.argv[:1])
    app.setApplicationName("AirFrame")
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setStyle("Fusion")
    events,frames=Events(),Frames()
    audio=AudioOut()
    server=LocalServer(args.port,events,frames,audio)
    renderer_mode=args.renderer
    try:
        server.start()
    except RuntimeError as exc:
        QMessageBox.critical(None,"AirFrame",str(exc))
        return
    window=Desktop(server, renderer_factory=lambda: video_render.create_renderer(renderer_mode))
    window.show()
    app.aboutToQuit.connect(server.close)
    code=app.exec()
    # Interpreter shutdown destroys PySide objects in arbitrary order; widgets
    # dying after the QApplication segfault inside QWidget::~QWidget. Delete
    # the windows while the application object still exists.
    shiboken6.delete(window.obs)
    shiboken6.delete(window)
    sys.exit(code)
