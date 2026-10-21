"""Native UI regression: QR pixels, profile button -> WS -> ACK, and frame lifetime.
Run using the Docker-built macOS bundle's Python. No Android benchmark is implied.
"""
import asyncio
import gc
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if not os.environ.get('AIRFRAME_SMOKE_BUNDLED'):sys.path.insert(0,str(ROOT))
from aiohttp import ClientSession
from av import VideoFrame
from PySide6.QtWidgets import QApplication,QScrollArea
from PySide6.QtGui import QColor
from airframe.desktop import Desktop,Events,Frames,LocalServer

app=QApplication([])
events,frames=Events(),Frames()
server=LocalServer(18766,events,frames);server.start()
window=Desktop(server);window.show()
ready=threading.Event();done=threading.Event();release=threading.Event();result={}

async def sender():
    try:
        async with ClientSession() as client:
            async with client.ws_connect('http://127.0.0.1:18766/signal') as ws:
                await ws.send_json({'role':'sender','credential':server.room.qr_token})
                assert (await ws.receive_json())['type']=='authenticated'
                await ws.receive_json();ready.set()
                message=await ws.receive_json(timeout=10)
                assert message['type']=='configure'
                assert message['settings']=={'fps':60,'maxSize':1920,'bitrate':30}
                assert message['requestId']
                await ws.send_json({'type':'configured','requestId':message['requestId'],'settings':message['settings'],'applied':True})
                while not release.is_set():await asyncio.sleep(.02)
    except Exception as error:result['error']=repr(error)
    finally:done.set()

def until(predicate,seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        app.processEvents()
        if predicate():return
        if result:raise AssertionError(result)
        time.sleep(.01)
    raise AssertionError('Timeout: '+window.journal.toPlainText())

worker=threading.Thread(target=lambda:asyncio.run(sender()),daemon=True)
try:
    # The QR updates with code rotation; no secret is printed.
    before=window.qr_payload
    assert window.qr.pixmap().width()>=180
    server.rotate()
    until(lambda:server.room.qr_token not in before)
    window.refresh_code();assert window.qr_payload!=before
    (ROOT/'artifacts/v0.3.0/screenshots').mkdir(parents=True,exist_ok=True)
    window.qr.pixmap().save(str(ROOT/'artifacts/v0.3.0/screenshots/qr-test.png'))
    worker.start();until(ready.is_set)
    window.target_fps.setCurrentIndex(1);window.max_size.setCurrentIndex(1);window.rate.setValue(30)
    window.apply_button.click()
    assert not window.apply_button.isEnabled()
    until(lambda:'подтвердил' in window.profile_result.text())
    assert window.apply_button.isEnabled()
    print('PASS: QR auth; actual Apply button -> sender settings -> confirmation',flush=True)
    window.playout_mode.setCurrentIndex(1)
    until(lambda:server.receiver.playout.mode=='low')
    assert server.receiver.playout.target==0
    window.playout_mode.setCurrentIndex(0)
    until(lambda:server.receiver.playout.mode=='stable')
    assert server.receiver.playout.target==.25
    print('PASS: native playout selector switches low/stable without Android renegotiation',flush=True)
    # Both UI surfaces keep an old buffer valid after a new frame replaces it.
    notices=[]
    frames.notice.changed.connect(lambda:notices.append(1))
    frame=VideoFrame(1920,880,'yuv420p')
    frames.put(frame);del frame;gc.collect()
    old=frames.get()[1];expected=old.pixelColor(10,10)
    for _ in range(20):frames.put(VideoFrame(1920,880,'yuv420p'))
    gc.collect();assert old.pixelColor(10,10)==expected
    window.open_obs();app.processEvents()
    assert len(notices)==1, 'Frame notifications accumulated in the GUI queue'
    print('PASS: 21 frame writes coalesce into one GUI notification',flush=True)
    window.findChildren(QScrollArea)[0].ensureWidgetVisible(window.playout_mode)
    app.processEvents()
    window.grab().save(str(ROOT/'artifacts/v0.3.0/screenshots/desktop-quality.png'))
    print('PASS: 1920x880 shared-buffer lifetime and native OBS painting',flush=True)
    release.set();until(done.is_set);until(lambda:'sender' not in server.room.peers)
    window.apply_button.click()
    until(lambda:'Сохранено' in window.profile_result.text())
    print('PASS: offline profile saved for the next phone connection',flush=True)
finally:
    release.set();window.close();server.close();app.processEvents()
