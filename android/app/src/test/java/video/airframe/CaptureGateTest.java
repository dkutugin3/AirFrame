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

    @Test public void negotiationDoesNotStartCapture(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();
        assertFalse("SDP/ICE negotiation must not run the compositor",gate.shouldCapture());
    }

    @Test public void connectedReceiverCapturesUntilPeerLeft(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();
        gate.onConnected();
        assertTrue(gate.shouldCapture());
        gate.onPeerLeft();
        assertFalse(gate.shouldCapture());
        gate.onOffer();
        assertFalse(gate.shouldCapture());
        gate.onConnected();
        assertTrue(gate.shouldCapture());
    }

    @Test public void socketLostStopsCaptureUntilNextOffer(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();
        gate.onConnected();
        gate.onSocketLost();
        assertFalse(gate.shouldCapture());
        gate.onOffer();
        assertFalse(gate.shouldCapture());
        gate.onConnected();
        assertTrue(gate.shouldCapture());
    }

    @Test public void transientDisconnectionPausesAndRecoveryResumes(){
        CaptureGate gate=new CaptureGate();
        gate.onOffer();gate.onConnected();
        gate.onDisconnected();assertFalse(gate.shouldCapture());
        gate.onConnected();assertTrue(gate.shouldCapture());
        gate.onOffer();assertFalse("new negotiation is not connected",gate.shouldCapture());
    }

    @Test public void staleConnectedEventCannotWakeAnAbsentReceiver(){
        CaptureGate gate=new CaptureGate();
        gate.onConnected();assertFalse(gate.shouldCapture());
        gate.onOffer();gate.onPeerLeft();
        gate.onConnected();assertFalse(gate.shouldCapture());
        gate.onOffer();gate.onSocketLost();
        gate.onConnected();assertFalse(gate.shouldCapture());
    }
}
