package video.airframe;

import java.nio.ByteBuffer;
import java.util.function.Consumer;

/** One playback-capture worker owns its backend until release, including failed startup. */
final class PlaybackAudio {
    interface PacketConsumer {
        boolean send(ByteBuffer encoded,int offset,int size,boolean config);
    }
    interface Backend {
        void start() throws Exception;
        int read(ByteBuffer pcm) throws Exception;
        void encode(ByteBuffer pcm,int length,PacketConsumer output) throws Exception;
        void stopRecording();
        void close();
    }
    interface Sink {
        boolean isOpen();
        boolean send(ByteBuffer packet);
    }
    interface Transport extends Sink {void close();}
    static final class ChannelSink implements Sink {
        private final Transport transport;
        private boolean closed;
        ChannelSink(Transport transport){this.transport=transport;}
        public synchronized boolean isOpen(){return !closed && transport.isOpen();}
        public synchronized boolean send(ByteBuffer packet){return !closed && transport.send(packet);}
        synchronized void close(){
            if(closed)return;
            closed=true;
            transport.close();
        }
    }
    private final Backend backend;
    private final Sink sink;
    private final Consumer<PlaybackAudio> onStopped;
    private final Thread worker;
    private final PacketConsumer output=this::sendPacket;
    private volatile boolean running,stopRequested;
    private ByteBuffer message=ByteBuffer.allocate(1024);

    PlaybackAudio(Backend backend,Sink sink,Consumer<PlaybackAudio> onStopped){
        this.backend=backend;this.sink=sink;this.onStopped=onStopped;
        worker=new Thread(this::capture,"AirFrame-audio");
    }
    void start(){running=true;worker.start();}
    boolean stop(){
        stopRequested=true;running=false;
        try{backend.stopRecording();}catch(Exception ignored){}
        worker.interrupt();
        if(Thread.currentThread()!=worker){
            try{worker.join(500);}catch(InterruptedException ignored){Thread.currentThread().interrupt();}
        }
        return !worker.isAlive();
    }
    boolean isAlive(){return worker.isAlive();}
    boolean isStopping(){return stopRequested;}

    private void capture(){
        ByteBuffer pcm=ByteBuffer.allocateDirect(16384);
        try{
            if(!running)return;
            backend.start();
            while(running && sink.isOpen()){
                int read=backend.read(pcm);
                // This is a blocking recorder: errors and anomalous empty reads are terminal.
                // Retrying a failed native recorder here can otherwise spin an entire CPU core.
                if(read<=0 || !running)break;
                backend.encode(pcm,read,output);
            }
        }catch(Exception ignored){
        }finally{
            running=false;
            try{backend.close();}finally{onStopped.accept(this);}
        }
    }

    private boolean sendPacket(ByteBuffer encoded,int offset,int size,boolean config){
        if(!running || !sink.isOpen())return false;
        if(size<=0)return true;
        if(message.capacity()<size+1)message=ByteBuffer.allocate(Math.max(message.capacity()*2,size+1));
        message.clear();
        encoded.limit(offset+size);encoded.position(offset);
        message.put((byte)(config?0:1)).put(encoded).flip();
        // The pinned DataChannel.send copies the remaining bytes synchronously before returning.
        // Reusing this storage removes the extra payload array and per-packet message allocation.
        if(!sink.send(message)){running=false;return false;}
        return true;
    }
}
