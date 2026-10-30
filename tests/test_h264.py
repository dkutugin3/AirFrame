"""H.264 level arithmetic and honest profile-level-id advertising for the SDP answer.

Behavioural contract:
- the declared receive level must cover the configured capture geometry and rate;
- only H.264 fmtp lines are rewritten, profile/constraint bytes are preserved;
- the rewrite is idempotent and touches nothing else in the SDP.
"""
import re
import unittest

from aiortc.sdp import parse_h264_profile_level_id

from airframe.playout import Playout  # noqa: F401  (import order smoke: package imports work)
from airframe.h264 import (
    advertise_answer_level,
    level_name,
    required_level_idc,
    rewrite_profile_level_id,
)

# Minimal but realistic answer SDP: VP8 + two H.264 payloads, as aiortc emits it.
ANSWER = """v=0\r
o=- 1 1 IN IP4 0.0.0.0\r
s=-\r
t=0 0\r
m=video 9 UDP/TLS/RTP/SAVPF 96 99 101\r
a=rtpmap:96 VP8/90000\r
a=rtcp-fb:96 nack\r
a=rtpmap:99 H264/90000\r
a=fmtp:99 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f\r
a=rtcp-fb:99 nack\r
a=rtcp-fb:99 nack pli\r
a=rtpmap:101 H264/90000\r
a=fmtp:101 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r
"""


def profile_ids(sdp):
    return re.findall(r"profile-level-id=([0-9a-fA-F]{6})", sdp)


class RequiredLevelTests(unittest.TestCase):
    def test_level_table_cases(self):
        cases = [
            # (width, height, fps, expected level_idc, expected name)
            (1280, 720, 30, 0x1f, "3.1"),
            (1280, 720, 60, 0x20, "3.2"),
            (1920, 1080, 30, 0x28, "4"),
            (1920, 1080, 60, 0x2a, "4.2"),
            (2560, 1440, 30, 0x32, "5"),
            (2560, 1440, 60, 0x33, "5.1"),
            (2560, 1440, 120, 0x34, "5.2"),
            (2560, 1600, 60, 0x33, "5.1"),
            (640, 360, 60, 0x1f, "3.1"),
            (2560, 2560, 60, 0x34, "5.2"),
        ]
        for width, height, fps, level_idc, name in cases:
            level = required_level_idc(width, height, fps)
            self.assertEqual(level, level_idc, f"{width}x{height}@{fps}")
            self.assertEqual(level_name(level), name, f"{width}x{height}@{fps}")

    def test_macroblocks_round_up_to_16_and_limits_use_both_constraints(self):
        # 1920x1080 is 120x68 = 8160 macroblocks: exactly level 4.0 MaxFS.
        # 1920x1082 is 120x68 -> still 8160 (last row partial), so same level;
        # 1936x1080 adds one Macroblock column and needs level 4.2 (MaxFS 8704).
        self.assertEqual(required_level_idc(1920, 1082, 30), 0x28)
        self.assertEqual(required_level_idc(1936, 1080, 30), 0x2a)

    def test_invalid_geometry_and_rate_are_rejected(self):
        for width, height, fps in ((0, 720, 60), (1280, 0, 60), (-16, 720, 60),
                                   (1280, 720, 0), (1280, 720, -1)):
            with self.assertRaises(ValueError):
                required_level_idc(width, height, fps)
        # 16384x16384@240 exceeds level 6.2 and is an explicit failure, not a clamp.
        with self.assertRaises(ValueError):
            required_level_idc(16384, 16384, 240)


class AdvertisedLevelTests(unittest.TestCase):
    def test_base_profile_60_requests_level_5_1(self):
        self.assertEqual(advertise_answer_level(2560, 60), 0x33)

    def test_weaker_profiles_declare_lower_levels(self):
        self.assertEqual(advertise_answer_level(1920, 60), 0x2a)
        self.assertEqual(advertise_answer_level(1920, 30), 0x28)
        self.assertEqual(advertise_answer_level(1280, 30), 0x1f)

    def test_experimental_120_declares_level_5_2(self):
        self.assertEqual(advertise_answer_level(2560, 120), 0x34)


class RewriteTests(unittest.TestCase):
    def test_preserves_profile_and_constraint_bytes(self):
        rewritten = rewrite_profile_level_id(ANSWER, 0x33)
        self.assertEqual(profile_ids(rewritten), ["420033", "42e033"])
        # aiortc parses profile and level back; the profile is still Baseline /
        # Constrained Baseline, only the level byte changed.
        from aiortc.sdp import H264Level, H264Profile
        baseline, level = parse_h264_profile_level_id("420033")
        self.assertEqual(baseline, H264Profile.PROFILE_BASELINE)
        self.assertEqual(level, H264Level.LEVEL5_1)
        constrained, level = parse_h264_profile_level_id("42e033")
        self.assertEqual(constrained, H264Profile.PROFILE_CONSTRAINED_BASELINE)
        self.assertEqual(level, H264Level.LEVEL5_1)

    def test_only_h264_fmtp_lines_are_rewritten(self):
        sdp = (
            ANSWER
            + "a=fmtp:96 profile-level-id=42001f\r\n"       # not H.264 -> untouched
            + "a=rtpmap:98 H265/90000\r\n"
            + "a=fmtp:98 profile-level-id=42001f\r\n"       # HEVC -> untouched
            + "a=fmtp:99 x-google-min-bitrate=1000\r\n"     # extra H.264 param kept
        )
        rewritten = rewrite_profile_level_id(sdp, 0x34)
        self.assertIn("a=fmtp:96 profile-level-id=42001f", rewritten)
        self.assertIn("a=fmtp:98 profile-level-id=42001f", rewritten)
        self.assertIn("a=fmtp:99 x-google-min-bitrate=1000", rewritten)
        self.assertIn("a=rtpmap:99 H264/90000", rewritten)
        self.assertIn("packetization-mode=1", rewritten)
        self.assertEqual(profile_ids(rewritten),
                         ["420034", "42e034", "42001f", "42001f"])

    def test_rewrite_is_idempotent_and_leaves_other_text_alone(self):
        once = rewrite_profile_level_id(ANSWER, 0x33)
        twice = rewrite_profile_level_id(once, 0x33)
        self.assertEqual(once, twice)
        self.assertEqual(once.replace("42e033", "42e01f").replace("420033", "42001f"),
                         ANSWER)

    def test_sdp_without_h264_is_returned_unchanged(self):
        sdp = "v=0\r\na=rtpmap:96 VP8/90000\r\na=fmtp:96 max-fr=60\r\n"
        self.assertEqual(rewrite_profile_level_id(sdp, 0x33), sdp)

    def test_invalid_level_idc_is_rejected(self):
        for level in (0, 5, 0x3f, 1000):
            with self.assertRaises(ValueError):
                rewrite_profile_level_id(ANSWER, level)


class RealAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiortc_answer_for_2560_can_be_corrected(self):
        from fractions import Fraction

        from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, VideoStreamTrack
        from av import VideoFrame

        class Track(VideoStreamTrack):
            async def recv(self):
                frame = VideoFrame(2560, 1440, "yuv420p")
                frame.pts = 0
                frame.time_base = Fraction(1, 90000)
                return frame

        offerer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        answerer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            offerer.addTrack(Track())
            offerer.getTransceivers()[0].setCodecPreferences(
                [c for c in RTCRtpSender.getCapabilities("video").codecs
                 if c.mimeType == "video/H264"])
            await offerer.setLocalDescription(await offerer.createOffer())
            await answerer.setRemoteDescription(offerer.localDescription)
            await answerer.setLocalDescription(await answerer.createAnswer())
            original = answerer.localDescription.sdp
            # The pinned aiortc declares level 3.1 for every target: that is the
            # defect this module compensates for.
            self.assertEqual(profile_ids(original), ["42001f", "42e01f"])
            corrected = rewrite_profile_level_id(original, advertise_answer_level(2560, 60))
            self.assertEqual(profile_ids(corrected), ["420033", "42e033"])
            # The rest of the answer (pt mapping, feedback, packetization) is intact.
            for line in original.splitlines():
                if "profile-level-id" not in line:
                    self.assertIn(line, corrected)
        finally:
            await offerer.close()
            await answerer.close()
