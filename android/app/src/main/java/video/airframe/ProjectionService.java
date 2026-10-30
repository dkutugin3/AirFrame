package video.airframe;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.projection.MediaProjection;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import org.json.JSONObject;
import org.webrtc.*;
import okhttp3.*;

import java.lang.reflect.Field;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.TimeUnit;

public class ProjectionService extends Service {
    public static volatile boolean running=false;
    public static volatile String status="Готов к подключению";
    private final Handler main=new Handler(Looper.getMainLooper());
    private OkHttpClient http;
    private WebSocket socket;
    private EglBase egl;
    private PeerConnectionFactory factory;
    private ScreenCapturerAndroid capturer;
    private SurfaceTextureHelper texture;
    private VideoSource source;
    private VideoTrack track;
    private PeerConnection peer;
    private RtpSender sender;
    private DataChannel audioChannel;
    private AudioRecord audioCapture;
    private MediaCodec audioEncoder;
    private Thread audioThread;
    private volatile boolean audioEnabled=true, audioRunning=false;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private AudioManager audio;
    private String address, credential;
    private boolean closing=false, remoteSet=false, negotiating=false, restartPending=false;
    private int attempts=0, restarts=0, generation=0;
    /**
     * Base profile for a new session (plan stage 1): 60 FPS / 24 Mbit/s / long side 2560 px.
     * Shared by the peer-ready path (peer sends no settings) and by configure() fallbacks so the
     * Android sender and the Python session defaults agree.
     */
    static final int DEFAULT_FPS=60, DEFAULT_BITRATE=24, DEFAULT_MAX_SIZE=2560;
    private volatile int fps=DEFAULT_FPS;
    private int bitrate=DEFAULT_BITRATE, maxSize=DEFAULT_MAX_SIZE, contentWidth=0, contentHeight=0;
    private String profileRequest="";
    private final List<IceCandidate> pendingIce=new ArrayList<>();
    private Runnable reconnectTask, restartTask, stableTask;
    private long lastFrames=0, lastStatsNs=0;
    private final Runnable statistics=new Runnable(){
        @Override public void run(){
            final PeerConnection current=peer;
            if(current!=null && remoteSet)current.getStats(report -> main.post(() -> {
                if(peer!=current || closing)return;
                for(RTCStats stat:report.getStatsMap().values()){
                    if(!"outbound-rtp".equals(stat.getType()))continue;
                    java.util.Map<String,Object> members=stat.getMembers();
                    if(!"video".equals(members.get("kind")) || !members.containsKey("framesEncoded"))continue;
                    long now=System.nanoTime(), frames=number(members.get("framesEncoded")).longValue();
                    double actual=lastStatsNs==0 ? 0 : Math.max(0,frames-lastFrames)*1e9/Math.max(1,now-lastStatsNs);
                    lastStatsNs=now;lastFrames=frames;
                    send(json("type","telemetry","session",String.valueOf(generation),
                            "encodedFps",actual,"width",number(members.get("frameWidth")).intValue(),
                            "height",number(members.get("frameHeight")).intValue(),
                            "volume",phoneVolume(),
                            "limitation",String.valueOf(members.getOrDefault("qualityLimitationReason","unknown"))));
                }
            }));
            if(!closing)main.postDelayed(this,1000);
        }
    };
    private static Number number(Object value){return value instanceof Number ? (Number)value : 0;}
    private final Runnable renewWake=new Runnable(){
        @Override public void run(){
            if(!closing && wake!=null){wake.acquire(10*60*1000L);main.postDelayed(this,5*60*1000L);}
        }
    };

    private static JSONObject json(Object... pairs) {
        JSONObject result=new JSONObject();
        try {for(int i=0;i<pairs.length;i+=2)result.put((String)pairs[i],pairs[i+1]);}
        catch(Exception e){throw new IllegalArgumentException(e);}
        return result;
    }

    @Override public IBinder onBind(Intent intent) {return null;}

    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        if(intent==null || "STOP".equals(intent.getAction())) {stopSelf();return START_NOT_STICKY;}
        if(running)return START_NOT_STICKY;
        running=true; closing=false;
        NotificationManager notifications=getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("stream","Трансляция экрана",NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop=PendingIntent.getService(this,1,new Intent(this,ProjectionService.class).setAction("STOP"),PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open=PendingIntent.getActivity(this,2,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE);
        Notification notification=new Notification.Builder(this,"stream")
                .setContentTitle("AirFrame · захват экрана")
                .setContentText("Экран передаётся на ваш компьютер")
                .setSmallIcon(video.airframe.R.drawable.ic_airframe)
                .setContentIntent(open).setOngoing(true).addAction(0,"Остановить",stop).build();
        if(Build.VERSION.SDK_INT>=29)startForeground(1,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        else startForeground(1,notification);
        try {
            address=intent.getStringExtra("address"); credential=intent.getStringExtra("code");
            Intent permission=intent.getParcelableExtra("permission");
            if(permission==null || address==null || credential==null)throw new IllegalArgumentException("Нет разрешения на захват");
            PowerManager pm=(PowerManager)getSystemService(POWER_SERVICE);
            wake=pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"airframe:stream");
            wake.setReferenceCounted(false);renewWake.run();
            WifiManager wm=(WifiManager)getApplicationContext().getSystemService(WIFI_SERVICE);
            wifi=wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF,"airframe:wifi");wifi.acquire();
            audio=(AudioManager)getSystemService(AUDIO_SERVICE);
            PeerConnectionFactory.initialize(PeerConnectionFactory.InitializationOptions.builder(this).createInitializationOptions());
            egl=EglBase.create();
            factory=PeerConnectionFactory.builder()
                    .setVideoEncoderFactory(new DefaultVideoEncoderFactory(egl.getEglBaseContext(),true,true))
                    .setVideoDecoderFactory(new DefaultVideoDecoderFactory(egl.getEglBaseContext()))
                    .createPeerConnectionFactory();
            texture=SurfaceTextureHelper.create("AirFrame-capture",egl.getEglBaseContext());
            source=factory.createVideoSource(true);
            capturer=new ScreenCapturerAndroid(permission,new MediaProjection.Callback(){
                @Override public void onStop(){main.post(()->{if(!closing){status="Захват завершён системой. Разрешите его заново.";stopSelf();}});}
                @Override public void onCapturedContentResize(int width,int height){main.post(()->resize(width,height));}
            });
            CapturerObserver downstream=source.getCapturerObserver();
            capturer.initialize(texture,this,new CapturerObserver(){
                private final FramePacer pacer=new FramePacer();
                @Override public void onCapturerStarted(boolean success){downstream.onCapturerStarted(success);}
                @Override public void onCapturerStopped(){downstream.onCapturerStopped();}
                @Override public void onFrameCaptured(VideoFrame frame){
                    if(!pacer.accept(frame.getTimestampNs(),fps))return;
                    downstream.onFrameCaptured(frame);
                }
            });
            int[] size=captureSize(0,0);capturer.startCapture(size[0],size[1],fps);
            source.adaptOutputFormat(size[0],size[1],fps);
            track=factory.createVideoTrack("airframe-screen",source);
            http=new OkHttpClient.Builder().pingInterval(15,TimeUnit.SECONDS).connectTimeout(10,TimeUnit.SECONDS).readTimeout(0,TimeUnit.SECONDS).build();
            connect();
            main.post(statistics);
        }catch(Exception e){status="Ошибка запуска: "+e.getMessage();stopSelf();}
        return START_NOT_STICKY;
    }

    /**
     * Phone audio rides the WebRTC data channel, not an RTP audio track: the pinned
     * io.github.webrtc-sdk has no setAudioRecord(AudioRecord) to feed an AudioPlaybackCapture
     * AudioRecord into JavaAudioDeviceModule. We capture playback PCM ourselves, encode AAC
     * with MediaCodec and send self-describing config+frame messages; the desktop decodes
     * with PyAV. The projection for playback capture is taken from ScreenCapturerAndroid
     * via reflection, so the single projection grant is never consumed twice.
     */
    private MediaProjection projectionForAudio() {
        if(Build.VERSION.SDK_INT<29)return null;
        try{
            Field field=ScreenCapturerAndroid.class.getDeclaredField("mediaProjection");
            field.setAccessible(true);
            return (MediaProjection)field.get(capturer);
        }catch(Exception error){return null;}
    }

    private void startAudio() {
        if(audioRunning || audioChannel==null || audioChannel.state()!=DataChannel.State.OPEN)return;
        if(Build.VERSION.SDK_INT<29)return; // playback capture needs Android 10+
        if(getApplicationContext().checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                !=PackageManager.PERMISSION_GRANTED)return; // feature needs a granted runtime permission
        try{
            MediaProjection projection=projectionForAudio();
            if(projection==null)return;
            audioCapture=new AudioRecord.Builder()
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(48000)
                            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO).build())
                    .setBufferSizeInBytes(8192)
                    .setAudioPlaybackCaptureConfig(new AudioPlaybackCaptureConfiguration.Builder(projection)
                            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                            .addMatchingUsage(AudioAttributes.USAGE_GAME)
                            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN).build())
                    .build();
            audioEncoder=MediaCodec.createEncoderByType("audio/mp4a-latm");
            MediaFormat format=MediaFormat.createAudioFormat("audio/mp4a-latm",48000,2);
            format.setInteger(MediaFormat.KEY_BIT_RATE,96000);
            format.setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC);
            audioEncoder.configure(format,null,null,MediaCodec.CONFIGURE_FLAG_ENCODE);
            audioEncoder.start();
            audioCapture.startRecording();
            audioRunning=true;
            audioThread=new Thread(this::captureAudioLoop,"AirFrame-audio");
            audioThread.start();
        }catch(Exception error){stopAudio();}
    }

    private void stopAudio() {
        audioRunning=false;
        if(audioCapture!=null){try{audioCapture.stop();}catch(Exception ignored){}}
        Thread worker=audioThread;
        if(worker!=null){try{worker.join(500);}catch(InterruptedException ignored){Thread.currentThread().interrupt();}}
        if(audioThread==worker)audioThread=null;
    }

    /** Capture thread: owns the record and the codec; releases them on exit. */
    private void captureAudioLoop() {
        ByteBuffer pcm=ByteBuffer.allocateDirect(8192);
        MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
        long pts=0;
        try{
            while(audioRunning && audioChannel!=null && audioChannel.state()==DataChannel.State.OPEN){
                if(!audioEnabled){Thread.sleep(50);continue;}
                int read=audioCapture.read(pcm,4096);
                if(read<=0)continue;
                int input=audioEncoder.dequeueInputBuffer(10000);
                if(input>=0){
                    ByteBuffer in=audioEncoder.getInputBuffer(input);
                    in.clear();pcm.position(0);pcm.limit(read);in.put(pcm);
                    audioEncoder.queueInputBuffer(input,0,read,pts,0);
                    pts+=read*1000000L/(48000*4);
                }
                int output;
                while(audioRunning && (output=audioEncoder.dequeueOutputBuffer(info,0))>=0){
                    ByteBuffer encoded=audioEncoder.getOutputBuffer(output);
                    byte[] payload=new byte[info.size];
                    encoded.position(info.offset);encoded.get(payload);
                    ByteBuffer message=ByteBuffer.allocate(1+payload.length);
                    message.put((byte)(((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG)!=0)?0:1)).put(payload).flip();
                    // This WebRTC build takes a DataChannel.Buffer, not a raw ByteBuffer. Audio
                    // frames are binary; the first payload byte keeps the codec-config/audio flag.
                    audioChannel.send(new DataChannel.Buffer(message,true));
                    audioEncoder.releaseOutputBuffer(output,false);
                }
            }
        }catch(Exception ignored){
        }finally{
            if(audioEncoder!=null){try{audioEncoder.stop();}catch(Exception ignored){}try{audioEncoder.release();}catch(Exception ignored){}audioEncoder=null;}
            if(audioCapture!=null){try{audioCapture.release();}catch(Exception ignored){}audioCapture=null;}
            audioRunning=false;
        }
    }

    private int[] captureSize(int width,int height) {
        if(width<=0 || height<=0) {
            WindowManager wm=(WindowManager)getSystemService(WINDOW_SERVICE);
            if(Build.VERSION.SDK_INT>=30) {android.graphics.Rect bounds=wm.getMaximumWindowMetrics().getBounds();width=bounds.width();height=bounds.height();}
            else {DisplayMetrics metrics=new DisplayMetrics();wm.getDefaultDisplay().getRealMetrics(metrics);width=metrics.widthPixels;height=metrics.heightPixels;}
        }
        double scale=Math.min(1.0,(double)maxSize/Math.max(width,height));
        return new int[]{Math.max(2,((int)(width*scale)/2)*2),Math.max(2,((int)(height*scale)/2)*2)};
    }

    private void resize(int width,int height) {
        if(closing || capturer==null)return;
        if(width>0 && height>0){contentWidth=width;contentHeight=height;}
        int[] size=captureSize(contentWidth,contentHeight);
        capturer.changeCaptureFormat(size[0],size[1],fps);
        source.adaptOutputFormat(size[0],size[1],fps);
    }

    private void configure(JSONObject settings) {
        if(settings==null)return;
        fps=setting(settings,"fps",DEFAULT_FPS);
        bitrate=setting(settings,"bitrate",DEFAULT_BITRATE);
        maxSize=setting(settings,"maxSize",DEFAULT_MAX_SIZE);
        resize(0,0);
        applyEncoding();
    }

    /** A settings field sent by the peer, or the base-profile default when omitted/invalid. */
    private static int setting(JSONObject settings,String key,int fallback) {
        int value=settings.optInt(key,0);
        return value>0 ? value : fallback;
    }

    private int phoneVolume() {
        int max=audio==null?0:audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        return max<=0 ? -1 : Math.round(100f*audio.getStreamVolume(AudioManager.STREAM_MUSIC)/max);
    }

    private void setPhoneVolume(int percent) {
        int max=audio==null?0:audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        if(max<=0)return;
        int index=Math.max(0,Math.min(max,Math.round(percent/100f*max)));
        try{audio.setStreamVolume(AudioManager.STREAM_MUSIC,index,AudioManager.FLAG_SHOW_UI);}
        catch(Exception ignored){}
    }

    private void applyEncoding() {
        // Encodings are negotiated by SDP. Apply again after the remote answer.
        if(sender==null || peer==null || !remoteSet)return;
        RtpParameters params=sender.getParameters();
        params.degradationPreference=RtpParameters.DegradationPreference.MAINTAIN_RESOLUTION;
        for(RtpParameters.Encoding encoding:params.encodings) {
            encoding.maxBitrateBps=bitrate*1000000;
            encoding.maxFramerate=fps;
            encoding.scaleResolutionDownBy=1.0;
        }
        boolean applied=!params.encodings.isEmpty() && sender.setParameters(params);
        boolean budget=peer.setBitrate(500000,Math.min(bitrate,12)*1000000,bitrate*1000000);
        applied=applied && budget;
        status=applied ? "Профиль применён · "+fps+" fps · до "+maxSize+" px" : "Кодировщик отклонил профиль";
        send(json("type","configured","requestId",profileRequest,"applied",applied,
                "settings",json("fps",fps,"bitrate",bitrate,"maxSize",maxSize)));
    }

    private void connect() {
        if(closing)return;
        status="Подключение к компьютеру…";
        socket=http.newWebSocket(new Request.Builder().url("ws://"+address+"/signal").build(),new WebSocketListener(){
            @Override public void onOpen(WebSocket ws,Response response){main.post(()->{if(ws==socket && !closing)ws.send(json("role","sender","credential",credential).toString());});}
            @Override public void onMessage(WebSocket ws,String text){main.post(()->{if(ws!=socket || closing)return;try{message(new JSONObject(text));}catch(Exception e){status="Ошибка согласования: "+e.getMessage();stopSelf();}});}
            @Override public void onFailure(WebSocket ws,Throwable error,Response response){main.post(()->lost(ws));}
            @Override public void onClosed(WebSocket ws,int code,String reason){main.post(()->{if(code==1008 && ws==socket){status="Код отклонён или сеанс занят. Введите новый код.";stopSelf();}else lost(ws);});}
            @Override public void onClosing(WebSocket ws,int code,String reason){ws.close(code,reason);}
        });
    }

    private void lost(WebSocket ws) {
        if(closing || ws!=socket)return;
        socket=null;closePeer();
        if(attempts>=8){status="Компьютер недоступен. Начните новый сеанс.";stopSelf();return;}
        long delay=Math.min(1000L << attempts++,15000L);
        status="Восстановление связи "+attempts+"/8…";
        reconnectTask=this::connect;main.postDelayed(reconnectTask,delay);
    }

    private void send(JSONObject message){if(socket!=null)socket.send(message.toString());}

    private void message(JSONObject message) throws Exception {
        switch(message.getString("type")) {
            case "authenticated":credential=message.getString("resumeToken");status="Ожидание приёмника…";break;
            case "peer-ready":configure(message.optJSONObject("settings"));offer();break;
            case "configure":profileRequest=message.optString("requestId");configure(message.optJSONObject("settings"));break;
            case "answer":
                if(!message.optString("session").equals(String.valueOf(generation)))break;
                if(peer!=null) {
                    final PeerConnection current=peer;
                    current.setRemoteDescription(new Sdp(){@Override public void onSetSuccess(){main.post(()->{
                        if(peer!=current || closing)return;remoteSet=true;negotiating=false;applyEncoding();
                        for(IceCandidate ice:pendingIce)current.addIceCandidate(ice);pendingIce.clear();
                    });}},new SessionDescription(SessionDescription.Type.ANSWER,message.getString("sdp")));
                }
                break;
            case "ice":
                if(!message.optString("session").equals(String.valueOf(generation)))break;
                IceCandidate ice=new IceCandidate(message.optString("sdpMid","0"),message.getInt("sdpMLineIndex"),message.getString("candidate"));
                if(peer!=null && remoteSet)peer.addIceCandidate(ice);else if(pendingIce.size()<128)pendingIce.add(ice);
                break;
            case "restart":scheduleRestart();break;
            case "volume":setPhoneVolume(message.optInt("value",50));break;
            case "audio":
                audioEnabled=message.optBoolean("enabled");
                if(audioEnabled)startAudio();else stopAudio();
                break;
            case "peer-left":closePeer();status="Окно приёмника закрыто. Ожидание…";break;
            case "stop":status="Трансляция завершена с компьютера";stopSelf();break;
            case "error":status=message.optString("message","Ошибка подключения");stopSelf();break;
        }
    }

    private void offer() {
        if(closing || socket==null)return;
        closePeer();negotiating=true;
        final int epoch=++generation;
        PeerConnection.RTCConfiguration config=new PeerConnection.RTCConfiguration(Collections.emptyList());
        config.sdpSemantics=PeerConnection.SdpSemantics.UNIFIED_PLAN;
        config.bundlePolicy=PeerConnection.BundlePolicy.MAXBUNDLE;
        config.continualGatheringPolicy=PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY;
        peer=factory.createPeerConnection(config,new PeerConnection.Observer(){
            @Override public void onSignalingChange(PeerConnection.SignalingState state){}
            @Override public void onIceConnectionChange(PeerConnection.IceConnectionState state){}
            @Override public void onIceConnectionReceivingChange(boolean receiving){}
            @Override public void onIceGatheringChange(PeerConnection.IceGatheringState state){}
            @Override public void onIceCandidatesRemoved(IceCandidate[] candidates){}
            @Override public void onAddStream(MediaStream stream){}
            @Override public void onRemoveStream(MediaStream stream){}
            @Override public void onDataChannel(DataChannel channel){}
            @Override public void onRenegotiationNeeded(){}
            @Override public void onAddTrack(RtpReceiver receiver,MediaStream[] streams){}
            @Override public void onIceCandidate(IceCandidate ice){main.post(()->{if(epoch==generation && !closing)send(json("type","ice","session",String.valueOf(epoch),"candidate",ice.sdp,"sdpMid",ice.sdpMid,"sdpMLineIndex",ice.sdpMLineIndex));});}
            @Override public void onConnectionChange(PeerConnection.PeerConnectionState state){main.post(()->{
                if(epoch!=generation || closing)return;
                if(state==PeerConnection.PeerConnectionState.CONNECTED){
                    status="В эфире · до "+fps+" fps";
                    if(restartTask!=null)main.removeCallbacks(restartTask);
                    restartPending=false;
                    if(stableTask!=null)main.removeCallbacks(stableTask);
                    stableTask=()->{attempts=0;restarts=0;};main.postDelayed(stableTask,30000);
                }else if(state==PeerConnection.PeerConnectionState.DISCONNECTED || state==PeerConnection.PeerConnectionState.FAILED){
                    status="Восстановление медиасоединения…";scheduleRestart();
                }
            });}
        });
        if(peer==null){status="Не удалось создать WebRTC сеанс";stopSelf();return;}
        sender=peer.addTrack(track,Collections.singletonList("airframe"));
        DataChannel.Init audioInit=new DataChannel.Init();
        audioInit.ordered=true;
        audioChannel=peer.createDataChannel("airframe-audio",audioInit);
        audioChannel.registerObserver(new DataChannel.Observer(){
            @Override public void onBufferedAmountChange(long previousAmount){}
            @Override public void onMessage(DataChannel.Buffer buffer){}
            @Override public void onStateChange(){main.post(()->{
                if(audioChannel==null)return;
                if(audioChannel.state()==DataChannel.State.OPEN && audioEnabled)startAudio();
                else stopAudio();
            });}
        });
        // Prefer hardware H.264; keep VP8/VP9 as negotiated fallbacks.
        List<RtpCapabilities.CodecCapability> codecs=new ArrayList<>(factory.getRtpSenderCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO).codecs);
        codecs.sort((a,b)->Boolean.compare(!a.name.equalsIgnoreCase("H264"),!b.name.equalsIgnoreCase("H264")));
        for(RtpTransceiver transceiver:peer.getTransceivers())
            if(transceiver.getMediaType()==MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO)
                transceiver.setCodecPreferences(codecs);
        configure(json("fps",fps,"bitrate",bitrate,"maxSize",maxSize));
        final PeerConnection current=peer;
        current.createOffer(new Sdp(){@Override public void onCreateSuccess(SessionDescription sdp){main.post(()->{
            if(peer!=current || closing)return;
            current.setLocalDescription(new Sdp(){@Override public void onSetSuccess(){main.post(()->{
                if(peer==current && !closing)send(json("type","offer","session",String.valueOf(epoch),"sdp",sdp.description));
            });}},sdp);
        });}},new MediaConstraints());
        restartTask=()->{if(peer==current && negotiating)scheduleRestart();};main.postDelayed(restartTask,15000);
    }

    private void scheduleRestart() {
        if(closing || restartPending)return;
        if(stableTask!=null)main.removeCallbacks(stableTask);
        if(restartTask!=null)main.removeCallbacks(restartTask);
        if(restarts>=8){status="Не удалось восстановить видео. Проверьте сеть и повторите подключение.";stopSelf();return;}
        restarts++;
        restartPending=true;
        restartTask=()->{restartPending=false;offer();};main.postDelayed(restartTask,Math.min(1000L << Math.min(restarts-1,4),15000));
    }

    private void closePeer() {
        lastFrames=0;lastStatsNs=0;generation++;remoteSet=false;negotiating=false;restartPending=false;pendingIce.clear();
        if(restartTask!=null)main.removeCallbacks(restartTask);
        if(stableTask!=null)main.removeCallbacks(stableTask);
        audioChannel=null;stopAudio();
        if(peer!=null){peer.close();peer.dispose();peer=null;sender=null;}
    }

    private class Sdp implements SdpObserver {
        private final int expectedGeneration=generation;
        @Override public void onCreateSuccess(SessionDescription description){}
        @Override public void onSetSuccess(){}
        @Override public void onCreateFailure(String error){main.post(()->{if(!closing && expectedGeneration==generation){status="Ошибка SDP: "+error;stopSelf();}});}
        @Override public void onSetFailure(String error){onCreateFailure(error);}
    }

    @Override public void onDestroy() {
        closing=true;running=false;main.removeCallbacksAndMessages(null);
        if(socket!=null){socket.close(1000,"Stopped");socket=null;}
        closePeer();
        if(capturer!=null){try{capturer.stopCapture();}catch(Exception ignored){}capturer.dispose();}
        if(track!=null)track.dispose();
        if(source!=null)source.dispose();
        if(texture!=null)texture.dispose();
        if(factory!=null)factory.dispose();
        if(egl!=null)egl.release();
        if(http!=null){http.dispatcher().executorService().shutdown();http.connectionPool().evictAll();}
        if(wifi!=null && wifi.isHeld())wifi.release();
        if(wake!=null && wake.isHeld())wake.release();
        if(status.startsWith("В эфире") || status.startsWith("Подключение"))status="Трансляция остановлена";
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }
}
