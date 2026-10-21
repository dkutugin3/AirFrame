"""Synthetic motion over H.264 to native Qt at 1920x880 / 60 target fps.
Ideal local-network QA; not an Android, Wi-Fi or OBS capture benchmark.
"""
import asyncio
from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1]
if not os.environ.get('AIRFRAME_SMOKE_BUNDLED'):sys.path.insert(0,str(ROOT))
from aiohttp import ClientSession
from aiortc import RTCConfiguration,RTCPeerConnection,RTCSessionDescription,VideoStreamTrack,RTCRtpSender
from av import VideoFrame
from PySide6.QtWidgets import QApplication
from airframe.desktop import Desktop,Events,Frames,LocalServer

JITTER = os.environ.get("AIRFRAME_SMOKE_JITTER") == "1"

class Motion(VideoStreamTrack):
    def __init__(self):
        super().__init__();self.index=0;self.next=0;self.started=None
        self.patterns=[]
        for n in range(30):
            row=bytes(210 if (x//32+n)%8==0 else 48 for x in range(1920))
            self.patterns.append(row*880)
    async def recv(self):
        loop=asyncio.get_running_loop()
        if JITTER:
            if self.started is None:self.started=loop.time()
            await asyncio.sleep(max(0,self.started+self.index/60-loop.time()))
            if self.index in (120,240,360):await asyncio.sleep(.225)
        else:
            await asyncio.sleep(max(0,self.next-loop.time()))
            self.next=max(self.next+1/60,loop.time()+1/120)
        frame=VideoFrame(1920,880,'yuv420p')
        frame.planes[0].update(self.patterns[self.index%30])
        for plane in frame.planes[1:]:plane.update(bytes([128])*plane.buffer_size)
        frame.pts=self.index*1500;frame.time_base=Fraction(1,90000);self.index+=1
        return frame

app=QApplication([]);events,frames=Events(),Frames();server=LocalServer(18767,events,frames)
server.start();server.room.settings["fps"]=60;window=Desktop(server);window.show();metrics=[];error=[];done=threading.Event()
events.stats.connect(lambda stats: metrics.append(stats) if stats.get('fps',0)>0 else None)
async def send():
    peer=RTCPeerConnection(RTCConfiguration(iceServers=[]));peer.addTrack(Motion())
    peer.getTransceivers()[0].setCodecPreferences([c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType=='video/H264'])
    try:
        async with ClientSession() as client:
            async with client.ws_connect('http://127.0.0.1:18767/signal') as ws:
                await ws.send_json({'role':'sender','credential':server.room.qr_token});await ws.receive_json();await ws.receive_json()
                await peer.setLocalDescription(await peer.createOffer())
                await ws.send_json({'type':'offer','session':'motion','sdp':peer.localDescription.sdp})
                answer=await ws.receive_json()
                await peer.setRemoteDescription(RTCSessionDescription(type='answer',sdp=answer['sdp']))
                await asyncio.sleep(10)
    except Exception as exc:error.append(repr(exc))
    finally:await peer.close();done.set()
thread=threading.Thread(target=lambda:asyncio.run(send()),daemon=True)
try:
    thread.start();deadline=time.monotonic()+22
    obs_open=False;paint_started=0;paint_baseline=0
    while not done.is_set() and time.monotonic()<deadline:
        app.processEvents()
        if frames.get()[1] is not None and not obs_open:
            window.open_obs();obs_open=True
            paint_started=time.monotonic();paint_baseline=window.obs.centralWidget().painted_frames
        time.sleep(.002)
    assert done.is_set() and not error,error
    assert len(metrics)>=5,metrics
    stable=metrics[2:-1]
    report={key:round(sum(m[key] for m in stable)/len(stable),2) for key in ('fps','convertedFps','skipped','conversionMs','frameGapMs')}
    report['nativePaintFps']=round((window.obs.centralWidget().painted_frames-paint_baseline)/max(.001,time.monotonic()-paint_started),2)
    report['maxPacketGapMs']=round(max(m.get('packetGapMs',0) for m in metrics),2)
    report['maxOutputGapMs']=round(max(m.get('outputGapMs',0) for m in metrics),2)
    report['playoutUnderflows']=max(m.get('playoutUnderflows',0) for m in metrics)
    report['targetMs']=max(m.get('playoutTargetMs',0) for m in metrics)
    out=ROOT/('artifacts/v0.3.0/jitter-qa.json' if JITTER else 'artifacts/v0.3.0/motion-qa.json');out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2))
    print(('Burst/225ms pauses: ' if JITTER else '')+'Synthetic 1920x880 H264 motion / target 60 fps:',json.dumps(report),flush=True)
    assert report['fps']>=55 and report['nativePaintFps']>=55,'Slow synthetic pipeline; see report'
    if JITTER:
        assert report['maxPacketGapMs']>=180,'Test did not inject a real RTP pause'
        assert report['maxOutputGapMs']<80,'Network pauses reached the output'
        assert report['playoutUnderflows']==0,'Playout buffer ran dry' 
finally:
    window.close();server.close();app.processEvents()
