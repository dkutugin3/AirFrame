package video.airframe;

import org.json.JSONObject;
import org.junit.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

/**
 * Stage-5 sender telemetry contract (plan section 8).
 *
 * <p>Pure-JVM tests: explicit timestamps (no sleeps), explicit window sizes and fake RTC stat maps.
 * The rules under test:
 * <ul>
 *   <li>window rates (capture/pacer/encoded FPS, actual bitrate, average encode time) are computed
 *       from the measurement window and always finite and bounded;</li>
 *   <li>cumulative RTC counters are reported raw and omitted when RTC does not report them - never
 *       fabricated as 0;</li>
 *   <li>0 means "measured zero"; absent/"unknown" means unknown (temperature, hardware encoder,
 *       codec);</li>
 *   <li>old telemetry fields stay present and numeric so old receivers keep working;</li>
 *   <li>counter resets, equal timestamps, NaN/Infinity and wrong-type stats cannot produce negative
 *       or non-finite values.</li>
 * </ul>
 */
public class SenderTelemetryTest {
    private static final long NANOS=1000000000L;

    @Test public void firstSampleMeasuresWindowAndRawCounters(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<120;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,i%2==0);
        Map<String,Object> sample=telemetry.sample(2*NANOS,-1,video(120,119,3000000L,0.24),h264());

        assertEquals("captureFps",120.0,decimal(sample,"captureFps"),0.01);
        assertEquals("pacerFps",60.0,decimal(sample,"pacerFps"),0.01);
        assertEquals("maxCaptureIntervalMs",8.33,decimal(sample,"maxCaptureIntervalMs"),0.02);
        assertEquals("telemetryWindowMs",1000.0,decimal(sample,"telemetryWindowMs"),0.01);
        assertEquals("framesEncoded raw",120L,whole(sample,"framesEncoded"));
        assertEquals("framesSent raw",119L,whole(sample,"framesSent"));
        // First sample has no previous RTC baseline; the old-schema rate field stays 0.
        assertEquals("encodedFps",0.0,decimal(sample,"encodedFps"),0.001);
        // 3 MB over 1000 ms = 24000 kbit/s actual video bitrate from bytesSent.
        assertEquals("videoBitrateKbps",24000.0,decimal(sample,"videoBitrateKbps"),1.0);
        // 0.24 s cumulative encode time over 120 frames = 2 ms average per encoded frame.
        assertEquals("avgEncodeTimeMs",2.0,decimal(sample,"avgEncodeTimeMs"),0.001);
        assertEquals("codec","H264",sample.get("codec"));
        assertEquals("codecProfile","42e01f",sample.get("codecProfile"));
        assertEquals("thermal unknown","unknown",sample.get("thermalStatus"));
        assertEquals("hardwareEncoder","unknown",sample.get("hardwareEncoder"));
        assertEquals("limitation","none",sample.get("limitation"));
        assertEquals("width",1280L,whole(sample,"width"));
        assertEquals("height",720L,whole(sample,"height"));
        assertJsonSerializable(sample);
    }

    @Test public void secondSampleUsesWindowDeltas(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<120;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        telemetry.sample(2*NANOS,-1,video(120,120,3000000L,0.24),h264());
        for(int i=0;i<120;i++)telemetry.onFrame(2*NANOS+i*NANOS/120,true);
        Map<String,Object> second=telemetry.sample(3*NANOS,-1,video(240,240,6000000L,0.48),h264());

        assertEquals("captureFps",120.0,decimal(second,"captureFps"),0.01);
        assertEquals("pacerFps",120.0,decimal(second,"pacerFps"),0.01);
        assertEquals("encodedFps window delta",120.0,decimal(second,"encodedFps"),0.01);
        assertEquals("actual bitrate from bytesSent delta",24000.0,decimal(second,"videoBitrateKbps"),1.0);
        assertEquals("average encode time window delta",2.0,decimal(second,"avgEncodeTimeMs"),0.001);
        assertEquals("framesEncoded stays cumulative",240L,whole(second,"framesEncoded"));
        assertJsonSerializable(second);
    }

    @Test public void counterResetDoesNotFabricateNegativeRates(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<120;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        telemetry.sample(2*NANOS,-1,video(1000,1000,1000000L,2.0),h264());
        for(int i=0;i<120;i++)telemetry.onFrame(2*NANOS+i*NANOS/120,true);
        Map<String,Object> after=telemetry.sample(3*NANOS,-1,video(10,10,500L,0.01),h264());

        assertEquals("encodedFps",0.0,decimal(after,"encodedFps"),0.001);
        assertFalse("bitrate is unknown after a counter reset",after.containsKey("videoBitrateKbps"));
        assertFalse("average encode time is unknown after a counter reset",after.containsKey("avgEncodeTimeMs"));
        assertEquals("raw counter still reported",10L,whole(after,"framesEncoded"));
        assertJsonSerializable(after);
    }

    @Test public void missingRtcFieldsAreOmittedNotZero(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<60;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        Map<String,Object> sample=telemetry.sample(2*NANOS,-1,null,null);

        for(String key:new String[]{"framesEncoded","framesSent","videoBitrateKbps","avgEncodeTimeMs",
                "codec","codecProfile","encoderImplementation","captureWidth","captureHeight"}){
            assertFalse("must be omitted when unknown: "+key,sample.containsKey(key));
        }
        // Old-schema fields stay present and numeric for old receivers.
        assertEquals("encodedFps",0.0,decimal(sample,"encodedFps"),0.001);
        assertEquals("width",0L,whole(sample,"width"));
        assertEquals("height",0L,whole(sample,"height"));
        assertEquals("limitation","unknown",sample.get("limitation"));
        assertEquals("thermalStatus","unknown",sample.get("thermalStatus"));
        assertEquals("hardwareEncoder","unknown",sample.get("hardwareEncoder"));
        assertEquals("captureFps measured from real frames",60.0,decimal(sample,"captureFps"),0.01);
        assertJsonSerializable(sample);
    }

    @Test public void nonFiniteAndWrongTypeStatsAreSanitized(){
        Map<String,Object> bad=new LinkedHashMap<>();
        bad.put("framesEncoded","120");                 // wrong type: not a number
        bad.put("framesSent",Double.NaN);               // non-finite
        bad.put("bytesSent",Double.POSITIVE_INFINITY);  // non-finite
        bad.put("totalEncodeTime",Double.NaN);
        bad.put("frameWidth",-5);                       // invalid dimension
        bad.put("frameHeight",1000000000L);             // out of bounds
        bad.put("qualityLimitationReason","alien");     // not in the allowed enum
        bad.put("encoderImplementation",42);            // wrong type
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<10;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        Map<String,Object> sample=telemetry.sample(2*NANOS,-1,bad,null);

        for(String key:new String[]{"framesEncoded","framesSent","videoBitrateKbps","avgEncodeTimeMs",
                "encoderImplementation","codec","codecProfile"}){
            assertFalse("must be omitted when not usable: "+key,sample.containsKey(key));
        }
        assertEquals("width clamped",0L,whole(sample,"width"));
        assertEquals("height clamped",16384L,whole(sample,"height"));
        assertEquals("limitation falls back to unknown","unknown",sample.get("limitation"));
        assertFinite(sample);
        assertJsonSerializable(sample);
    }

    @Test public void equalTimestampsDoNotDivideByZero(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        telemetry.onFrame(1*NANOS,true);
        telemetry.onFrame(1*NANOS,false);
        Map<String,Object> first=telemetry.sample(1*NANOS,-1,null,null);
        Map<String,Object> second=telemetry.sample(1*NANOS,-1,null,null);
        for(Map<String,Object> sample:new Map[]{first,second}){
            assertEquals("captureFps",0.0,decimal(sample,"captureFps"),0.001);
            assertEquals("pacerFps",0.0,decimal(sample,"pacerFps"),0.001);
            assertEquals("maxCaptureIntervalMs",0.0,decimal(sample,"maxCaptureIntervalMs"),0.001);
            assertEquals("telemetryWindowMs",0.0,decimal(sample,"telemetryWindowMs"),0.001);
            assertJsonSerializable(sample);
        }
    }

    @Test public void resetStartsNewMeasurementSession(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        for(int i=0;i<120;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        telemetry.sample(2*NANOS,-1,video(120,120,3000000L,0.24),h264());
        telemetry.reset(5*NANOS);
        for(int i=0;i<120;i++)telemetry.onFrame(5*NANOS+i*NANOS/120,i%2==0);
        Map<String,Object> sample=telemetry.sample(6*NANOS,-1,video(120,120,3000000L,0.24),h264());

        assertEquals("captureFps fresh window",120.0,decimal(sample,"captureFps"),0.01);
        assertEquals("pacerFps fresh window",60.0,decimal(sample,"pacerFps"),0.01);
        assertEquals("telemetryWindowMs",1000.0,decimal(sample,"telemetryWindowMs"),0.01);
        assertEquals("encodedFps has no baseline after reset",0.0,decimal(sample,"encodedFps"),0.001);
        assertEquals("framesEncoded raw",120L,whole(sample,"framesEncoded"));
        assertJsonSerializable(sample);
    }

    @Test public void timestampResetDoesNotFakeAHugeCaptureInterval(){
        SenderTelemetry telemetry=new SenderTelemetry(4*NANOS);
        long t=4900000000L;
        for(int i=0;i<120;i++)telemetry.onFrame(t+i*NANOS/120,true);
        telemetry.onFrame(1,true);                       // capture timestamps restarted at ~1 ns
        for(int i=0;i<120;i++)telemetry.onFrame(1+i*NANOS/120,true);
        Map<String,Object> sample=telemetry.sample(6*NANOS,-1,null,null);
        assertEquals("maximum capture interval ignores the backwards jump",
                8.33,decimal(sample,"maxCaptureIntervalMs"),0.02);
        assertJsonSerializable(sample);
    }

    @Test public void captureFormatIsReportedSeparatelyFromEncodedSize(){
        SenderTelemetry telemetry=new SenderTelemetry(1*NANOS);
        telemetry.setCaptureFormat(1280,720);
        for(int i=0;i<10;i++)telemetry.onFrame(1*NANOS+i*NANOS/120,true);
        Map<String,Object> sample=telemetry.sample(2*NANOS,-1,video(10,10,250000L,0.02),h264());
        assertEquals("captureWidth",1280L,whole(sample,"captureWidth"));
        assertEquals("captureHeight",720L,whole(sample,"captureHeight"));
        assertEquals("encoded width",1280L,whole(sample,"width"));
        telemetry.setCaptureFormat(0,0);   // rejected, previous format stays
        Map<String,Object> again=telemetry.sample(3*NANOS,-1,video(10,10,250000L,0.02),h264());
        assertEquals("captureWidth stays",1280L,whole(again,"captureWidth"));
    }

    @Test public void thermalStatusMappingNeverReturnsZeroForUnknown(){
        assertEquals("unknown",SenderTelemetry.thermalStatusName(SenderTelemetry.UNKNOWN_THERMAL));
        assertEquals("none",SenderTelemetry.thermalStatusName(0));
        assertEquals("light",SenderTelemetry.thermalStatusName(1));
        assertEquals("moderate",SenderTelemetry.thermalStatusName(2));
        assertEquals("severe",SenderTelemetry.thermalStatusName(3));
        assertEquals("critical",SenderTelemetry.thermalStatusName(4));
        assertEquals("emergency",SenderTelemetry.thermalStatusName(5));
        assertEquals("shutdown",SenderTelemetry.thermalStatusName(6));
        assertEquals("unknown",SenderTelemetry.thermalStatusName(99));
    }

    @Test public void codecAndProfileComeFromRtcOnlyWhenPresent(){
        assertEquals("H264",SenderTelemetry.codecName(h264()));
        assertEquals("42e01f",SenderTelemetry.codecProfile(h264()));
        assertEquals("VP8",SenderTelemetry.codecName(codec("video/VP8",null)));
        assertEquals(null,SenderTelemetry.codecProfile(codec("video/H264","packetization-mode=1")));
        assertEquals(null,SenderTelemetry.codecName(null));
        assertEquals(null,SenderTelemetry.codecProfile(null));
    }

    private static Map<String,Object> video(long framesEncoded,long framesSent,long bytesSent,double encodeSeconds){
        Map<String,Object> map=new LinkedHashMap<>();
        map.put("framesEncoded",framesEncoded);
        map.put("framesSent",framesSent);
        map.put("bytesSent",bytesSent);
        map.put("totalEncodeTime",encodeSeconds);
        map.put("frameWidth",1280);
        map.put("frameHeight",720);
        map.put("qualityLimitationReason","none");
        map.put("encoderImplementation","OMX.example.H264");
        return map;
    }

    private static Map<String,Object> h264(){
        return codec("video/H264","packetization-mode=1;profile-level-id=42e01f;level-asymmetry-allowed=1");
    }

    private static Map<String,Object> codec(String mimeType,String fmtp){
        Map<String,Object> map=new LinkedHashMap<>();
        map.put("mimeType",mimeType);
        if(fmtp!=null)map.put("sdpFmtpLine",fmtp);
        return map;
    }

    private static void assertJsonSerializable(Map<String,Object> sample){
        try{new JSONObject(sample).toString();}
        catch(Exception error){fail("telemetry fields must be valid JSON: "+error);}
    }

    private static void assertFinite(Map<String,Object> sample){
        for(Map.Entry<String,Object> entry:sample.entrySet()){
            if(entry.getValue() instanceof Double){
                double value=(Double)entry.getValue();
                assertTrue(entry.getKey()+" must be finite",Double.isFinite(value));
            }
        }
    }

    private static Object value(Map<String,Object> map,String key){
        assertTrue("missing telemetry field: "+key,map.containsKey(key));
        return map.get(key);
    }

    private static long whole(Map<String,Object> map,String key){
        Object value=value(map,key);
        assertTrue(key+" must be a number, was "+value,value instanceof Number);
        return ((Number)value).longValue();
    }

    private static double decimal(Map<String,Object> map,String key){
        Object value=value(map,key);
        assertTrue(key+" must be a number, was "+value,value instanceof Number);
        return ((Number)value).doubleValue();
    }
}
