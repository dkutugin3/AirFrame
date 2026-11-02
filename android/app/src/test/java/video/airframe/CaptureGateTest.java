package video.airframe;
import org.junit.Test;
import static org.junit.Assert.*;
public class CaptureGateTest {
    /** Before the first offer the service is waiting for a receiver: no capture. */
    @Test public void noCaptureBeforeFirstOffer(){
        CaptureGate gate=new CaptureGate();
        gate.onPeerLeft();gate.onSocketLost();
        assertFalse(gate.shouldCapture());
    }

    @Test public void offerArmsCaptureUntilPeerLeft(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();
        assertTrue(gate.shouldCapture());
        gate.onPeerLeft();
        assertFalse(gate.shouldCapture());
        gate.onOffer();
        assertTrue(gate.shouldCapture());
    }

    @Test public void socketLostStopsCaptureUntilNextOffer(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();
        gate.onSocketLost();
        assertFalse(gate.shouldCapture());
        gate.onOffer();
        assertTrue(gate.shouldCapture());
    }
}
