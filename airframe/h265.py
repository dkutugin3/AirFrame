"""RFC 7798 H.265 receive support for the pinned aiortc 1.15.0.

aiortc 1.15.0 ships no H.265 RTP stack: no ``video/H265`` capability, no
depayload dispatch and no decoder mapping (the exact gaps are recorded in
``artifacts/2026-10-30-stable60fps/core/stage9a-hevc-analysis.txt``). Instead
of forking the library, the missing pieces are registered at runtime against
the pinned version. The pinned structure is verified before patching and any
deviation fails loudly - the same policy ``airframe/transport.py`` applies to
the RTP video buffers. Negotiation stays stock: a non-H264 codec matches on
mimeType + clockRate only, so one capability entry with no fmtp accepts any
H.265 offer, and H.264 remains untouched as the fallback.

An H265Encoder (libx265) is included so aiortc<->aiortc tests can exercise the
full RTP path; the phone uses its own hardware encoder.

Frame assembly relies on aiortc's jitter buffer exactly like H.264: the
depacketizer emits Annex-B chunks per RTP packet and packets sharing a
timestamp are concatenated into one JitterFrame.
"""
from collections import deque
from typing import List, Optional, Tuple

import av
from av.frame import Frame
from av.packet import Packet
from av.video.frame import VideoFrame

import aiortc.codecs
from aiortc.codecs.base import Decoder, Encoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
from aiortc.mediastreams import VIDEO_TIME_BASE, convert_timebase

def _x265_available() -> bool:
    try:
        av.CodecContext.create("hevc", "w")
        return True
    except Exception:
        return False

# RFC 7798 aggregation / fragmentation payload types
NAL_TYPE_AP = 48
NAL_TYPE_FU = 49
NAL_TYPE_PACI = 50

START_CODE = b"\x00\x00\x00\x01"
PAYLOAD_HEADER_SIZE = 2
FU_HEADER_SIZE = 1
LENGTH_FIELD_SIZE = 2
PACKET_MAX = 1200

PINNED_AIORTC = "1.15.0"
H265_PAYLOAD_TYPE = 103
H265_RTX_PAYLOAD_TYPE = 104


def _nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


class H265PayloadDescriptor:
    def __init__(self, first_fragment: bool) -> None:
        self.first_fragment = first_fragment

    def __repr__(self) -> str:
        return f"H265PayloadDescriptor(FF={self.first_fragment})"

    @classmethod
    def parse(cls, data: bytes) -> Tuple["H265PayloadDescriptor", bytes]:
        output = b""

        if len(data) < PAYLOAD_HEADER_SIZE:
            raise ValueError("H.265 RTP payload is too short")

        nal_type = _nal_type(data)
        pos = PAYLOAD_HEADER_SIZE

        if nal_type in range(0, NAL_TYPE_AP):
            # Single NAL unit packet: the complete NAL follows the payload header.
            output = START_CODE + data
            obj = cls(first_fragment=True)
        elif nal_type == NAL_TYPE_AP:
            # Aggregation packet: 2-byte NAL sizes (no DONL: we never signal
            # sprop-max-don-diff, so senders must not include it).
            output = b""
            while pos < len(data):
                if len(data) < pos + LENGTH_FIELD_SIZE:
                    raise ValueError("H.265 aggregation length field is truncated")
                size = int.from_bytes(data[pos:pos + LENGTH_FIELD_SIZE], "big")
                pos += LENGTH_FIELD_SIZE
                if len(data) < pos + size:
                    raise ValueError("H.265 aggregation NAL is truncated")
                output += START_CODE + data[pos:pos + size]
                pos += size
            obj = cls(first_fragment=True)
        elif nal_type == NAL_TYPE_FU:
            # Fragmentation unit: FU header carries start/end and the original
            # NAL type; fragments sharing a timestamp are concatenated by the
            # jitter buffer.
            if len(data) < pos + FU_HEADER_SIZE:
                raise ValueError("H.265 FU header is truncated")
            fu_header = data[pos]
            first_fragment = bool(fu_header & 0x80)
            original_type = fu_header & 0x3F
            pos += FU_HEADER_SIZE
            if first_fragment:
                header = bytes([(data[0] & 0x81) | (original_type << 1), data[1]])
                output += START_CODE + header
            output += data[pos:]
            obj = cls(first_fragment=first_fragment)
        else:
            raise ValueError(f"unsupported H.265 RTP payload type {nal_type}")

        return obj, output


def h265_depayload(payload: bytes) -> bytes:
    descriptor, data = H265PayloadDescriptor.parse(payload)
    return data


class H265Decoder(Decoder):
    def __init__(self) -> None:
        self.codec = av.CodecContext.create("hevc", "r")

    def decode(self, encoded_frame: JitterFrame) -> List[Frame]:
        try:
            packet = Packet(encoded_frame.data)
            packet.pts = encoded_frame.timestamp
            packet.time_base = VIDEO_TIME_BASE
            return self.codec.decode(packet)  # type: ignore
        except av.FFmpegError as e:
            import logging
            logging.getLogger(__name__).warning(
                "H265Decoder() failed to decode, skipping package: " + str(e))
            return []


class H265Encoder(Encoder):
    """libx265 sender used by the aiortc<->aiortc tests; the phone encodes in hardware."""

    def __init__(self) -> None:
        self.codec: Optional[av.CodecContext] = None
        self._pending: deque = deque()
        self.__target_bitrate = 1_000_000

    @staticmethod
    def _split_bitstream(data: bytes):
        nals = []
        pos = 0
        while True:
            idx = data.find(b"\x00\x00\x01", pos)
            if idx < 0:
                break
            begin = idx + 3
            end = data.find(b"\x00\x00\x01", begin)
            nals.append(data[begin:end if end >= 0 else len(data)])
            pos = begin
        return [nal for nal in nals if nal]

    @staticmethod
    def _packetize_fu(nal: bytes) -> List[bytes]:
        available = PACKET_MAX - PAYLOAD_HEADER_SIZE - FU_HEADER_SIZE
        # RFC 7798: replace the original two-byte NAL header with a type-49
        # payload header and FU header; only the NAL body is fragmented.
        body = nal[PAYLOAD_HEADER_SIZE:]
        chunks = [body[i:i + available] for i in range(0, len(body), available)]
        header = bytes([(nal[0] & 0x81) | (NAL_TYPE_FU << 1), nal[1]])
        packages = []
        for i, chunk in enumerate(chunks):
            first, last = i == 0, i == len(chunks) - 1
            fu_header = bytes([(0x80 if first else 0) | (0x40 if last else 0) | _nal_type(nal)])
            packages.append(header + fu_header + chunk)
        return packages

    def _packetize(self, nals: List[bytes]) -> List[bytes]:
        packages = []
        for nal in nals:
            if len(nal) + PAYLOAD_HEADER_SIZE <= PACKET_MAX:
                packages.append(nal)
            else:
                packages.extend(self._packetize_fu(nal))
        return packages

    def _ensure_codec(self, frame: VideoFrame) -> None:
        if self.codec is None:
            self.codec = av.CodecContext.create("hevc", "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.pix_fmt = "yuv420p"
            self.codec.bit_rate = self.__target_bitrate
            # Zero lookahead on the encode side; the FFmpeg wrapper still
            # releases a frame late, so finished frames are queued and emitted
            # in order with their own packet timestamps.
            self.codec.options = {
                "x265-params": "bframes=0:rc-lookahead=0:repeat-headers=1:"
                               "keyint=30:log-level=error"}
            self.codec.open()

    def pack(self, packet: Packet) -> Tuple[List[bytes], int]:
        timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
        return self._packetize(self._split_bitstream(bytes(packet))), timestamp

    def encode(self, frame: Frame, force_keyframe: bool = False) -> Tuple[List[bytes], int]:
        assert isinstance(frame, av.VideoFrame)
        self._ensure_codec(frame)
        for packet in self.codec.encode(frame):
            timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
            self._pending.append(
                (self._packetize(self._split_bitstream(bytes(packet))), int(timestamp)))
        if self._pending:
            return self._pending.popleft()
        return [], 0

    @property
    def target_bitrate(self) -> int:
        return self.__target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate: int) -> None:
        self.__target_bitrate = bitrate
        if self.codec is not None:
            self.codec.bit_rate = bitrate


def _h265_entries() -> List[RTCRtpCodecParameters]:
    feedback = [
        RTCRtcpFeedback(type="nack"),
        RTCRtcpFeedback(type="nack", parameter="pli"),
        RTCRtcpFeedback(type="goog-remb"),
    ]
    return [
        RTCRtpCodecParameters(
            mimeType="video/H265",
            clockRate=90000,
            payloadType=H265_PAYLOAD_TYPE,
            rtcpFeedback=feedback,
        ),
        RTCRtpCodecParameters(
            mimeType="video/rtx",
            clockRate=90000,
            payloadType=H265_RTX_PAYLOAD_TYPE,
            parameters={"apt": H265_PAYLOAD_TYPE},
        ),
    ]


def _verify_pinned_structure(codecs_module) -> None:
    from importlib.metadata import version
    if version("aiortc") != PINNED_AIORTC:
        raise RuntimeError(
            f"airframe.h265 is written for aiortc {PINNED_AIORTC}, found {version('aiortc')}")
    codecs = getattr(codecs_module, "CODECS", None)
    if not isinstance(codecs, dict) or not isinstance(codecs.get("video"), list):
        raise RuntimeError("incompatible aiortc codec registry; update airframe.h265")
    for name in ("depayload", "get_decoder"):
        if not callable(getattr(codecs_module, name, None)):
            raise RuntimeError(f"incompatible aiortc codecs.{name}; update airframe.h265")


_installed = None


def install_h265(prefer_first: bool = False, codecs_module=None, receiver_module=None,
                 decode_module=None) -> bool:
    """Register H.265 into aiortc 1.15.0. Idempotent; returns True when patched."""
    global _installed
    import sys
    codecs_module = codecs_module if codecs_module is not None else aiortc.codecs
    receiver_module = (receiver_module if receiver_module is not None
                       else sys.modules.get("aiortc.rtcrtpreceiver"))
    decode_module = decode_module if decode_module is not None else sys.modules.get("airframe.decode")
    _verify_pinned_structure(codecs_module)

    if any(c.mimeType == "video/H265" for c in codecs_module.CODECS["video"]):
        return False

    entries = _h265_entries()
    used = {c.payloadType for c in codecs_module.CODECS["video"]}
    if used & {H265_PAYLOAD_TYPE, H265_RTX_PAYLOAD_TYPE}:
        raise RuntimeError("H.265 payload types collide; update airframe.h265")
    if prefer_first:
        codecs_module.CODECS["video"][0:0] = entries
    else:
        codecs_module.CODECS["video"].extend(entries)

    original_depayload = codecs_module.depayload
    original_get_decoder = codecs_module.get_decoder

    def depayload(codec, payload):
        if codec.name == "H265":
            return h265_depayload(payload)
        return original_depayload(codec, payload)

    def get_decoder(codec):
        if codec.mimeType.lower() == "video/h265":
            return H265Decoder()
        return original_get_decoder(codec)

    def get_encoder(codec):
        if codec.mimeType.lower() == "video/h265":
            if not _x265_available():
                raise ValueError("No encoder found for MIME type `video/h265` (libx265 absent)")
            return H265Encoder()
        return original_get_encoder(codec)

    codecs_module.depayload = depayload
    codecs_module.get_decoder = get_decoder

    original_get_encoder = codecs_module.get_encoder
    codecs_module.get_encoder = get_encoder

    # Modules that imported the names directly keep their own bindings.
    rebound = []
    for module in filter(None, (receiver_module, decode_module)):
        if getattr(module, "depayload", None) is original_depayload:
            module.depayload = depayload
            rebound.append((module, "depayload"))
        if getattr(module, "get_decoder", None) is original_get_decoder:
            module.get_decoder = get_decoder
            rebound.append((module, "get_decoder"))
    sender_module = sys.modules.get("aiortc.rtcrtpsender")
    if sender_module is not None and getattr(sender_module, "get_encoder", None) is original_get_encoder:
        sender_module.get_encoder = get_encoder
        rebound.append((sender_module, "get_encoder"))

    _installed = (codecs_module, original_depayload, original_get_decoder,
                  original_get_encoder, entries, rebound)
    return True


def uninstall_h265() -> None:
    """Restore the pinned aiortc state (used by tests)."""
    global _installed
    if _installed is None:
        return
    (codecs_module, original_depayload, original_get_decoder,
     original_get_encoder, entries, rebound) = _installed
    codecs_module.depayload = original_depayload
    codecs_module.get_decoder = original_get_decoder
    codecs_module.get_encoder = original_get_encoder
    for module, name in rebound:
        original = {"depayload": original_depayload, "get_decoder": original_get_decoder,
                    "get_encoder": original_get_encoder}[name]
        setattr(module, name, original)
    for codec in entries:
        if codec in codecs_module.CODECS["video"]:
            codecs_module.CODECS["video"].remove(codec)
    _installed = None
