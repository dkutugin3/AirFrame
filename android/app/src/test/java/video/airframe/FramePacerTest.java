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
}
