"""Stage 7 contracts: hardware H.264 decode with an honest software fallback.

Run on the native host (VideoToolbox needs the real machine):

    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

Real VideoToolbox cases are skipped only when the pinned PyAV does not report a
verified device; on this stand they run and are the acceptance evidence.
"""
import asyncio
import os
import tempfile
import unittest

import av

import airframe.hwdecode as hw
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from airframe.decode import DecodePipeline

H264_CODEC = RTCRtpCodecParameters(mimeType="video/H264", clockRate=90000, payloadType=96)
VP8_CODEC = RTCRtpCodecParameters(mimeType="video/VP8", clockRate=90000, payloadType=97)

REAL_PROBE = hw.probe_h264_hwaccel()


def _encode(width, height, frames=48, keyint=16):
    """Encode a real H.264 stream with libx264 and return its Annex-B access units."""
    handle = tempfile.NamedTemporaryFile(suffix=".h264", delete=False)
    path = handle.name
    handle.close()
    try:
        container = av.open(path, "w", format="h264")
        stream = container.add_stream("libx264", rate=60)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.options = {"preset": "ultrafast", "tune": "zerolatency",
                          "g": str(keyint), "bf": "0", "crf": "28"}
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p")
            for plane_index, plane in enumerate(frame.planes):
                mv = memoryview(plane)
                for row in range(plane.height):
                    if plane_index == 0:
                        value = 16 + ((row // 3 + index * 9) % 200)
                    else:
                        value = 128
                    mv[row * plane.line_size:(row + 1) * plane.line_size] = bytes([value]) * plane.line_size
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        packets = []
        container = av.open(path, "r")
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            packets.append((bytes(packet), packet.pts))
        container.close()
        return packets
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


_STREAMS = {}


def stream(width, height):
    key = (width, height)
    if key not in _STREAMS:
        _STREAMS[key] = _encode(width, height)
    return _STREAMS[key]


class Jitter:
    """aiortc JitterFrame-compatible input for the decoder contract."""

    def __init__(self, data, timestamp=None):
        self.data = data
        self.timestamp = timestamp


def _software_reference(packets):
    decoder = av.CodecContext.create("h264", "r")
    frames = []
    for data, pts in packets:
        packet = av.Packet(data)
        packet.pts = pts
        packet.time_base = hw.VIDEO_TIME_BASE
        frames.extend(decoder.decode(packet))
    return frames


def _decode(decoder, packets):
    frames = []
    for data, pts in packets:
        frames.extend(decoder.decode(Jitter(data, pts)))
    return frames


def _rgb(frame):
    return frame.reformat(format="bgra")


def _max_channel_diff(first, second):
    a, b = _rgb(first), _rgb(second)
    mva, mvb = memoryview(a.planes[0]), memoryview(b.planes[0])
    step = max(1, len(mva) // 4000)
    return max(abs(mva[i] - mvb[i]) for i in range(0, len(mva), step))


class ProbeTests(unittest.TestCase):
    def test_probe_reports_the_pinned_pyav_state_honestly(self):
        probe = hw.probe_h264_hwaccel()
        self.assertEqual(probe.device_type, "videotoolbox")
        self.assertIn("videotoolbox", hw.available_hw_devices())
        self.assertTrue(probe.available)
        self.assertTrue(probe.verified, "VideoToolbox context must declare is_hwaccel=True")
        self.assertEqual(probe.reason, "")

    def test_unlisted_device_is_not_reported_as_success(self):
        probe = hw.probe_h264_hwaccel("cuda")  # not available on macOS
        if REAL_PROBE.verified:
            self.assertFalse(probe.available)
            self.assertFalse(probe.verified)
            self.assertIn("not listed", probe.reason)

    def test_context_created_but_not_engaged_is_not_success(self):
        class NotEngaged:
            is_hwaccel = False

        decoder = hw.H264HardwareDecoder(
            mode=hw.MODE_HARDWARE, context_factory=lambda: NotEngaged())
        self.assertEqual(decoder.status["actualMode"], hw.MODE_SOFTWARE)
        self.assertFalse(decoder.status["verifiedHardware"])
        self.assertIn("did not engage", decoder.status["reason"])
        decoder.close()


class RealVideoToolboxTests(unittest.TestCase):
    def setUp(self):
        if not REAL_PROBE.verified:
            self.skipTest(f"VideoToolbox not available: {REAL_PROBE.reason}")
        av.logging.set_level(av.logging.VERBOSE)

    def test_real_stream_decodes_on_the_hardware_path_and_matches_software(self):
        packets = stream(640, 360)
        reference = _software_reference(packets)
        self.assertGreater(len(reference), 30)
        with av.logging.Capture() as logs:
            decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE)
            frames = _decode(decoder, packets)
        status = decoder.status
        decoder.close()
        self.assertEqual(status["actualMode"], hw.MODE_HARDWARE)
        self.assertTrue(status["verifiedHardware"], "hardware decode never produced frames")
        self.assertGreater(status["hardwareFrames"], 30)
        self.assertGreaterEqual(len(frames), len(reference) - 2)
        self.assertEqual(len(frames), len(reference))
        for got, expected in zip(frames, reference):
            self.assertEqual(got.pts, expected.pts)
            self.assertEqual((got.width, got.height), (expected.width, expected.height))
        diffs = [_max_channel_diff(a, b) for a, b in zip(frames[:4], reference[:4])]
        self.assertLessEqual(max(diffs), 2, f"hardware frames differ from software: {diffs}")
        hardware_format = frames[0].format.name
        self.assertEqual(hardware_format, "nv12",
                         "VideoToolbox output format changed; update the measured frame path")
        messages = "\n".join(message for _, _, message in logs)
        self.assertIn("videotoolbox", messages.lower(),
                      "FFmpeg log did not confirm the VideoToolbox pixel format")

    def test_resolution_change_reopens_decode_without_crashing(self):
        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE)
        first = _decode(decoder, stream(320, 180))
        self.assertTrue(first)
        self.assertEqual((first[0].width, first[0].height), (320, 180))
        second = _decode(decoder, stream(640, 360))
        self.assertTrue(second, f"no frames after resolution change: {decoder.status}")
        self.assertEqual((second[-1].width, second[-1].height), (640, 360))
        decoder.close()

    def test_close_releases_and_stops_decoding(self):
        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE)
        _decode(decoder, stream(320, 180)[:8])
        decoder.close()
        decoder.close()  # idempotent
        self.assertTrue(decoder.status["closed"])
        self.assertEqual(decoder.decode(Jitter(b"\x00\x00\x01\x65")), [])
        # A fresh decoder can be created after release (contexts are not leaked).
        replacement = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE)
        self.assertEqual(replacement.status["actualMode"], hw.MODE_HARDWARE)
        replacement.close()


class FaultFallbackTests(unittest.TestCase):
    def setUp(self):
        self.packets = stream(320, 180)
        # Use the software decoder behind a fake hardware context for determinism.
        self.reference = _software_reference(self.packets)

    def _flaky_factory(self, fail_after):
        class FlakyContext:
            is_hwaccel = True

            def __init__(self):
                self.calls = 0
                self._inner = av.CodecContext.create("h264", "r")

            def decode(self, packet):
                self.calls += 1
                if fail_after is not None and self.calls > fail_after:
                    raise av.FFmpegError(0, "simulated VideoToolbox failure")
                return self._inner.decode(packet)

        return lambda: FlakyContext()

    def test_init_failure_falls_back_to_software_with_a_visible_reason(self):
        def failing():
            raise RuntimeError("no decoder")

        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE, context_factory=failing)
        self.assertEqual(decoder.status["actualMode"], hw.MODE_SOFTWARE)
        self.assertIn("failed to initialise", decoder.status["reason"])
        frames = _decode(decoder, self.packets)
        self.assertGreater(len(frames), 10)
        decoder.close()

    def test_mid_session_failure_restarts_on_the_next_idr_without_retry_loop(self):
        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE,
                                        context_factory=self._flaky_factory(fail_after=6))
        delivered = []
        for data, pts in self.packets:
            delivered.extend(decoder.decode(Jitter(data, pts)))
        status = decoder.status
        self.assertTrue(status["failedOver"])
        self.assertEqual(status["actualMode"], hw.MODE_SOFTWARE)
        self.assertEqual(status["fallbacks"], 1)
        self.assertIn("hardware decode failed", status["reason"])
        self.assertGreater(status["droppedAwaitingKeyframe"], 0)
        self.assertEqual(status["hardwareRecreations"], 0, "hardware must not be retried in-session")
        self.assertGreater(status["softwareFrames"], 0)
        # The frames decoded before the failure and after the IDR restart are valid.
        self.assertGreater(len(delivered), 5)
        decoder.close()

    def test_failure_on_an_idr_restarts_software_immediately(self):
        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE,
                                        context_factory=self._flaky_factory(fail_after=0))
        frames = _decode(decoder, self.packets)
        self.assertTrue(decoder.status["failedOver"])
        self.assertGreater(len(frames), 10, "software restart on the IDR produced no frames")
        decoder.close()


class SelectionTests(unittest.TestCase):
    def _verified(self, device):
        return hw.HwProbe(device, True, True, "")

    def _not_engaged(self, device):
        return hw.HwProbe(device, True, False, "context created but hardware decode did not engage")

    def test_forced_software_never_creates_hardware(self):
        factory = hw.select_decoder_factory(hw.MODE_SOFTWARE)
        decoder = factory(H264_CODEC)
        self.assertEqual(decoder.status["actualMode"], hw.MODE_SOFTWARE)
        self.assertEqual(decoder.status["requestedMode"], hw.MODE_SOFTWARE)
        self.assertIsNone(factory.probe)
        decoder.close()

    def test_forced_hardware_verified_selects_hardware(self):
        factory = hw.select_decoder_factory(hw.MODE_HARDWARE, probe=self._verified)
        self.assertEqual(factory.status["selectedMode"], hw.MODE_HARDWARE)
        self.assertTrue(factory.status["hardwareVerified"])
        decoder = factory(H264_CODEC)
        self.assertEqual(decoder.status["actualMode"], hw.MODE_HARDWARE)
        decoder.close()

    def test_forced_hardware_unavailable_reports_impossibility_honestly(self):
        factory = hw.select_decoder_factory(hw.MODE_HARDWARE, probe=self._not_engaged)
        self.assertEqual(factory.status["selectedMode"], hw.MODE_SOFTWARE)
        self.assertIn("did not engage", factory.status["reason"])
        decoder = factory(H264_CODEC)
        self.assertEqual(decoder.status["actualMode"], hw.MODE_SOFTWARE)
        self.assertNotEqual(decoder.status["actualMode"], hw.MODE_HARDWARE,
                            "software must never be labelled hardware")
        decoder.close()

    def test_auto_uses_a_verified_path_only_and_explains_the_choice(self):
        factory = hw.select_decoder_factory(hw.MODE_AUTO, probe=self._verified)
        self.assertEqual(factory.status["selectedMode"], hw.MODE_SOFTWARE)
        self.assertIn("measured slower", factory.status["reason"])
        opted_in = hw.select_decoder_factory(hw.MODE_AUTO, probe=self._verified,
                                             prefer_hardware=True)
        self.assertEqual(opted_in.status["selectedMode"], hw.MODE_HARDWARE)

    def test_non_h264_codecs_stay_on_the_aiortc_decoder(self):
        """VP8/VP9 must be delegated to aiortc unchanged, even in hardware mode."""
        factory = hw.select_decoder_factory(hw.MODE_HARDWARE, probe=self._verified)
        decoder = factory(VP8_CODEC)
        from aiortc.codecs.vpx import Vp8Decoder

        self.assertNotIsInstance(decoder, hw.H264HardwareDecoder)
        self.assertIsInstance(decoder, Vp8Decoder)

    def test_env_opt_in_for_hardware(self):
        previous = os.environ.get("AIRFRAME_HW_DECODE")
        try:
            os.environ["AIRFRAME_HW_DECODE"] = "hardware"
            factory = hw.select_decoder_factory(probe=self._verified)
            self.assertEqual(factory.mode, hw.MODE_HARDWARE)
            self.assertEqual(factory.status["selectedMode"], hw.MODE_HARDWARE)
            os.environ["AIRFRAME_HW_DECODE"] = "software"
            factory = hw.select_decoder_factory()
            self.assertEqual(factory.mode, hw.MODE_SOFTWARE)
        finally:
            if previous is None:
                os.environ.pop("AIRFRAME_HW_DECODE", None)
            else:
                os.environ["AIRFRAME_HW_DECODE"] = previous


class Stage4IntegrationPointTests(unittest.TestCase):
    def test_decode_pipeline_accepts_an_injected_decoder_factory(self):
        loop = asyncio.new_event_loop()
        try:
            factory = hw.select_decoder_factory(hw.MODE_SOFTWARE)
            pipeline = DecodePipeline(loop, decoder_factory=factory)
            self.assertIs(pipeline.decoder_factory, factory)
            pipeline.close()
        finally:
            loop.close()

    def test_software_path_honours_the_stage4_get_decoder_injection(self):
        """The stage-4 injection point (airframe.decode.get_decoder) still governs software."""
        import airframe.decode as decode_module

        original = decode_module.get_decoder
        calls = []

        class MarkerDecoder:
            def decode(self, frame):
                calls.append(frame)
                return []

        decode_module.get_decoder = lambda codec: MarkerDecoder()
        try:
            decoder = hw.H264HardwareDecoder(mode=hw.MODE_SOFTWARE, codec=H264_CODEC)
            decoder.decode(Jitter(stream(320, 180)[0][0]))
        finally:
            decode_module.get_decoder = original
            decoder.close()
        self.assertEqual(len(calls), 1)

    def test_install_decoder_pipelines_accepts_the_factory(self):
        import inspect

        from airframe.decode import install_decoder_pipelines

        parameters = inspect.signature(install_decoder_pipelines).parameters
        self.assertIn("decoder_factory", parameters)
        self.assertIsNone(parameters["decoder_factory"].default)


if __name__ == "__main__":
    unittest.main()
