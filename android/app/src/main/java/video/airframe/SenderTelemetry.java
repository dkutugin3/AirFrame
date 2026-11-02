package video.airframe;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Sender-side measurement accumulator and telemetry field builder (plan stage 5).
 *
 * <p>Pure Java (no Android imports) so the measurement contract is unit-tested on the JVM with
 * explicit timestamps and fake RTC stat maps. One instance lives for the whole service;
 * {@link #reset(long)} starts a new measurement session (new PeerConnection / RTC counters reset).
 *
 * <p>Rules:
 * <ul>
 *   <li>window rates are computed from the window between two consecutive samples; the window
 *       duration itself is reported as {@code telemetryWindowMs} so the receiver can validate
 *       rates instead of trusting them;</li>
 *   <li>cumulative RTC counters ({@code framesEncoded}, {@code framesSent}) are reported raw; a
 *       counter that RTC does not report is omitted, never fabricated as 0;</li>
 *   <li>0 means "measured zero"; absent/"unknown" means unknown (temperature, hardware encoder,
 *       codec). The temperature status is never 0 for unknown;</li>
 *   <li>all numbers are finite and bounded so the JSON message is always valid and old receivers
 *       keep working.</li>
 * </ul>
 *
 * <p>State is O(1) counters only: no per-frame history is retained (bounded memory).
 */
final class SenderTelemetry {
    static final int UNKNOWN_THERMAL=-1;
    static final String UNKNOWN="unknown";

    private static final double NANOS_PER_MS=1000000.0;
    private static final double MAX_FPS=1000.0;
    private static final double MAX_INTERVAL_MS=60000.0;
    private static final double MAX_BITRATE_KBPS=1000000.0;
    private static final double MAX_ENCODE_MS=60000.0;
    private static final int MAX_DIMENSION=16384;
    private static final int MAX_TEXT=128;
    private static final int MAX_FMTP=1024;
    private static final long MAX_SAFE_COUNTER=9007199254740991L;
    private static final List<String> LIMITATIONS=Arrays.asList("none","cpu","bandwidth","other");
    private static final Pattern PROFILE_LEVEL_ID=
            Pattern.compile("(?:^|[;\\s])profile-level-id=([0-9a-fA-F]{6})(?:$|[;\\s])");

    private long windowStartNs;
    private long lastSampleNs;
    private long captureCount;
    private long pacerCount;
    private long lastCaptureNs;
    private long maxCaptureGapNs;
    private long baselineFramesEncoded=-1;
    private long baselineFramesSent=-1;
    private long baselineBytesSent=-1;
    private double baselineEncodeTime=-1;
    private int captureWidth;
    private int captureHeight;

    SenderTelemetry(long startNs){
        windowStartNs=startNs;
    }

    /** Actual ScreenCapturerAndroid capture format for the current window (separate from encoded size). */
    synchronized void setCaptureFormat(int width,int height){
        if(width>0 && height>0 && width<=MAX_DIMENSION && height<=MAX_DIMENSION){
            captureWidth=width;
            captureHeight=height;
        }
    }

    /** A real captured frame; {@code paced} is the FramePacer decision for it. */
    synchronized void onFrame(long timestampNs,boolean paced){
        captureCount++;
        if(paced)pacerCount++;
        if(captureCount==1){
            lastCaptureNs=timestampNs;
            return;
        }
        long delta=timestampNs-lastCaptureNs;
        if(delta>0){
            if(delta>maxCaptureGapNs)maxCaptureGapNs=delta;
            lastCaptureNs=timestampNs;
        }else if(delta<0){
            // ScreenCapturerAndroid restarted its timestamp scale: do not turn the reset into a
            // fake multi-second capture interval.
            lastCaptureNs=timestampNs;
        }
    }

    /** Start a new measurement session; cumulative counter baselines are dropped. */
    synchronized void reset(long nowNs){
        windowStartNs=nowNs;
        lastSampleNs=0;
        captureCount=0;
        pacerCount=0;
        lastCaptureNs=0;
        maxCaptureGapNs=0;
        baselineFramesEncoded=-1;
        baselineFramesSent=-1;
        baselineBytesSent=-1;
        baselineEncodeTime=-1;
    }

    /**
     * Build the telemetry fields for the window ending at {@code nowNs}.
     *
     * @param nowNs       {@link System#nanoTime()} sample instant (same scale as the constructor)
     * @param thermalStatus raw PowerManager thermal status, or {@link #UNKNOWN_THERMAL}
     * @param video       RTC {@code outbound-rtp} video stat members, or null when unavailable
     * @param codec       RTC {@code codec} stat members (mimeType/sdpFmtpLine), or null
     * @return ordered telemetry fields; old-schema fields are always present, new ones only when known
     */
    synchronized Map<String,Object> sample(long nowNs,int thermalStatus,
            Map<String,Object> video,Map<String,Object> codec){
        long windowBaseline=lastSampleNs!=0?lastSampleNs:windowStartNs;
        double windowMs=nowNs>windowBaseline?(nowNs-windowBaseline)/NANOS_PER_MS:0;
        windowMs=clamp(windowMs,0,MAX_INTERVAL_MS);

        long framesEncoded=counter(video,"framesEncoded");
        long framesSent=counter(video,"framesSent");
        long bytesSent=counter(video,"bytesSent");
        double totalEncodeTime=number(video,"totalEncodeTime");

        // Window rates: safe only with a positive window.
        double captureFps=windowMs>0?clamp(captureCount*1000.0/windowMs,0,MAX_FPS):0;
        double pacerFps=windowMs>0?clamp(pacerCount*1000.0/windowMs,0,MAX_FPS):0;
        double encodedFps=0;
        if(windowMs>0 && framesEncoded>=0 && baselineFramesEncoded>=0
                && framesEncoded>=baselineFramesEncoded){
            encodedFps=clamp((framesEncoded-baselineFramesEncoded)*1000.0/windowMs,0,MAX_FPS);
        }

        Map<String,Object> out=new LinkedHashMap<>();
        out.put("encodedFps",encodedFps);
        out.put("width",dimension(video,"frameWidth"));
        out.put("height",dimension(video,"frameHeight"));
        out.put("limitation",limitation(video));
        out.put("captureFps",captureFps);
        out.put("maxCaptureIntervalMs",clamp(maxCaptureGapNs/NANOS_PER_MS,0,MAX_INTERVAL_MS));
        out.put("pacerFps",pacerFps);
        out.put("telemetryWindowMs",windowMs);
        if(framesEncoded>=0)out.put("framesEncoded",framesEncoded);
        if(framesSent>=0)out.put("framesSent",framesSent);
        if(bytesSent>=0){
            long base=baselineBytesSent>=0?baselineBytesSent:0;
            if(bytesSent>=base && windowMs>0){
                // Actual video bitrate from the bytesSent delta of this window, not the configured cap.
                out.put("videoBitrateKbps",clamp((bytesSent-base)*8.0/windowMs,0,MAX_BITRATE_KBPS));
            }
        }
        if(framesEncoded>=0 && !Double.isNaN(totalEncodeTime)){
            long framesBase=baselineFramesEncoded>=0?baselineFramesEncoded:0;
            double timeBase=baselineEncodeTime>=0?baselineEncodeTime:0;
            double deltaTime=totalEncodeTime-timeBase;
            long deltaFrames=framesEncoded-framesBase;
            if(deltaTime>=0 && deltaFrames>0){
                // Average encode time of this window from cumulative RTC counters
                // (totalEncodeTime / framesEncoded). It is NOT a p95 of individual frames:
                // aggregate counters cannot produce that.
                out.put("avgEncodeTimeMs",clamp(deltaTime*1000.0/deltaFrames,0,MAX_ENCODE_MS));
            }
        }
        if(captureWidth>0 && captureHeight>0){
            out.put("captureWidth",captureWidth);
            out.put("captureHeight",captureHeight);
        }
        String codecName=codecName(codec);
        if(codecName!=null)out.put("codec",codecName);
        String codecProfile=codecProfile(codec);
        if(codecProfile!=null)out.put("codecProfile",codecProfile);
        String implementation=text(video,"encoderImplementation",MAX_TEXT);
        if(implementation!=null)out.put("encoderImplementation",implementation);
        // The SDK exposes no reliable hardware-encoder status; report "unknown", never infer it
        // from the encoder implementation name.
        out.put("hardwareEncoder",UNKNOWN);
        out.put("thermalStatus",thermalStatusName(thermalStatus));

        // Next window starts here; a counter that RTC stops reporting clears its baseline so a
        // missing sample can never manufacture a stale multi-window rate.
        baselineFramesEncoded=framesEncoded>=0?framesEncoded:-1;
        baselineFramesSent=framesSent>=0?framesSent:-1;
        baselineBytesSent=bytesSent>=0?bytesSent:-1;
        baselineEncodeTime=!Double.isNaN(totalEncodeTime)&&totalEncodeTime>=0?totalEncodeTime:-1;
        lastSampleNs=nowNs;
        captureCount=0;
        pacerCount=0;
        maxCaptureGapNs=0;
        return out;
    }

    /**
     * Telemetry cadence: 1 s while the session is young (negotiation plus the first adaptive
     * decisions), 2 s once stable - getStats walks the whole stats tree and allocates, so a
     * settled stream does not need it every second.
     *
     * @param fastRemainingMs milliseconds left in the fast window; negative once it has passed
     */
    static long telemetryDelayMs(long fastRemainingMs){
        return fastRemainingMs>0 ? 1000L : 2000L;
    }

    /** PowerManager thermal constants (API 29+) mapped to wire strings; unknown stays "unknown". */
    static String thermalStatusName(int status){
        switch(status){
            case 0:return "none";
            case 1:return "light";
            case 2:return "moderate";
            case 3:return "severe";
            case 4:return "critical";
            case 5:return "emergency";
            case 6:return "shutdown";
            default:return UNKNOWN;
        }
    }

    /** RTC codec mimeType ("video/H264") to a short codec name, or null when not reported. */
    static String codecName(Map<String,Object> codec){
        String mimeType=text(codec,"mimeType",MAX_TEXT);
        if(mimeType==null)return null;
        int slash=mimeType.indexOf('/');
        String name=slash>=0?mimeType.substring(slash+1):mimeType;
        if(name.isEmpty()||name.length()>32)return null;
        return name.toUpperCase(Locale.ROOT);
    }

    /** Raw profile-level-id from the RTC codec fmtp line, or null when not reported. */
    static String codecProfile(Map<String,Object> codec){
        String fmtp=text(codec,"sdpFmtpLine",MAX_FMTP);
        if(fmtp==null)return null;
        Matcher matcher=PROFILE_LEVEL_ID.matcher(fmtp);
        if(!matcher.find())return null;
        return matcher.group(1).toLowerCase(Locale.ROOT);
    }

    private static String limitation(Map<String,Object> video){
        String reason=text(video,"qualityLimitationReason",MAX_TEXT);
        return reason!=null && LIMITATIONS.contains(reason)?reason:UNKNOWN;
    }

    /** Cumulative RTC counter: raw non-negative long, or -1 when not reported/not usable. */
    private static long counter(Map<String,Object> stats,String key){
        Object value=stats==null?null:stats.get(key);
        if(!(value instanceof Number))return -1;
        double number=((Number)value).doubleValue();
        if(!Double.isFinite(number) || number<0 || number>(double)MAX_SAFE_COUNTER)return -1;
        return (long)number;
    }

    private static double number(Map<String,Object> stats,String key){
        Object value=stats==null?null:stats.get(key);
        if(!(value instanceof Number))return Double.NaN;
        double number=((Number)value).doubleValue();
        return Double.isFinite(number)?number:Double.NaN;
    }

    private static long dimension(Map<String,Object> stats,String key){
        Object value=stats==null?null:stats.get(key);
        if(!(value instanceof Number))return 0;
        double number=((Number)value).doubleValue();
        return (long)clamp(number,0,MAX_DIMENSION);
    }

    private static String text(Map<String,Object> stats,String key,int maxLength){
        Object value=stats==null?null:stats.get(key);
        if(!(value instanceof String))return null;
        String text=(String)value;
        if(text.isEmpty()||text.length()>maxLength)return null;
        return text;
    }

    private static double clamp(double value,double min,double max){
        if(!Double.isFinite(value))return min;
        return value<min?min:Math.min(value,max);
    }
}
