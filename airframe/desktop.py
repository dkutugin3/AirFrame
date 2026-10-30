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
from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QImage, QPainter
from PySide6.QtMultimedia import QAudioFormat, QAudioSink
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLayout, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
    QVBoxLayout, QWidget,
)

from .receiver import Receiver
from .server import (BASE_PROFILE, Room, TelemetrySampler, create_app,
                     format_telemetry_lines, telemetry_bottleneck, video_settings)

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
QWidget {background:#101216;color:#eef0f3;font-size:12px;}
QFrame#panel {background:#1a1d22;border:1px solid #30353d;border-radius:12px;}
QFrame#panel QLabel {background:transparent;}
QLabel#brand {font-size:25px;font-weight:650;color:#b6f5a1;}
QLabel#title {font-size:32px;font-weight:600;}
QLabel#muted {color:#9299a5;}
QLabel#eyebrow {color:#b6f5a1;font-size:10px;font-weight:600;}
QLabel#code {color:#b6f5a1;background:#11171a;font-size:31px;padding:14px;border-radius:8px;}
QLabel#metric {font-size:26px;font-weight:500;}
QPushButton {background:#262d35;border:1px solid #3b434d;border-radius:7px;padding:11px 15px;}
QPushButton:hover {background:#344035;}
QPushButton#primary {background:#b6f5a1;color:#192a14;font-weight:600;border:0;}
QPushButton:disabled {color:#66717b;background:#1a1d22;}
QComboBox,QSpinBox {background:#242931;color:#eef0f3;border:1px solid #39424c;border-radius:6px;padding:9px;min-height:19px;}
QSlider::groove:horizontal {background:#364139;height:5px;border-radius:2px;}
QSlider::handle:horizontal {background:#b6f5a1;width:15px;margin:-5px 0;border-radius:7px;}
QPlainTextEdit {background:#15191e;border:1px solid #30353d;border-radius:7px;color:#aeb7c3;font-family:monospace;font-size:11px;}
"""


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
    """One-slot mailbox: slow UI painting never queues old frames."""
    def __init__(self):
        self.lock = threading.Lock()
        self.image = None
        self.serial = 0
        self.notification_pending = False
        self.notice = FrameNotice(self)

    def put(self, frame):
        image = None
        if frame is not None:
            converted = frame.reformat(format="bgra")
            plane = converted.planes[0]
            image = QImage(memoryview(plane), converted.width, converted.height, plane.line_size,
                           QImage.Format.Format_ARGB32)
            image._frame_owner = converted  # Pin the PyAV buffer for both Qt windows.
        with self.lock:
            self.image = image
            self.serial += 1
            notify = not self.notification_pending
            self.notification_pending = True
        if notify:
            self.notice.wake.emit()

    def get(self):
        with self.lock:
            return self.serial, self.image


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
            # The request is saved for the next connection immediately; while a
            # phone is connected it becomes effective only after its ack.
            effective = self.room.request_profile(requested, request_id)
            if not sender_connected:
                self.events.stats.emit({"configured": {"requestId": request_id, "saved": True,
                                                       "settings": effective,
                                                       "effective": dict(effective)}})
                return
            await self.room.send("sender", {"type": "configure", "settings": effective,
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


class VideoSurface(QWidget):
    def __init__(self, frames, preview=False):
        super().__init__()
        self.frames, self.preview = frames, preview
        self.seen = -1
        self.painted_serial = -1
        self.painted_frames = 0
        self.last_metric_at = time.monotonic()
        self.previous_painted = 0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self.refresh)
        self.frames.notice.changed.connect(self.frame_ready)

    def frame_ready(self):
        # Frame arrival schedules one GUI update, without a second 16 ms clock.
        # Qt coalesces paints; FrameNotice coalesces cross-thread notifications.
        if not self.isVisible():
            return
        if self.timer.interval() == 0:
            self.refresh()
        elif not self.timer.isActive():
            self.timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def presentation_fps(self):
        now = time.monotonic()
        fps = (self.painted_frames - self.previous_painted) / max(.001, now - self.last_metric_at)
        self.previous_painted, self.last_metric_at = self.painted_frames, now
        return fps

    def refresh(self):
        serial, _ = self.frames.get()
        if serial != self.seen:
            self.seen = serial
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        serial, image = self.frames.get()
        if image is not None:
            if serial != self.painted_serial:
                self.painted_serial = serial
                self.painted_frames += 1
            size = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect((self.width()-size.width())//2, (self.height()-size.height())//2, size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, image)
        elif self.preview:
            painter.fillRect(self.rect(), QColor("#151d19"))
            painter.setPen(QColor("#b6f5a1"))
            painter.setFont(QFont("Arial", 21))
            painter.drawText(self.rect().adjusted(20,-35,-20,-35), Qt.AlignmentFlag.AlignCenter, "Здесь будет ваш Android")
            painter.setPen(QColor("#929d95"))
            painter.setFont(QFont("Arial", 12))
            painter.drawText(self.rect().adjusted(20,40,-20,40), Qt.AlignmentFlag.AlignCenter,
                             "Введите адрес и код на телефоне\nБез USB · Без отладки · Без root")
        painter.end()


def text(value, kind="", wrap=False):
    item = QLabel(value)
    item.setObjectName(kind)
    item.setWordWrap(wrap)
    return item


def panel():
    widget = QFrame()
    widget.setObjectName("panel")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20,20,20,20)
    layout.setSpacing(12)
    return widget, layout


class Desktop(QMainWindow):
    def __init__(self, server):
        super().__init__()
        self.server = server
        self.setWindowTitle("AirFrame")
        self.resize(1200,900)
        self.setMinimumSize(950,780)
        self.setStyleSheet(STYLE)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30,26,30,24)
        layout.setSpacing(18)
        header = QHBoxLayout()
        header.addWidget(text("◉  airframe", "brand"))
        header.addStretch()
        self.status = text("●  Ожидание телефона", "eyebrow")
        header.addWidget(self.status)
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(24)
        left = QVBoxLayout()
        left.addWidget(text("ANDROID → КОМПЬЮТЕР    /    ПРЕДПРОСМОТР", "eyebrow"))
        self.surface = VideoSurface(server.frames, preview=True)
        left.addWidget(self.surface,1)
        self.detail = text("Готов к подключению по Wi-Fi", "muted", True)
        left.addWidget(self.detail)
        metrics, metrics_layout = panel()
        row = QHBoxLayout()
        self.fps = text("—", "metric")
        self.bitrate = text("—", "metric")
        self.jitter = text("—", "metric")
        for title, value in (("FPS ДЕКОДИРОВАНИЯ",self.fps),("ПРИЁМ, МБИТ/С",self.bitrate),("ДЖИТТЕР, МС",self.jitter)):
            column = QVBoxLayout()
            column.addWidget(text(title,"muted"))
            column.addWidget(value)
            row.addLayout(column,1)
        metrics_layout.addLayout(row)
        self.loss = text("Потери: — · размер: —", "muted", True)
        metrics_layout.addWidget(self.loss)
        left.addWidget(metrics)
        self.obs_button = QPushButton("↗  Открыть чистое окно для OBS")
        self.obs_button.clicked.connect(self.open_obs)
        left.addWidget(self.obs_button)
        left.addWidget(text("Для 60 fps: Wi-Fi 5/6 на 5 или 6 ГГц; компьютер желательно подключить по Ethernet. "
                            "Для 120 fps дополнительно нужен телефон с экраном 120 Гц. "
                            "Фактические fps зависят от телефона, сети и компьютера.","muted",True))
        body.addLayout(left,1)
        right = QVBoxLayout()
        connection, cl = panel()
        connection.setMinimumHeight(590)
        cl.addWidget(text("01   ПОДКЛЮЧИТЕ ТЕЛЕФОН","eyebrow"))
        cl.addWidget(text("Адрес компьютера","muted"))
        self.addresses = QComboBox()
        self.refresh_addresses()
        cl.addWidget(self.addresses)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.qr)
        self.qr_payload = None
        self.addresses.currentTextChanged.connect(lambda _: self.refresh_code())
        cl.addWidget(text("Резервный код подключения","muted"))
        self.code = text("", "code")
        self.code.setMinimumHeight(76)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.code)
        self.expires = text("Код действует 10 минут","muted")
        cl.addWidget(self.expires)
        rotate = QPushButton("Обновить код")
        rotate.clicked.connect(server.rotate)
        cl.addWidget(rotate)
        cl.addWidget(text("На Android нажмите «Сканировать QR». После сканирования разрешите захват экрана.","muted",True))
        right.addWidget(connection)
        quality, ql = panel()
        quality.setMinimumHeight(430)
        ql.addWidget(text("02   НАСТРОЙТЕ КАЧЕСТВО","eyebrow"))
        self.profile = QComboBox()
        self.profile.addItems([*PRESETS,"Свой профиль"])
        ql.addWidget(self.profile)
        row = QHBoxLayout()
        self.target_fps = QComboBox()
        for rate in (30,60,120):
            self.target_fps.addItem(f"{rate} fps",rate)
        self.max_size = QComboBox()
        for size in (1280,1920,2560):
            self.max_size.addItem(f"{size} px",size)
        self.max_size.setCurrentIndex(1)
        row.addWidget(self.target_fps)
        row.addWidget(self.max_size)
        ql.addLayout(row)
        ql.addWidget(text("Максимальная длинная сторона кадра","muted"))
        self.rate_label = text("Битрейт: максимум 10 Мбит/с","muted")
        ql.addWidget(self.rate_label)
        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(4,80)
        ql.addWidget(self.rate)
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
        ql.addWidget(text("Плавность вывода", "eyebrow"))
        self.playout_mode = QComboBox()
        self.playout_mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.playout_mode.setMinimumContentsLength(12)
        self.playout_mode.addItem("Стабильность OBS · 250–350 мс", "stable")
        self.playout_mode.addItem("Баланс · 120–200 мс", "balanced")
        self.playout_mode.addItem("Минимальная задержка · без запаса", "low")
        self.playout_mode.currentIndexChanged.connect(self.playout_selected)
        ql.addWidget(self.playout_mode)
        ql.addWidget(text("Стабильность сглаживает поступление кадров пачками ценой дополнительной задержки. "
                          "Паузы длиннее запаса или пропущенные телефоном кадры могут остаться заметны.","muted",True))
        ql.addWidget(text("Громкость телефона","eyebrow"))
        volume_row = QHBoxLayout()
        self.phone_volume = QSlider(Qt.Orientation.Horizontal)
        self.phone_volume.setRange(0,100)
        self.phone_volume.setEnabled(False)
        self.volume_value = text("—","muted")
        self.volume_value.setMinimumWidth(40)
        volume_row.addWidget(self.phone_volume,1)
        volume_row.addWidget(self.volume_value)
        ql.addLayout(volume_row)
        self.volume_dragging = False
        self.volume_sent_at = 0.0
        self.phone_volume.sliderPressed.connect(lambda: setattr(self,"volume_dragging",True))
        self.phone_volume.sliderReleased.connect(self.volume_released)
        self.phone_volume.sliderMoved.connect(self.volume_moved)
        ql.addWidget(text("Мультимедиа-громкость подключённого телефона. Ползунок синхронизируется, "
                          "если менять звук на самом телефоне.","muted",True))
        self.audio_capture = QCheckBox("Захватывать звук телефона")
        self.audio_capture.setChecked(True)
        self.audio_capture.toggled.connect(self.audio_toggled)
        ql.addWidget(self.audio_capture)
        ql.addWidget(text("Звук телефона воспроизводится на этом компьютере и попадает в OBS как звук "
                          "рабочего стола. Требуется Android 10+ и разрешение захвата. Звук звонков "
                          "и запретившие захват приложения не передаются.","muted",True))
        ql.addWidget(text("WebRTC адаптирует передачу к сети; звук и видео идут одним зашифрованным потоком.","muted",True))
        right.addWidget(quality)
        self.stop = QPushButton("■  Завершить трансляцию")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(server.stop_stream)
        right.addWidget(self.stop)
        right.addStretch()
        right_container = QWidget()
        right_container.setLayout(right)
        right.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        right_container.setMinimumWidth(325)
        right_area = QScrollArea()
        right_area.setWidgetResizable(True)
        right_area.setFrameShape(QFrame.Shape.NoFrame)
        right_area.setWidget(right_container)
        right_area.setMinimumWidth(350)
        right_area.setMaximumWidth(370)
        body.addWidget(right_area)
        layout.addLayout(body,1)
        footer = QHBoxLayout()
        footer.addWidget(text("AIRFRAME 0.5.0   ·   НАТИВНЫЙ ПРИЁМНИК","muted"))
        footer.addStretch()
        diagnostics = QPushButton("Диагностика и инструкция")
        diagnostics.clicked.connect(self.show_diagnostics)
        footer.addWidget(diagnostics)
        layout.addLayout(footer)
        self.setCentralWidget(root)
        self.obs = QMainWindow()
        self.obs.setWindowTitle("AirFrame — OBS")
        self.obs.resize(1280,720)
        self.obs.setCentralWidget(VideoSurface(server.frames))
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(1000)
        self.diagnostics = QMainWindow(self)
        self.diagnostics.setWindowTitle("AirFrame — Диагностика")
        self.diagnostics.resize(780,480)
        diag_widget = QWidget()
        dl = QVBoxLayout(diag_widget)
        dl.addWidget(text("Оба устройства должны быть в одной сети. Разрешите локальную сеть в брандмауэре. "
                          "Для OBS: источник «Захват окна» → «AirFrame — OBS». Окно вывода оставьте открытым. "
                          "На macOS дайте OBS разрешение на запись экрана.\n"
                          "Код действует 10 минут; после блокировки Android нужно заново разрешить захват.","muted",True))
        dl.addWidget(self.journal)
        export = QPushButton("Сохранить журнал…")
        export.clicked.connect(self.export_log)
        dl.addWidget(export)
        self.diagnostics.setCentralWidget(diag_widget)
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
            scale = max(1, 240 // len(cells))
            image = QImage(len(cells)*scale,len(cells)*scale,QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.white)
            painter = QPainter(image)
            for y,row in enumerate(cells):
                for x,dark in enumerate(row):
                    if dark:painter.fillRect(x*scale,y*scale,scale,scale,Qt.GlobalColor.black)
            painter.end()
            from PySide6.QtGui import QPixmap
            self.qr.setPixmap(QPixmap.fromImage(image))
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

    def apply_profile(self):
        settings = video_settings({"fps":self.target_fps.currentData(),
                                   "maxSize":self.max_size.currentData(),
                                   "bitrate":self.rate.value()})
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
        _, image = self.server.frames.get()
        size=f"{image.width()} × {image.height()}" if image is not None else "—"
        # Keep the clean output at 60 Hz; avoid scaling two large windows at 60 Hz.
        preview_interval = 33 if self.obs.isVisible() else 0
        if self.surface.timer.interval() != preview_interval:
            self.surface.timer.setInterval(preview_interval)
        surface = self.obs.centralWidget() if self.obs.isVisible() else self.surface
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
        sender_detail = (f" · кодировщик: {stats['encodedFps']:.0f} fps, {stats['width']} × {stats['height']}"
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
        self.loss.setText(f"Потери: {stats['loss']:.1f}% · {size} · приём включает WebRTC overhead{sender_detail}{telemetry_detail}{pipeline_detail}" if stats else "Потери: — · размер: —")

    def open_obs(self):
        self.obs.show()
        self.obs.raise_()

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
        if self.server.audio is not None:
            self.server.audio.stop()
        self.server.close()
        event.accept()


def main():
    parser=argparse.ArgumentParser(description="AirFrame — native Android receiver")
    parser.add_argument("--port",type=int,default=8765)
    args=parser.parse_args()
    app=QApplication(sys.argv[:1])
    app.setApplicationName("AirFrame")
    app.setStyle("Fusion")
    events,frames=Events(),Frames()
    audio=AudioOut()
    server=LocalServer(args.port,events,frames,audio)
    try:
        server.start()
    except RuntimeError as exc:
        QMessageBox.critical(None,"AirFrame",str(exc))
        return
    window=Desktop(server)
    window.show()
    app.aboutToQuit.connect(server.close)
    code=app.exec()
    # Interpreter shutdown destroys PySide objects in arbitrary order; widgets
    # dying after the QApplication segfault inside QWidget::~QWidget. Delete
    # the windows while the application object still exists.
    shiboken6.delete(window.obs)
    shiboken6.delete(window)
    sys.exit(code)
