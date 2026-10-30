package video.airframe;
import org.junit.Test;
import static org.junit.Assert.*;
public class FramePacerTest {
    @Test public void limits120HzCapture(){
        for(int fps:new int[]{30,60}){
            FramePacer pacer=new FramePacer();int count=0;
            for(int i=0;i<1200;i++)if(pacer.accept(1000000000L+i*1000000000L/120,fps))count++;
            assertTrue("fps="+fps+" count="+count,Math.abs(count-fps*10)<=1);
        }
    }
    @Test public void recoversAfterPauseAndTimestampReset(){
        FramePacer p=new FramePacer();assertTrue(p.accept(1000000000L,60));
        assertFalse(p.accept(1000000001L,60));assertTrue(p.accept(5000000000L,30));
        assertTrue(p.accept(1,30));
    }

    private static final long NANOS_PER_SECOND=1000000000L;

    /** The experimental 120 FPS mode must keep every 120 Hz frame (plan stage 5). */
    @Test public void preservesExperimental120HzMode(){
        FramePacer pacer=new FramePacer();int count=0;
        for(int i=0;i<1200;i++)if(pacer.accept(1000000000L+i*NANOS_PER_SECOND/120,120))count++;
        assertTrue("120 fps count="+count,Math.abs(count-1200)<=1);
    }

    /**
     * ScreenCapturerAndroid can restart its capture timestamps at ~1 ns after a screen rotation or
     * capture restart. The first frame of the new scale is real and must pass, the next 1 ns later
     * must be paced out, and pacing must resume one target period later instead of keeping the stale
     * `next` from the previous ~5 s scale.
     */
    @Test public void timestampResetDoesNotKeepOldScale(){
        FramePacer p=new FramePacer();
        long lastAccepted=0;
        for(int i=0;i<240;i++){
            long now=4900000000L+i*NANOS_PER_SECOND/120;
            if(p.accept(now,60))lastAccepted=now;
        }
        assertTrue("pacing was active before the reset",lastAccepted>=4900000000L);
        assertTrue("first frame after the timestamp reset",p.accept(1,60));
        assertFalse("frame 1 ns later must be paced out",p.accept(2,60));
        assertTrue("frame one target period later",p.accept(1+NANOS_PER_SECOND/60,60));
    }

    /** After a timestamp reset the pacer keeps limiting the new stream, not just the first frame. */
    @Test public void recoversPacingAfterTimestampReset(){
        FramePacer p=new FramePacer();
        for(int i=0;i<240;i++)p.accept(4900000000L+i*NANOS_PER_SECOND/120,60);
        assertTrue(p.accept(1,60));
        int count=0;
        for(int i=0;i<120;i++)if(p.accept(1+i*NANOS_PER_SECOND/120,60))count++;
        assertTrue("accepted after reset="+count,Math.abs(count-60)<=1);
    }

    /** 30 -> 60 -> 120 -> 60 mode changes on a live 120 Hz source follow each target rate. */
    @Test public void fpsChangesFollowEachMode(){
        int[] modes={30,60,120,60};int[] expected={60,120,240,120};
        FramePacer p=new FramePacer();
        for(int phase=0;phase<modes.length;phase++){
            int count=0;
            for(int i=0;i<240;i++){
                long now=1000000000L+(phase*240+i)*NANOS_PER_SECOND/120;
                if(p.accept(now,modes[phase]))count++;
            }
            assertTrue("phase "+phase+" mode="+modes[phase]+" count="+count,
                    Math.abs(count-expected[phase])<=2);
        }
    }

    /**
     * A long pause is not a licence to release a burst: the first frame after the gap is accepted,
     * the next 120 Hz frame must still be paced out and pacing resumes one target period later.
     */
    @Test public void longPauseDoesNotBurstFrames(){
        FramePacer p=new FramePacer();
        assertTrue(p.accept(1000000000L,60));
        assertTrue("first frame after a 3 s pause",p.accept(4000000000L,60));
        assertFalse("next 120 Hz frame after the pause",p.accept(4000000000L+NANOS_PER_SECOND/120,60));
        assertTrue("frame one target period after the pause",p.accept(4000000000L+NANOS_PER_SECOND/60,60));
    }

    /**
     * Irregular capture timing must never exceed the target rate and must not invent frames: only
     * timestamps of supplied frames are accepted, and the accepted rate stays near the target.
     */
    @Test public void irregularInputNeverExceedsTargetRate(){
        FramePacer p=new FramePacer();
        int[] deltas={5000000,12000000,7000000,9000000,11000000,4000000};
        long now=1000000000L,firstAccepted=0,lastAccepted=0;int accepted=0;
        for(int i=0;i<1200;i++){
            if(p.accept(now,60)){
                if(accepted==0)firstAccepted=now;
                lastAccepted=now;accepted++;
            }
            now+=deltas[i%deltas.length];
        }
        double duration=(lastAccepted-firstAccepted)/1e9;
        assertTrue("accepted timestamps must move forward",lastAccepted>firstAccepted);
        assertTrue("accepted="+accepted+" duration="+duration,
                accepted<=Math.ceil(60*duration)+1);
        assertTrue("accepted="+accepted+" duration="+duration,accepted>=55*duration);
    }
}
