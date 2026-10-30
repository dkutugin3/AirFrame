"""Stage 8 wiring: telemetry reaches the controller, manual profile wins.

Offscreen; deterministic through an injected adaptive clock.
"""
import unittest

try:
    from tests_native import qt_harness
except ImportError:  # pragma: no cover - discovery from tests_native
    import qt_harness


class AdaptiveWiringTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()
        self.scenario = None

    def tearDown(self):
        if self.scenario is not None:
            self.scenario.close()

    def window(self):
        self.scenario = qt_harness.ProfileScenario()
        server = self.scenario.server
        self.calls = []
        original = server.configure
        def spy(settings, request_id=""):
            self.calls.append((dict(settings), request_id))
            return original(settings, request_id)
        server.configure = spy
        return self.scenario.window, server

    @staticmethod
    def stats(limitation, fps):
        return {"fps": fps, "bitrate": 1.0, "jitter": 0.0, "loss": 0.0,
                "encodedFps": fps, "width": 2560, "height": 1183,
                "limitation": limitation}

    def test_sustained_limitation_sends_one_downgrade_and_reports_the_floor(self):
        window, server = self.window()
        clock = [0.0]
        window.adaptive_now = lambda: (clock.__setitem__(0, clock[0] + 1.0) or clock[0])
        window.smoothness.setChecked(True)
        qt_harness.pump_for(0.02)
        for _ in range(3):
            window.on_stats(self.stats("cpu", 41.0))
            qt_harness.pump_for(0.02)
        downgrades = [settings for settings, request_id in self.calls
                      if settings["maxSize"] == 1920]
        self.assertEqual(downgrades, [{"fps": 60, "bitrate": 24, "maxSize": 1920}])
        for _ in range(4):
            window.on_stats(self.stats("cpu", 38.0))
            qt_harness.pump_for(0.02)
        self.assertIn("60 FPS не удерживаются", window.profile_result.text())
        self.assertEqual(len(self.calls), 1)

    def test_manual_apply_disables_adaptation_until_rearmed(self):
        window, server = self.window()
        clock = [0.0]
        window.adaptive_now = lambda: (clock.__setitem__(0, clock[0] + 1.0) or clock[0])
        window.smoothness.setChecked(True)
        window.profile.setCurrentText("Качество · 60 fps")
        window.apply_button.click()
        qt_harness.pump_for(0.05)
        manual_count = len(self.calls)
        for _ in range(6):
            window.on_stats(self.stats("bandwidth", 30.0))
            qt_harness.pump_for(0.02)
        self.assertEqual(len(self.calls), manual_count,
                         "a manual profile must not be overwritten automatically")
        window.smoothness.setChecked(False)
        window.smoothness.setChecked(True)
        for _ in range(3):
            window.on_stats(self.stats("cpu", 41.0))
            qt_harness.pump_for(0.02)
        self.assertEqual(len(self.calls), manual_count + 1)


if __name__ == "__main__":
    unittest.main()
