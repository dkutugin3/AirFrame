"""Hardware H.264 decode with an honest software fallback (plan stage 7).

Pinned stack: aiortc 1.15.0 requires ``av>=14,<18``; the project installs PyAV
17.1.0. PyAV 17.1.0 exposes ``av.codec.hwaccel.HWAccel`` and
``CodecContext.create(..., hwaccel=...)``; on this macOS stand the available
device type is ``videotoolbox`` (D3D11VA on Windows is unverified - no Windows
machine here).

What was measured on this stand (Apple M4, PyAV 17.1.0, real libx264 H.264
2560x1440@60 stream, see artifacts/2026-10-30-stable60fps/native/):

* the VideoToolbox path really engages: ``CodecContext.is_hwaccel`` is True and
  FFmpeg logs ``pix_fmt: videotoolbox_vld`` while decoding a real IDR + P-frame
  sequence; frames come back as ``nv12`` (VideoToolbox output format) instead of
  the software decoder's ``yuv420p``; decoded pixels, PTS and sizes are
  identical to the software reference (0 code-value difference after RGB
  conversion);
* PyAV downloads hardware frames to system memory: this is **not** zero-copy,
  and there is no GPU-surface sharing;
* for this workload the hardware path through PyAV 17.1.0 is *slower per frame*
  than the software decoder (about 3 ms/frame wall versus 0.3 ms/frame for the
  2560x1440@60 test stream), while using less process CPU (about 0.55 versus
  1.2 ms/frame) because VideoToolbox does the decode outside Python but PyAV
  waits for each downloaded frame. ``auto`` therefore keeps the software
  decoder (lower latency, identical output); ``hardware`` is an explicitly
  verified opt-in path.

Selection contract:

* ``software`` - exactly the aiortc/FFmpeg software decoder, no hardware context.
* ``hardware`` - verified VideoToolbox context; if the device is unavailable the
  status honestly says ``actualMode="software"`` with the reason (it is never
  labelled hardware).
* ``auto`` - only a verified path is used; on this pinned stack it selects
  software with a visible policy reason because hardware measured slower.
  ``AIRFRAME_HW_DECODE=hardware`` (or ``prefer_hardware=True``) opts in.

A hardware context that was created but did not actually engage
(``is_hwaccel`` False, as observed for an unsupported device type) is treated as
**not success**. Any hardware decode error drops to a fresh software decoder,
drops dependent frames until the next IDR (correct restart point), reports the
reason, and never re-creates the hardware context in the same session (no retry
loop). ``close()`` releases both contexts.
"""
import os
import time
from dataclasses import dataclass
from fractions import Fraction

import av
import av.codec.hwaccel as av_hwaccel

from .transport import H264_NAL_IDR, h264_nal_types

MODE_AUTO = "auto"
MODE_HARDWARE = "hardware"
MODE_SOFTWARE = "software"
MODES = (MODE_AUTO, MODE_HARDWARE, MODE_SOFTWARE)

DEVICE_VIDEOTOOLBOX = "videotoolbox"
DEVICE_D3D11VA = "d3d11va"
DEFAULT_DEVICE = DEVICE_VIDEOTOOLBOX

#: Measured on this stand: PyAV 17.1.0 VideoToolbox is slower for the target
#: 2560x1440@60 H.264 stream, so `auto` keeps the software decoder by default.
AUTO_SOFTWARE_POLICY = (
    "software: hardware H.264 decode measured slower per frame on this stand "
    "(~3 ms/frame wall via PyAV 17.1.0 VideoToolbox vs ~0.3 ms/frame software for a "
    "2560x1440@60 H.264 stream; process CPU ~0.55 vs ~1.2 ms/frame because VideoToolbox "
    "offloads the CPU but PyAV waits for the downloaded frame); use "
    "AIRFRAME_HW_DECODE=hardware to opt in"
)

#: aiortc feeds 90 kHz RTP timestamps through JitterFrame.timestamp.
VIDEO_TIME_BASE = Fraction(1, 90000)

#: After a hardware failure the same session never recreates the hardware
#: context: one verified decision, then the software decoder until reopen.
MAX_HW_RECREATIONS = 0


@dataclass(frozen=True)
class HwProbe:
    """What the pinned PyAV actually reports for one hardware device type."""

    device_type: str
    available: bool
    engaged: bool
    reason: str

    @property
    def verified(self):
        """A device is verified only when the created decoder declares hwaccel."""
        return self.available and self.engaged


class HardwareUnavailable(Exception):
    """No usable hardware decoder: reported, never silently relabelled."""


def available_hw_devices():
    """Device type names PyAV reports as available on this machine."""
    try:
        return list(av_hwaccel.hwdevices_available())
    except Exception:  # pragma: no cover - defensive across PyAV builds
        return []


def _device_enum(device_type):
    device_type = str(device_type).lower()
    for name in dir(av_hwaccel.HWDeviceType):
        if name.lower() == device_type:
            return getattr(av_hwaccel.HWDeviceType, name)
    raise HardwareUnavailable(f"unknown hardware device type: {device_type!r}")


def probe_h264_hwaccel(device_type=DEFAULT_DEVICE):
    """Create a real H.264 hardware decoder context and report whether it engaged.

    "Context created" is not success: the created context must report
    ``is_hwaccel`` True. A context whose hwaccel silently did not engage (observed
    for an unsupported device type) is reported as ``engaged=False``.
    """
    try:
        enum = _device_enum(device_type)
    except HardwareUnavailable as exc:
        return HwProbe(device_type, False, False, str(exc))
    if str(device_type).lower() not in available_hw_devices():
        return HwProbe(device_type, False, False,
                       f"{device_type} is not listed by PyAV hwdevices_available() on this machine")
    try:
        accel = av_hwaccel.HWAccel(enum, allow_software_fallback=False)
        context = av.CodecContext.create("h264", "r", hwaccel=accel)
    except Exception as exc:  # av.FFmpegError / RuntimeError / ValueError
        return HwProbe(device_type, False, False, f"{type(exc).__name__}: {exc}")
    engaged = bool(getattr(context, "is_hwaccel", False))
    if not engaged:
        return HwProbe(device_type, True, False,
                       f"{device_type} context was created but hardware decode did not engage "
                       "(is_hwaccel=False); treating it as software")
    return HwProbe(device_type, True, True, "")


def _to_packet(encoded_frame):
    """Accept aiortc's JitterFrame or an av.Packet and normalise to av.Packet."""
    if isinstance(encoded_frame, av.Packet):
        return encoded_frame
    data = getattr(encoded_frame, "data", None)
    if data is None:
        raise TypeError(f"unsupported encoded frame type: {type(encoded_frame)!r}")
    packet = av.Packet(data)
    timestamp = getattr(encoded_frame, "timestamp", None)
    packet.pts = timestamp
    packet.time_base = VIDEO_TIME_BASE
    return packet


def _packet_is_idr(packet):
    try:
        return H264_NAL_IDR in h264_nal_types(bytes(packet))
    except Exception:  # pragma: no cover - malformed packet: let the decoder judge
        return False


class DecoderStatus(dict):
    """Visible decoder state: declared vs actual mode, reasons and counters."""

    def __init__(self, requested_mode, device_type):
        super().__init__(
            requestedMode=requested_mode,
            actualMode=MODE_SOFTWARE,
            device=device_type,
            verifiedHardware=False,
            failedOver=False,
            reason="",
            framesDecoded=0,
            hardwareFrames=0,
            softwareFrames=0,
            hardwareFailures=0,
            fallbacks=0,
            awaitingKeyframe=False,
            droppedAwaitingKeyframe=0,
            hardwareRecreations=0,
            closed=False,
        )


class H264HardwareDecoder:
    """aiortc-compatible H.264 decoder (``decode(JitterFrame) -> frames``).

    ``mode="hardware"`` starts on VideoToolbox and never retries it after a
    failure; ``mode="software"`` is the untouched aiortc/FFmpeg behaviour
    (same PyAV context, same packet timestamps).
    """

    name = "h264-hardware"

    def __init__(self, device_type=DEFAULT_DEVICE, mode=MODE_HARDWARE,
                 context_factory=None, clock=time.monotonic, log=None):
        if mode not in (MODE_HARDWARE, MODE_SOFTWARE):
            raise ValueError(f"decoder instance mode must be hardware/software, not {mode!r}")
        self.device_type = device_type
        self.clock = clock
        self._log = log or (lambda message: None)
        self.status = DecoderStatus(mode, device_type)
        self._hardware = None
        self._software = None
        self._awaiting_keyframe = False
        self._context_factory = context_factory
        self._make_software()
        if mode == MODE_SOFTWARE:
            return
        try:
            self._hardware = self._create_hardware(device_type, context_factory)
        except HardwareUnavailable as exc:
            self.status["reason"] = str(exc)
            self._log(f"hardware H.264 decode unavailable: {exc}")
            return
        self.status["actualMode"] = MODE_HARDWARE

    # ---- contexts ---------------------------------------------------------
    def _make_software(self):
        self._software = av.CodecContext.create("h264", "r")
        self._awaiting_keyframe = False
        self.status["awaitingKeyframe"] = False

    @staticmethod
    def _create_hardware(device_type, context_factory=None):
        if context_factory is not None:
            try:
                context = context_factory()
            except HardwareUnavailable:
                raise
            except Exception as exc:
                raise HardwareUnavailable(f"{device_type} decoder failed to initialise: {exc}")
            if not bool(getattr(context, "is_hwaccel", False)):
                raise HardwareUnavailable(
                    f"{device_type} context created but hardware decode did not engage (is_hwaccel=False)"
                )
            return context
        probe = probe_h264_hwaccel(device_type)
        if not probe.verified:
            raise HardwareUnavailable(probe.reason or f"{device_type} hardware decode unavailable")
        try:
            accel = av_hwaccel.HWAccel(_device_enum(device_type), allow_software_fallback=False)
            context = av.CodecContext.create("h264", "r", hwaccel=accel)
        except Exception as exc:
            raise HardwareUnavailable(f"{device_type} decoder failed to initialise: {exc}")
        if not bool(getattr(context, "is_hwaccel", False)):
            raise HardwareUnavailable(
                f"{device_type} context created but hardware decode did not engage (is_hwaccel=False)"
            )
        return context

    # ---- aiortc decoder interface ----------------------------------------
    def decode(self, encoded_frame):
        if self.status["closed"]:
            return []
        packet = _to_packet(encoded_frame)
        if self._hardware is not None:
            try:
                frames = self._hardware.decode(packet)
            except av.FFmpegError as exc:
                return self._hardware_failed(str(exc), packet)
            self._count(frames, hardware=True)
            return frames
        if self._awaiting_keyframe:
            if not _packet_is_idr(packet):
                self.status["droppedAwaitingKeyframe"] += 1
                return []
            self._awaiting_keyframe = False
            self.status["awaitingKeyframe"] = False
        try:
            frames = self._software.decode(packet)
        except av.FFmpegError:
            return []
        self._count(frames, hardware=False)
        return frames

    def _count(self, frames, hardware):
        if not frames:
            return
        count = len(frames)
        self.status["framesDecoded"] += count
        if hardware:
            self.status["hardwareFrames"] += count
            self.status["actualMode"] = MODE_HARDWARE
            self.status["verifiedHardware"] = True
        else:
            self.status["softwareFrames"] += count

    def _hardware_failed(self, reason, packet):
        """Fall back to a fresh software decoder and restart at the next IDR."""
        self.status["hardwareFailures"] += 1
        self.status["fallbacks"] += 1
        self.status["failedOver"] = True
        self.status["actualMode"] = MODE_SOFTWARE
        self.status["verifiedHardware"] = False
        self.status["reason"] = (
            f"hardware decode failed ({reason}); switched to software, waiting for the next IDR"
        )
        self._log(self.status["reason"])
        self._hardware = None
        # Fresh software context: the old one never saw a complete hardware epoch.
        self._make_software()
        if not _packet_is_idr(packet):
            self._awaiting_keyframe = True
            self.status["awaitingKeyframe"] = True
            return []
        # The failing access unit was an IDR: software can restart on it right away.
        try:
            frames = self._software.decode(packet)
        except av.FFmpegError:
            return []
        self._count(frames, hardware=False)
        return frames

    def close(self):
        if self.status["closed"]:
            return
        self._hardware = None
        self._software = None
        self._awaiting_keyframe = False
        self.status["closed"] = True
        self.status["awaitingKeyframe"] = False

    def reset(self):
        """Reopen decode after reconnect/resolution change (releases both contexts)."""
        self._hardware = None
        self._software = None
        self._awaiting_keyframe = False
        self.status["closed"] = False
        self._make_software()
        if self.status["requestedMode"] == MODE_HARDWARE and not self.status["failedOver"]:
            try:
                self._hardware = self._create_hardware(self.device_type, self._context_factory)
                self.status["actualMode"] = MODE_HARDWARE
                self.status["reason"] = ""
            except HardwareUnavailable as exc:
                self.status["reason"] = str(exc)
                self.status["actualMode"] = MODE_SOFTWARE


class DecoderFactory:
    """Callable for ``DecodePipeline``/``install_decoder_pipelines(decoder_factory=...)``.

    The selection is resolved once per factory (per session): ``auto`` only ever
    selects a verified path, forced ``hardware`` reports impossibility in
    ``status`` instead of pretending, and every decoder it creates shares the
    resolved mode.
    """

    def __init__(self, mode=MODE_AUTO, device_type=DEFAULT_DEVICE, probe=None,
                 prefer_hardware=False, clock=time.monotonic, log=None):
        if mode not in MODES:
            raise ValueError(f"unknown decoder mode: {mode!r}")
        self.mode = mode
        self.device_type = device_type
        self.clock = clock
        self._log = log or (lambda message: None)
        self._probe = probe or probe_h264_hwaccel
        self.probe = self._probe(device_type) if mode != MODE_SOFTWARE else None
        self.decoders = []
        self.status = {
            "requestedMode": mode,
            "selectedMode": MODE_SOFTWARE,
            "device": device_type,
            "hardwareVerified": False,
            "reason": "",
        }
        if mode == MODE_SOFTWARE:
            return
        probe_result = self.probe
        if mode == MODE_HARDWARE:
            if probe_result.verified:
                self.status.update(selectedMode=MODE_HARDWARE, hardwareVerified=True)
            else:
                self.status.update(
                    selectedMode=MODE_SOFTWARE,
                    reason=f"hardware H.264 decode unavailable: {probe_result.reason or 'not verified'}",
                )
            return
        # auto: only a verified path; the measured-faster path wins by default.
        if probe_result.verified and prefer_hardware:
            self.status.update(selectedMode=MODE_HARDWARE, hardwareVerified=True)
        elif probe_result.verified:
            self.status.update(selectedMode=MODE_SOFTWARE, reason=AUTO_SOFTWARE_POLICY)
        else:
            self.status.update(
                selectedMode=MODE_SOFTWARE,
                reason=f"hardware H.264 decode unavailable: {probe_result.reason or 'not verified'}",
            )

    def __call__(self, codec=None):
        """DecodePipeline calls this once per codec; returns an aiortc-style decoder."""
        mode = self.status["selectedMode"]
        decoder = H264HardwareDecoder(
            device_type=self.device_type,
            mode=mode,
            context_factory=None,
            clock=self.clock,
            log=self._log,
        )
        self.decoders.append(decoder)
        return decoder

    def status_snapshot(self):
        snapshot = dict(self.status)
        if self.decoders:
            latest = self.decoders[-1].status
            snapshot.update({
                "actualMode": latest["actualMode"],
                "verifiedHardware": latest["verifiedHardware"],
                "failedOver": latest["failedOver"],
                "decoderReason": latest["reason"],
                "framesDecoded": latest["framesDecoded"],
                "hardwareFrames": latest["hardwareFrames"],
                "softwareFrames": latest["softwareFrames"],
                "hardwareFailures": latest["hardwareFailures"],
            })
            if latest["reason"] and not snapshot.get("reason"):
                snapshot["reason"] = latest["reason"]
        return snapshot

    def close(self):
        for decoder in self.decoders:
            decoder.close()
        self.decoders.clear()


def _env_prefers_hardware():
    value = os.environ.get("AIRFRAME_HW_DECODE", "").strip().lower()
    if value in ("", "auto", "0", "software", "no", "off"):
        return False
    return value in ("1", "hardware", "videotoolbox", "yes", "on")


def select_decoder_factory(mode=None, device_type=DEFAULT_DEVICE, probe=None,
                           prefer_hardware=None, clock=time.monotonic, log=None):
    """Build the decoder factory for the stage-4 integration layer.

    ``mode`` defaults to ``AIRFRAME_HW_DECODE`` (``auto`` when unset); a bare
    ``AIRFRAME_HW_DECODE=hardware`` opts the auto policy into the verified
    VideoToolbox path. Forced ``hardware`` is never downgraded silently: the
    factory status carries the reason when the device cannot be verified.
    """
    env = os.environ.get("AIRFRAME_HW_DECODE", "").strip().lower()
    if mode is None:
        mode = MODE_HARDWARE if env == MODE_HARDWARE else (MODE_SOFTWARE if env == MODE_SOFTWARE else MODE_AUTO)
    if prefer_hardware is None:
        prefer_hardware = mode == MODE_HARDWARE or env in ("1", "hardware", "yes", "on")
    if mode not in MODES:
        raise ValueError(f"unknown decoder mode: {mode!r}")
    return DecoderFactory(mode=mode, device_type=device_type, probe=probe,
                          prefer_hardware=prefer_hardware, clock=clock, log=log)
