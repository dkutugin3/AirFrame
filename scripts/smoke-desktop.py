"""Native UI / OBS rendering check using synthetic frames, not an Android benchmark."""
import asyncio
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if not os.environ.get('AIRFRAME_SMOKE_BUNDLED'):
    sys.path.insert(0,str(ROOT))
from aiohttp import ClientSession
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from PySide6.QtWidgets import QApplication
from airframe.desktop import Desktop, Events, Frames, LocalServer

app = QApplication([])
events, frames = Events(), Frames()
server = LocalServer(18765,events,frames)
server.start()
window = Desktop(server)
window.show()
result = {}
finished = threading.Event()


async def synthetic_sender():
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTrack(VideoStreamTrack())
    try:
        async with ClientSession() as session:
            async with session.ws_connect('http://127.0.0.1:18765/signal') as socket:
                await socket.send_json({'role':'sender','credential':server.room.code})
                await socket.receive_json()
                await socket.receive_json()
                await peer.setLocalDescription(await peer.createOffer())
                await socket.send_json({'type':'offer','session':'gui-test','sdp':peer.localDescription.sdp})
                # Signaling now interleaves volume/audio state, so wait for the answer itself.
                while True:
                    answer = await socket.receive_json()
                    if answer.get('type') == 'answer':
                        break
                await peer.setRemoteDescription(RTCSessionDescription(type='answer',sdp=answer['sdp']))
                await asyncio.sleep(8)
    except Exception as exc:
        result['error']=str(exc)
    finally:
        await peer.close()
        finished.set()


worker=threading.Thread(target=lambda:asyncio.run(synthetic_sender()),daemon=True)
try:
    out=ROOT/'artifacts'/'screenshots'
    out.mkdir(parents=True,exist_ok=True)
    app.processEvents()
    window.grab().save(str(out/'desktop.png'))
    worker.start()
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        app.processEvents()
        serial,image=frames.get()
        if image is not None and window.fps.text() not in ('—','0'):
            assert image.width()==640
            window.open_obs()
            app.processEvents()
            window.obs.grab().save(str(out/'obs-synthetic.png'))
            print(f'PASS: native decoded image {image.width()}x{image.height()}, {window.fps.text()} fps, OBS window visible',flush=True)
            break
        if finished.is_set():raise AssertionError(result)
        time.sleep(0.01)
    else:raise AssertionError('No native video: '+str(result)+' '+window.journal.toPlainText())
    while not finished.is_set():app.processEvents();time.sleep(0.01)
    app.processEvents()
    assert frames.get()[1] is None
    print('PASS: disconnect clears native video output',flush=True)
finally:
    window.close();server.close();app.processEvents()
