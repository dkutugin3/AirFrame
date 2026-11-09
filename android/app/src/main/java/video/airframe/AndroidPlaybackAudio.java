package video.airframe;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.projection.MediaProjection;
import androidx.annotation.RequiresApi;
import java.nio.ByteBuffer;

/** Android resources for one PlaybackAudio worker; only stopRecording crosses threads. */
@RequiresApi(29)
final class AndroidPlaybackAudio implements PlaybackAudio.Backend {
    private final Context context;
    private final MediaProjection projection;
    private volatile AudioRecord recording;
    private MediaCodec encoder;
    private final MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
    private long pts;

    AndroidPlaybackAudio(Context context,MediaProjection projection){this.context=context;this.projection=projection;}

    public void start() throws Exception {
        // Startup runs on the worker, so permission may have changed after the service check.
        // The worker handles revocation exceptions and releases any partially opened resources.
        if(context.checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED)
            throw new SecurityException("Playback audio permission was revoked");
        recording=new AudioRecord.Builder()
                .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(48000).setChannelMask(AudioFormat.CHANNEL_IN_STEREO).build())
                .setBufferSizeInBytes(16384)
                .setAudioPlaybackCaptureConfig(new AudioPlaybackCaptureConfiguration.Builder(projection)
                        .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                        .addMatchingUsage(AudioAttributes.USAGE_GAME)
                        .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN).build()).build();
        encoder=MediaCodec.createEncoderByType("audio/mp4a-latm");
        MediaFormat format=MediaFormat.createAudioFormat("audio/mp4a-latm",48000,2);
        format.setInteger(MediaFormat.KEY_BIT_RATE,96000);
        format.setInteger(MediaFormat.KEY_AAC_PROFILE,MediaCodecInfo.CodecProfileLevel.AACObjectLC);
        encoder.configure(format,null,null,MediaCodec.CONFIGURE_FLAG_ENCODE);
        encoder.start();
        recording.startRecording();
    }

    public int read(ByteBuffer pcm){
        // 8192-byte blocking reads wake the capture worker about 23 times per second.
        return recording.read(pcm,8192);
    }

    public void encode(ByteBuffer pcm,int length,PlaybackAudio.PacketConsumer output){
        feedEncoder(pcm,length);
        int index;
        while((index=encoder.dequeueOutputBuffer(info,0))>=0){
            try{
                if(!output.send(encoder.getOutputBuffer(index),info.offset,info.size,
                        (info.flags&MediaCodec.BUFFER_FLAG_CODEC_CONFIG)!=0))return;
            }finally{encoder.releaseOutputBuffer(index,false);}
        }
    }

    /** Split reads across smaller codec input buffers; advance PTS only for queued PCM. */
    private void feedEncoder(ByteBuffer pcm,int length){
        pcm.position(0);pcm.limit(length);
        while(pcm.hasRemaining()){
            int index=encoder.dequeueInputBuffer(10000);
            if(index<0)return;
            ByteBuffer input=encoder.getInputBuffer(index);
            input.clear();
            int chunk=Math.min(pcm.remaining(),input.remaining());
            if(chunk<=0){encoder.queueInputBuffer(index,0,0,pts,0);return;}
            int limit=pcm.limit();pcm.limit(pcm.position()+chunk);input.put(pcm);pcm.limit(limit);
            encoder.queueInputBuffer(index,0,chunk,pts,0);
            pts+=chunk*1000000L/(48000*4);
        }
    }

    public void stopRecording(){
        AudioRecord current=recording;
        if(current!=null){try{current.stop();}catch(Exception ignored){}}
    }

    public void close(){
        // These fields belong only to this session, including partially completed startup.
        if(encoder!=null){
            try{encoder.stop();}catch(Exception ignored){}
            try{encoder.release();}catch(Exception ignored){}
            encoder=null;
        }
        AudioRecord current=recording;recording=null;
        if(current!=null){
            try{current.stop();}catch(Exception ignored){}
            try{current.release();}catch(Exception ignored){}
        }
    }
}
