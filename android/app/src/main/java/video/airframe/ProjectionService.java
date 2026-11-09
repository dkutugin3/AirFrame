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
import android.media.AudioManager;
import android.media.projection.MediaProjection;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import org.json.JSONObject;
import org.webrtc.*;
import okhttp3.*;

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
    private ScreenCapture capturer;
    private SurfaceTextureHelper texture;
    private VideoSource source;
    private VideoTrack track;
    private PeerConnection peer;
    private RtpSender sender;
    private PlaybackAudio playbackAudio;
    private PlaybackAudio.ChannelSink audioSink;
    private boolean audioEnabled=true,audioRestartPending=false;
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
    private volatile boolean preferHevc=false;
    private int bitrate=DEFAULT_BITRATE, maxSize=DEFAULT_MAX_SIZE, contentWidth=0, contentHeight=0;
    private final CaptureGate captureGate=new CaptureGate();
    private boolean captureRunning=false;
    /** Telemetry keeps its 1 s cadence for this long after each (re)connect, then drops to 2 s. */
    private static final long TELEMETRY_FAST_MS=30000;
    private volatile long telemetryFastUntil=0;
    private String profileRequest="";
    private final List<IceCandidate> pendingIce=new ArrayList<>();
    private Runnable reconnectTask, restartTask, stableTask;
    private final SenderTelemetry telemetry=new SenderTelemetry(System.nanoTime());
    private final Runnable statistics=new Runnable(){
        @Override public void run(){
            final PeerConnection current=peer;
            if(current!=null && remoteSet && captureRunning)current.getStats(report -> main.post(() -> {
                if(peer!=current || closing || !captureRunning)return;
                java.util.Map<String,RTCStats> stats=report.getStatsMap();
                java.util.Map<String,Object> video=null, codec=null;
                for(RTCStats stat:stats.values()){
                    if(!"outbound-rtp".equals(stat.getType()))continue;
                    java.util.Map<String,Object> members=stat.getMembers();
                    if(!"video".equals(members.get("kind")) || !members.containsKey("framesEncoded"))continue;
                    video=members;
                }
                if(video!=null){
                    Object codecId=video.get("codecId");
                    RTCStats codecStat=codecId==null?null:stats.get(String.valueOf(codecId));
                    if(codecStat!=null && "codec".equals(codecStat.getType()))codec=codecStat.getMembers();
                }
                // Old-schema fields (encodedFps/width/height/limitation) stay present; the optional
                // measurement fields are added only when actually measured.
                JSONObject message=json("type","telemetry","session",String.valueOf(generation),
                        "volume",phoneVolume(),"sampledMonotonicMs",SystemClock.elapsedRealtime());
                for(java.util.Map.Entry<String,Object> field
                        :telemetry.sample(System.nanoTime(),thermalStatus(),video,codec).entrySet()){
                    try{message.put(field.getKey(),field.getValue());}catch(Exception ignored){}
                }
                send(message);
            }));
            if(!closing && captureRunning)main.postDelayed(this,
                    SenderTelemetry.telemetryDelayMs(telemetryFastUntil-SystemClock.elapsedRealtime()));
        }
    };
    private final Runnable renewWake=new Runnable(){
        @Override public void run(){
            if(!closing && captureRunning && wake!=null){wake.acquire(10*60*1000L);main.postDelayed(this,5*60*1000L);}
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
            wake.setReferenceCounted(false); // held only while media is connected
            WifiManager wm=(WifiManager)getApplicationContext().getSystemService(WIFI_SERVICE);
            wifi=wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF,"airframe:wifi");
            wifi.setReferenceCounted(false); // acquired/released together with the live capture
            audio=(AudioManager)getSystemService(AUDIO_SERVICE);
            PeerConnectionFactory.initialize(PeerConnectionFactory.InitializationOptions.builder(this).createInitializationOptions());
            egl=EglBase.create();
            factory=PeerConnectionFactory.builder()
                    .setVideoEncoderFactory(new DefaultVideoEncoderFactory(egl.getEglBaseContext(),true,true))
                    .setVideoDecoderFactory(new DefaultVideoDecoderFactory(egl.getEglBaseContext()))
                    .createPeerConnectionFactory();
            texture=SurfaceTextureHelper.create("AirFrame-capture",egl.getEglBaseContext());
            source=factory.createVideoSource(true);
            capturer=new ScreenCapture(permission,new MediaProjection.Callback(){
                @Override public void onStop(){main.post(()->{if(!closing){status="Захват завершён системой. Разрешите его заново.";stopSelf();}});}
                @Override public void onCapturedContentResize(int width,int height){main.post(()->resize(width,height));}
            });
            CapturerObserver downstream=source.getCapturerObserver();
            capturer.initialize(texture,this,new CapturerObserver(){
                private final FramePacer pacer=new FramePacer();
                @Override public void onCapturerStarted(boolean success){downstream.onCapturerStarted(success);}
                @Override public void onCapturerStopped(){downstream.onCapturerStopped();}
                @Override public void onFrameCaptured(VideoFrame frame){
                    boolean paced=pacer.accept(frame.getTimestampNs(),fps);
                    telemetry.onFrame(frame.getTimestampNs(),paced);
                    if(!paced)return;
                    downstream.onFrameCaptured(frame);
                }
            });
            // Capture starts lazily on CONNECTED: until a peer session is live it would only
            // burn GPU on a service that is waiting for a receiver (see CaptureGate).
            track=factory.createVideoTrack("airframe-screen",source);
            http=new OkHttpClient.Builder().pingInterval(15,TimeUnit.SECONDS).connectTimeout(10,TimeUnit.SECONDS).readTimeout(0,TimeUnit.SECONDS).build();
            connect();
        }catch(Exception e){status="Ошибка запуска: "+e.getMessage();stopSelf();}
        return START_NOT_STICKY;
    }

    /**
     * Phone audio rides the WebRTC data channel, not an RTP audio track: the pinned
     * io.github.webrtc-sdk has no setAudioRecord(AudioRecord) to feed an AudioPlaybackCapture
     * AudioRecord into JavaAudioDeviceModule. We capture playback PCM ourselves, encode AAC
     * with MediaCodec and send self-describing config+frame messages; the desktop decodes
     * with PyAV. Video and playback capture share the same projection grant.
     */
    private MediaProjection projectionForAudio() {
        if(Build.VERSION.SDK_INT<29)return null;
        return capturer==null?null:capturer.getMediaProjection();
    }

    private void startAudio() {
        if(closing || !captureRunning || !audioEnabled || audioSink==null || !audioSink.isOpen())return;
        if(playbackAudio!=null){
            if(playbackAudio.isAlive()){
                // An off/on toggle may arrive while the previous backend is still releasing.
                if(playbackAudio.isStopping())audioRestartPending=true;
                return;
            }
            playbackAudio=null;
        }
        if(Build.VERSION.SDK_INT<29)return; // playback capture needs Android 10+
        if(getApplicationContext().checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                !=PackageManager.PERMISSION_GRANTED)return; // feature needs a granted runtime permission
        MediaProjection projection=projectionForAudio();
        if(projection==null)return;
        audioRestartPending=false;
        playbackAudio=new PlaybackAudio(new AndroidPlaybackAudio(getApplicationContext(),projection),audioSink,finished->main.post(()->{
            if(playbackAudio!=finished)return;
            playbackAudio=null;
            if(audioRestartPending){audioRestartPending=false;startAudio();}
        }));
        playbackAudio.start();
    }

    private void stopAudio() {
        audioRestartPending=false;
        if(playbackAudio!=null && playbackAudio.stop())playbackAudio=null;
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

    /** Capture, audio and performance locks follow media readiness, including transient outages. */
    private boolean syncCaptureToGate() {
        if(closing || capturer==null)return false;
        boolean want=captureGate.shouldCapture();
        if(want && !captureRunning){
            int[] size=captureSize(contentWidth,contentHeight);
            try{
                capturer.startCapture(size[0],size[1],fps);
                source.adaptOutputFormat(size[0],size[1],fps);
                telemetry.setCaptureFormat(size[0],size[1]);
                captureRunning=true;
            }catch(RuntimeException error){
                // CONNECTED is delivered outside the signalling message's exception guard.
                // A revoked projection grant must end the service rather than crash the app.
                status="Не удалось начать захват: "+error.getMessage();stopSelf();return false;
            }
        }else if(!want && captureRunning){
            stopAudio();
            try{capturer.stopCapture();}catch(Exception ignored){}
            captureRunning=false;
        }
        if(want){
            if(wake!=null && !wake.isHeld())renewWake.run();
            if(wifi!=null && !wifi.isHeld())wifi.acquire();
        }else{
            main.removeCallbacks(renewWake);
            main.removeCallbacks(statistics);
            if(wake!=null && wake.isHeld())wake.release();
            if(wifi!=null && wifi.isHeld())wifi.release();
        }
        return true;
    }

    private void resize(int width,int height) {
        if(closing || capturer==null)return;
        if(width>0 && height>0){contentWidth=width;contentHeight=height;}
        int[] size=captureSize(contentWidth,contentHeight);
        // changeCaptureFormat targets a running capture; CONNECTED picks up the fresh size.
        if(captureRunning)capturer.changeCaptureFormat(size[0],size[1],fps);
        source.adaptOutputFormat(size[0],size[1],fps);
        if(captureRunning)telemetry.setCaptureFormat(size[0],size[1]);
    }

    private void configure(JSONObject settings) {
        if(settings==null)return;
        fps=setting(settings,"fps",DEFAULT_FPS);
        bitrate=setting(settings,"bitrate",DEFAULT_BITRATE);
        maxSize=setting(settings,"maxSize",DEFAULT_MAX_SIZE);
        boolean hevc=settings.optBoolean("preferHevc",false);
        boolean codecChanged=hevc!=preferHevc;
        preferHevc=hevc;
        resize(0,0);
        applyEncoding();
        // A codec preference change needs a fresh offer to take effect; the
        // negotiated fallback stays H.264 when HEVC is not accepted.
        if(codecChanged && peer!=null && remoteSet)scheduleRestart();
    }

    /** Full profile for offers and acknowledgements, including the requested codec. */
    private JSONObject currentSettings(){
        return json("fps",fps,"bitrate",bitrate,"maxSize",maxSize,"preferHevc",preferHevc);
    }

    /** Pure ordering for unit tests: preferred codec first, H.264 next, rest unchanged. */
    static void sortCodecPreferences(List<RtpCapabilities.CodecCapability> codecs, boolean hevcFirst) {
        String preferred=hevcFirst ? "H265" : "H264";
        codecs.sort((a,b)->{
            int rankA=a.name.equalsIgnoreCase(preferred)?0:(a.name.equalsIgnoreCase("H264")?1:2);
            int rankB=b.name.equalsIgnoreCase(preferred)?0:(b.name.equalsIgnoreCase("H264")?1:2);
            return Integer.compare(rankA,rankB);
        });
    }

    /** A settings field sent by the peer, or the base-profile default when omitted/invalid. */
    private static int setting(JSONObject settings,String key,int fallback) {
        int value=settings.optInt(key,0);
        return value>0 ? value : fallback;
    }

    /** System thermal status where Android exposes it (API 29+); unknown is never 0. */
    private int thermalStatus() {
        if(Build.VERSION.SDK_INT<29)return SenderTelemetry.UNKNOWN_THERMAL;
        try{
            PowerManager pm=(PowerManager)getSystemService(POWER_SERVICE);
            return pm==null?SenderTelemetry.UNKNOWN_THERMAL:pm.getCurrentThermalStatus();
        }catch(Exception error){
            return SenderTelemetry.UNKNOWN_THERMAL;
        }
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
                "settings",currentSettings()));
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
        captureGate.onSocketLost();syncCaptureToGate(); // nothing streams during the backoff
        if(attempts>=8){status="Компьютер недоступен. Начните новый сеанс.";stopSelf();return;}
        long delay=Math.min(1000L << attempts++,15000L);
        status="Восстановление связи "+attempts+"/8…";
        reconnectTask=this::connect;main.postDelayed(reconnectTask,delay);
    }

    private void send(JSONObject message){if(socket!=null)socket.send(message.toString());}

    private void message(JSONObject message) throws Exception {
        switch(message.getString("type")) {
            case "authenticated":credential=message.getString("resumeToken");status="Ожидание приёмника…";break;
            case "peer-ready":profileRequest="";configure(message.optJSONObject("settings"));offer();break;
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
            case "peer-left":closePeer();captureGate.onPeerLeft();syncCaptureToGate();
                status="Окно приёмника закрыто. Ожидание…";break;
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
                    captureGate.onConnected();if(!syncCaptureToGate())return;
                    // DataChannel.OPEN may precede projection creation; retry once it exists.
                    if(audioEnabled)startAudio();
                    status="В эфире · до "+fps+" fps";
                    telemetryFastUntil=SystemClock.elapsedRealtime()+TELEMETRY_FAST_MS;
                    main.removeCallbacks(statistics);main.post(statistics);
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
        final DataChannel channel=peer.createDataChannel("airframe-audio",audioInit);
        if(channel==null){status="Не удалось создать аудиоканал";stopSelf();return;}
        final PlaybackAudio.ChannelSink channelSink=new PlaybackAudio.ChannelSink(new PlaybackAudio.Transport(){
            @Override public boolean isOpen(){return channel.state()==DataChannel.State.OPEN;}
            @Override public boolean send(ByteBuffer packet){return channel.send(new DataChannel.Buffer(packet,true));}
            @Override public void close(){
                try{channel.unregisterObserver();}
                finally{try{channel.close();}finally{channel.dispose();}}
            }
        });
        audioSink=channelSink;
        channel.registerObserver(new DataChannel.Observer(){
            @Override public void onBufferedAmountChange(long previousAmount){}
            @Override public void onMessage(DataChannel.Buffer buffer){}
            @Override public void onStateChange(){main.post(()->{
                if(closing || epoch!=generation || audioSink!=channelSink)return;
                if(channelSink.isOpen() && audioEnabled)startAudio();
                else stopAudio();
            });}
        });
        // Prefer hardware H.264, or HEVC when the profile asks for it;
        // the rest stay as negotiated fallbacks.
        List<RtpCapabilities.CodecCapability> codecs=new ArrayList<>(factory.getRtpSenderCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO).codecs);
        sortCodecPreferences(codecs,preferHevc);
        for(RtpTransceiver transceiver:peer.getTransceivers())
            if(transceiver.getMediaType()==MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO)
                transceiver.setCodecPreferences(codecs);
        configure(currentSettings());
        telemetryFastUntil=SystemClock.elapsedRealtime()+TELEMETRY_FAST_MS;
        captureGate.onOffer();syncCaptureToGate(); // negotiation alone needs no capture
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
        captureGate.onDisconnected();syncCaptureToGate();
        if(stableTask!=null)main.removeCallbacks(stableTask);
        if(restartTask!=null)main.removeCallbacks(restartTask);
        if(restarts>=8){status="Не удалось восстановить видео. Проверьте сеть и повторите подключение.";stopSelf();return;}
        restarts++;
        restartPending=true;
        restartTask=()->{restartPending=false;offer();};main.postDelayed(restartTask,Math.min(1000L << Math.min(restarts-1,4),15000));
    }

    private void closePeer() {
        captureGate.onPeerLeft();syncCaptureToGate();
        main.removeCallbacks(statistics);
        telemetry.reset(System.nanoTime());generation++;remoteSet=false;negotiating=false;restartPending=false;pendingIce.clear();
        if(restartTask!=null)main.removeCallbacks(restartTask);
        if(stableTask!=null)main.removeCallbacks(stableTask);
        PlaybackAudio.ChannelSink previousAudioSink=audioSink;audioSink=null;
        stopAudio();
        if(previousAudioSink!=null)previousAudioSink.close();
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
