package video.airframe;

import android.content.Context;
import android.media.projection.MediaProjection;
import org.junit.Test;
import org.webrtc.CapturerObserver;
import org.webrtc.SurfaceTextureHelper;
import org.webrtc.VideoFrame;
import org.webrtc.VideoSink;
import static org.junit.Assert.*;

/** The real capturer lifecycle must never consume the permission a second time. */
public class ScreenCaptureTest {
    static final class Backend implements ScreenCapture.Backend {
        int grants, pauses, resumes, closes, resizes, width, height;
        boolean active;
        public void initialize(SurfaceTextureHelper helper,Context context,VideoSink sink){}
        public void start(int w,int h,Runnable started){grants++;width=w;height=h;started.run();}
        public void resume(int w,int h,Runnable started){resumes++;width=w;height=h;started.run();}
        public void pause(Runnable stopped){pauses++;stopped.run();}
        public void resize(int w,int h,boolean active){resizes++;width=w;height=h;this.active=active;}
        public void close(){closes++;}
        public MediaProjection projection(){return null;}
    }
    private ScreenCapture capturer(Backend backend){
        ScreenCapture capture=new ScreenCapture(backend);
        capture.initialize(null,null,new CapturerObserver(){
            public void onCapturerStarted(boolean success){}
            public void onCapturerStopped(){}
            public void onFrameCaptured(VideoFrame frame){}
        });
        return capture;
    }
    @Test public void reconnectResumesOneGrantAndKeepsFreshDimensions(){
        Backend backend=new Backend();ScreenCapture capture=capturer(backend);
        capture.startCapture(2560,1440,60);
        capture.stopCapture();
        capture.startCapture(1920,1080,60);
        capture.stopCapture();
        capture.startCapture(1080,1920,60);
        assertEquals(1,backend.grants);
        assertEquals(2,backend.pauses);
        assertEquals(2,backend.resumes);
        assertEquals(1080,backend.width);
        assertEquals(1920,backend.height);
        assertEquals(0,backend.closes);
        capture.dispose();assertEquals(1,backend.closes);
    }
    @Test public void stoppingBeforeFirstOfferDoesNotConsumeOrReleasePermission(){
        Backend backend=new Backend();ScreenCapture capture=capturer(backend);
        capture.stopCapture();capture.stopCapture();
        assertEquals(0,backend.grants);assertEquals(0,backend.pauses);
        capture.dispose();capture.dispose();assertEquals(1,backend.closes);
    }
    @Test public void repeatedStartAndStopAreIdempotent(){
        Backend backend=new Backend();ScreenCapture capture=capturer(backend);
        capture.startCapture(1920,1080,60);capture.startCapture(1920,1080,60);
        capture.stopCapture();capture.stopCapture();
        assertEquals(1,backend.grants);assertEquals(0,backend.resumes);
        assertEquals(1,backend.pauses);
    }
    @Test public void resizeWhilePausedDoesNotResumeAndDuplicateFormatDoesNoWork(){
        Backend backend=new Backend();ScreenCapture capture=capturer(backend);
        capture.startCapture(1920,1080,60);
        capture.changeCaptureFormat(1920,1080,30);
        assertEquals(0,backend.resizes);
        capture.stopCapture();capture.changeCaptureFormat(1080,1920,60);
        assertEquals(1,backend.resizes);assertFalse(backend.active);
        assertEquals(0,backend.resumes);assertEquals(1,backend.grants);
    }
    @Test(expected=IllegalStateException.class) public void disposedGrantCannotBeReused(){
        Backend backend=new Backend();ScreenCapture capture=capturer(backend);
        capture.startCapture(1920,1080,60);capture.dispose();
        capture.startCapture(1920,1080,60);
    }
}
