package video.airframe;

import org.json.JSONObject;
import org.junit.Test;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

import static org.junit.Assert.assertEquals;

/**
 * Stage-1 Android half of the unified profile contract (plan section 4).
 *
 * <p>The desktop peer sends the session profile in the {@code peer-ready}/{@code configure}
 * messages. When settings are missing (old receiver, peer-ready without settings) the sender must
 * fall back to the base profile 60 FPS / 24 Mbit/s / 2560 px, not to the retired 30/10/1920
 * initial values or the retired 120/80/2560 configure defaults. Settings that are present, such as
 * the experimental 120 FPS mode, are still honoured unchanged for old clients.
 */
public class ProfileDefaultsTest {
    @Test public void freshSenderUsesBaseProfile() throws Exception {
        assertBaseProfile(new ProjectionService());
    }

    @Test public void peerReadyWithoutSettingsKeepsBaseProfile() throws Exception {
        ProjectionService service = new ProjectionService();
        configure(service, new JSONObject());
        assertBaseProfile(service);
    }

    @Test public void missingFieldsFallBackToBaseProfile() throws Exception {
        ProjectionService service = new ProjectionService();
        configure(service, new JSONObject("{\"fps\":30}"));
        assertEquals("honoured fps", 30, intField(service, "fps"));
        assertEquals("bitrate fallback", 24, intField(service, "bitrate"));
        assertEquals("maxSize fallback", 2560, intField(service, "maxSize"));
    }

    @Test public void oldReceiverSettingsStillReachTheEncoder() throws Exception {
        ProjectionService service = new ProjectionService();
        configure(service, new JSONObject("{\"fps\":120,\"bitrate\":80,\"maxSize\":1920}"));
        assertEquals(120, intField(service, "fps"));
        assertEquals(80, intField(service, "bitrate"));
        assertEquals(1920, intField(service, "maxSize"));
    }

    @Test public void nonPositiveSettingsFallBackToBaseProfile() throws Exception {
        ProjectionService service = new ProjectionService();
        configure(service, new JSONObject("{\"fps\":0,\"bitrate\":-1,\"maxSize\":0}"));
        assertBaseProfile(service);
    }

    @Test public void offerSettingsKeepHevcAcrossRepeatedNegotiations() throws Exception {
        ProjectionService service=new ProjectionService();
        configure(service,new JSONObject("{\"fps\":60,\"bitrate\":24,\"maxSize\":2560,\"preferHevc\":true}"));
        Method snapshot=ProjectionService.class.getDeclaredMethod("currentSettings");
        snapshot.setAccessible(true);
        for(int i=0;i<3;i++){
            JSONObject settings=(JSONObject)snapshot.invoke(service);
            assertEquals(true,settings.getBoolean("preferHevc"));
            configure(service,settings);
        }
    }

    private static void configure(ProjectionService service, JSONObject settings) throws Exception {
        Method configure = ProjectionService.class.getDeclaredMethod("configure", JSONObject.class);
        configure.setAccessible(true);
        configure.invoke(service, settings);
    }

    private static void assertBaseProfile(ProjectionService service) throws Exception {
        assertEquals("fps", 60, intField(service, "fps"));
        assertEquals("bitrate", 24, intField(service, "bitrate"));
        assertEquals("maxSize", 2560, intField(service, "maxSize"));
    }

    private static int intField(ProjectionService service, String name) throws Exception {
        Field field = ProjectionService.class.getDeclaredField(name);
        field.setAccessible(true);
        return field.getInt(service);
    }
}
