import asyncio
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
        room.settings = {**room.settings, 'fps': 120, 'bitrate': 24, 'maxSize': 2560}
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
