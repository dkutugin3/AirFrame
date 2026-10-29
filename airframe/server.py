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
DEFAULTS = {"fps": 120, "bitrate": 80, "maxSize": 2560}


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
        self.settings = dict(DEFAULTS)
        self.failures = defaultdict(deque)
        self.total_failures = deque()
        self.rotate_code()

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
            await self.send("sender", {"type": "peer-ready", "settings": self.settings})


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
                              "addresses": lan_addresses(), "settings": room.settings,
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
        return (isinstance(message.get("session"), str) and 1 <= len(message["session"]) <= 32
                and all(type(message.get(key)) in (int, float) and math.isfinite(message[key])
                        and 0 <= message[key] <= bound for key,bound in (("encodedFps",1000),("width",16384),("height",16384)))
                and message.get("limitation") in ("none","cpu","bandwidth","other","unknown"))
    if kind == "configured" and role == "sender":
        try:
            video_settings(message.get("settings"))
        except ValueError:
            return False
        return (type(message.get("applied")) is bool and isinstance(message.get("requestId"), str)
                and len(message["requestId"]) <= 64)
    if kind == "volume" and role == "receiver":
        return type(message.get("value")) is int and 0 <= message["value"] <= 100
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
                room.settings = video_settings(message.get("settings"))
                await room.send("sender", {"type": "configure", "settings": room.settings, "requestId": str(message.get("requestId", ""))[:64]})
            elif message.get("type") == "stop" and role == "receiver":
                await room.send("sender", {"type": "stop"})
            elif validate_signal(role, message):
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


