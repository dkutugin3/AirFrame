"""Local authenticated signaling between Android and the native desktop receiver."""
import asyncio
from collections import defaultdict, deque
import ipaddress
import json
import math
import secrets
import socket
import time

from aiohttp import WSMsgType, web

STATE = web.AppKey("state", object)
# Base profile of a new session: 60 FPS / 2560 px long side / at most 24 Mbit/s,
# with the stable OBS playout mode. 120 FPS stays an explicit experimental
# choice, never a default. The lighter 60/16/1920 profile remains selectable for
# weaker machines. The Android sender mirrors these defaults (configure()).
BASE_PROFILE = {"fps": 60, "bitrate": 24, "maxSize": 2560}
DEFAULTS = dict(BASE_PROFILE)

# --- Sender telemetry (stage 5, additive) -----------------------------------
# Optional fields are bounded; absence means "unknown" and 0 is a measured zero.
# The legacy fields (session/encodedFps/width/height/limitation) stay required so
# old senders keep working unchanged.
TELEMETRY_LIMITATIONS = ("none", "cpu", "bandwidth", "other", "unknown")
TELEMETRY_THERMAL_STATUSES = ("unknown", "none", "light", "moderate", "severe",
                              "critical", "emergency", "shutdown")
TELEMETRY_FLOAT_BOUNDS = {
    "captureFps": 1000.0,
    "maxCaptureIntervalMs": 60000.0,
    "pacerFps": 1000.0,
    "videoBitrateKbps": 1000000.0,
    "avgEncodeTimeMs": 60000.0,
    "telemetryWindowMs": 60000.0,
}
TELEMETRY_INT_BOUNDS = {
    "framesEncoded": (0, None),
    "framesSent": (0, None),
    "captureWidth": (1, 16384),
    "captureHeight": (1, 16384),
    "sampledMonotonicMs": (0, None),
}
TELEMETRY_TEXT_LIMITS = {"codec": 32, "codecProfile": 128, "encoderImplementation": 128}
TELEMETRY_COUNTERS = ("framesEncoded", "framesSent")
TELEMETRY_BOTTLENECKS = ("capture", "encoder", "transport", "none", "unknown")
UNKNOWN_TEXT = "неизвестно"
TELEMETRY_THERMAL_LABELS = {
    "unknown": UNKNOWN_TEXT,
    "none": "нет перегрева",
    "light": "слабое ограничение",
    "moderate": "умеренное ограничение",
    "severe": "сильное ограничение",
    "critical": "критическое ограничение",
    "emergency": "аварийное ограничение",
    "shutdown": "отключение",
}


def _telemetry_number(value, low=0, high=None):
    if type(value) is int:
        return value >= low and (high is None or value <= high)
    return (type(value) is float and math.isfinite(value)
            and value >= low and (high is None or value <= high))


def telemetry_measurement_valid(message):
    """Validate the additive optional telemetry fields (bounded, finite, typed)."""
    for key, bound in TELEMETRY_FLOAT_BOUNDS.items():
        if key in message and not _telemetry_number(message[key], 0, bound):
            return False
    for key, (low, high) in TELEMETRY_INT_BOUNDS.items():
        if key not in message:
            continue
        value = message[key]
        if type(value) is not int or value < low or (high is not None and value > high):
            return False
    for key, limit in TELEMETRY_TEXT_LIMITS.items():
        if key in message:
            value = message[key]
            if not isinstance(value, str) or len(value) > limit:
                return False
    if "hardwareEncoder" in message:
        # Only "unknown" is trustworthy until a reliable implementation source exists.
        value = message["hardwareEncoder"]
        if not isinstance(value, str) or len(value) > 16 or value != "unknown":
            return False
    if "thermalStatus" in message and message["thermalStatus"] not in TELEMETRY_THERMAL_STATUSES:
        return False
    return True


def telemetry_valid(message):
    """Full telemetry message: legacy required measurements + additive optional ones."""
    if not (isinstance(message.get("session"), str) and 1 <= len(message["session"]) <= 32):
        return False
    if not all(_telemetry_number(message.get(key), 0, bound)
               for key, bound in (("encodedFps", 1000), ("width", 16384), ("height", 16384))):
        return False
    if message.get("limitation") not in TELEMETRY_LIMITATIONS:
        return False
    return telemetry_measurement_valid(message)


class TelemetrySampler:
    """Derive honest rates and diagnostics from cumulative sender telemetry.

    Rates are computed only from a real positive time span; the first sample,
    equal timestamps and a counter reset all mean "unknown", never 0.
    """

    def __init__(self):
        self.previous = None

    def reset(self):
        self.previous = None

    @staticmethod
    def _snapshot(message):
        counters = {key: message[key] for key in TELEMETRY_COUNTERS
                    if type(message.get(key)) is int and message[key] >= 0}
        timestamp = message.get("sampledMonotonicMs")
        window = message.get("telemetryWindowMs")
        return {
            "counters": counters,
            "timestamp": timestamp if type(timestamp) is int else None,
            "windowMs": window if _telemetry_number(window, 0) else None,
        }

    def update(self, message):
        current = self._snapshot(message)
        previous, self.previous = self.previous, current
        rates = {"encodedFps": None, "sentFps": None, "encodedDelta": None,
                 "sentDelta": None, "windowMs": current["windowMs"], "reason": None}
        if not current["counters"]:
            rates["reason"] = "no-counters"
            return rates
        if previous is None or not previous["counters"]:
            rates["reason"] = "first-sample"
            return rates
        deltas = {key: current["counters"].get(key, 0) - previous["counters"].get(key, 0)
                  for key in current["counters"]}
        if any(delta < 0 for delta in deltas.values()):
            rates["reason"] = "counter-reset"
            return rates
        span = None
        if current["timestamp"] is not None and previous["timestamp"] is not None:
            if current["timestamp"] == previous["timestamp"]:
                rates["reason"] = "equal-timestamps"
                return rates
            span = current["timestamp"] - previous["timestamp"]
        elif current["windowMs"]:
            span = current["windowMs"]
        if not span or span <= 0:
            rates["reason"] = "no-timestamp"
            return rates
        if "framesEncoded" in deltas:
            rates["encodedDelta"] = deltas["framesEncoded"]
            rates["encodedFps"] = deltas["framesEncoded"] * 1000 / span
        if "framesSent" in deltas:
            rates["sentDelta"] = deltas["framesSent"]
            rates["sentFps"] = deltas["framesSent"] * 1000 / span
        return rates


def telemetry_bottleneck(message, rates=None, received_fps=None, target_fps=60):
    """Name the stage that falls short: capture, encoder or transport/PC.

    Uses only measurements that are actually present; without phone-side capture/
    pacer data and without a PC-side frame rate the cause stays "unknown".
    """
    rates = rates or {}
    target = float(target_fps or 60)
    floor = target * 0.9
    capture = message.get("captureFps")
    pacer = message.get("pacerFps")
    encoded = message.get("encodedFps")
    if encoded is None:
        encoded = rates.get("encodedFps")
    sent = rates.get("sentFps")
    if capture is None and pacer is None and received_fps is None:
        return "unknown"
    if capture is not None and capture < floor:
        return "capture"
    if (pacer is not None and pacer < floor) or (encoded is not None and encoded < floor):
        return "encoder"
    if (sent is not None and sent < floor) or (received_fps is not None and received_fps < floor):
        return "transport"
    return "none"


def _fmt(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return UNKNOWN_TEXT
    return f"{value:g}"


def _fmt_text(value):
    return value if isinstance(value, str) else UNKNOWN_TEXT


def _bottleneck_label(bottleneck, target_fps):
    target = f"{float(target_fps or 60):g}"
    return {
        "capture": f"захват не дал {target} FPS",
        "encoder": f"кодировщик не успевает за {target} FPS",
        "transport": f"транспорт/ПК не успевают за {target} FPS",
        "none": "ограничений не видно",
        "unknown": "недостаточно данных",
    }[bottleneck]


def format_telemetry_lines(message, rates=None, received_fps=None, target_fps=60):
    """Human-readable diagnostics for the desktop surface (never invents a p95).

    Absent measurements are reported as "неизвестно"; a measured 0 stays 0.
    """
    rates = rates or {}
    target = float(target_fps or 60)
    capture = message.get("captureFps")
    pacer = message.get("pacerFps")
    encoded = message.get("encodedFps")
    if encoded is None:
        encoded = rates.get("encodedFps")
    sent = rates.get("sentFps")
    window = message.get("telemetryWindowMs")
    bitrate = message.get("videoBitrateKbps")
    bitrate_text = (f"Фактический видеобитрейт: {_fmt(bitrate)} кбит/с "
                    f"({_fmt(None if bitrate is None else bitrate / 1000)} Мбит/с)")
    encode_text = "среднее время кодирования за окно"
    if window is not None:
        encode_text += f" {_fmt(window)} мс"
    encode_text += f": {_fmt(message.get('avgEncodeTimeMs'))} мс"
    codec = _fmt_text(message.get("codec"))
    if message.get("codecProfile") is not None:
        codec += f" ({message['codecProfile']})"
    width = message.get("captureWidth")
    height = message.get("captureHeight")
    if width is None:
        width = message.get("width")
    if height is None:
        height = message.get("height")
    hardware = message.get("hardwareEncoder")
    hardware_text = UNKNOWN_TEXT if hardware in (None, "unknown") else str(hardware)
    thermal = message.get("thermalStatus")
    thermal_text = (TELEMETRY_THERMAL_LABELS.get(thermal, UNKNOWN_TEXT)
                    if thermal is not None else UNKNOWN_TEXT)
    lines = [
        (f"Телеметрия телефона: захват {_fmt(capture)} FPS "
         f"(макс. пауза {_fmt(message.get('maxCaptureIntervalMs'))} мс)"
         f" · pacer {_fmt(pacer)} FPS · кодировщик {_fmt(encoded)} FPS"
         f" · отправлено {_fmt(sent)} FPS · приём/декод {_fmt(received_fps)} FPS"),
        f"{bitrate_text} · {encode_text}",
        (f"Кодек: {codec} · кадр {_fmt(width)}×{_fmt(height)}"
         f" · ограничение: {_fmt_text(message.get('limitation'))}"
         f" · кодировщик: {_fmt_text(message.get('encoderImplementation'))}"
         f" · аппаратный кодировщик: {hardware_text}"),
        (f"Счётчики за сеанс: закодировано {_fmt(message.get('framesEncoded'))}"
         f" · отправлено {_fmt(message.get('framesSent'))} кадров"
         f" · температура: {thermal_text}"),
    ]
    reason = rates.get("reason")
    note = {
        "first-sample": "Прирост счётчиков: первый замер — скорости ещё неизвестны",
        "equal-timestamps": "Прирост счётчиков: одинаковые метки времени — скорости неизвестны",
        "counter-reset": "Счётчики сброшены (новый сеанс) — скорости неизвестны",
        "no-timestamp": "Прирост счётчиков: нет метки времени — скорости неизвестны",
    }.get(reason)
    if note:
        lines.insert(1, note)
    bottleneck = telemetry_bottleneck(message, rates, received_fps=received_fps, target_fps=target)
    lines.append("Узкое место: " + _bottleneck_label(bottleneck, target))
    return lines


def video_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Expected settings")
    result = {key: value.get(key, default) for key, default in DEFAULTS.items()}
    if (type(result["fps"]) is not int or result["fps"] not in (30, 60, 120)
            or type(result["bitrate"]) is not int or not 4 <= result["bitrate"] <= 80
            or type(result["maxSize"]) is not int or result["maxSize"] not in (1280, 1920, 2560)):
        raise ValueError("Invalid video settings")
    return result


class Room:
    def __init__(self):
        self.receiver_token = secrets.token_urlsafe(32)
        self.resume_token = ""
        self.resume_until = 0.0
        self.peers = {}
        # The session runs the *effective* profile; the user's wish is *saved*
        # immediately but becomes effective only through the phone's applied ack.
        self.effective_profile = dict(DEFAULTS)
        self.saved_profile = dict(DEFAULTS)
        self.pending_profile = None
        self.failures = defaultdict(deque)
        self.total_failures = deque()
        self.rotate_code()

    @property
    def settings(self):
        """Legacy name for the profile the session actually runs."""
        return self.effective_profile

    def rotate_code(self):
        self.qr_token = secrets.token_urlsafe(24)
        self.code = f"{secrets.randbelow(100_000_000):08d}"
        self.code_until = time.monotonic() + 600

    def authenticate(self, role, credential, address):
        if not isinstance(credential, str) or not credential.isascii() or len(credential) > 128:
            return False
        if role == "receiver":
            return secrets.compare_digest(credential, self.receiver_token)
        if role != "sender":
            return False
        now = time.monotonic()
        if self.resume_token and secrets.compare_digest(credential, self.resume_token) and now < self.resume_until:
            return True
        for attempts in (self.failures[address], self.total_failures):
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
        if len(self.failures[address]) >= 5 or len(self.total_failures) >= 50:
            return False
        if now < self.code_until and (secrets.compare_digest(credential, self.code) or secrets.compare_digest(credential, self.qr_token)):
            self.resume_token = secrets.token_urlsafe(32)
            self.resume_until = now + 86400
            self.rotate_code()
            return True
        self.failures[address].append(now)
        self.total_failures.append(now)
        if len(self.failures) > 512:
            self.failures.clear()
        return False

    def request_profile(self, settings, request_id=""):
        """Save the user's wish; effective only after acknowledge_profile().

        The wish is stored right away for the next connection. While a sender
        is connected the request also becomes pending: the phone is asked to
        apply it, and only its applied ack makes it effective. Returns the
        saved wish for forwarding to the sender.
        """
        settings = video_settings(settings)
        self.saved_profile = settings
        if "sender" in self.peers:
            self.pending_profile = {"requestId": request_id, "settings": dict(settings)}
        return dict(settings)

    def acknowledge_profile(self, message):
        """Reconcile the phone's acknowledgement with the effective profile.

        An acknowledgement for an unknown/stale/superseded request cannot
        change anything. A rejection keeps the previously effective profile
        and the saved wish; an applied change adopts the settings the phone
        actually reports. Returns the new effective profile, or None when the
        acknowledgement did not match the pending request.
        """
        pending = self.pending_profile
        if pending is None or message.get("requestId") != pending["requestId"]:
            return None
        self.pending_profile = None
        if message.get("applied") is True:
            try:
                self.effective_profile = video_settings(message.get("settings"))
            except ValueError:
                pass  # A malformed applied ack keeps the previous effective profile.
        adopt = getattr(self.peers.get("receiver"), "effective_profile_changed", None)
        if callable(adopt):
            adopt()
        return dict(self.effective_profile)

    async def send(self, role, message):
        peer = self.peers.get(role)
        if peer is not None and not peer.closed:
            try:
                await peer.send_json(message)
            except ConnectionError:
                pass

    async def ready(self):
        if all(role in self.peers for role in ("sender", "receiver")):
            await self.send("receiver", {"type": "peer-ready"})
            # A joining phone is asked for the saved wish; when it differs from
            # the effective profile its applied ack is required to switch over.
            await self.send("sender", {"type": "peer-ready", "settings": dict(self.saved_profile)})
            if self.saved_profile != self.effective_profile:
                self.pending_profile = {"requestId": "", "settings": dict(self.saved_profile)}


@web.middleware
async def headers(request, handler):
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Cross-origin request rejected")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'")
    return response


def receiver_authorized(request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return token.isascii() and secrets.compare_digest(token, request.app[STATE].receiver_token)


async def index(request):
    return web.Response(text="AirFrame native receiver. Connect using the Android application.")


async def asset(request):
    raise web.HTTPNotFound()


async def session(request):
    if not receiver_authorized(request):
        raise web.HTTPUnauthorized()
    room = request.app[STATE]
    if time.monotonic() >= room.code_until:
        room.rotate_code()
    return web.json_response({"code": room.code, "expiresIn": int(room.code_until - time.monotonic()),
                              "addresses": lan_addresses(),
                              "settings": room.settings,
                              "effectiveSettings": room.effective_profile,
                              "savedSettings": room.saved_profile,
                              "pendingRequestId": (room.pending_profile or {}).get("requestId"),
                              "senderConnected": "sender" in room.peers})


async def rotate(request):
    if not receiver_authorized(request):
        raise web.HTTPUnauthorized()
    room = request.app[STATE]
    room.rotate_code()
    return web.json_response({"code": room.code})


def validate_signal(role, message):
    kind = message.get("type")
    if kind in ("offer", "answer", "ice") and (
            not isinstance(message.get("session"), str) or not 1 <= len(message["session"]) <= 32):
        return False
    if kind == ("offer" if role == "sender" else "answer"):
        return isinstance(message.get("sdp"), str) and len(message["sdp"]) <= 60000
    if kind == "ice":
        return (isinstance(message.get("candidate"), str) and len(message["candidate"]) <= 4096
                and isinstance(message.get("sdpMid"), (str, type(None)))
                and type(message.get("sdpMLineIndex")) is int
                and 0 <= message["sdpMLineIndex"] < 32)
    if kind == "telemetry" and role == "sender":
        return telemetry_valid(message)
    if kind == "configured" and role == "sender":
        try:
            video_settings(message.get("settings"))
        except ValueError:
            return False
        return (type(message.get("applied")) is bool and isinstance(message.get("requestId"), str)
                and len(message["requestId"]) <= 64)
    if kind == "volume" and role == "receiver":
        return type(message.get("value")) is int and 0 <= message["value"] <= 100
    if kind == "audio" and role == "receiver":
        return type(message.get("enabled")) is bool
    return kind == "restart" and role == "receiver"


async def signal(request):
    room = request.app[STATE]
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=65536)
    await ws.prepare(request)
    role = None
    try:
        auth = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if not isinstance(auth, dict):
            raise ValueError("Invalid authentication")
        role = auth.get("role")
        if role not in ("sender", "receiver"):
            role = None
            raise ValueError("Unknown role")
        if role in room.peers and not room.peers[role].closed:
            await ws.send_json({"type": "error", "message": "Устройство уже подключено. Завершите прежний сеанс."})
            await ws.close(code=1008)
            return ws
        if not room.authenticate(role, auth.get("credential"), request.remote or "unknown"):
            await ws.send_json({"type": "error", "message": "Неверный или истёкший код. Проверьте приёмник."})
            await ws.close(code=1008)
            return ws
        room.peers[role] = ws
        await ws.send_json({"type": "authenticated", **({"resumeToken": room.resume_token} if role == "sender" else {})})
        await room.ready()
        other = "receiver" if role == "sender" else "sender"
        async for packet in ws:
            if packet.type != WSMsgType.TEXT:
                continue
            message = json.loads(packet.data)
            if not isinstance(message, dict):
                raise ValueError("Invalid message")
            if message.get("type") == "configure" and role == "receiver":
                request_id = str(message.get("requestId", ""))[:64]
                settings = room.request_profile(message.get("settings"), request_id)
                await room.send("sender", {"type": "configure", "settings": settings, "requestId": request_id})
            elif message.get("type") == "stop" and role == "receiver":
                await room.send("sender", {"type": "stop"})
            elif validate_signal(role, message):
                if message.get("type") == "configured" and role == "sender":
                    room.acknowledge_profile(message)
                await room.send(other, message)
            else:
                raise ValueError("Unsupported signaling message")
    except (asyncio.TimeoutError, ValueError, TypeError, KeyError):
        await ws.close(code=1008)
    finally:
        if room.peers.get(role) is ws:
            del room.peers[role]
            if role == "sender":
                room.resume_until = time.monotonic() + 600
                room.pending_profile = None
            await room.send("receiver" if role == "sender" else "sender", {"type": "peer-left"})
        await ws.close()
    return ws


async def cleanup(app):
    await asyncio.gather(*(ws.close(code=1001, message=b"Receiver shutting down")
                           for ws in list(app[STATE].peers.values())))


def create_app(room=None):
    app = web.Application(middlewares=[headers], client_max_size=65536)
    app[STATE] = room or Room()
    app.router.add_get("/", index)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/rotate", rotate)
    app.router.add_get("/signal", signal)
    app.on_shutdown.append(cleanup)
    return app


def lan_addresses():
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = result[4][0]
            if not ipaddress.ip_address(ip).is_loopback:
                addresses.add(ip)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            ip = probe.getsockname()[0]
            if not ipaddress.ip_address(ip).is_loopback:
                addresses.add(ip)
    except OSError:
        pass
    return sorted(addresses)


