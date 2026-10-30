"""Native WebRTC video reception; no browser, STUN, TURN, or cloud services."""
import asyncio
import time

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp

from .transport import VIDEO_LOSS_DEADLINE, install_video_buffers
from .playout import Playout


class Receiver:
    # Watchdog tick for RTP loss deadlines; the deadline itself is 150 ms.
    RECOVERY_POLL = .020

    def __init__(self, room, on_frame, on_state, on_stats, on_log, on_audio=None, playout_mode="stable"):
        self.room = room
        self.on_frame, self.on_state, self.on_stats, self.on_log = on_frame, on_state, on_stats, on_log
        self.on_audio = on_audio
        self.peer = None
        self.session = ""
        self.tasks = set()
        self.video_buffers = []
        self.closed = False
        self.frame_count = 0
        self.sender_stats = {}
        self.converted_count = self.dropped_count = 0
        self.conversion_ms = 0.0
        self.last_frame_at = 0.0
        self.max_frame_gap = 0.0
        self.playout = Playout(playout_mode)
        self.playout_issue = None
        self.playout_changed = None
        self.last_output_at = self.output_gap = 0.0

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send_json(self, message):
        kind = message["type"]
        if kind == "peer-ready":
            self.on_state("connecting", "Android подключён. Ожидаем видеопоток…")
        elif kind == "peer-left":
            await self.close_peer()
            self.on_state("idle", "Телефон отключился. Ожидаем повторного подключения.")
        elif kind == "telemetry" and message["session"] == self.session:
            self.sender_stats = {k: message[k] for k in ("encodedFps", "width", "height", "limitation")}
            if "volume" in message:
                self.sender_stats["volume"] = message["volume"]
        elif kind == "configured":
            self.on_stats({"configured": message})
        elif kind == "offer":
            await self.offer(message)
        elif kind == "ice" and self.peer and message["session"] == self.session:
            candidate = candidate_from_sdp(message["candidate"].removeprefix("candidate:"))
            candidate.sdpMid = message["sdpMid"]
            candidate.sdpMLineIndex = message["sdpMLineIndex"]
            await self.peer.addIceCandidate(candidate)

    async def offer(self, message):
        await self.close_peer()
        self.session = message["session"]
        current = self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.frame_count = 0

        @current.on("connectionstatechange")
        async def changed():
            if current is not self.peer:
                return
            state = current.connectionState
            self.on_log(f"WebRTC: {state}")
            if state == "connected":
                self.on_state("live", "Прямой зашифрованный поток с Android")
            elif state == "failed":
                self.on_state("retry", "Связь потеряна. Восстанавливаем медиасеанс…")
                await self.room.send("sender", {"type": "restart"})

        @current.on("track")
        def track_received(track):
            if track.kind == "video":
                self.task(self.frames(current, track))
            elif track.kind == "audio" and self.on_audio is not None:
                self.task(self.audio(current, track))

        try:
            await current.setRemoteDescription(RTCSessionDescription(sdp=message["sdp"], type="offer"))
            self.video_buffers = install_video_buffers(current)
            self.task(self.watch_video_buffers(current, self.video_buffers))
            self.on_log(f"Видеобуфер RTP: 2048 пакетов, потеря ждёт "
                        f"{VIDEO_LOSS_DEADLINE*1000:.0f} мс, без дополнительной задержки вывода")
            await current.setLocalDescription(await current.createAnswer())
            await self.room.send("sender", {"type": "answer", "session": self.session, "sdp": current.localDescription.sdp})
            self.task(self.statistics(current))
        except Exception as exc:
            self.on_log(f"Не удалось согласовать видео: {exc}")
            self.on_state("error", "Ошибка формата видео. Повторите подключение.")
            await self.close_peer()

    async def watch_video_buffers(self, current, buffers):
        """Advance the RTP loss deadlines off the packet path; request keyframes."""
        try:
            while current is self.peer:
                await asyncio.sleep(self.RECOVERY_POLL)
                if current is not self.peer:
                    break
                for buffer in buffers:
                    pli = buffer.expire()
                    events = buffer.take_events()
                    if events['expiries'] or events['ssrc']:
                        self.reset_nack(buffer)
                    if events['expiries']:
                        self.on_log(f"Потеря RTP: кадр не собран за "
                                    f"{buffer.deadline*1000:.0f} мс, запрошен ключевой кадр (PLI)")
                    if events['recoveries']:
                        self.on_log("Видеопоток восстановлен с нового ключевого кадра")
                    if events['ssrc']:
                        self.on_log("Смена SSRC: таймеры и запросы прежней эпохи отменены")
                    if pli:
                        await self.request_keyframe(buffer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.on_log(f"Восстановление видео: {exc}")

    async def request_keyframe(self, buffer):
        receiver = getattr(buffer, 'receiver', None)
        ssrc = buffer.last_ssrc
        if receiver is None or ssrc is None:
            return
        try:
            await receiver._send_rtcp_pli(ssrc)
        except Exception as exc:
            self.on_log(f"Не удалось запросить ключевой кадр: {exc}")

    @staticmethod
    def reset_nack(buffer):
        """After dropping a damaged epoch, stop asking for its packets."""
        receiver = getattr(buffer, 'receiver', None)
        nack = getattr(receiver, '_RTCRtpReceiver__nack_generator', None) if receiver else None
        if nack is not None:
            nack.max_seq = None
            nack.missing.clear()

    LABELS = {'stable': 'стабильность, запас 250–350 мс',
              'balanced': 'баланс, запас 120–200 мс',
              'low': 'минимальная задержка'}

    def set_playout(self, mode):
        self.playout.reset(mode=mode)
        if self.playout_changed:
            self.playout_changed.set()
        self.on_stats({"playoutChanged": mode})
        self.on_log("Вывод: " + self.LABELS.get(mode, mode))

    async def audio(self, current, track):
        """Opus arrives as 48 kHz frames; hand packed s16 stereo to the audio sink."""
        try:
            while current is self.peer:
                frame = await track.recv()
                if current is not self.peer:
                    break
                pcm = frame.reformat(format="s16", layout="stereo")
                self.on_audio(bytes(pcm.planes[0]))
        except MediaStreamError:
            pass
        except Exception as exc:
            self.on_log(f"Ошибка звука: {exc}")

    def report_playout_issue(self):
        """Surface an incompatible playout budget once, with the safe suggestion."""
        issue = self.playout.incompatible
        if issue is not None and self.playout_issue != issue:
            self.playout_issue = issue
            self.on_log(issue['message'])
            self.on_state("error", issue['message'])
        elif issue is None:
            self.playout_issue = None

    async def frames(self, current, track):
        self.playout.reset(fps=self.room.settings["fps"])
        changed = self.playout_changed = asyncio.Event()
        ended = False

        async def receive():
            nonlocal ended
            try:
                while current is self.peer:
                    frame = await track.recv()
                    if current is not self.peer:
                        break
                    now = time.monotonic()
                    if self.last_frame_at:
                        self.max_frame_gap = max(self.max_frame_gap, now-self.last_frame_at)
                    self.last_frame_at = now
                    self.frame_count += 1
                    fps = self.room.settings["fps"]
                    if self.playout.fps != fps:
                        # A profile change explicitly recreates budget and timeline.
                        self.playout.reset(fps=fps)
                    self.playout.push(frame, now)
                    self.report_playout_issue()
                    changed.set()
                    if self.frame_count % 8 == 0:
                        await asyncio.sleep(0)
            except MediaStreamError:
                pass
            finally:
                ended = True
                changed.set()

        reader = asyncio.create_task(receive())
        try:
            while current is self.peer:
                changed.clear()
                item, delay = self.playout.take(time.monotonic())
                if item is None:
                    if ended and not self.playout.frames:
                        break
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=min(.020, delay))
                    except asyncio.TimeoutError:
                        pass
                    continue
                started = time.monotonic()
                conversion = asyncio.create_task(asyncio.to_thread(self.on_frame, item.frame))
                try:
                    await asyncio.shield(conversion)
                except asyncio.CancelledError:
                    await conversion
                    raise
                now = time.monotonic()
                if self.last_output_at:
                    self.output_gap = max(self.output_gap, now-self.last_output_at)
                self.last_output_at = now
                self.converted_count += 1
                self.conversion_ms = (now-started)*1000
            await reader
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.on_log(f"Ошибка видеовывода: {exc}")
            self.on_state("error", "Ошибка подготовки видеокадра")
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            if self.playout_changed is changed:
                self.playout_changed = None

    async def statistics(self, current):
        previous_ready = 0
        last_gap_log = 0.0
        previous_converted, previous_dropped = self.converted_count, self.playout.skipped
        previous_time, previous_frames, previous_bytes, previous_lost, previous_packets = time.monotonic(), self.frame_count, 0, 0, 0
        try:
            while current is self.peer:
                await asyncio.sleep(1)
                if current is not self.peer:
                    break
                report = await current.getStats()
                received = packets = lost = 0
                jitter = 0
                for stat in report.values():
                    if stat.type == "transport":
                        received += stat.bytesReceived
                    elif stat.type == "inbound-rtp" and stat.kind == "video":
                        packets += stat.packetsReceived
                        lost += stat.packetsLost
                        jitter = stat.jitter / 90  # Video RTP clock: 90 kHz → milliseconds.
                now = time.monotonic()
                elapsed = max(now - previous_time, 0.001)
                delta_lost = max(0, lost - previous_lost)
                delta_packets = max(0, packets - previous_packets)
                ready = sum(buffer.completed for buffer in self.video_buffers)
                transport = {"assembledFps": max(0,ready-previous_ready)/elapsed}
                for buffer in self.video_buffers:
                    for key,value in buffer.interval().items():
                        transport[key] = max(transport.get(key,0),value)
                previous_ready = ready
                if now-last_gap_log >= 5 and max(self.max_frame_gap*1000,transport.get('assemblyGapMs',0)) >= 100:
                    last_gap_log = now
                    self.on_log(f"Пауза видео: RTP {transport.get('packetGapMs',0):.0f} мс; "
                                f"сборка {transport.get('assemblyGapMs',0):.0f} мс; "
                                f"декодер {self.max_frame_gap*1000:.0f} мс; "
                                f"кадр до {transport.get('largestFrameKiB',0):.0f} КиБ; "
                                f"восстановлений буфера {transport.get('bufferRecoveries',0)}")
                self.on_stats({**self.sender_stats, **transport, **self.playout.metrics(), "fps": (self.frame_count - previous_frames) / elapsed,
                               "bitrate": max(0, received - previous_bytes) * 8 / elapsed / 1e6,
                               "jitter": jitter,
                               "convertedFps": (self.converted_count - previous_converted) / elapsed,
                               "skipped": self.playout.skipped - previous_dropped,
                               "outputGapMs": max(self.output_gap, now-self.last_output_at if self.last_output_at else 0)*1000,
                               "conversionMs": self.conversion_ms,
                               "frameGapMs": max(self.max_frame_gap, now - self.last_frame_at if self.last_frame_at else 0) * 1000,
                               "loss": delta_lost * 100 / max(1, delta_packets + delta_lost)})
                previous_converted, previous_dropped = self.converted_count, self.playout.skipped
                self.max_frame_gap = self.output_gap = 0.0
                previous_time, previous_frames, previous_bytes, previous_lost, previous_packets = now, self.frame_count, received, lost, packets
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.on_log(f"Статистика: {exc}")

    async def close_peer(self):
        old, self.peer = self.peer, None
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if old:
            await old.close()
        self.sender_stats = {}
        self.video_buffers = []
        self.converted_count = self.dropped_count = 0
        self.conversion_ms = self.last_frame_at = self.max_frame_gap = 0.0
        self.playout = Playout(self.playout.mode, self.room.settings["fps"])
        self.playout_issue = None
        self.last_output_at = self.output_gap = 0.0
        self.on_stats({})
        self.on_frame(None)

    async def close(self, **kwargs):
        self.closed = True
        await self.close_peer()
