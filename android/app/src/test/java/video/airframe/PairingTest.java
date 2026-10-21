package video.airframe;
import org.junit.Test;
import static org.junit.Assert.*;
public class PairingTest {
    private static final String LINK="airframe://connect?v=1&host=192.168.1.5&port=8765&key=abcdefghijklmnopqrstuvwxyzABCDEF";
    @Test public void valid(){Pairing p=Pairing.parse(LINK);assertEquals("192.168.1.5:8765",p.address);assertEquals(32,p.key.length());}
    @Test public void invalid(){
        String[] bad={"https://example.com",LINK+"&v=1",LINK.replace("v=1","v=2"),LINK.replace("192.168.1.5","999.1.1.1"),LINK.replace("8765","0"),LINK.replace("8765","65536"),LINK.replace("connect?","connect/path?"),LINK+"#fragment",LINK.replace("abcdefghijklmnopqrstuvwxyzABCDEF","12345678")};
        for(String value:bad){try{Pairing.parse(value);fail("Accepted invalid QR");}catch(IllegalArgumentException expected){}}
    }
}
