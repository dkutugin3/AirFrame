"""Stage 9a: H.264 High receive-capability negotiation (desktop side).

The contract: aiortc 1.15.0 statically advertises only Baseline/Constrained
Baseline H.264 receive entries, so it cannot answer an offer that advertises
H.264 High even though its PyAV decoder decodes High. AirFrame adds process-local
High/Constrained High receive entries before creating the peer connection. These
tests use real SDP: an aiortc offer is restricted to a single H.264 payload and
its ``profile-level-id`` is rewritten to High/Constrained High by hand.

The Android WebRTC encoder capability and HEVC are explicitly out of scope here
(no phone, no H.265 RTP depacketizer); see the task-17 report section.
"""
import asyncio
import json
import re
import unittest
from fractions import Fraction

import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import VIDEO_TIME_BASE
from aiortc.rtcrtpsender import RTCRtpSender
from aiortc.sdp import SessionDescription

from airframe import codec_capabilities
from airframe.receiver import Receiver

H264_RTPMAP = re.compile(r"a=rtpmap:(\d+) H264/90000", re.IGNORECASE)
H264_FMTP = re.compile(
    r"(a=fmtp:(\d+)[^\r\n]*?profile-level-id=)([0-9a-fA-F]{6})")
PAYLOAD_LINE = re.compile(r"(a=rtpmap:|a=fmtp:|a=rtcp-fb:)(\d+)")
# 2560x1440 @ 60 FPS needs H.264 level 5.1 (0x33); 1280x720 @ 30 FPS needs 3.1 (0x1f).
LEVEL_2560_60 = "33"
LEVEL_1280_30 = "1f"


class FakeRoom:
    """Minimal Room stand-in: the receiver only reads settings and sends signaling."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {"fps": 60, "bitrate": 24, "maxSize": 2560})
        self.sent = []

    async def send(self, role, message):
        self.sent.append(message)


def h264_payload_types(sdp):
    return [int(match.group(1)) for match in H264_RTPMAP.finditer(sdp)]


def single_high_payload_offer(sdp, profile_level_id):
    """Restrict a real aiortc offer to one H.264 payload declaring ``profile_level_id``.

    This is a hand-modified but structurally real SDP offer: exactly one H.264
    payload type remains in the m-line, its fmtp advertises the requested profile,
    and all other H.264 payloads/rtx entries are removed.
    """
    payloads = h264_payload_types(sdp)
    if not payloads:
        raise AssertionError("offer has no H.264 payload")
    target = payloads[0]
    drop = {payload for payload in payloads if payload != target}

    def not_dropped(line):
        match = PAYLOAD_LINE.match(line)
        return not (match and int(match.group(2)) in drop)

    lines = [line for line in sdp.split("\r\n") if not_dropped(line)]
    offer = "\r\n".join(lines)

    def rewrite(match):
        if int(match.group(2)) != target:
            return match.group(0)
        return match.group(1) + profile_level_id

    offer = H264_FMTP.sub(rewrite, offer)

    def prune_mline(match):
        fields = match.group(1).split()
        formats = [value for value in fields[3:] if int(value) == target]
        return " ".join([fields[0], fields[1], fields[2]] + formats)

    return re.sub(r"(m=video [^\r\n]*)", prune_mline, offer), target


def answer_h264_codecs(answer_sdp):
    parsed = SessionDescription.parse(answer_sdp)
    codecs = []
    for media in parsed.media:
        if media.kind != "video":
            continue
        codecs.extend(codec for codec in media.rtp.codecs
                      if codec.mimeType.lower() == "video/h264")
    return codecs


BASELINE_PROFILES = ("42001f", "42e01f")


def baseline_capabilities():
    """The two Baseline/Constrained-Baseline H.264 capabilities, independent of
    whether the process-local High extension is already installed."""
    return [codec for codec in RTCRtpSender.getCapabilities("video").codecs
            if codec.mimeType == "video/H264"
            and codec.parameters.get("profile-level-id") in BASELINE_PROFILES]


async def h264_only_sender():
    """A real aiortc peer whose offer contains only H.264 Baseline payloads."""
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    sender.addTrack(VideoStreamTrack())
    capabilities = baseline_capabilities()
    sender.getTransceivers()[0].setCodecPreferences(capabilities)
    await sender.setLocalDescription(await sender.createOffer())
    return sender


async def answer_offer(offer_sdp, settings=None):
    """Run Receiver.offer against an offer; returns (answer sdp or None, logs)."""
    room = FakeRoom(settings)
    logs = []
    receiver = Receiver(room, lambda frame: None, lambda *args: None,
                        lambda *args: None, logs.append)
    try:
        await asyncio.wait_for(receiver.offer({"session": "codec-test", "sdp": offer_sdp}), 10)
    finally:
        await receiver.close()
    answers = [message for message in room.sent if message.get("type") == "answer"]
    return (answers[0]["sdp"] if answers else None), logs


class HighProfileTrack(VideoStreamTrack):
    """Produces a REAL libx264 High-profile H.264 bitstream as pre-encoded packets."""

    def __init__(self, width=640, height=360):
        super().__init__()
        self.index = 0
        self.sps_profile_idc = None
        self.codec = av.CodecContext.create("libx264", "w")
        self.codec.width, self.codec.height = width, height
        self.codec.pix_fmt = "yuv420p"
        self.codec.time_base = Fraction(1, 90000)
        self.codec.framerate = Fraction(30)
        # Immediate output (no lookahead) while explicitly declaring High profile.
        self.codec.options = {"tune": "zerolatency", "x264-params": "profile=high"}

    def _record_sps(self, data):
        index = 0
        while True:
            index = data.find(b"\x00\x00\x01", index)
            if index < 0 or index + 4 >= len(data):
                return
            if data[index + 3] & 0x1F == 7:      # SPS NAL
                self.sps_profile_idc = data[index + 4]
                return
            index += 3

    async def recv(self):
        await asyncio.sleep(1 / 30)
        frame = av.VideoFrame(self.codec.width, self.codec.height, "yuv420p")
        frame.pts = self.index * 3000
        frame.time_base = VIDEO_TIME_BASE
        luma = 32 + (self.index % 16) * 12
        frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
        for plane in frame.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        packets = self.codec.encode(frame)
        self.index += 1
        if not packets:
            raise AssertionError("libx264 High produced no packet")
        packet = packets[0]
        packet.pts, packet.time_base = (self.index - 1) * 3000, Fraction(1, 90000)
        if self.sps_profile_idc is None:
            self._record_sps(bytes(packet))
        return packet


class CapabilityInstallTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_entries_are_scoped_to_the_negotiation(self):
        before = [(codec.payloadType, codec.mimeType, dict(codec.parameters))
                  for codec in codec_capabilities.CODECS["video"]]
        with codec_capabilities.h264_high_receive_capabilities() as added:
            self.assertEqual(sorted(added), ["Constrained High", "High"])
            codecs = codec_capabilities.CODECS["video"]
            profiles = {codec.parameters.get("profile-level-id")
                        for codec in codecs if codec.mimeType == "video/H264"}
            self.assertIn("64001f", profiles)
            self.assertIn("640c1f", profiles)
            self.assertIn("42001f", profiles)
            self.assertIn("42e01f", profiles)
            payload_types = [codec.payloadType for codec in codecs]
            self.assertEqual(len(payload_types), len(set(payload_types)))
            for codec in codecs:
                if codec.mimeType == "video/rtx":
                    self.assertIsInstance(codec.parameters.get("apt"), int)
        after = [(codec.payloadType, codec.mimeType, dict(codec.parameters))
                 for codec in codec_capabilities.CODECS["video"]]
        self.assertEqual(after, before, "entries must be removed after negotiation")
        with codec_capabilities.h264_high_receive_capabilities() as again:
            self.assertEqual(sorted(again), ["Constrained High", "High"])

    def test_version_pin_is_validated(self):
        from unittest import mock
        with mock.patch.object(codec_capabilities, "PINNED_AIORTC", "0.0.0"):
            with self.assertRaises(codec_capabilities.CapabilityError):
                with codec_capabilities.h264_high_receive_capabilities():
                    pass

    async def test_receiver_keeps_baseline_fallback_when_extension_fails(self):
        from unittest import mock
        import airframe.receiver as receiver_module
        sender = await h264_only_sender()
        room = FakeRoom()
        logs = []
        receiver = Receiver(room, lambda frame: None, lambda *args: None,
                            lambda *args: None, logs.append)
        try:
            with mock.patch.object(receiver_module, "h264_high_receive_capabilities",
                                   side_effect=codec_capabilities.CapabilityError("test")):
                await asyncio.wait_for(receiver.offer(
                    {"session": "fallback", "sdp": sender.localDescription.sdp}), 10)
        finally:
            await receiver.close()
            await sender.close()
        answers = [message for message in room.sent if message.get("type") == "answer"]
        self.assertTrue(answers, logs)
        profiles = {codec.parameters.get("profile-level-id").lower()
                    for codec in answer_h264_codecs(answers[0]["sdp"])}
        self.assertTrue(profiles, answers[0]["sdp"])
        self.assertTrue(all(profile.startswith(("4200", "42e0")) for profile in profiles))
        self.assertTrue(any("Baseline" in message for message in logs), logs)


class CapabilityNegotiationTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_offer_is_answered_with_high_profile(self):
        sender = await h264_only_sender()
        try:
            for profile, expected in (("640c1f", "640c" + LEVEL_2560_60),
                                      ("64001f", "6400" + LEVEL_2560_60)):
                with self.subTest(profile=profile):
                    offer, target = single_high_payload_offer(sender.localDescription.sdp, profile)
                    self.assertIn(f"profile-level-id={profile}", offer)
                    answer, logs = await answer_offer(offer)
                    self.assertIsNotNone(answer, f"H.264 {profile} offer was not answered: {logs}")
                    codecs = answer_h264_codecs(answer)
                    self.assertEqual(len(codecs), 1, answer)
                    selected = codecs[0]
                    self.assertEqual(selected.payloadType, target,
                                     "the answer must select the offered High payload")
                    self.assertEqual(selected.parameters.get("profile-level-id").lower(),
                                     expected,
                                     "profile bytes must be preserved, level rewritten")
                    self.assertEqual(selected.parameters.get("packetization-mode"), "1")
                    self.assertEqual(selected.parameters.get("level-asymmetry-allowed"), "1")
        finally:
            await sender.close()

    async def test_high_answer_is_valid_json_and_parsable_sdp(self):
        sender = await h264_only_sender()
        try:
            offer, target = single_high_payload_offer(sender.localDescription.sdp, "640c1f")
            room = FakeRoom()
            receiver = Receiver(room, lambda frame: None, lambda *args: None,
                                lambda *args: None, lambda message: None)
            try:
                await asyncio.wait_for(receiver.offer({"session": "codec-test", "sdp": offer}), 10)
            finally:
                await receiver.close()
            answers = [message for message in room.sent if message.get("type") == "answer"]
            self.assertTrue(answers)
            message = answers[0]
            encoded = json.dumps(message)                 # no SDP/JSON breakage
            self.assertEqual(json.loads(encoded)["type"], "answer")
            parsed = SessionDescription.parse(message["sdp"])
            self.assertEqual([media.kind for media in parsed.media], ["video"])
            video = parsed.media[0]
            self.assertIsNotNone(video.dtls)
            h264 = [codec for codec in video.rtp.codecs
                    if codec.mimeType.lower() == "video/h264"]
            self.assertEqual([codec.payloadType for codec in h264], [target])
        finally:
            await sender.close()

    async def test_baseline_only_offer_still_negotiates_baseline(self):
        sender = await h264_only_sender()
        try:
            offer = sender.localDescription.sdp
            answer, logs = await answer_offer(offer)
            self.assertIsNotNone(answer, logs)
            codecs = answer_h264_codecs(answer)
            self.assertTrue(codecs, answer)
            profiles = {codec.parameters.get("profile-level-id").lower() for codec in codecs}
            self.assertTrue(profiles <= {"4200" + LEVEL_2560_60, "42e0" + LEVEL_2560_60},
                            f"fallback must stay Baseline/Constrained Baseline: {profiles}")
            self.assertFalse(any(profile.startswith(("640c", "6400")) for profile in profiles))
        finally:
            await sender.close()

    async def test_declared_level_follows_the_configured_profile(self):
        sender = await h264_only_sender()
        try:
            offer, target = single_high_payload_offer(sender.localDescription.sdp, "640c1f")
            answer, logs = await answer_offer(offer, {"fps": 30, "bitrate": 16, "maxSize": 1280})
            self.assertIsNotNone(answer, logs)
            codec = answer_h264_codecs(answer)[0]
            self.assertEqual(codec.parameters.get("profile-level-id").lower(),
                             "640c" + LEVEL_1280_30)
        finally:
            await sender.close()

    async def test_real_libx264_high_stream_decodes_after_negotiation(self):
        # Simulated High-capable sender (like an Android build that advertises High):
        # it builds its offer and matches the answer inside the same scoped
        # capability window the receiver uses.
        track = HighProfileTrack()
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        sender.addTrack(track)
        with codec_capabilities.h264_high_receive_capabilities():
            capabilities = [codec for codec in
                            RTCRtpSender.getCapabilities("video").codecs
                            if codec.mimeType == "video/H264"]
            sender.getTransceivers()[0].setCodecPreferences(capabilities)
            await sender.setLocalDescription(await sender.createOffer())
        offer, target = single_high_payload_offer(sender.localDescription.sdp, "640c1f")
        room = FakeRoom()
        decoded = []
        logs = []
        receiver = Receiver(room, decoded.append, lambda *args: None,
                            lambda *args: None, logs.append)
        try:
            await asyncio.wait_for(receiver.offer({"session": "codec-test", "sdp": offer}), 10)
            answers = [message for message in room.sent if message.get("type") == "answer"]
            self.assertTrue(answers, logs)
            answer = answers[0]["sdp"]
            codec = answer_h264_codecs(answer)[0]
            self.assertEqual(codec.payloadType, target)
            self.assertEqual(codec.parameters.get("profile-level-id").lower(),
                             "640c" + LEVEL_2560_60)
            with codec_capabilities.h264_high_receive_capabilities():
                await sender.setRemoteDescription(
                    RTCSessionDescription(type="answer", sdp=answer))
            limit = asyncio.get_running_loop().time() + 10
            while len([frame for frame in decoded if frame is not None]) < 8:
                if asyncio.get_running_loop().time() > limit:
                    self.fail(f"H.264 High stream was not decoded: {logs}")
                await asyncio.sleep(.05)
            frames = [frame for frame in decoded if frame is not None]
            self.assertEqual(len(frames) >= 8, True)
            self.assertEqual({(frame.width, frame.height) for frame in frames[:8]},
                             {(640, 360)})
            self.assertEqual([frame.pts for frame in frames[:8]],
                             [index * 3000 for index in range(8)])
            self.assertEqual(track.sps_profile_idc, 100,
                             "evidence: the sent bitstream SPS declares H.264 High (profile_idc 100)")
        finally:
            await receiver.close()
            await sender.close()
