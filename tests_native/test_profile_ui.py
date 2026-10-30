"""Stage 1 native contract: session profile, profile acknowledgements, playout modes.

Run with a Qt-capable interpreter (Docker's tests/ image has no PySide6):

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests_native -v
"""
import unittest

try:  # package import when discovered with top_level_dir=repo root
    from tests_native import qt_harness
except ImportError:  # discovery imports test modules as top-level names
    import qt_harness

from airframe.desktop import PRESETS
from airframe.server import video_settings

BASE = {"fps": 60, "bitrate": 24, "maxSize": 2560}
LIGHT = {"fps": 60, "bitrate": 16, "maxSize": 1920}


class ProfileContractTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()

    def test_initial_controls_and_apply_match_session(self):
        """A new window shows the session profile; Apply without edits changes nothing."""
        scenario = qt_harness.ProfileScenario(sender=True, auto_ack=True)
        try:
            window, server = scenario.window, scenario.server
            self.assertEqual(video_settings({}), BASE)
            # Immediately after window creation the preset, every field and the
            # room settings coincide.
            self.assertEqual(server.room.settings, BASE)
            self.assertEqual(window.target_fps.currentData(), BASE["fps"])
            self.assertEqual(window.max_size.currentData(), BASE["maxSize"])
            self.assertEqual(window.rate.value(), BASE["bitrate"])
            base_preset = next(name for name, profile in PRESETS.items() if profile == BASE)
            self.assertEqual(window.profile.currentText(), base_preset)
            # Pressing Apply without editing sends exactly the session profile and
            # must not change it.
            window.apply_button.click()
            qt_harness.pump_until(lambda: "подтвердил" in window.profile_result.text(),
                                  description="phone profile confirmation")
            self.assertEqual(window.profile_ack_state, "applied")
            configure = scenario.sender.received("configure")
            self.assertEqual(len(configure), 1)
            self.assertEqual(configure[0]["settings"], BASE)
            self.assertTrue(configure[0]["requestId"])
            self.assertEqual(server.room.settings, BASE)
            self.assertEqual(window.target_fps.currentData(), BASE["fps"])
            self.assertEqual(window.max_size.currentData(), BASE["maxSize"])
            self.assertEqual(window.rate.value(), BASE["bitrate"])
            self.assertEqual(window.profile.currentText(), base_preset)
            self.assertTrue(window.apply_button.isEnabled())
        finally:
            scenario.close()


class ProfileAckTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()

    def choose(self, window, name):
        window.profile.setCurrentText(name)
        self.assertEqual(window.profile.currentText(), name)
        return PRESETS[name]

    def test_profile_ack_matches_effective_settings(self):
        """Offline save, applied, rejected and timeout must be reported honestly.

        A rejection must never be presented as an applied profile, and the UI
        and session must show the profile that is still effectively running.
        """
        with self.subTest(state="offline-save"):
            self.check_offline_save()
        with self.subTest(state="applied"):
            self.check_applied()
        with self.subTest(state="rejected"):
            self.check_rejected()
        with self.subTest(state="timeout"):
            self.check_timeout_and_late_ack()

    def check_offline_save(self):
        scenario = qt_harness.ProfileScenario()
        try:
            window, server = scenario.window, scenario.server
            requested = self.choose(window, "Плавность · 60 fps")
            window.apply_button.click()
            qt_harness.pump_until(lambda: "Сохранено" in window.profile_result.text(),
                                  description="offline profile save")
            self.assertEqual(server.room.settings, requested)
            self.assertNotIn("подтвердил", window.profile_result.text())
            self.assertTrue(window.apply_button.isEnabled())
            self.assertEqual(window.profile_ack_state, "saved")
        finally:
            scenario.close()

    def check_applied(self):
        scenario = qt_harness.ProfileScenario(sender=True, auto_ack=True)
        try:
            window, server = scenario.window, scenario.server
            requested = self.choose(window, "Плавность · 60 fps")
            window.apply_button.click()
            qt_harness.pump_until(lambda: "подтвердил" in window.profile_result.text(),
                                  description="applied profile ack")
            configure = scenario.sender.received("configure")
            self.assertEqual(configure[0]["settings"], requested)
            self.assertEqual(server.room.settings, requested)
            self.assertEqual(window.target_fps.currentData(), requested["fps"])
            self.assertEqual(window.max_size.currentData(), requested["maxSize"])
            self.assertEqual(window.rate.value(), requested["bitrate"])
            self.assertEqual(window.profile_ack_state, "applied")
        finally:
            scenario.close()

    def check_rejected(self):
        scenario = qt_harness.ProfileScenario(sender=True, auto_ack=False)
        try:
            window, server = scenario.window, scenario.server
            previous = dict(video_settings({}))
            base_preset = next(name for name, profile in PRESETS.items() if profile == previous)
            self.choose(window, "Плавность · 60 fps")
            window.apply_button.click()
            qt_harness.pump_until(lambda: "отклонил" in window.profile_result.text(),
                                  description="rejected profile ack")
            self.assertNotIn("подтвердил применение", window.profile_result.text())
            # Effective settings are still the previous profile, in the session
            # and in the controls; the rejected wish is not presented as applied.
            self.assertEqual(server.room.settings, previous)
            self.assertEqual(window.target_fps.currentData(), previous["fps"])
            self.assertEqual(window.max_size.currentData(), previous["maxSize"])
            self.assertEqual(window.rate.value(), previous["bitrate"])
            self.assertEqual(window.profile.currentText(), base_preset)
            self.assertTrue(window.apply_button.isEnabled())
            self.assertEqual(window.profile_ack_state, "rejected")
        finally:
            scenario.close()

    def check_timeout_and_late_ack(self):
        scenario = qt_harness.ProfileScenario(sender=True, auto_ack=None)
        try:
            window, server = scenario.window, scenario.server
            window.profile_ack_timeout_ms = 200
            requested = self.choose(window, "Плавность · 60 fps")
            window.apply_button.click()
            qt_harness.pump_until(lambda: "не подтвердил" in window.profile_result.text(),
                                  timeout=5, description="profile ack timeout")
            self.assertTrue(window.apply_button.isEnabled())
            self.assertEqual(window.profile_ack_state, "timeout")
            # An unconfirmed profile is saved for the next connection, not
            # reported as applied to the running phone.
            self.assertEqual(server.room.settings, requested)
            configure = scenario.sender.received("configure")
            self.assertEqual(len(configure), 1)
            request_id = configure[0]["requestId"]
            # A mismatched/duplicate acknowledgement must not resolve the pending request.
            scenario.sender.send({"type": "configured", "requestId": "stale-ack",
                                  "applied": True, "settings": configure[0]["settings"]})
            qt_harness.pump_for(0.3)
            self.assertEqual(window.profile_ack_state, "timeout")
            self.assertEqual(server.room.settings, requested)
            # A late acknowledgement for the real request still reconciles honestly.
            scenario.sender.send({"type": "configured", "requestId": request_id,
                                  "applied": True, "settings": configure[0]["settings"]})
            qt_harness.pump_until(lambda: window.profile_ack_state == "applied",
                                  description="late profile ack")
            self.assertEqual(server.room.settings, requested)
        finally:
            scenario.close()


class PlayoutModeTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()
        self.server = qt_harness.StubServer()
        self.window = qt_harness.Desktop(self.server)
        self.window.show()

    def tearDown(self):
        qt_harness.close_window(self.window)

    def test_playout_mode_switch_uses_stable_value(self):
        """Playout modes are selected by stable value low/balanced/stable, not index.

        Reordering the combo-box items must not change which mode is selected.
        """
        combo = self.window.playout_mode
        self.assertEqual([combo.itemData(i) for i in range(combo.count())],
                         ["stable", "balanced", "low"])
        self.assertEqual(combo.currentData(), "stable")  # session default
        # Reorder the items: the stable values stay attached to their labels.
        items = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
        combo.clear()
        for label, value in reversed(items):
            combo.addItem(label, value)
        self.assertEqual([combo.itemData(i) for i in range(combo.count())],
                         ["low", "balanced", "stable"])
        self.assertNotEqual(combo.findData("low"), 2)
        self.window.select_playout_mode("low")
        self.assertEqual(self.server.playout_modes[-1], "low")
        self.assertEqual(combo.currentData(), "low")
        self.window.select_playout_mode("stable")
        self.assertEqual(self.server.playout_modes[-1], "stable")
        self.assertEqual(combo.currentData(), "stable")
        with self.assertRaises(ValueError):
            self.window.select_playout_mode("unknown")


if __name__ == "__main__":
    unittest.main()
