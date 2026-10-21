package video.airframe;

/** Limit capture before encoding; ScreenCapturerAndroid ignores its fps argument. */
final class FramePacer {
    private long next;
    boolean accept(long now,int fps) {
        long period=1000000000L/fps;
        if(next!=0 && now<next && next-now<1000000000L)return false;
        next=Math.max(next+period,now+period/2);
        return true;
    }
}
