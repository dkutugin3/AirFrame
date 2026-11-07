import asyncio
import base64
import re
import unittest

from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, VideoStreamTrack
from aiohttp.test_utils import AioHTTPTestCase

from airframe.receiver import Receiver
from airframe.server import Room, create_app


class NativeVideoTests(AioHTTPTestCase):
    async def get_application(self):
        self.room = Room()
        self.count = 0
        self.received = asyncio.Event()
        self.states = []
        self.metrics = []
        self.logs = []

        loop = asyncio.get_running_loop()

        def record_frame(frame):
            if frame is not None:
                self.count += 1
                self.assertEqual(frame.width, 640)
                if self.count >= 8:
                    self.received.set()

        def frame_received(frame):
            loop.call_soon_threadsafe(record_frame, frame)

        self.receiver = Receiver(self.room, frame_received, lambda *args:self.states.append(args),
                                 self.metrics.append, self.logs.append)
        self.room.peers['receiver'] = self.receiver
        return create_app(self.room)

    async def test_encoded_video_reaches_native_decoder_through_signaling(self):
        await self.check_video()

    async def test_h264_reaches_native_decoder(self):
        await self.check_video('video/H264')

    async def test_h265_reaches_native_decoder(self):
        # Exercise the actual Receiver factory, not only aiortc's decoder registry.
        await self.check_video('video/H265')

    async def test_large_h264_keyframe_over_real_webrtc(self):
        from test_transport import large_h264_packet
        class LargeFrameTrack(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.codec,self.frame,self.first=large_h264_packet()
                self.index=0
            async def recv(self):
                await asyncio.sleep(1/30)
                if self.index==0:
                    result=self.first
                else:
                    self.frame.pts=self.index*3000
                    result=self.codec.encode(self.frame)[0]
                self.index+=1
                return result
        await self.check_video('video/H264',LargeFrameTrack())
        self.assertTrue(any(m.get('largestFrameKiB',0)>156 for m in self.metrics),self.metrics)

    async def check_video(self, codec=None, track=None):
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(track or VideoStreamTrack())
        if codec:
            capabilities = [c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType==codec]
            self.assertTrue(capabilities)
            sender.getTransceivers()[0].setCodecPreferences(capabilities)
        socket = await self.client.ws_connect('/signal')
        try:
            await socket.send_json({'role':'sender','credential':self.room.code})
            self.assertEqual((await socket.receive_json(timeout=3))['type'],'authenticated')
            self.assertEqual((await socket.receive_json(timeout=3))['type'],'peer-ready')
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type':'offer','session':'native-test','sdp':sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            self.assertEqual(answer['type'],'answer')
            from aiortc import RTCSessionDescription
            await sender.setRemoteDescription(RTCSessionDescription(type='answer',sdp=answer['sdp']))
            await asyncio.wait_for(self.received.wait(),timeout=8)
            await asyncio.sleep(1.1)
            self.assertGreaterEqual(self.count,8)
            self.assertTrue(any(state[0]=='live' for state in self.states),self.logs)
            self.assertTrue(any(stat.get('fps',0)>0 for stat in self.metrics),self.logs)
        finally:
            await socket.close()
            await sender.close()
        await asyncio.sleep(0.05)
        self.assertIsNone(self.receiver.peer)


if __name__=='__main__':unittest.main()

class DummyPeer:
    async def close(self):pass


class LatestFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_statistics_reports_actual_software_mode_after_hardware_fallback(self):
        class Peer:
            async def getStats(self):
                return {}

        class Factory:
            def status_snapshot(self):
                return {'selectedMode': 'hardware', 'actualMode': 'software',
                        'verifiedHardware': False, 'reason': 'GPU device lost'}

        metrics = []
        receiver = Receiver(Room(), lambda *args: None, lambda *args: None,
                            metrics.append, lambda *args: None)
        peer = receiver.peer = Peer()
        receiver.decoder_factory = Factory()
        worker = asyncio.create_task(receiver.statistics(peer))
        try:
            for _ in range(150):
                if metrics:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(metrics)
            self.assertEqual(metrics[0]['hwDecodeMode'], 'software')
            self.assertFalse(metrics[0]['hwDecodeVerified'])
        finally:
            receiver.peer = None
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_slow_conversion_does_not_backlog_decoded_frames(self):
        import threading
        from aiortc.mediastreams import MediaStreamError
        incoming = asyncio.Queue()
        class Track:
            async def recv(self):
                value = await incoming.get()
                if value is None:raise MediaStreamError
                return value
        blocked, release = threading.Event(), threading.Event()
        output=[]
        def convert(frame):
            if frame==0:
                blocked.set()
                release.wait(3)
            output.append(frame)
        receiver=Receiver(Room(),convert,lambda *a:None,lambda *a:None,lambda *a:None)
        receiver.set_playout("low")
        receiver.peer=DummyPeer()
        worker=receiver.task(receiver.frames(receiver.peer,Track()))
        try:
            await incoming.put(0)
            self.assertTrue(await asyncio.to_thread(blocked.wait,2))
            # Complete decoded frames arrive while the first conversion stalls.
            for frame in range(1,121):await incoming.put(frame)
            for _ in range(100):
                if receiver.frame_count==121:break
                await asyncio.sleep(.002)
            self.assertEqual(receiver.frame_count,121)
            self.assertEqual(receiver.playout.skipped,119)
            release.set()
            for _ in range(100):
                if len(output)>=2:break
                await asyncio.sleep(.002)
            self.assertEqual(output,[0,120])
        finally:
            release.set()
            await incoming.put(None)
            await asyncio.wait_for(worker,2)
            await receiver.close_peer()

    async def test_disconnect_waits_for_inflight_conversion_before_clearing(self):
        import threading
        started,release=threading.Event(),threading.Event()
        incoming=asyncio.Queue()
        class Track:
            async def recv(self):return await incoming.get()
        output=[]
        def convert(frame):
            if frame is not None:
                started.set();release.wait(3)
            output.append(frame)
        receiver=Receiver(Room(),convert,lambda *a:None,lambda *a:None,lambda *a:None)
        receiver.set_playout("low")
        receiver.peer=DummyPeer()
        receiver.task(receiver.frames(receiver.peer,Track()))
        await incoming.put(1)
        self.assertTrue(await asyncio.to_thread(started.wait,2))
        close=asyncio.create_task(receiver.close_peer())
        await asyncio.sleep(.02)
        self.assertFalse(close.done())
        release.set()
        await asyncio.wait_for(close,2)
        self.assertEqual(output,[1,None])


class PlayoutBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_playout_budget_is_refused_with_60_fps_suggestion(self):
        import av
        from fractions import Fraction
        from aiortc.mediastreams import MediaStreamError
        room = Room()
        room.effective_profile = {**room.effective_profile, 'fps': 120, 'bitrate': 24, 'maxSize': 2560}
        logs, states, delivered = [], [], []
        receiver = Receiver(room, delivered.append, lambda *args: states.append(args),
                            lambda *args: None, logs.append)
        receiver.set_playout('stable')
        frames = []
        for index in range(3):
            frame = av.VideoFrame(1920, 2560, 'yuv420p')
            frame.pts, frame.time_base = index, Fraction(1, 120)
            frames.append(frame)

        class Track:
            index = 0
            async def recv(self):
                if Track.index >= len(frames):
                    raise MediaStreamError
                frame = frames[Track.index]
                Track.index += 1
                return frame

        receiver.peer = DummyPeer()
        worker = receiver.task(receiver.frames(receiver.peer, Track()))
        await asyncio.wait_for(worker, 3)
        try:
            self.assertIsNotNone(receiver.playout.incompatible)
            self.assertEqual(receiver.playout.incompatible['suggestionFps'], 60)
            self.assertTrue(any('60 FPS' in log for log in logs), logs)
            self.assertTrue(any(state[0] == 'error' for state in states), states)
            self.assertEqual(delivered, [])
            self.assertEqual(receiver.playout.metrics()['playoutIncompatible'], True)
        finally:
            await receiver.close_peer()


class PacketLossRecoveryTests(AioHTTPTestCase):
    async def get_application(self):
        self.room = Room()
        self.frames = []
        self.first_frames = asyncio.Event()
        self.states, self.metrics, self.logs = [], [], []
        loop = asyncio.get_running_loop()

        def record(frame):
            if frame is not None:
                self.frames.append(frame)
                if len(self.frames) >= 8:
                    self.first_frames.set()

        self.receiver = Receiver(self.room, lambda frame: loop.call_soon_threadsafe(record, frame),
                                 lambda *args: self.states.append(args),
                                 self.metrics.append, self.logs.append)
        self.room.peers['receiver'] = self.receiver
        return create_app(self.room)

    async def test_lost_rtp_packet_recovers_from_requested_keyframe(self):
        from fractions import Fraction
        import av
        from aiortc import RTCSessionDescription, RTCRtpSender
        from aiortc.mediastreams import VIDEO_TIME_BASE

        class LumaTrack(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.index = 0

            async def recv(self):
                await asyncio.sleep(1 / 30)
                frame = av.VideoFrame(320, 180, 'yuv420p')
                luma = 20 + (self.index % 25) * 7
                frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
                for plane in frame.planes[1:]:
                    plane.update(bytes([128]) * plane.buffer_size)
                frame.pts = self.index * 3000
                frame.time_base = VIDEO_TIME_BASE
                self.index += 1
                return frame

        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(LumaTrack())
        capabilities = [codec for codec in RTCRtpSender.getCapabilities('video').codecs
                        if codec.mimeType == 'video/H264']
        self.assertTrue(capabilities)
        sender.getTransceivers()[0].setCodecPreferences(capabilities)
        socket = await self.client.ws_connect('/signal')
        recovered = {'block_rtx': True}
        try:
            await socket.send_json({'role': 'sender', 'credential': self.room.code})
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'authenticated')
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'peer-ready')
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type': 'offer', 'session': 'loss-test',
                                    'sdp': sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            self.assertEqual(answer['type'], 'answer')
            await sender.setRemoteDescription(RTCSessionDescription(type='answer', sdp=answer['sdp']))
            await asyncio.wait_for(self.first_frames.wait(), timeout=8)

            # Lose one original RTP packet and its RTX retransmissions: the
            # damaged frame can only be abandoned by the time deadline.
            rtp_receiver = self.receiver.peer.getReceivers()[0]
            codecs = getattr(rtp_receiver, '_RTCRtpReceiver__codecs')
            h264_types = {pt for pt, codec in codecs.items() if codec.mimeType == 'video/H264'}
            rtx_types = {pt for pt, codec in codecs.items() if codec.mimeType == 'video/rtx'}
            self.assertTrue(h264_types)
            original = rtp_receiver._handle_rtp_packet
            state = {'shoot': True, 'seen': 0, 'dropped': None, 'rtx': 0}

            async def lossy(packet, arrival_time_ms):
                if recovered['block_rtx'] and packet.payload_type in rtx_types:
                    state['rtx'] += 1
                    return
                if state['shoot'] and packet.payload_type in h264_types:
                    state['seen'] += 1
                    if state['seen'] == 3:
                        state['dropped'] = packet.sequence_number
                        state['shoot'] = False
                        return
                await original(packet, arrival_time_ms)

            rtp_receiver._handle_rtp_packet = lossy
            loop = asyncio.get_running_loop()
            limit = loop.time() + 8
            while state['dropped'] is None:          # wait until the packet loss is real
                if loop.time() > limit:
                    self.fail(f'loss was never injected: logs={self.logs}')
                await asyncio.sleep(.01)
            limit = loop.time() + 5
            while not any('PLI' in log for log in self.logs):   # deadline demand for a keyframe
                if loop.time() > limit:
                    self.fail(f'no PLI after the RTP loss: logs={self.logs}')
                await asyncio.sleep(.05)
            received_before = len(self.frames)
            limit = loop.time() + 10
            while len(self.frames) < received_before + 6:       # decoding resumes on the keyframe
                if loop.time() > limit:
                    self.fail(f'stream did not resume after loss: frames={len(self.frames)} logs={self.logs}')
                await asyncio.sleep(.05)
            recovered['block_rtx'] = False
            await asyncio.sleep(1.2)   # statistics interval publishes the buffer metrics
            self.assertTrue(any(metric.get('deadlineExpiries', 0) >= 1 for metric in self.metrics),
                            self.metrics)
            self.assertTrue(any(metric.get('pliRequests', 0) >= 1 for metric in self.metrics),
                            self.metrics)
            self.assertGreaterEqual(len(self.frames), received_before + 6)
        finally:
            recovered['block_rtx'] = False
            await socket.close()
            await sender.close()
        await asyncio.sleep(.05)
        self.assertIsNone(self.receiver.peer)


class DataChannelAudioTests(unittest.TestCase):
    # ffmpeg -f lavfi -i sine=frequency=440:sample_rate=48000 -t 0.1 -ac 2 -c:a aac ADTS frames.
    ADTS = "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"

    def test_aac_messages_decode_to_packed_s16(self):
        stream = base64.b64decode(self.ADTS)
        frames = []
        offset = 0
        while offset < len(stream):
            length = ((stream[offset+3] & 0x03) << 11) | (stream[offset+4] << 3) | ((stream[offset+5] >> 5) & 0x07)
            frames.append(stream[offset+7:offset+length])
            offset += length

        class FakeChannel:
            label = "airframe-audio"
            def __init__(self): self.handlers = {}
            def on(self, event):
                def register(handler):
                    self.handlers[event] = handler
                    return handler
                return register

        channel = FakeChannel()
        delivered = []
        receiver = Receiver(Room(), lambda f: None, lambda *a: None, lambda *a: None,
                            lambda *a: None, delivered.append)
        receiver.phone_audio(channel)
        channel.handlers["message"](b"\x00" + bytes([0x12, 0x10]))  # AAC-LC 48 kHz stereo ASC
        for payload in frames:
            channel.handlers["message"](b"\x01" + payload)
        self.assertTrue(delivered)
        for chunk in delivered:
            self.assertEqual(len(chunk) % 4, 0)



class DecoderOverloadTests(AioHTTPTestCase):
    async def get_application(self):
        self.room = Room()
        self.count = 0
        self.frames_received = asyncio.Event()
        self.metrics, self.logs = [], []
        loop = asyncio.get_running_loop()

        def record(frame):
            if frame is not None:
                self.count += 1
                if self.count >= 5:
                    self.frames_received.set()

        self.receiver = Receiver(self.room, lambda frame: loop.call_soon_threadsafe(record, frame),
                                 lambda *args: None, self.metrics.append, self.logs.append)
        self.room.peers['receiver'] = self.receiver
        return create_app(self.room)

    async def test_decode_overload_is_bounded_and_recovered_with_metrics(self):
        import time as time_module
        import av
        import airframe.decode as decode_module
        from aiortc import RTCSessionDescription, RTCRtpSender
        from aiortc.mediastreams import VIDEO_TIME_BASE

        real_factory = decode_module.get_decoder

        def slow_factory(codec):
            inner = real_factory(codec)

            class SlowDecoder:
                def decode(self, frame):
                    time_module.sleep(.25)
                    return inner.decode(frame)

            return SlowDecoder()

        class SourceTrack(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.index = 0

            async def recv(self):
                await asyncio.sleep(1 / 30)
                frame = av.VideoFrame(320, 180, 'yuv420p')
                luma = 20 + (self.index % 25) * 7
                frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
                for plane in frame.planes[1:]:
                    plane.update(bytes([128]) * plane.buffer_size)
                frame.pts = self.index * 3000
                frame.time_base = VIDEO_TIME_BASE
                self.index += 1
                return frame

        decode_module.get_decoder = slow_factory
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        socket = None
        try:
            sender.addTrack(SourceTrack())
            capabilities = [codec for codec in RTCRtpSender.getCapabilities('video').codecs
                            if codec.mimeType == 'video/H264']
            sender.getTransceivers()[0].setCodecPreferences(capabilities)
            socket = await self.client.ws_connect('/signal')
            await socket.send_json({'role': 'sender', 'credential': self.room.code})
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'authenticated')
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'peer-ready')
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type': 'offer', 'session': 'overload-test',
                                    'sdp': sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            await sender.setRemoteDescription(RTCSessionDescription(type='answer', sdp=answer['sdp']))
            await asyncio.wait_for(self.frames_received.wait(), timeout=10)
            loop = asyncio.get_running_loop()
            limit = loop.time() + 8
            while not any(metric.get('decodeRecoveries', 0) >= 1 for metric in self.metrics):
                if loop.time() > limit:
                    self.fail(f'decode overload was not recovered: {self.logs}')
                await asyncio.sleep(.1)
            self.assertTrue(any('Перегрузка декодера' in log for log in self.logs), self.logs)
            for metric in self.metrics:
                if 'decodeQueueMaxFrames' in metric:
                    self.assertLessEqual(metric['decodeQueueFrames'], metric['decodeQueueMaxFrames'])
                    self.assertLess(metric['decodeQueueAgeMs'], 2000)
                if 'decodedQueueMaxFrames' in metric:
                    self.assertLessEqual(metric['decodedQueueFrames'], metric['decodedQueueMaxFrames'])
            # Session close with slow/in-flight decoding must not hang.
            started = time_module.monotonic()
            await asyncio.wait_for(self.receiver.close(), 6)
            self.assertLess(time_module.monotonic() - started, 5)
            self.assertIsNone(self.receiver.peer)
        finally:
            decode_module.get_decoder = real_factory
            if socket is not None:
                await socket.close()
            await sender.close()


class AnswerLevelTests(AioHTTPTestCase):
    """The answer must declare an H.264 level that covers the receive profile.

    aiortc 1.15.0 advertises level 3.1 (profile-level-id 42001f/42e01f) for every
    target. At 2560 px / 60 fps that understates the requirement (level 5.1), so a
    sender that honours the declared level is forced to downscale or fall back.
    The answer AirFrame sends must declare the level it is configured to receive.
    """

    async def get_application(self):
        self.room = Room()
        self.receiver = Receiver(self.room, lambda frame: None, lambda *args: None,
                                 lambda *args: None, lambda *args: None)
        self.room.peers['receiver'] = self.receiver
        return create_app(self.room)

    async def answer_for(self, settings):
        self.room.effective_profile = dict(settings)
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(VideoStreamTrack())
        capabilities = [codec for codec in RTCRtpSender.getCapabilities('video').codecs
                        if codec.mimeType == 'video/H264']
        self.assertTrue(capabilities)
        sender.getTransceivers()[0].setCodecPreferences(capabilities)
        socket = await self.client.ws_connect('/signal')
        try:
            await socket.send_json({'role': 'sender', 'credential': self.room.code})
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'authenticated')
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'peer-ready')
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type': 'offer', 'session': 'level-test',
                                    'sdp': sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            self.assertEqual(answer['type'], 'answer')
            return answer['sdp']
        finally:
            await socket.close()
            await sender.close()

    @staticmethod
    def levels(sdp):
        return [int(value[4:6], 16) for value in
                re.findall(r'profile-level-id=([0-9a-fA-F]{6})', sdp)]

    async def test_base_profile_declares_level_5_1(self):
        sdp = await self.answer_for({'fps': 60, 'bitrate': 24, 'maxSize': 2560})
        self.assertEqual(self.levels(sdp), [0x33, 0x33], sdp)
        self.assertIn('profile-level-id=420033', sdp.lower(), sdp)
        self.assertIn('profile-level-id=42e033', sdp.lower(), sdp)

    async def test_1080p60_declares_level_4_2(self):
        sdp = await self.answer_for({'fps': 60, 'bitrate': 16, 'maxSize': 1920})
        self.assertEqual(self.levels(sdp), [0x2a, 0x2a], sdp)

    async def test_1080p30_declares_level_4_0(self):
        sdp = await self.answer_for({'fps': 30, 'bitrate': 10, 'maxSize': 1920})
        self.assertEqual(self.levels(sdp), [0x28, 0x28], sdp)

    async def test_offer_without_h264_keeps_answer_unchanged(self):
        self.room.effective_profile = dict({'fps': 60, 'bitrate': 16, 'maxSize': 1920})
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(VideoStreamTrack())
        capabilities = [codec for codec in RTCRtpSender.getCapabilities('video').codecs
                        if codec.mimeType == 'video/VP8']
        self.assertTrue(capabilities)
        sender.getTransceivers()[0].setCodecPreferences(capabilities)
        socket = await self.client.ws_connect('/signal')
        try:
            await socket.send_json({'role': 'sender', 'credential': self.room.code})
            await socket.receive_json(timeout=3)
            await socket.receive_json(timeout=3)
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type': 'offer', 'session': 'vp8-test',
                                    'sdp': sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            self.assertEqual(answer['type'], 'answer')
            self.assertEqual(self.levels(answer['sdp']), [], answer['sdp'])
        finally:
            await socket.close()
            await sender.close()


# --- Stage 5b: additive telemetry must reach on_stats/sender_stats -------------
TELEMETRY_LEGACY = {"type": "telemetry", "session": "telemetry-session",
                    "encodedFps": 60, "width": 2560, "height": 1440,
                    "limitation": "none"}
TELEMETRY_OPTIONAL = {
    "captureFps": 59.5, "maxCaptureIntervalMs": 34.2, "pacerFps": 60,
    "framesEncoded": 1200, "framesSent": 1190, "videoBitrateKbps": 21500,
    "avgEncodeTimeMs": 7.4, "codec": "H264", "codecProfile": "42e01f",
    "encoderImplementation": "c2.android.avc.encoder", "hardwareEncoder": "unknown",
    "thermalStatus": "light", "captureWidth": 2560, "captureHeight": 1440,
    "telemetryWindowMs": 1000, "sampledMonotonicMs": 123456,
}
TELEMETRY_OPTIONAL_KEYS = set(TELEMETRY_OPTIONAL)


class TelemetryPassThroughTests(unittest.IsolatedAsyncioTestCase):
    """The receiver passes validated optional telemetry through without inventing data."""

    def make_receiver(self):
        self.states, self.stats, self.logs = [], [], []
        receiver = Receiver(Room(), lambda frame: None,
                            lambda *args: self.states.append(args),
                            self.stats.append, self.logs.append)
        receiver.session = "telemetry-session"
        return receiver

    async def test_old_telemetry_payload_still_accepted(self):
        receiver = self.make_receiver()
        await receiver.send_json({**TELEMETRY_LEGACY, "volume": 42})
        self.assertEqual(receiver.sender_stats,
                         {"encodedFps": 60, "width": 2560, "height": 1440,
                          "limitation": "none", "volume": 42})

    async def test_optional_telemetry_fields_reach_sender_stats(self):
        receiver = self.make_receiver()
        await receiver.send_json({**TELEMETRY_LEGACY, **TELEMETRY_OPTIONAL,
                                  "volume": 42, "junk": "must not reach stats"})
        expected = {"encodedFps": 60, "width": 2560, "height": 1440,
                    "limitation": "none", "volume": 42, **TELEMETRY_OPTIONAL}
        self.assertEqual(receiver.sender_stats, expected)
        # Only known, server-validated fields are forwarded (bounded stats dict).
        self.assertNotIn("junk", receiver.sender_stats)

    async def test_absent_optional_fields_stay_absent(self):
        receiver = self.make_receiver()
        await receiver.send_json(dict(TELEMETRY_LEGACY))
        self.assertEqual(set(receiver.sender_stats),
                         {"encodedFps", "width", "height", "limitation"})
        for key in TELEMETRY_OPTIONAL_KEYS:
            self.assertNotIn(key, receiver.sender_stats)

    async def test_first_sample_counter_reset_and_new_session(self):
        receiver = self.make_receiver()
        # First sample: raw cumulative counters, no delta/rate invented by the receiver.
        await receiver.send_json({**TELEMETRY_LEGACY, "framesEncoded": 1200,
                                  "framesSent": 1190, "sampledMonotonicMs": 1000})
        self.assertIn("framesEncoded", receiver.sender_stats)
        self.assertEqual(receiver.sender_stats["framesEncoded"], 1200)
        self.assertEqual(receiver.sender_stats["framesSent"], 1190)
        # Counter reset (new session / re-encoded stream): raw values stay raw;
        # deriving rates and detecting the reset is the desktop sampler's job.
        await receiver.send_json({**TELEMETRY_LEGACY, "framesEncoded": 5,
                                  "framesSent": 4, "sampledMonotonicMs": 1020})
        self.assertIn("framesEncoded", receiver.sender_stats)
        self.assertEqual(receiver.sender_stats["framesEncoded"], 5)
        self.assertEqual(receiver.sender_stats["framesSent"], 4)
        # A new media session clears stale sender stats; telemetry of the old
        # session is ignored, telemetry of the new session is accepted.
        await receiver.close_peer()
        self.assertEqual(receiver.sender_stats, {})
        receiver.session = "next-telemetry-session"
        await receiver.send_json(dict(TELEMETRY_LEGACY))
        self.assertEqual(receiver.sender_stats, {})
        await receiver.send_json({**TELEMETRY_LEGACY, "session": "next-telemetry-session",
                                  "captureFps": 60})
        self.assertIn("captureFps", receiver.sender_stats)
        self.assertEqual(receiver.sender_stats["captureFps"], 60)


class TelemetrySignalingTests(AioHTTPTestCase):
    """End-to-end: sender telemetry over real signaling reaches receiver on_stats."""

    async def get_application(self):
        self.room = Room()
        self.metrics, self.logs = [], []
        self.receiver = Receiver(self.room, lambda frame: None, lambda *args: None,
                                 self.metrics.append, self.logs.append)
        self.room.peers['receiver'] = self.receiver
        return create_app(self.room)

    async def test_telemetry_reaches_on_stats_through_signaling(self):
        from aiortc import RTCSessionDescription
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(VideoStreamTrack())
        socket = await self.client.ws_connect('/signal')
        try:
            await socket.send_json({'role': 'sender', 'credential': self.room.code})
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'authenticated')
            self.assertEqual((await socket.receive_json(timeout=3))['type'], 'peer-ready')
            await sender.setLocalDescription(await sender.createOffer())
            await socket.send_json({'type': 'offer', 'session': 'telemetry-e2e',
                                    'sdp': sender.localDescription.sdp})
            answer = await socket.receive_json(timeout=5)
            self.assertEqual(answer['type'], 'answer')
            await sender.setRemoteDescription(
                RTCSessionDescription(type='answer', sdp=answer['sdp']))
            await socket.send_json({**TELEMETRY_LEGACY, **TELEMETRY_OPTIONAL,
                                    "session": "telemetry-e2e", "volume": 42,
                                    "junk": "must not reach stats"})
            # The receiver merges sender_stats into the periodic on_stats sample.
            for _ in range(250):
                if any(isinstance(stat, dict) and "captureFps" in stat
                       for stat in self.metrics):
                    break
                await asyncio.sleep(.02)
            else:
                self.fail(f"telemetry never reached on_stats: {self.metrics[-3:]}")
            stats = next(stat for stat in self.metrics
                         if isinstance(stat, dict) and "captureFps" in stat)
            self.assertEqual(stats["captureFps"], 59.5)
            self.assertEqual(stats["framesEncoded"], 1200)
            self.assertEqual(stats["encodedFps"], 60)
            self.assertEqual(stats["volume"], 42)
            self.assertNotIn("junk", stats)
        finally:
            await socket.close()
            await sender.close()
        await asyncio.sleep(.05)
        self.assertIsNone(self.receiver.peer)


class EffectiveProfileReceiverTests(unittest.IsolatedAsyncioTestCase):
    """I3/declared_answer: receiver reserve and answer level follow effective, not the wish."""

    class Peer:
        closed = False

    def make_receiver(self):
        self.stats, self.logs, self.states = [], [], []
        room = self.room = Room()
        receiver = Receiver(room, lambda frame: None,
                            lambda *args: self.states.append(args),
                            self.stats.append, self.logs.append)
        room.peers['receiver'] = receiver
        room.peers['sender'] = self.Peer()
        return receiver

    @staticmethod
    def sdp():
        return ("m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                "a=rtpmap:96 H264/90000\r\n"
                "a=fmtp:96 profile-level-id=42001f;packetization-mode=1\r\n")

    async def test_unacked_request_does_not_change_receiver_reserve(self):
        receiver = self.make_receiver()
        self.assertEqual(receiver.playout.max_frames, 24)      # 60 fps stable reserve
        wanted = {'fps': 30, 'bitrate': 10, 'maxSize': 1920}
        self.room.request_profile(wanted, 'wish-30')
        self.assertEqual(self.room.effective_profile,
                         {'fps': 60, 'bitrate': 24, 'maxSize': 2560})
        self.assertEqual(self.room.saved_profile, wanted)
        self.assertEqual(receiver.playout.max_frames, 24)      # still effective 60 fps
        self.room.acknowledge_profile({'requestId': 'wish-30', 'applied': False,
                                       'settings': wanted})
        self.assertEqual(receiver.playout.max_frames, 24)
        self.assertEqual(self.room.saved_profile, wanted)

    async def test_declared_answer_uses_previous_effective_after_rejection(self):
        receiver = self.make_receiver()
        self.room.request_profile({'fps': 30, 'bitrate': 10, 'maxSize': 1920}, 'r1')
        self.room.acknowledge_profile({'requestId': 'r1', 'applied': False,
                                       'settings': {'fps': 30, 'bitrate': 10, 'maxSize': 1920}})
        answer = receiver.declared_answer(self.sdp())
        self.assertIn('profile-level-id=420033', answer.lower())   # 2560@60 effective
        self.assertNotIn('profile-level-id=420028', answer.lower())  # not the 30 fps wish

    async def test_declared_answer_uses_applied_effective_profile(self):
        receiver = self.make_receiver()
        applied = {'fps': 30, 'bitrate': 10, 'maxSize': 1920}
        self.room.request_profile(applied, 'r2')
        self.room.acknowledge_profile({'requestId': 'r2', 'applied': True, 'settings': applied})
        answer = receiver.declared_answer(self.sdp())
        self.assertIn('profile-level-id=420028', answer.lower())  # 1920@30 level
