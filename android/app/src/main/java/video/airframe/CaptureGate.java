package video.airframe;

/**
 * When the screen capture may run: only while a peer session is live.
 *
 * <p>Capture at display rate is the phone's heaviest idle cost (GPU compositing into the
 * virtual display, pacing and telemetry for every frame), so it starts lazily on the first
 * offer and stops when the peer disappears - the receiver window closed or the signalling
 * socket was lost - instead of running while the service waits for a receiver.
 */
final class CaptureGate {
    private boolean capturing;

    boolean shouldCapture(){return capturing;}

    /** A peer session wants frames; re-arms after any stop (reconnect, new receiver). */
    void onOffer(){capturing=true;}

    /** The receiver window closed; the service keeps waiting, the capture must not. */
    void onPeerLeft(){capturing=false;}

    /** Signalling was lost; nothing streams until a new offer arrives after reconnect. */
    void onSocketLost(){capturing=false;}
}
