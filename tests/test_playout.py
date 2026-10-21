from dataclasses import dataclass
from fractions import Fraction
from types import SimpleNamespace
import unittest

from airframe.playout import Playout


@dataclass
class Frame:
    pts: int
    time_base: Fraction = Fraction(1,60)
    width: int = 1920
    height: int = 886


def simulate(mode, fps=60, count=240):
    buffer=Playout(mode,fps=fps)
    arrivals=[]
    for i in range(count):
        source=i/fps
        arrival=source+.005
        for start,end in ((.6,.825),(1.5,1.65),(2.3,2.525)):
            if start<=source<end:arrival=end+.005
        arrivals.append((arrival,i))
    arrivals.sort()
    output=[];index=0;peak=0
    # Virtual clock: no timing assertions depend on OS scheduling.
    for tick in range(int((count/fps+.6)*2000)):
        now=tick/2000
        while index<len(arrivals) and arrivals[index][0]<=now:
            _,i=arrivals[index];buffer.push(Frame(i,Fraction(1,fps)),now);index+=1
        item,_=buffer.take(now)
        if item is not None:output.append((now,item.frame.pts))
        peak=max(peak,len(buffer.frames))
    return buffer,output,peak


class PlayoutTests(unittest.TestCase):
    def test_225ms_network_pauses_and_catchup_bursts_are_smoothed(self):
        for fps in (30,60):
            buffer,output,peak=simulate('stable',fps)
            self.assertEqual([i for _,i in output],list(range(240)))
            gaps=[b[0]-a[0] for a,b in zip(output,output[1:])]
            self.assertLessEqual(max(gaps),1/fps+.001)
            self.assertGreaterEqual(output[0][0],.250)
            self.assertEqual(buffer.skipped,0)
            self.assertLessEqual(peak,buffer.max_frames)

    def test_balanced_mode_buffers_half_of_stable(self):
        for fps in (30,60):
            buffer,output,peak=simulate('balanced',fps)
            frames=[i for _,i in output]
            self.assertEqual(frames,sorted(set(frames)))
            self.assertGreaterEqual(output[0][0],.120)
            self.assertLess(output[0][0],.250)
            self.assertLessEqual(peak,buffer.max_frames)
            self.assertLessEqual(buffer.target,.200)
            # 225 ms pauses exceed the reserve: trimmed, but still visible.
            gaps=[b[0]-a[0] for a,b in zip(output,output[1:])]
            self.assertLessEqual(max(gaps),.150)
            self.assertGreaterEqual(len(frames),238)

    def test_low_latency_remains_immediate_and_exposes_network_pause(self):
        _,output,peak=simulate('low')
        self.assertLess(output[0][0],.010)
        self.assertLessEqual(peak,1)
        self.assertGreater(max(b[0]-a[0] for a,b in zip(output,output[1:])),.200)

    def test_slow_consumer_skips_late_frames_without_rushing_a_backlog(self):
        buffer=Playout()
        for i in range(100):buffer.push(Frame(i),i/60)
        self.assertLessEqual(len(buffer.frames),24)
        self.assertLessEqual(buffer.bytes,buffer.MAX_BYTES)
        item,_=buffer.take(1.7)
        self.assertIsNotNone(item)
        next_item,delay=buffer.take(1.7)
        self.assertIsNone(next_item)
        self.assertGreater(delay,0)
        self.assertGreater(buffer.skipped,70)

    def test_clock_reset_rotation_and_mode_switch_clear_old_frames(self):
        buffer=Playout()
        buffer.push(Frame(100),0)
        buffer.push(Frame(101),.017)
        buffer.push(Frame(0),.034)
        self.assertEqual([p.frame.pts for p in buffer.frames],[0])
        buffer.push(Frame(1,width=886,height=1920),.05)
        self.assertEqual(len(buffer.frames),1)
        self.assertEqual(buffer.rebuffers,2)
        buffer.reset(mode='low')
        self.assertEqual(buffer.bytes,0)
        buffer.push(Frame(2),.06)
        self.assertIsNotNone(buffer.take(.06)[0])

    def test_byte_limit_and_oversized_frame(self):
        buffer=Playout()
        for i in range(40):
            frame=Frame(i);frame.planes=[SimpleNamespace(buffer_size=16*1024*1024)]
            buffer.push(frame,i/60)
            self.assertLessEqual(buffer.bytes,128*1024*1024)
        frame=Frame(40);frame.planes=[SimpleNamespace(buffer_size=129*1024*1024)]
        before=len(buffer.frames);buffer.push(frame,1)
        self.assertEqual(len(buffer.frames),before)

    def test_underflow_is_counted_once_and_adaptation_has_a_ceiling(self):
        buffer=Playout();buffer.push(Frame(0),0)
        self.assertIsNotNone(buffer.take(.25)[0])
        for i in range(30):buffer.take(.3+i*.01)
        self.assertEqual(buffer.underflows,1)
        buffer.push(Frame(1),.5)
        self.assertLessEqual(buffer.target,.350)
        self.assertIsNotNone(buffer.take(.6)[0])
        buffer.push(Frame(2),1)
        self.assertLessEqual(buffer.target,.350)
        self.assertGreater(buffer.rebuffers,0)

    def test_source_clock_drift_does_not_accumulate_unbounded_delay(self):
        buffer=Playout()
        for i in range(10000):
            now=i/61
            buffer.push(Frame(i),now)
            buffer.take(now)
            if buffer.frames:
                self.assertLessEqual(buffer.phase+buffer.frames[-1].media-now,buffer.max_delay+.011)
            self.assertLessEqual(len(buffer.frames),buffer.max_frames)
        self.assertGreater(buffer.rebuffers,0)
