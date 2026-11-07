package video.airframe;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.view.Surface;
import org.webrtc.CapturerObserver;
import org.webrtc.SurfaceTextureHelper;
import org.webrtc.ThreadUtils;
import org.webrtc.VideoCapturer;
import org.webrtc.VideoFrame;
import org.webrtc.VideoSink;

/** One permission grant / virtual display for the service lifetime, with a detachable surface.
 * ScreenCapturerAndroid.stopCapture() ends its projection and cannot be restarted with the same
 * permission on Android 14+. An idle receiver must only pause the virtual display instead.
 * All platform resources are serialized on SurfaceTextureHelper's thread.
 */
final class ScreenCapture implements VideoCapturer,VideoSink {
    interface Backend {
        void initialize(SurfaceTextureHelper helper,Context context,VideoSink sink);
        void start(int width,int height,Runnable onStarted);
        void resume(int width,int height,Runnable onStarted);
        void pause(Runnable onStopped);
        void resize(int width,int height,boolean active);
        void close();
        MediaProjection projection();
    }
    private final Backend backend;
    private CapturerObserver observer;
    private boolean created,active,disposed;
    private int width,height;

    ScreenCapture(Intent permission,MediaProjection.Callback callback){
        this(new AndroidBackend(permission,callback));
    }
    ScreenCapture(Backend backend){this.backend=backend;}
    @Override public void initialize(SurfaceTextureHelper helper,Context context,CapturerObserver observer){
        this.observer=observer;backend.initialize(helper,context,this);
    }
    @Override public void startCapture(int width,int height,int fps){
        if(disposed)throw new IllegalStateException("capturer is disposed");
        if(active){changeCaptureFormat(width,height,fps);return;}
        Runnable started=()->observer.onCapturerStarted(true);
        if(created)backend.resume(width,height,started);else backend.start(width,height,started);
        this.width=width;this.height=height;created=true;active=true;
    }
    @Override public void stopCapture(){
        if(!active)return;
        backend.pause(observer::onCapturerStopped);active=false;
    }
    @Override public void changeCaptureFormat(int width,int height,int fps){
        if(created && (this.width!=width || this.height!=height)){
            backend.resize(width,height,active);this.width=width;this.height=height;
        }
    }
    @Override public void dispose(){
        if(disposed)return;
        stopCapture();backend.close();disposed=true;
    }
    @Override public boolean isScreencast(){return true;}
    @Override public void onFrame(VideoFrame frame){observer.onFrameCaptured(frame);}
    MediaProjection getMediaProjection(){return backend.projection();}

    private static final class AndroidBackend implements Backend {
        private static final int DPI=400;
        private final Intent permission;
        private final MediaProjection.Callback callback;
        private SurfaceTextureHelper helper;
        private MediaProjectionManager manager;
        private MediaProjection projection;
        private VirtualDisplay display;
        private Surface surface;
        private VideoSink sink;
        AndroidBackend(Intent permission,MediaProjection.Callback callback){
            this.permission=permission;this.callback=callback;
        }
        public void initialize(SurfaceTextureHelper helper,Context context,VideoSink sink){
            this.helper=helper;this.sink=sink;
            manager=(MediaProjectionManager)context.getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        }
        private void run(Runnable task){ThreadUtils.invokeAtFrontUninterruptibly(helper.getHandler(),task);}
        public void start(int width,int height,Runnable onStarted){run(()->{
            projection=manager.getMediaProjection(Activity.RESULT_OK,permission);
            projection.registerCallback(callback,helper.getHandler());
            helper.setTextureSize(width,height);
            surface=new Surface(helper.getSurfaceTexture());
            createDisplay(width,height);
            onStarted.run();helper.startListening(sink);
        });}
        public void resume(int width,int height,Runnable onStarted){run(()->{
            helper.setTextureSize(width,height);resizeDisplay(width,height);
            display.setSurface(surface);onStarted.run();helper.startListening(sink);
        });}
        public void pause(Runnable onStopped){run(()->{
            display.setSurface(null);helper.stopListening();onStopped.run();
        });}
        public void resize(int width,int height,boolean active){run(()->{
            helper.setTextureSize(width,height);resizeDisplay(width,height);
            // Preserve a detached surface while the service is waiting for a receiver.
            display.setSurface(active?surface:null);
        });}
        private void createDisplay(int width,int height){
            display=projection.createVirtualDisplay("AirFrame-screen",width,height,DPI,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC|DisplayManager.VIRTUAL_DISPLAY_FLAG_PRESENTATION,
                    surface,null,null);
        }
        private void resizeDisplay(int width,int height){
            if(Build.VERSION.SDK_INT<31){
                // Match the SDK capturer's pre-Android-12 scaling workaround. Recreating
                // a display on the SAME projection is supported on these old versions.
                display.release();createDisplay(width,height);
            }else display.resize(width,height,DPI);
        }
        public void close(){
            if(helper==null)return;
            run(()->{
                helper.stopListening();
                if(display!=null){display.release();display=null;}
                if(surface!=null){surface.release();surface=null;}
                if(projection!=null){
                    projection.unregisterCallback(callback);projection.stop();projection=null;
                }
            });
        }
        public MediaProjection projection(){return projection;}
    }
}
