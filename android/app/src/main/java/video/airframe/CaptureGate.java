package video.airframe;

/**
 * When the screen capture may run: a receiver wants it AND media is connected.
 *
 * <p>Capture at display rate is the phone's heaviest idle cost (GPU compositing into the
 * virtual display, pacing and telemetry for every frame). SDP/ICE negotiation needs the track
 * and its capabilities, not captured frames; disconnected backoff needs neither.
 */
final class CaptureGate {
    private boolean requested,connected;

    boolean shouldCapture(){return requested && connected;}

    /** A fresh peer must establish its own media connection before it may consume frames. */
    void onOffer(){requested=true;connected=false;}

    void onConnected(){connected=true;}
    void onDisconnected(){connected=false;}

    /** The receiver window closed; the service keeps waiting, the capture must not. */
    void onPeerLeft(){requested=false;connected=false;}

    /** Signalling was lost; nothing streams until a new offer arrives after reconnect. */
    void onSocketLost(){onPeerLeft();}
}
