import random
from fractions import Fraction
import unittest

import av
from aiortc.codecs import depayload
from aiortc.codecs.h264 import H264Decoder, H264Encoder
from aiortc.jitterbuffer import JitterBuffer
from aiortc.rtp import RtpPacket
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from airframe.transport import VideoBuffer


def packet(sequence, timestamp, data):
    result=RtpPacket(sequence_number=sequence%65536,timestamp=timestamp,payload_type=96)
    result._data=data
    return result


def large_h264_packet():
    codec=av.CodecContext.create('libx264','w')
    codec.width=640;codec.height=360;codec.pix_fmt='yuv420p'
    codec.time_base=Fraction(1,90000);codec.framerate=Fraction(30)
    codec.options={'preset':'ultrafast','tune':'zerolatency','crf':'23'}
    frame=av.VideoFrame(640,360,'yuv420p');frame.pts=0
    frame.planes[0].update(random.Random(7).randbytes(frame.planes[0].buffer_size))
    for plane in frame.planes[1:]:plane.update(bytes([128])*plane.buffer_size)
    return codec,frame,codec.encode(frame)[0]


class VideoBufferTests(unittest.TestCase):
    def test_large_frame_is_not_truncated_on_lossless_link(self):
        expected=b''.join(bytes([i%256])*1200 for i in range(300))
        buffer=VideoBuffer();frames=[];pli_count=0
        for i in range(301):
            pli,frame=buffer.add(packet(65400+i,100 if i<300 else 200,expected[i*1200:(i+1)*1200] if i<300 else b'next'))
            pli_count+=pli
            if frame is not None:frames.append(frame)
        self.assertEqual(pli_count,0)
        self.assertEqual(len(frames),1)
        self.assertEqual(frames[0].data,expected)
        self.assertEqual(buffer.completed,1)
        self.assertGreater(buffer.interval()['largestFrameKiB'],300)

    def test_reordering_waits_for_missing_packet_without_extra_prefetch(self):
        buffer=VideoBuffer()
        self.assertIsNone(buffer.add(packet(0,100,b'a'))[1])
        self.assertIsNone(buffer.add(packet(2,100,b'c'))[1])
        self.assertIsNone(buffer.add(packet(3,200,b'd'))[1])
        pli,frame=buffer.add(packet(1,100,b'b'))
        self.assertFalse(pli)
        self.assertEqual(frame.data,b'abc')

    def test_bounded_capacity_and_recovery_on_actual_overflow(self):
        buffer=VideoBuffer();pli=False
        for i in range(2050):
            flag,_=buffer.add(packet(i,100,b'x'))
            pli|=flag
        self.assertTrue(pli)
        self.assertEqual(buffer.capacity,2048)
        self.assertEqual(len(buffer._packets),2048)
        self.assertGreater(buffer.interval()['bufferRecoveries'],0)

    def test_real_h264_keyframe_larger_than_128_packets_decodes_intact(self):
        _,_,encoded=large_h264_packet()
        payloads,timestamp=H264Encoder().pack(encoded)
        self.assertGreater(len(payloads),128)
        codec=RTCRtpCodecParameters(mimeType='video/H264',clockRate=90000,payloadType=96)
        buffer=VideoBuffer();old=JitterBuffer(128,is_video=True)
        frames=[];old_frames=[];flags=old_flags=0
        for i in range(len(payloads)+1):
            # The next timestamp completes assembly, exactly as in aiortc's receiver.
            data=depayload(codec,payloads[i]) if i<len(payloads) else b'next'
            p=packet(i,timestamp if i<len(payloads) else timestamp+3000,data)
            pli,frame=buffer.add(p);old_pli,old_frame=old.add(p)
            flags+=pli;old_flags+=old_pli
            if frame is not None:frames.append(frame)
            if old_frame is not None:old_frames.append(old_frame)
        self.assertEqual(flags,0)
        self.assertGreater(old_flags,0)
        self.assertEqual(len(frames),1)
        self.assertNotEqual(old_frames[0].data,frames[0].data)
        decoded=H264Decoder().decode(frames[0])
        self.assertEqual(len(decoded),1)
        self.assertEqual((decoded[0].width,decoded[0].height),(640,360))
        self.assertGreater(len(frames[0].data),160000)
