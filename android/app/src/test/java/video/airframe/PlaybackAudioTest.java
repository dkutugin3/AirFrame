package video.airframe;

import org.junit.Test;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class PlaybackAudioTest {
    @Test public void failedStartupReleasesPartiallyCreatedResources() throws Exception {
        FakeBackend backend=new FakeBackend();
        backend.failStart=true;
        PlaybackAudio audio=new PlaybackAudio(backend,new CollectingSink(),ignored->{});
        audio.start();
        assertTrue("startup failure must release resources",backend.closed.await(2,TimeUnit.SECONDS));
        assertEquals(1,backend.closes.get());
        assertEquals(0,backend.reads.get());
        assertTrue(audio.stop());
    }

    @Test public void recorderErrorEndsWorkerInsteadOfRetrying() throws Exception {
        assertTerminalRead(-6);
    }

    @Test public void zeroBlockingReadEndsWorkerInsteadOfSpinning() throws Exception {
        assertTerminalRead(0);
    }

    private void assertTerminalRead(int result) throws Exception {
        FakeBackend backend=new FakeBackend();
        backend.readResult=result;
        PlaybackAudio audio=new PlaybackAudio(backend,new CollectingSink(),ignored->{});
        audio.start();
        assertTrue("terminal read must release resources",backend.closed.await(2,TimeUnit.SECONDS));
        assertEquals("failed recorder must never be polled in a hot loop",1,backend.reads.get());
        assertEquals(1,backend.closes.get());
        assertTrue(audio.stop());
    }

    @Test public void packetsKeepWireFlagsAndOffsetsWhileReusingStorage() throws Exception {
        FakeBackend backend=new FakeBackend(){
            @Override public int read(ByteBuffer pcm){return reads.incrementAndGet()==1?8192:-6;}
            @Override public void encode(ByteBuffer pcm,int length,PlaybackAudio.PacketConsumer output){
                assertEquals(8192,length);
                assertTrue(output.send(ByteBuffer.wrap(new byte[]{99,11,12,98}),1,2,true));
                assertTrue(output.send(ByteBuffer.wrap(new byte[]{97,21,22,96}),1,2,false));
            }
        };
        CollectingSink sink=new CollectingSink();
        PlaybackAudio audio=new PlaybackAudio(backend,sink,ignored->{});
        audio.start();
        assertTrue(backend.closed.await(2,TimeUnit.SECONDS));
        assertEquals(2,sink.packets.size());
        assertArrayEquals(new byte[]{0,11,12},sink.packets.get(0));
        assertArrayEquals(new byte[]{1,21,22},sink.packets.get(1));
        assertSame("equal-sized packets should reuse storage",sink.buffers.get(0),sink.buffers.get(1));
        assertTrue(audio.stop());
    }

    @Test public void stopUnblocksRecordingAndReleasesResources() throws Exception {
        FakeBackend backend=new BlockingBackend();
        PlaybackAudio audio=new PlaybackAudio(backend,new CollectingSink(),ignored->{});
        audio.start();
        assertTrue(backend.reading.await(2,TimeUnit.SECONDS));
        assertTrue(audio.stop());
        assertEquals(1,backend.closes.get());
        assertEquals(1,backend.stopRequests.get());
    }

    @Test public void delayedOldTeardownCannotReleaseTheNextSessionsResources() throws Exception {
        CountDownLatch allowOldClose=new CountDownLatch(1);
        CountDownLatch oldClosing=new CountDownLatch(1);
        FakeBackend oldBackend=new FakeBackend(){
            @Override public void close(){
                oldClosing.countDown();
                awaitUninterruptibly(allowOldClose);
                super.close();
            }
        };
        PlaybackAudio oldAudio=new PlaybackAudio(oldBackend,new CollectingSink(),ignored->{});
        oldAudio.start();
        assertTrue(oldClosing.await(2,TimeUnit.SECONDS));
        assertFalse("stop must return after its bounded wait",oldAudio.stop());
        assertTrue(oldAudio.isStopping());
        FakeBackend nextBackend=new BlockingBackend();
        PlaybackAudio nextAudio=new PlaybackAudio(nextBackend,new CollectingSink(),ignored->{});
        try {
            nextAudio.start();
            assertTrue(nextBackend.reading.await(2,TimeUnit.SECONDS));
            allowOldClose.countDown();
            assertTrue(oldBackend.closed.await(2,TimeUnit.SECONDS));
            assertEquals("old worker owns only old resources",0,nextBackend.closes.get());
            assertTrue(nextAudio.isAlive());
        } finally {
            allowOldClose.countDown();
            oldAudio.stop();
            nextAudio.stop();
        }
        assertEquals(1,oldBackend.closes.get());
        assertEquals(1,nextBackend.closes.get());
    }

    @Test public void completionRunsAfterReleaseWithoutMarkingFailureAsRequestedStop() throws Exception {
        FakeBackend backend=new FakeBackend();
        CountDownLatch completed=new CountDownLatch(1);
        AtomicInteger releasedAtCompletion=new AtomicInteger();
        PlaybackAudio audio=new PlaybackAudio(backend,new CollectingSink(),finished->{
            releasedAtCompletion.set(backend.closes.get());completed.countDown();
        });
        audio.start();
        assertTrue(completed.await(2,TimeUnit.SECONDS));
        assertEquals(1,releasedAtCompletion.get());
        assertFalse("recorder failure must not request an automatic retry",audio.isStopping());
        assertTrue(audio.stop());
    }

    @Test public void channelCloseWaitsForSendThenRejectsLateWorkerCalls() throws Exception {
        CountDownLatch sending=new CountDownLatch(1),finishSend=new CountDownLatch(1);
        AtomicInteger nativeCalls=new AtomicInteger(),closes=new AtomicInteger();
        PlaybackAudio.ChannelSink sink=new PlaybackAudio.ChannelSink(new PlaybackAudio.Transport(){
            public boolean isOpen(){nativeCalls.incrementAndGet();return true;}
            public boolean send(ByteBuffer packet){
                nativeCalls.incrementAndGet();sending.countDown();awaitUninterruptibly(finishSend);return true;
            }
            public void close(){closes.incrementAndGet();}
        });
        Thread sender=new Thread(()->sink.send(ByteBuffer.wrap(new byte[]{1})));
        CountDownLatch closeAttempted=new CountDownLatch(1),closed=new CountDownLatch(1);
        Thread closer=new Thread(()->{closeAttempted.countDown();sink.close();closed.countDown();});
        sender.start();
        try {
            assertTrue(sending.await(2,TimeUnit.SECONDS));
            closer.start();
            assertTrue(closeAttempted.await(2,TimeUnit.SECONDS));
            assertFalse("native channel cannot close during a send",closed.await(50,TimeUnit.MILLISECONDS));
        } finally {
            finishSend.countDown();sender.join(2000);closer.join(2000);
        }
        assertFalse(sender.isAlive());assertFalse(closer.isAlive());
        assertFalse(sink.isOpen());
        assertFalse(sink.send(ByteBuffer.wrap(new byte[]{2})));
        sink.close();
        assertEquals(1,closes.get());
        assertEquals("closed wrapper must never touch disposed native channel",1,nativeCalls.get());
    }

    private static class FakeBackend implements PlaybackAudio.Backend {
        final AtomicInteger reads=new AtomicInteger(),closes=new AtomicInteger(),stopRequests=new AtomicInteger();
        final CountDownLatch closed=new CountDownLatch(1),reading=new CountDownLatch(1),unblock=new CountDownLatch(1);
        boolean failStart;
        int readResult=-6;
        public void start(){if(failStart)throw new IllegalStateException("encoder startup failed");}
        public int read(ByteBuffer pcm){
            if(reads.incrementAndGet()>5)throw new IllegalStateException("guard against broken hot loop");
            return readResult;
        }
        public void encode(ByteBuffer pcm,int length,PlaybackAudio.PacketConsumer output){}
        public void stopRecording(){stopRequests.incrementAndGet();unblock.countDown();}
        public void close(){closes.incrementAndGet();closed.countDown();}
    }

    private static final class BlockingBackend extends FakeBackend {
        @Override public int read(ByteBuffer pcm){
            reads.incrementAndGet();reading.countDown();awaitUninterruptibly(unblock);return 0;
        }
    }

    private static final class CollectingSink implements PlaybackAudio.Sink {
        final List<byte[]> packets=new ArrayList<>();
        final List<ByteBuffer> buffers=new ArrayList<>();
        public boolean isOpen(){return true;}
        public boolean send(ByteBuffer packet){
            buffers.add(packet);
            byte[] copy=new byte[packet.remaining()];packet.get(copy);packets.add(copy);return true;
        }
    }

    private static void awaitUninterruptibly(CountDownLatch latch){
        boolean interrupted=false;
        while(true){try{latch.await();break;}catch(InterruptedException ignored){interrupted=true;}}
        if(interrupted)Thread.currentThread().interrupt();
    }
}
