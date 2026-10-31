"""Stage 9b: RFC 7798 H.265 receive support for the pinned aiortc 1.15.0.

Deterministic RED-first contracts:
- the depacketizer handles single NAL units, aggregation packets (AP) and
  fragmentation units (FU) spanning several RTP packets, and rejects garbage;
- the runtime registration adds an H.265 capability + decoder to aiortc,
  dispatches depayload()/get_decoder(), is idempotent and fails loudly when
  the pinned aiortc internals deviate (same policy as transport.py);
- a real libx265 bitstream (embedded, generated with ffmpeg) decodes through
  H265Decoder after depacketization;
- a full aiortc<->aiortc WebRTC session negotiates H.265 and delivers decoded
  frames end-to-end (the phone side still needs a real-device check).
"""
import asyncio
import base64
from fractions import Fraction
import unittest

import aiortc.codecs
import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.codecs.base import Decoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import VideoStreamTrack
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from airframe import h265

VECTOR = base64.b64decode(
    "AAAAAUABDAH//wFgAAADAJAAAAMAAAMAHpKAkAAAAAFCAQEBYAAAAwCQAAADAAADAB6gFCBh8mWSpJMrwFoCAAADAAIAAAMAFBAAAAABRAHBcrRiQAAAAU4BBf//////////9Cyi3gm1F0fbu1Wk/n/C/E54MjY1IChidWlsZCAyMTcpIC0gNC4zKzEtZTliODgxMjpbTWFjIE9TIFhdW2NsYW5nIDIxLjAuMF1bNjQgYml0XSA4Yml0KzEwYml0KzEyYml0IC0gSC4yNjUvSEVWQyBjb2RlYyAtIENvcHlyaWdodCAyMDEzLTIwMTggKGMpIE11bHRpY29yZXdhcmUsIEluYyAtIGh0dHA6Ly94MjY1Lm9yZyAtIG9wdGlvbnM6IGNwdWlkPTk4IGZyYW1lLXRocmVhZHM9MyB3cHAgbm8tcG1vZGUgbm8tcG1lIG5vLXBzbnIgbm8tc3NpbSBsb2ctbGV2ZWw9MCBiaXRkZXB0aD04IGlucHV0LWNzcD0xIGZwcz0xMC8xIGlucHV0LXJlcz0xNjB4OTAgaW50ZXJsYWNlPTAgdG90YWwtZnJhbWVzPTAgbGV2ZWwtaWRjPTAgaGlnaC10aWVyPTEgdWhkLWJkPTAgcmVmPTMgbm8tYWxsb3ctbm9uLWNvbmZvcm1hbmNlIHJlcGVhdC1oZWFkZXJzIGFubmV4YiBuby1hdWQgbm8tZW9iIG5vLWVvcyBuby1ocmQgaW5mbyBoYXNoPTAgdGVtcG9yYWwtbGF5ZXJzPTAgb3Blbi1nb3AgbWluLWtleWludD0xIGtleWludD02IGdvcC1sb29rYWhlYWQ9MCBiZnJhbWVzPTAgYi1hZGFwdD0yIG5vLWItcHlyYW1pZCBiZnJhbWUtYmlhcz0wIHJjLWxvb2thaGVhZD0yMCBsb29rYWhlYWQtc2xpY2VzPTAgc2NlbmVjdXQ9NDAgbm8taGlzdC1zY2VuZWN1dCByYWRsPTAgbm8tc3BsaWNlIG5vLWludHJhLXJlZnJlc2ggY3R1PTY0IG1pbi1jdS1zaXplPTggbm8tcmVjdCBuby1hbXAgbWF4LXR1LXNpemU9MzIgdHUtaW50ZXItZGVwdGg9MSB0dS1pbnRyYS1kZXB0aD0xIGxpbWl0LXR1PTAgcmRvcS1sZXZlbD0wIGR5bmFtaWMtcmQ9MC4wMCBuby1zc2ltLXJkIHNpZ25oaWRlIG5vLXRza2lwIG5yLWludHJhPTAgbnItaW50ZXI9MCBuby1jb25zdHJhaW5lZC1pbnRyYSBzdHJvbmctaW50cmEtc21vb3RoaW5nIG1heC1tZXJnZT0zIGxpbWl0LXJlZnM9MSBuby1saW1pdC1tb2RlcyBtZT0xIHN1Ym1lPTIgbWVyYW5nZT01NyB0ZW1wb3JhbC1tdnAgbm8tZnJhbWUtZHVwIG5vLWhtZSB3ZWlnaHRwIG5vLXdlaWdodGIgbm8tYW5hbHl6ZS1zcmMtcGljcyBkZWJsb2NrPTA6MCBzYW8gbm8tc2FvLW5vbi1kZWJsb2NrIHJkPTMgc2VsZWN0aXZlLXNhbz00IGVhcmx5LXNraXAgcnNraXAgbm8tZmFzdC1pbnRyYSBuby10c2tpcC1mYXN0IG5vLWN1LWxvc3NsZXNzIGItaW50cmEgbm8tc3BsaXRyZC1za2lwIHJkcGVuYWx0eT0wIHBzeS1yZD0yLjAwIHBzeS1yZG9xPTAuMDAgbm8tcmQtcmVmaW5lIG5vLWxvc3NsZXNzIGNicXBvZmZzPTAgY3JxcG9mZnM9MCByYz1jcmYgY3JmPTI4LjAgcWNvbXA9MC42MCBxcHN0ZXA9NCBzdGF0cy13cml0ZT0wIHN0YXRzLXJlYWQ9MCBpcHJhdGlvPTEuNDAgYXEtbW9kZT0yIGFxLXN0cmVuZ3RoPTEuMDAgY3V0cmVlIHpvbmUtY291bnQ9MCBuby1zdHJpY3QtY2JyIHFnLXNpemU9MzIgbm8tcmMtZ3JhaW4gcXBtYXg9NjkgcXBtaW49MCBuby1jb25zdC12YnYgc2FyPTEgb3ZlcnNjYW49MCB2aWRlb2Zvcm1hdD01IHJhbmdlPTAgY29sb3JwcmltPTIgdHJhbnNmZXI9MiBjb2xvcm1hdHJpeD0yIGNocm9tYWxvYz0wIGRpc3BsYXktd2luZG93PTAgY2xsPTAsMCBtaW4tbHVtYT0wIG1heC1sdW1hPTI1NSBsb2cyLW1heC1wb2MtbHNiPTggdnVpLXRpbWluZy1pbmZvIHZ1aS1ocmQtaW5mbyBzbGljZXM9MSBuby1vcHQtcXAtcHBzIG5vLW9wdC1yZWYtbGlzdC1sZW5ndGgtcHBzIG5vLW11bHRpLXBhc3Mtb3B0LXJwcyBzY2VuZWN1dC1iaWFzPTAuMDUgbm8tb3B0LWN1LWRlbHRhLXFwIG5vLWFxLW1vdGlvbiBuby1oZHIxMCBuby1oZHIxMC1vcHQgbm8tZGhkcjEwLW9wdCBuby1pZHItcmVjb3Zlcnktc2VpIGFuYWx5c2lzLXJldXNlLWxldmVsPTAgYW5hbHlzaXMtc2F2ZS1yZXVzZS1sZXZlbD0wIGFuYWx5c2lzLWxvYWQtcmV1c2UtbGV2ZWw9MCBzY2FsZS1mYWN0b3I9MCByZWZpbmUtaW50cmE9MCByZWZpbmUtaW50ZXI9MCByZWZpbmUtbXY9MSByZWZpbmUtY3R1LWRpc3RvcnRpb249MCBuby1saW1pdC1zYW8gY3R1LWluZm89MCBuby1sb3dwYXNzLWRjdCByZWZpbmUtYW5hbHlzaXMtdHlwZT0wIGNvcHktcGljPTEgbWF4LWF1c2l6ZS1mYWN0b3I9MS4wIG5vLWR5bmFtaWMtcmVmaW5lIG5vLXNpbmdsZS1zZWkgbm8taGV2Yy1hcSBuby1zdnQgbm8tZmllbGQgcXAtYWRhcHRhdGlvbi1yYW5nZT0xLjAwIHNjZW5lY3V0LWF3YXJlLXFwPTBjb25mb3JtYW5jZS13aW5kb3ctb2Zmc2V0cyByaWdodD0wIGJvdHRvbT0wIGRlY29kZXItbWF4LXJhdGU9MCBuby12YnYtbGl2ZS1tdWx0aS1wYXNzIG5vLW1jc3RmIG5vLXNicmMgbm8tZnJhbWUtcmOAAAABKAGvEULzVF93Byoh0GnUkC7L/ZrMC69Ot1jnAwKISbg0B5cc9vt7aV/MaNYyj2ks+NxL8Eupj6YnhesiVtcboxCNYm/uh4EpoX4+y3zBn9+pOy4CxjAQ0uE1Ux4ZidnJ/8rfpT07/MZ/Ut3CZrrOTYbiCo7wmFdeEDzLyv57SVkWEU5vE1hfOYs6vC9DFy7zFKvcFYIi+vv9QNy38DzJ+ud+jUn1CuP9dY7s1e4AR5ZEgc7tKwmHW4cqkTfN0CUfi6/tOkJF9gfkSrM/WIUtkIf0h4fqgkO+mn7hNTbcKGIq1W2zzKADg8QQ9vpBK66ADve6+FuHaIrUi+5EiFSTXw+pzZTj37qRgAIZRdK7JXzPQYsH3mT1Ho4uq+bqAf1vPCbsTpsopajWpArgzvwcuiXDrovRdFu/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"
)

H265_CODEC = RTCRtpCodecParameters(mimeType="video/H265", clockRate=90000, payloadType=45)


def split_nals(stream: bytes):
    """Split an Annex-B .h265 bitstream into NAL units (headers included)."""
    codes = []
    i = 0
    while True:
        idx = stream.find(b"\x00\x00\x01", i)
        if idx < 0:
            break
        codes.append(idx)
        i = idx + 3
    nals = []
    for j, idx in enumerate(codes):
        start = idx + 3  # the NAL always follows the 00 00 01 match
        if j + 1 < len(codes):
            end = codes[j + 1] - (1 if stream[codes[j + 1] - 1] == 0 else 0)
        else:
            end = len(stream)
        nals.append(stream[start:end])
    return [nal for nal in nals if nal]


def nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


def payload_header(nal: bytes) -> bytes:
    return bytes([nal[0] & 0x81, nal[1]])


class H265DepacketizerTests(unittest.TestCase):
    def test_single_nal_passes_with_start_code(self):
        nal = VECTOR_NALS()[0]
        out = h265.h265_depayload(nal)
        self.assertEqual(out, b"\x00\x00\x00\x01" + nal)

    def test_aggregation_packet_yields_every_nal(self):
        nals = [nal for nal in VECTOR_NALS() if 40 < nal_type(nal) < 48][:2] or VECTOR_NALS()[:2]
        payload = bytes([48 << 1, 0])  # AP payload header
        for nal in nals:
            payload += len(nal).to_bytes(2, "big") + nal
        out = h265.h265_depayload(payload)
        expected = b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)
        self.assertEqual(out, expected)

    def test_fragmentation_unit_reassembles_across_packets(self):
        nal = max(VECTOR_NALS(), key=len)
        # an FU packet carries a NAL header with type 49 plus the FU header
        header = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        body = nal[2:]  # FU carries the NAL body; the header is reconstructed
        chunks = [body[i:i + 7] for i in range(0, len(body), 7)]
        fu_type = nal_type(nal)
        outs = []
        for i, chunk in enumerate(chunks):
            first, last = i == 0, i == len(chunks) - 1
            fu_header = bytes([(0x80 if first else 0) | (0x40 if last else 0) | fu_type])
            outs.append(h265.h265_depayload(header + fu_header + chunk))
        joined = b"".join(outs)
        self.assertEqual(joined, b"\x00\x00\x00\x01" + nal)

    def test_paci_and_garbage_are_rejected(self):
        with self.assertRaises(ValueError):
            h265.h265_depayload(bytes([50 << 1, 0, 0, 0]))  # PACI is not supported
        with self.assertRaises(ValueError):
            h265.h265_depayload(b"\x00")  # too short

    def test_truncated_aggregation_is_rejected(self):
        with self.assertRaises(ValueError):
            h265.h265_depayload(bytes([48 << 1, 0, 0, 5, 1, 2]))  # length overruns payload


class H265RegistrationTests(unittest.TestCase):
    def tearDown(self):
        h265.uninstall_h265()

    def test_install_adds_capability_decoder_and_dispatch(self):
        h265.install_h265()
        names = [c.mimeType for c in aiortc.codecs.CODECS["video"]]
        self.assertIn("video/H265", names)
        decoder = aiortc.codecs.get_decoder(H265_CODEC)
        self.assertIsInstance(decoder, h265.H265Decoder)
        self.assertIsInstance(decoder, Decoder)
        nal = VECTOR_NALS()[0]
        self.assertEqual(aiortc.codecs.depayload(H265_CODEC, nal),
                         b"\x00\x00\x00\x01" + nal)
        # the receiver module holds its own imports: patch must reach them
        import aiortc.rtcrtpreceiver as receiver_module
        self.assertIs(receiver_module.get_decoder, aiortc.codecs.get_decoder)

    def test_install_is_idempotent(self):
        h265.install_h265()
        h265.install_h265()
        names = [c.mimeType for c in aiortc.codecs.CODECS["video"]]
        self.assertEqual(names.count("video/H265"), 1)

    def test_uninstall_restores_pinned_state(self):
        h265.install_h265()
        h265.uninstall_h265()
        names = [c.mimeType for c in aiortc.codecs.CODECS["video"]]
        self.assertNotIn("video/H265", names)

    def test_deviant_internals_fail_loudly(self):
        class FakeModule:
            CODECS = {"video": []}
        with self.assertRaises(RuntimeError):
            h265.install_h265(codecs_module=FakeModule)


class H265DecoderTests(unittest.TestCase):
    def test_real_x265_bitstream_decodes_after_depacketization(self):
        decoder = h265.H265Decoder()
        frames = []
        for nal in VECTOR_NALS():
            data = h265.h265_depayload(nal)
            frames.extend(decoder.decode(JitterFrame(data=data, timestamp=0)))
        self.assertTrue(frames)
        self.assertEqual((frames[0].width, frames[0].height), (160, 90))


class H265EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_webrtc_session_negotiates_h265_and_decodes_frames(self):
        received = asyncio.Queue()
        h265.install_h265(prefer_first=True)
        try:
            sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))

            @receiver.on("track")
            def on_track(track):
                if track.kind == "video":
                    received.put_nowait(track)

            @sender.on("icecandidate")
            async def on_ice_sender(candidate):
                if candidate:
                    await receiver.addIceCandidate(candidate)

            @receiver.on("icecandidate")
            async def on_ice_receiver(candidate):
                if candidate:
                    await sender.addIceCandidate(candidate)

            source = VectorTrack()
            sender.addTrack(source)

            offer = await sender.createOffer()
            self.assertIn("H265", offer.sdp)
            await sender.setLocalDescription(offer)
            await receiver.setRemoteDescription(
                RTCSessionDescription(sdp=sender.localDescription.sdp, type="offer"))
            answer = await receiver.createAnswer()
            self.assertIn("H265", answer.sdp)
            await receiver.setLocalDescription(answer)
            await sender.setRemoteDescription(
                RTCSessionDescription(sdp=receiver.localDescription.sdp, type="answer"))

            track = await asyncio.wait_for(received.get(), 10)
            for _ in range(3):
                frame = await asyncio.wait_for(track.recv(), 10)
                self.assertEqual((frame.width, frame.height), (160, 90))
            await sender.close()
            await receiver.close()
        finally:
            h265.uninstall_h265()


class VectorTrack(VideoStreamTrack):
    """Feeds the embedded x265 vector frames as raw video through libx265."""

    TIME_BASE = Fraction(1, 90000)

    def __init__(self):
        super().__init__()
        self.frames = decode_vector_frames()
        self.index = 0
        self.pts = 0

    async def next_timestamp(self):
        if self.pts:
            await asyncio.sleep(0.02)
        self.pts += 90000 // 10
        return self.pts, self.TIME_BASE

    async def recv(self):
        frame = self.frames[self.index % len(self.frames)]
        self.index += 1
        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        return frame


def decode_vector_frames():
    codec = av.CodecContext.create("hevc", "r")
    frames = []
    # the decoder wants an Annex-B stream: start codes are mandatory
    for nal in VECTOR_NALS():
        frames.extend(codec.decode(av.Packet(b"\x00\x00\x00\x01" + nal)))
    return frames


def candidate_to_sdp(candidate):
    return candidate


_NALS = None


def VECTOR_NALS():
    global _NALS
    if _NALS is None:
        _NALS = split_nals(VECTOR)
    return _NALS


if __name__ == "__main__":
    unittest.main()
