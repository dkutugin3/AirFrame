"""Narrow, process-local H.264 High receive capability for aiortc 1.15.0.

aiortc 1.15.0 statically advertises only two H.264 receive entries in
``aiortc.codecs.CODECS['video']``: ``42001f`` (Baseline) and ``42e01f``
(Constrained Baseline). Its SDP matcher compares the H.264 *profile* of each
local entry with the offered one (``find_common_codecs`` / ``is_codec_compatible``
in ``rtcpeerconnection.py``), so an offer that advertises H.264 High or
Constrained High can never match: ``setRemoteDescription`` raises
"Failed to set remote video description send parameters" and no answer is sent.

Its receive path does not have a profile limitation: H.264 payloads are
depacketized by ``aiortc.codecs.h264.h264_depayload`` and decoded by the PyAV
``H264Decoder``, which decodes High/Constrained High bitstreams. The missing
piece is only capability *advertisement*.

This module adds H.264 High (``64001f``) and Constrained High (``640c1f``)
receive entries (plus their RTX companions) to aiortc's in-memory registry
**for the duration of one ``setRemoteDescription`` call**, after validating the
pinned aiortc version and registry structure, in the same style as
``airframe/transport.py`` and ``airframe/decode.py``. Nothing in
``site-packages`` is edited and no decoder/encoder factory is replaced: the
extension only makes negotiation possible, and the actual bitstream is still
decoded by aiortc's normal H.264 decoder. The answered SDP already carries the
selected payload types, so the extra registry entries are no longer needed after
the remote description is applied; removing them keeps sender-side offers in the
same process untouched (all existing negotiation tests keep their two-entry
Baseline/Constrained-Baseline answer).

Scope: AirFrame runs exactly one receiver peer per process and mutates the
registry only inside this short synchronous hand-off on the event loop, so no
other negotiation can observe the extra entries. A library with several
independent peer connections should build its own per-peer capability list
instead; do not copy this pattern into a multi-tenant server.

The level byte is still declared/pinned by ``airframe/h264.py``
(``Receiver.declared_answer``): a High offer answered at 2560 px / 60 FPS
becomes ``640c33`` / ``640033`` -- profile and constraint bytes are preserved and
only the level is rewritten.

The Android encoder's real High capability and HEVC are outside this module; see
the stage-9 report section (task-17).
"""
import contextlib

import aiortc
from aiortc.codecs import CODECS
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters

PINNED_AIORTC = "1.15.0"
#: Receive profiles to advertise: (profile-level-id, human name).
H264_HIGH_PROFILES = (("64001f", "High"), ("640c1f", "Constrained High"))
_BASELINE_PROFILES = ("42001f", "42e01f")
_DYNAMIC_PAYLOAD_TYPES = range(96, 128)
_H264 = "video/h264"
_RTX = "video/rtx"


class CapabilityError(RuntimeError):
    """The pinned aiortc capability registry does not have the expected shape."""


def _video_codecs():
    codecs = CODECS.get("video") if isinstance(CODECS, dict) else None
    if not isinstance(codecs, list):
        raise CapabilityError("aiortc.codecs.CODECS['video'] is not a list")
    return codecs


def _h264_entries(codecs):
    return [codec for codec in codecs if codec.mimeType.lower() == _H264]


def _validate_registry():
    """Fail explicitly if aiortc's private capability shape changed."""
    if aiortc.__version__ != PINNED_AIORTC:
        raise CapabilityError(
            f"AirFrame requires aiortc {PINNED_AIORTC} for its H.264 High receive "
            f"capability (found {aiortc.__version__})")
    codecs = _video_codecs()
    if not codecs or not all(isinstance(codec, RTCRtpCodecParameters) for codec in codecs):
        raise CapabilityError("aiortc video capability list has unexpected entries")
    baselines = {}
    for codec in _h264_entries(codecs):
        profile = str(codec.parameters.get("profile-level-id", "")).lower()
        if profile in _BASELINE_PROFILES:
            baselines[profile] = codec
    missing = [profile for profile in _BASELINE_PROFILES if profile not in baselines]
    if missing:
        raise CapabilityError(f"aiortc baseline H.264 entries are missing: {missing}")
    for profile, codec in baselines.items():
        if (codec.parameters.get("packetization-mode") != "1"
                or codec.parameters.get("level-asymmetry-allowed") != "1"):
            raise CapabilityError(
                f"aiortc H.264 {profile} entry has unexpected parameters: {codec.parameters}")
    for codec in codecs:
        if codec.mimeType.lower() == _RTX and not isinstance(
                codec.parameters.get("apt"), int):
            raise CapabilityError("aiortc RTX entry has no integer apt")
    return baselines


def _allocate_payload_type(codecs):
    used = {codec.payloadType for codec in codecs}
    for payload_type in _DYNAMIC_PAYLOAD_TYPES:
        if payload_type not in used and (payload_type + 1) not in used:
            return payload_type
    raise CapabilityError("no free dynamic RTP payload type pair for H.264 High")


@contextlib.contextmanager
def h264_high_receive_capabilities():
    """Expose H.264 High/Constrained High receive entries for one negotiation.

    Yields the names of the entries that this call added (empty when a High entry
    is already present). Entries are removed again on exit, so a process that only
    receives (AirFrame) gains the capability while sender-side offers built in the
    same process stay byte-identical to stock aiortc. Raises
    :class:`CapabilityError` on an unsupported aiortc version or unexpected
    registry structure; the caller must keep the Baseline fallback in that case.
    """
    baselines = _validate_registry()
    codecs = _video_codecs()
    present = {str(codec.parameters.get("profile-level-id", "")).lower()
               for codec in _h264_entries(codecs)}
    feedback = list(next(iter(baselines.values())).rtcpFeedback)
    appended = []
    added = []
    for profile, name in H264_HIGH_PROFILES:
        if profile in present:
            continue
        payload_type = _allocate_payload_type(codecs)
        high = RTCRtpCodecParameters(
            mimeType="video/H264",
            clockRate=90000,
            payloadType=payload_type,
            rtcpFeedback=[RTCRtcpFeedback(type=item.type, parameter=item.parameter)
                          for item in feedback],
            parameters={"level-asymmetry-allowed": "1",
                        "packetization-mode": "1",
                        "profile-level-id": profile})
        rtx = RTCRtpCodecParameters(
            mimeType="video/rtx",
            clockRate=90000,
            payloadType=payload_type + 1,
            parameters={"apt": payload_type})
        codecs.extend((high, rtx))
        appended.extend((high, rtx))
        added.append(name)
    try:
        yield added
    finally:
        # Remove by identity: never touch entries that were already registered.
        codecs[:] = [entry for entry in codecs
                     if not any(entry is own for own in appended)]
