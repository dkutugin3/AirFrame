package video.airframe;

/** Limit capture before encoding; ScreenCapturerAndroid ignores its fps argument. */
final class FramePacer {
    private static final long NANOS_PER_SECOND=1000000000L;
    private long next;

    /**
     * @param now capture timestamp in nanoseconds. Monotonic within one capture epoch, but
     *            ScreenCapturerAndroid restarts the scale near 1 ns after a capture restart or
     *            rotation, so a large backwards jump means "new epoch", not "early frame".
     * @param fps target output frames per second
     * @return true when the frame should be forwarded to the encoder
     */
    boolean accept(long now,int fps) {
        long period=NANOS_PER_SECOND/fps;
        if(next==0){next=now+period;return true;}
        long early=next-now;
        if(early>0){
            if(early<=period)return false;
            // A timestamp reset restarted the capture scale: keep pacing from the new epoch
            // instead of retaining the stale `next` from the previous scale.
            next=now+period;
            return true;
        }
        // Frame is due: keep the cadence, but after a real gap start a fresh epoch so a long
        // pause cannot release a burst of frames.
        next=now>=next+period ? now+period : next+period;
        return true;
    }
}
