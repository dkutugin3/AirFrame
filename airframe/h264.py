"""H.264 level arithmetic and honest profile-level-id advertising for the SDP answer.

aiortc 1.15.0 advertises a fixed ``42001f`` / ``42e01f`` (Baseline and Constrained
Baseline, level 3.1) receive codec in every answer, regardless of the configured
capture geometry. Level 3.1 cannot represent 2560x1440 at 60 fps (it needs level
5.1), so a sender that honours the declared level is forced to downscale or to
fall back. The answer must declare the level the receiver is actually configured
to accept.

This module is deliberately pure and small: it does not change the negotiated
profile, only the level byte of ``profile-level-id``, and it does not touch any
non-H.264 payload. The rewrite is applied to the answer text AirFrame sends over
signaling; aiortc's internal description is left untouched, which is acceptable
because level-asymmetry-allowed=1 and only the peer's view is authoritative.
"""
import math
import re

# ITU-T H.264 Table A-1: level_idc -> (MaxMBPS, MaxFS).
# MaxMBPS = macroblocks per second, MaxFS = frame size in macroblocks.
# MaxDpbMbs is not used to choose an advertisement level: the decoder's reference
# picture buffer is a property of the decoder, not of the declared receive level.
LEVEL_LIMITS = {
    10: (1485, 99),
    11: (3000, 396),
    12: (6000, 396),
    13: (11880, 396),
    20: (11880, 396),
    21: (19800, 792),
    22: (20250, 1620),
    30: (40500, 1620),
    31: (108000, 3600),
    32: (216000, 5120),
    40: (245760, 8192),
    41: (245760, 8192),
    42: (522240, 8704),
    50: (589824, 22080),
    51: (983040, 36864),
    52: (2073600, 36864),
    60: (4177920, 139264),
    61: (8355840, 139264),
    62: (16711680, 139264),
}

LEVEL_NAMES = {
    10: "1", 11: "1.1", 12: "1.2", 13: "1.3",
    20: "2", 21: "2.1", 22: "2.2",
    30: "3", 31: "3.1", 32: "3.2",
    40: "4", 41: "4.1", 42: "4.2",
    50: "5", 51: "5.1", 52: "5.2",
    60: "6", 61: "6.1", 62: "6.2",
}

_ORDER = sorted(LEVEL_LIMITS)
_RTPMAP = re.compile(r"a=rtpmap:(?P<pt>\d+) H264/90000", re.IGNORECASE)
_PROFILE_LEVEL_ID = re.compile(
    r"(?P<prefix>a=fmtp:(?P<pt>\d+)[^\r\n]*?\bprofile-level-id=)"
    r"(?P<plid>[0-9a-fA-F]{6})")


def macroblocks(width, height):
    """Number of 16x16 macroblocks needed for a frame (chroma not included)."""
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("H.264 frame geometry must be integers") from None
    if width <= 0 or height <= 0:
        raise ValueError("H.264 frame geometry must be positive")
    return math.ceil(width / 16) * math.ceil(height / 16)


def required_level_idc(width, height, fps):
    """Smallest H.264 level_idc whose MaxFS and MaxMBPS cover width x height @ fps."""
    try:
        fps = int(fps)
    except (TypeError, ValueError):
        raise ValueError("H.264 frame rate must be an integer") from None
    if fps <= 0:
        raise ValueError("H.264 frame rate must be positive")
    blocks = macroblocks(width, height)
    rate = blocks * fps
    for level in _ORDER:
        max_mbps, max_fs = LEVEL_LIMITS[level]
        if blocks <= max_fs and rate <= max_mbps:
            return level
    raise ValueError(f"{width}x{height}@{fps} exceeds H.264 level 6.2 limits")


def level_name(level_idc):
    """Human readable level name for a level_idc value."""
    try:
        return LEVEL_NAMES[level_idc]
    except KeyError:
        raise ValueError(f"Unknown H.264 level_idc {level_idc!r}") from None


def advertise_answer_level(max_size, fps):
    """Level to declare for the configured receive profile.

    ``max_size`` is the maximum long side in pixels; phone screens are at least
    16:9 in their longer dimension (ultra-wide phones have a shorter side), so
    16:9 is the conservative worst case for the number of coded pixels. Rotation
    does not change the macroblock count.
    """
    try:
        max_size = int(max_size)
        fps = int(fps)
    except (TypeError, ValueError):
        raise ValueError("Profile size/fps must be integers") from None
    if max_size <= 0 or fps <= 0:
        raise ValueError("Profile size/fps must be positive")
    short = int(math.ceil(max_size * 9 / 16 / 2) * 2)
    return required_level_idc(max_size, short, fps)


def rewrite_profile_level_id(sdp, level_idc):
    """Return ``sdp`` with H.264 profile-level-id level bytes set to ``level_idc``.

    Profile and constraint bytes are preserved; only fmtp lines belonging to an
    H.264 payload type are touched. Idempotent.
    """
    if level_idc not in LEVEL_LIMITS:
        raise ValueError(f"Unknown H.264 level_idc {level_idc!r}")
    level_hex = f"{level_idc:02x}"
    h264_payloads = {match.group("pt") for match in _RTPMAP.finditer(sdp)}

    def replace(match):
        if match.group("pt") not in h264_payloads:
            return match.group(0)
        plid = match.group("plid")
        return f"{match.group('prefix')}{plid[:4].lower()}{level_hex}"

    return _PROFILE_LEVEL_ID.sub(replace, sdp)
