"""Stage 8: bounded hysteresis controller for the quality/smoothness trade-off.

Inputs are the sender telemetry the desktop already sees every second: the
encoder's quality-limitation reason and the actual encoded fps. Outputs are
proposals only - the caller applies them through the normal profile path, so
a manual profile is never overwritten silently.

Contract (docs/IMPLEMENTATION_PLAN_TDD.md §11):
- priority "quality": never reduces resolution; sustained misses are reported.
- priority "smoothness": one controlled step of the long side 2560 -> 1920
  after LIMIT_STREAK consecutive limited seconds, never below the configured
  floor, at most one change per MIN_CHANGE_INTERVAL seconds; restoring the
  chosen size needs RECOVERY_STREAK consecutive clean seconds.

Streaks accumulate wall-clock seconds from the sample timestamps, not sample
counts: the sender's telemetry cadence (1 s while a session is young, 2 s once
stable) must not change how fast the controller reacts.

The thresholds are the plan's initial verification values; final values must
be chosen from target-phone reports and recorded before the comparison run.
"""

LIMIT_STREAK = 3          # consecutive limited seconds before one downgrade
RECOVERY_STREAK = 15      # consecutive clean seconds before restoring quality
MIN_CHANGE_INTERVAL = 10.0  # seconds between any two proposals
LADDER = (2560, 1920)     # the only controlled reduction step of the long side
MAX_STEP = 5.0            # longer gaps break a streak: silence is not evidence


class AdaptiveQuality:
    def __init__(self, base, floor=1920, priority="smoothness",
                 limit_streak=LIMIT_STREAK, recovery_streak=RECOVERY_STREAK,
                 min_change_interval=MIN_CHANGE_INTERVAL):
        if priority not in ("quality", "smoothness"):
            raise ValueError("Unknown priority")
        self.priority = priority
        self.floor = int(floor)
        self.limit_streak = int(limit_streak)
        self.recovery_streak = int(recovery_streak)
        self.min_change_interval = float(min_change_interval)
        self.base = dict(base)
        self._current = dict(base)
        self.clock = 0.0
        self._sampled = False
        self._limited_seconds = 0.0
        self._clean_seconds = 0.0
        self._last_change_at = None
        self._manual = False

    def current_settings(self):
        return dict(self._current)

    def manual_override(self, settings):
        """A user-applied profile wins: proposals stop until rearm()."""
        self._manual = True
        self._current = dict(settings)

    def rearm(self, base):
        """(Re)start managing from the given profile after a manual change."""
        self._manual = False
        self.base = dict(base)
        self._current = dict(base)
        self._limited_seconds = self._clean_seconds = 0.0
        self._last_change_at = None

    def _next_step_down(self):
        for size in LADDER:
            if self._current["maxSize"] > max(size, self.floor):
                return max(size, self.floor)
        return None

    def observe(self, limited, encoded_fps, now):
        """One telemetry sample. Returns a proposal dict or None.

        ``limited`` is the sender's quality-limitation reason ("none" when the
        encoder keeps up); ``encoded_fps`` is the actual encoded frame rate;
        ``now`` is monotonic seconds. Streaks are the wall-clock duration of the
        run, so a slower telemetry cadence does not slow the controller down.
        """
        step = now - self.clock
        self.clock = now
        if not self._sampled:
            self._sampled = True
            return None
        if self._manual:
            return None
        if step <= 0.0 or step > MAX_STEP or limited == "unknown":
            self._limited_seconds = self._clean_seconds = 0.0
            return None
        limited = limited != "none"
        if limited:
            self._limited_seconds += step
            self._clean_seconds = 0.0
        else:
            self._clean_seconds += step
            self._limited_seconds = 0.0
        if limited and self._limited_seconds >= self.limit_streak:
            smaller = None if self.priority == "quality" else self._next_step_down()
            if smaller is None:
                # At the floor (or in quality mode) the miss is reported once per
                # sustained run - exactly at the threshold crossing, which a slower
                # telemetry cadence still hits exactly once. Reporting is not gated
                # by the change cooldown.
                if self._limited_seconds - step < self.limit_streak:
                    return {"action": "notice",
                            "reason": f"60 FPS не удерживаются: кодировщик ограничен, "
                                      f"достигнут нижний предел {self.floor} px"}
                return None
            if self._last_change_at is not None and now - self._last_change_at < self.min_change_interval:
                return None
            settings = dict(self._current)
            settings["maxSize"] = smaller
            self._current = settings
            self._last_change_at = now
            self._limited_seconds = 0.0
            return {"action": "configure", "settings": dict(settings),
                    "reason": f"Плавность: длинная сторона {smaller} px на время ограничения"}
        if not limited and self._clean_seconds >= self.recovery_streak and self._current != self.base:
            self._current = dict(self.base)
            self._last_change_at = now
            self._clean_seconds = 0.0
            return {"action": "configure", "settings": dict(self._current),
                    "reason": "Плавность восстановлена: возвращён выбранный профиль"}
        return None
