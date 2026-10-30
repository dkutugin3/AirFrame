#!/usr/bin/env python3
"""Synthetic remote sender process for the §13.B acceptance harness.

In the real product the H.264 encoder runs on the Android phone, so the acceptance
harness must not run the source in the same process as the receiver: that would inflate
the app CPU/RSS measurement and starve the app with GIL contention. This process is the
"phone": it streams the machine-readable `BenchVideoTrack` source (1920x1080@60 by
default) over the real localhost signaling + WebRTC path.

Not a GUI tool; headless. Started by scripts/acceptance-60fps.py, which passes the room
token via the AIRFRAME_ACCEPTANCE_TOKEN environment variable and expects a status JSON
file so it can fail closed if the source never starts.

Exit codes: 0 stopped cleanly; 2 signaling/WebRTC failure (status file has the reason).
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import av

# The synthetic "phone" must sustain 60 new frames/s; default libx264 preset 'medium'
# cannot encode 1080p60 in a Python loop on CPU. This only changes the SENDER-side test
# encoder (a real phone uses a hardware encoder), and it is recorded in the report.
_ORIGINAL_CREATE = av.CodecContext.create


class _RealtimeEncoderProxy:
    def __init__(self, context):
        object.__setattr__(self, '_context', context)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_context'), name)

    def __setattr__(self, name, value):
        if name == 'options':
            value = dict(value)
            value['preset'] = 'ultrafast'
            value['x264-params'] = 'bframes=0:no-scenecut=1'
        setattr(object.__getattribute__(self, '_context'), name, value)


def _conditional_create(codec, mode):
    context = _ORIGINAL_CREATE(codec, mode)
    if codec == 'libx264' and mode == 'w':
        return _RealtimeEncoderProxy(context)
    return context


av.CodecContext.create = staticmethod(_conditional_create)

from aiohttp import ClientSession
from aiortc import (RTCConfiguration, RTCPeerConnection, RTCSessionDescription,
                    RTCRtpSender)

from scripts.bench_source import BenchScene, BenchVideoTrack


def write_status(path, payload):
    if not path:
        return
    Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')


async def run(args, status):
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    track = BenchVideoTrack(BenchScene(args.width, args.height, args.fps))
    peer.addTrack(track)
    capabilities = [codec for codec in RTCRtpSender.getCapabilities('video').codecs
                    if codec.mimeType == 'video/H264']
    if not capabilities:
        status['error'] = 'no H.264 codec capability in aiortc'
        return 2
    peer.getTransceivers()[0].setCodecPreferences(capabilities)
    token = os.environ.get('AIRFRAME_ACCEPTANCE_TOKEN', '')
    try:
        async with ClientSession() as client:
            async with client.ws_connect(
                    f'http://127.0.0.1:{args.port}/signal') as ws:
                await ws.send_json({'role': 'sender', 'credential': token})
                status['authenticated'] = (await ws.receive_json()).get('type')
                status['ready'] = (await ws.receive_json()).get('type')
                await peer.setLocalDescription(await peer.createOffer())
                status['offerCodecs'] = [line for line in peer.localDescription.sdp.splitlines()
                                         if line.startswith('a=rtpmap')]
                await ws.send_json({'type': 'offer', 'session': args.session,
                                    'sdp': peer.localDescription.sdp})
                answer = None
                while answer is None:
                    message = await ws.receive_json()
                    if message.get('type') == 'answer':
                        answer = message
                    else:
                        status.setdefault('ignoredControlMessages', []).append(
                            message.get('type'))
                await peer.setRemoteDescription(
                    RTCSessionDescription(type='answer', sdp=answer['sdp']))
                status['negotiated'] = [line for line in answer['sdp'].splitlines()
                                        if line.startswith('a=rtpmap')]
                status['streaming'] = True
                status['streamingAt'] = time.time()
                write_status(args.status_file, status)

                async def drain():
                    # Keep receiving so aiohttp can answer WebSocket heartbeats.
                    try:
                        async for message in ws:
                            status['receivedMessages'] = (
                                status.get('receivedMessages', []) + [
                                    getattr(message, 'type', '?')])[-40:]
                    except Exception:
                        pass

                drain_task = asyncio.create_task(drain())
                try:
                    if args.duration > 0:
                        await asyncio.sleep(args.duration)
                    else:
                        while True:
                            await asyncio.sleep(.2)
                finally:
                    drain_task.cancel()
    except Exception as error:
        status['error'] = repr(error)
        write_status(args.status_file, status)
        return 2
    finally:
        await peer.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--session', default='acceptance')
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--duration', type=float, default=0.0,
                        help='0 = stream until terminated')
    parser.add_argument('--status-file', default=None)
    args = parser.parse_args(argv)
    status = {'pid': os.getpid(), 'startedAt': time.time()}
    write_status(args.status_file, status)
    return asyncio.run(run(args, status))


if __name__ == '__main__':
    raise SystemExit(main())
