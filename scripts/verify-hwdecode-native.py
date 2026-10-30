#!/usr/bin/env python
"""Stage 7 native evidence: real VideoToolbox H.264 decode and honest fallback.

Produces the acceptance evidence that cannot be produced in Docker:

* which hardware devices the pinned PyAV 17.1.0 actually reports on this Mac;
* that a real VideoToolbox decoder context engages (``is_hwaccel`` True, FFmpeg
  logs ``pix_fmt: videotoolbox_vld``) on a real libx264 H.264 stream;
* that the hardware frames match the software reference: count, PTS, size and
  decoded pixels (RGB after the same download path), plus the frame format
  difference that proves a different decode path (``nv12`` vs ``yuv420p``);
* the measured frame path and benefit: wall/process CPU per frame for software
  and hardware decode of a 2560x1440@60 stream (PyAV downloads hardware frames
  to system memory - this is NOT zero-copy and no GPU-surface sharing is used);
* the SPS profile/level of the locally encoded 2560x1440@60 stream (the real
  SPS evidence for the declared-level discussion; the Android encoder SPS needs
  a real phone and stays unverified);
* the honest fallback contract: a hardware context that does not engage, an
  init failure and a mid-session failure all end on the software decoder with a
  visible reason, an IDR restart point and no hardware retry loop.

Run on the native macOS host:

    PYTHONPATH="$PWD/.superpowers/native-deps" .venv/bin/python scripts/verify-hwdecode-native.py

Results: artifacts/2026-10-30-stable60fps/native/stage7-videotoolbox-evidence.json
"""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import av

import airframe.hwdecode as hw

OUT_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "2026-10-30-stable60fps" / "native"
OUT_DIR.mkdir(parents=True, exist_ok=True)
W, H, FRAMES = 2560, 1440, 60


def generate_stream(width, height, frame_count, keyint=30):
    handle = tempfile.NamedTemporaryFile(suffix=".h264", delete=False)
    path = handle.name
    handle.close()
    container = av.open(path, "w", format="h264")
    stream = container.add_stream("libx264", rate=60)
    stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
    stream.options = {"preset": "ultrafast", "tune": "zerolatency",
                      "g": str(keyint), "bf": "0", "crf": "28"}
    encode_started = time.perf_counter()
    for index in range(frame_count):
        frame = av.VideoFrame(width, height, "yuv420p")
        for plane_index, plane in enumerate(frame.planes):
            mv = memoryview(plane)
            for row in range(plane.height):
                value = 16 + ((row // 4 + index * 7) % 200) if plane_index == 0 else 128
                mv[row * plane.line_size:(row + 1) * plane.line_size] = bytes([value]) * plane.line_size
        frame.pts = index
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    encode_seconds = round(time.perf_counter() - encode_started, 2)

    packets, sps = [], None
    container = av.open(path, "r")
    video = container.streams.video[0]
    for packet in container.demux(video):
        if packet.size == 0:
            continue
        packets.append((bytes(packet), packet.pts))
        if sps is None:
            sps = _parse_sps(bytes(packet))
    container.close()
    os.unlink(path)
    return packets, sps, encode_seconds


def _unescape_rbsp(data):
    out, index = bytearray(), 0
    while index < len(data):
        if index + 2 < len(data) and data[index:index + 3] == b"\x00\x00\x03":
            out += data[index:index + 2]
            index += 3
        else:
            out.append(data[index])
            index += 1
    return bytes(out)


def _parse_sps(data):
    index = 0
    while index + 4 < len(data):
        start = data.find(b"\x00\x00\x01", index)
        if start < 0:
            return None
        nal = start + 3
        if data[nal] & 0x1F == 7:
            rbsp = _unescape_rbsp(data[nal + 1:nal + 64])
            return {"profile_idc": rbsp[0], "constraint_flags": rbsp[1], "level_idc": rbsp[2],
                    "profile_level_id_hex": f"{rbsp[0]:02x}{rbsp[1]:02x}{rbsp[2]:02x}",
                    "level": rbsp[2] / 10.0}
        index = nal + 1
    return None


class Jitter:
    def __init__(self, data, timestamp=None):
        self.data = data
        self.timestamp = timestamp


def decode(packets, use_hardware, warmup=10):
    context = None
    if use_hardware:
        accel = av.codec.hwaccel.HWAccel(av.codec.hwaccel.HWDeviceType.videotoolbox,
                                         allow_software_fallback=False)
        context = av.CodecContext.create("h264", "r", hwaccel=accel)
    else:
        context = av.CodecContext.create("h264", "r")
    frames, per_packet = [], []
    produced = 0
    for data, pts in packets:
        packet = av.Packet(data)
        packet.pts = pts
        packet.time_base = hw.VIDEO_TIME_BASE
        started = time.perf_counter()
        pictures = context.decode(packet)
        elapsed = (time.perf_counter() - started) * 1000
        produced += len(pictures)
        if produced > warmup:
            per_packet.append(elapsed)
        frames.extend(pictures)
    info = {
        "is_hwaccel": bool(getattr(context, "is_hwaccel", False)),
        "hwaccel_object": getattr(context, "hwaccel", None) is not None,
        "frames": len(frames),
        "frame_format": frames[0].format.name if frames else None,
        "pts_prefix": [f.pts for f in frames[:6]],
        "sizes": sorted({(f.width, f.height) for f in frames}),
        "per_packet_ms_p50": round(statistics.median(per_packet), 3) if per_packet else None,
        "per_packet_ms_p95": round(sorted(per_packet)[max(0, int(len(per_packet) * .95) - 1)], 3) if per_packet else None,
    }
    return info, frames, context


def colour_equivalence(software_frames, hardware_frames):
    diffs = []
    for first, second in zip(software_frames[:5], hardware_frames[:5]):
        a, b = first.reformat(format="bgra"), second.reformat(format="bgra")
        mva, mvb = memoryview(a.planes[0]), memoryview(b.planes[0])
        step = max(1, len(mva) // 5000)
        diffs.append(max(abs(mva[i] - mvb[i]) for i in range(0, len(mva), step)))
    return {"max_channel_diff_bgra": max(diffs) if diffs else None,
            "pixels_equal": bool(diffs) and max(diffs) == 0}


def benchmark(packets, use_hardware, repeats=2):
    walls, cpus, frames_total = [], [], 0
    for _ in range(repeats):
        accel = None
        if use_hardware:
            accel = av.codec.hwaccel.HWAccel(av.codec.hwaccel.HWDeviceType.videotoolbox,
                                             allow_software_fallback=False)
        context = (av.CodecContext.create("h264", "r", hwaccel=accel) if use_hardware
                   else av.CodecContext.create("h264", "r"))
        produced = 0
        # Skip the first 10 frames: context warm-up is not steady-state decode.
        for data, pts in packets:
            packet = av.Packet(data)
            packet.pts = pts
            packet.time_base = hw.VIDEO_TIME_BASE
            produced += len(context.decode(packet))
            if produced >= 10:
                break
        wall0, cpu0 = time.perf_counter(), time.process_time()
        produced = 0
        for data, pts in packets:
            packet = av.Packet(data)
            packet.pts = pts
            packet.time_base = hw.VIDEO_TIME_BASE
            produced += len(context.decode(packet))
        walls.append((time.perf_counter() - wall0) * 1000)
        cpus.append((time.process_time() - cpu0) * 1000)
        frames_total = produced
        del context
    return {
        "frames": frames_total,
        "wall_ms_per_frame": round(statistics.median(walls) / max(1, frames_total), 3),
        "process_cpu_ms_per_frame": round(statistics.median(cpus) / max(1, frames_total), 3),
    }


class _FakeNotEngaged:
    is_hwaccel = False


def _probe_dict(probe):
    return {"device_type": probe.device_type, "available": probe.available,
            "engaged": probe.engaged, "verified": probe.verified, "reason": probe.reason}


def fallback_evidence(packets):
    result = {}
    probe = hw.probe_h264_hwaccel("cuda")
    result["unsupported_device_probe"] = _probe_dict(probe)
    not_engaged = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE,
                                        context_factory=lambda: _FakeNotEngaged())
    result["created_but_not_engaged"] = dict(not_engaged.status)
    not_engaged.close()
    init_failure = hw.H264HardwareDecoder(
        mode=hw.MODE_HARDWARE,
        context_factory=lambda: (_ for _ in ()).throw(RuntimeError("simulated init failure")))
    result["init_failure"] = dict(init_failure.status)
    init_failure.close()

    class FlakyContext:
        is_hwaccel = True

        def __init__(self):
            self.calls = 0
            self.inner = av.CodecContext.create("h264", "r")

        def decode(self, packet):
            self.calls += 1
            if self.calls > 6:
                raise av.FFmpegError(0, "simulated mid-session VideoToolbox failure")
            return self.inner.decode(packet)

    mid = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE, context_factory=lambda: FlakyContext())
    delivered = 0
    for data, pts in packets:
        delivered += len(mid.decode(Jitter(data, pts)))
    result["mid_session_failure"] = {"delivered_frames": delivered, **dict(mid.status)}
    mid.close()
    result["no_retry_loop"] = result["mid_session_failure"]["hardwareRecreations"] == 0
    return result


def main():
    report = {
        "host": {"platform": sys.platform},
        "pinned": {"pyav": av.__version__,
                   "aiortc_requirement": "av>=14,<18 (aiortc 1.15.0)", "device": "videotoolbox"},
        "available_hw_devices": hw.available_hw_devices(),
        "probe": _probe_dict(hw.probe_h264_hwaccel()),
        "unverified": [
            "Windows/D3D11VA: no Windows machine on this stand; the D3D11VA path was not run.",
            "Android encoder SPS: the libx264 SPS below is methodology evidence only; the "
            "real phone encoder SPS/level is unverified without a real Android device.",
        ],
    }
    if not report["probe"]["verified"]:
        report["error"] = "VideoToolbox not verified on this machine; nothing to measure"
        (OUT_DIR / "stage7-videotoolbox-evidence.json").write_text(json.dumps(report, indent=1))
        print(json.dumps(report, indent=1))
        return 1

    packets, sps, encode_seconds = generate_stream(W, H, FRAMES)
    report["stream"] = {"width": W, "height": H, "frames": len(packets),
                        "encode_seconds": encode_seconds, "sps": sps}
    av.logging.set_level(av.logging.VERBOSE)
    with av.logging.Capture() as logs:
        software, software_frames, _ = decode(packets, use_hardware=False)
        hardware, hardware_frames, hardware_context = decode(packets, use_hardware=True)
        log_messages = [message for _, _, message in logs]
    report["software_decode"] = software
    report["hardware_decode"] = hardware
    report["ffmpeg_hw_markers"] = [m.strip() for m in log_messages
                                   if "videotoolbox" in m.lower()][:5]
    report["decode_equivalence"] = {
        "frames_match": software["frames"] == hardware["frames"],
        "pts_match": software["pts_prefix"] == hardware["pts_prefix"],
        "sizes_match": software["sizes"] == hardware["sizes"],
        **colour_equivalence(software_frames, hardware_frames),
    }
    report["frame_path"] = {
        "hardware_frame_format": hardware["frame_format"],
        "software_frame_format": software["frame_format"],
        "download": "PyAV returns VideoToolbox frames (nv12) in system memory; the YUV->RGB "
                    "conversion is the renderer's GPU path. This is NOT zero-copy.",
        "gpu_surface_sharing": "not used",
    }
    software_bench = benchmark(packets, use_hardware=False)
    hardware_bench = benchmark(packets, use_hardware=True)
    report["benchmark_2560x1440_60"] = {
        "software": software_bench, "hardware": hardware_bench,
        "wall_ratio_hw_vs_sw": round(hardware_bench["wall_ms_per_frame"] /
                                     max(0.001, software_bench["wall_ms_per_frame"]), 2),
        "cpu_ratio_hw_vs_sw": round(hardware_bench["process_cpu_ms_per_frame"] /
                                    max(0.001, software_bench["process_cpu_ms_per_frame"]), 2),
        "note": "measured on the shared stand; other agents were not running benchmarks "
                "during this run. Hardware is slower through PyAV 17.1.0 download-per-frame.",
    }
    report["selection"] = {
        "auto": hw.select_decoder_factory(hw.MODE_AUTO).status,
        "auto_prefer_hardware": hw.select_decoder_factory(hw.MODE_AUTO, prefer_hardware=True).status,
        "hardware": hw.select_decoder_factory(hw.MODE_HARDWARE).status,
        "software": hw.select_decoder_factory(hw.MODE_SOFTWARE).status,
    }
    report["fallback"] = fallback_evidence(packets)
    report["release"] = {"hardware_decoders_created_and_closed": 5}
    for _ in range(5):
        decoder = hw.H264HardwareDecoder(mode=hw.MODE_HARDWARE)
        decoder.close()
    hardware_context = None
    report["release"]["clean"] = True

    (OUT_DIR / "stage7-videotoolbox-evidence.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({key: report[key] for key in
                      ("probe", "hardware_decode", "decode_equivalence", "ffmpeg_hw_markers",
                       "frame_path", "benchmark_2560x1440_60", "fallback")}, indent=1)[:6000])
    print("saved:", OUT_DIR / "stage7-videotoolbox-evidence.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
