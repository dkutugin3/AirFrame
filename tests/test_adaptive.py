"""Stage 8: the quality/smoothness compromise controller.

Deterministic virtual-clock tests for the hysteresis contract of
docs/IMPLEMENTATION_PLAN_TDD.md §11: a single bad sample changes nothing,
sustained limitation allows one controlled step 2560->1920, nothing happens
below the chosen floor, restoration needs sustained recovery, and a manual
profile is never overwritten silently. Thresholds are the plan's initial
verification values; final values require target-phone reports.
"""
import unittest

from airframe.adaptive import AdaptiveQuality

BASE = {"fps": 60, "bitrate": 24, "maxSize": 2560}
FLOOR_PROFILE = {"fps": 60, "bitrate": 24, "maxSize": 1920}


class AdaptiveQualityTests(unittest.TestCase):
    def setUp(self):
        self.controller = AdaptiveQuality(base=dict(BASE), floor=1920)

    def tick(self, seconds, limited, fps=41.0):
        """Advance whole seconds; return the list of actions produced."""
        actions = []
        for _ in range(seconds):
            action = self.controller.observe(
                limited="cpu" if limited else "none", encoded_fps=fps, now=self.controller.clock + 1.0)
            if action is not None:
                actions.append(action)
        return actions

    def configured(self, actions):
        return [a["settings"] for a in actions if a["action"] == "configure"]

    def test_single_bad_sample_changes_nothing(self):
        self.assertEqual(self.tick(1, limited=True), [])
        self.assertEqual(self.controller.current_settings(), BASE)

    def test_sustained_limitation_allows_exactly_one_step(self):
        self.tick(2, limited=True)
        actions = self.tick(1, limited=True)          # third consecutive second
        self.assertEqual(self.configured(actions), [FLOOR_PROFILE])
        # Continued limitation proposes nothing further: no second step, no flapping.
        self.assertEqual(self.configured(self.tick(12, limited=True)), [])
        self.assertEqual(self.controller.current_settings(), FLOOR_PROFILE)

    def test_below_the_floor_no_silent_reduction_only_a_notice(self):
        self.tick(3, limited=True)
        # The floor is reported once per sustained run, not silently endured.
        notices = [a for a in self.tick(11, limited=True) if a["action"] == "notice"]
        self.assertTrue(notices, "the floor must be reported, not silently endured")
        self.assertIn("60 FPS не удерживаются", notices[-1]["reason"])
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.controller.current_settings(), FLOOR_PROFILE)

    def test_blips_do_not_count_toward_the_streak(self):
        self.tick(2, limited=True)
        self.tick(1, limited=False)                   # blip resets the streak
        self.assertEqual(self.configured(self.tick(2, limited=True)), [])
        actions = self.tick(1, limited=True)
        self.assertEqual(self.configured(actions), [FLOOR_PROFILE])

    def test_recovery_upgrade_needs_fifteen_sustained_seconds(self):
        self.assertEqual(self.configured(self.tick(4, limited=True)), [FLOOR_PROFILE])
        # 14 clean seconds are not enough, and a limited blip restarts the clock.
        for _ in range(14):
            self.controller.observe(limited="none", encoded_fps=60.0,
                                    now=self.controller.clock + 1.0)
        self.controller.observe(limited="cpu", encoded_fps=40.0, now=self.controller.clock + 1.0)
        for _ in range(14):
            self.controller.observe(limited="none", encoded_fps=60.0,
                                    now=self.controller.clock + 1.0)
        self.assertEqual(self.controller.current_settings(), FLOOR_PROFILE)
        action = self.controller.observe(limited="none", encoded_fps=60.0,
                                         now=self.controller.clock + 1.0)
        self.assertEqual(action["action"], "configure")
        self.assertEqual(action["settings"], BASE)

    def test_no_upgrade_back_while_still_limited(self):
        self.tick(3, limited=True)
        self.tick(1, limited=True)
        # Recovery requires sustained absence of limitation, not just time.
        self.assertEqual(self.configured(self.tick(30, limited=True)), [])

    def test_quality_priority_never_downgrades(self):
        controller = AdaptiveQuality(base=dict(BASE), floor=1920, priority="quality")
        for _ in range(10):
            action = controller.observe(limited="cpu", encoded_fps=30.0, now=controller.clock + 1.0)
        self.assertIsNone(action)
        self.assertEqual(controller.current_settings(), BASE)

    def test_manual_override_disables_until_rearmed(self):
        self.tick(3, limited=True)
        self.tick(1, limited=True)                    # downgrade happened
        self.controller.manual_override({"fps": 60, "bitrate": 24, "maxSize": 2560})
        self.assertEqual(self.configured(self.tick(5, limited=True)), [])
        # Re-arming restarts the streak logic from the manual profile.
        self.controller.rearm({"fps": 60, "bitrate": 24, "maxSize": 2560})
        self.assertEqual(self.configured(self.tick(3, limited=True)), [FLOOR_PROFILE])

    def test_floor_equal_to_base_never_configures(self):
        controller = AdaptiveQuality(base=dict(FLOOR_PROFILE), floor=1920)
        actions = []
        for _ in range(10):
            action = controller.observe(limited="cpu", encoded_fps=30.0, now=controller.clock + 1.0)
            if action:
                actions.append(action)
        self.assertEqual(self.configured(actions), [])
        self.assertTrue(any(a["action"] == "notice" for a in actions))


if __name__ == "__main__":
    unittest.main()
