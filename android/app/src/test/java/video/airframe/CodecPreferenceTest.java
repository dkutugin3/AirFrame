package video.airframe;

import org.junit.Test;
import org.webrtc.RtpCapabilities;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.assertEquals;

/** Stage 9b: codec preference ordering for the HEVC/H.264 compromise. */
public class CodecPreferenceTest {
    private static RtpCapabilities.CodecCapability codec(String name) {
        RtpCapabilities.CodecCapability capability=new RtpCapabilities.CodecCapability();
        capability.name=name;
        return capability;
    }

    private static List<String> names(List<RtpCapabilities.CodecCapability> codecs) {
        List<String> result=new ArrayList<>();
        for(RtpCapabilities.CodecCapability codec:codecs)result.add(codec.name);
        return result;
    }

    @Test
    public void h264FirstIsTheDefaultAndFallback() {
        List<RtpCapabilities.CodecCapability> codecs=new ArrayList<>();
        codecs.add(codec("VP8"));
        codecs.add(codec("H265"));
        codecs.add(codec("H264"));
        ProjectionService.sortCodecPreferences(codecs,false);
        assertEquals(names(codecs),List.of("H264","VP8","H265"));
    }

    @Test
    public void hevcFirstKeepsH264AsTheNextFallback() {
        List<RtpCapabilities.CodecCapability> codecs=new ArrayList<>();
        codecs.add(codec("VP8"));
        codecs.add(codec("H264"));
        codecs.add(codec("H265"));
        ProjectionService.sortCodecPreferences(codecs,true);
        assertEquals(names(codecs),List.of("H265","H264","VP8"));
    }

    @Test
    public void absentCodecsLeaveTheOrderStable() {
        List<RtpCapabilities.CodecCapability> codecs=new ArrayList<>();
        codecs.add(codec("VP9"));
        codecs.add(codec("VP8"));
        ProjectionService.sortCodecPreferences(codecs,true);
        assertEquals(names(codecs),List.of("VP9","VP8"));
    }
}
