# AirFrame profile contract (stage 1)

This document records the unified video-profile contract introduced in stage 1
of the stable-60-fps TDD work, including the deliberate behaviour changes and
the acknowledgement rules. It is a run/behaviour document; the binding plan is
`docs/IMPLEMENTATION_PLAN_TDD.md`.

## Base profile of a new session

- **60 fps / 2560 px long side / at most 24 Mbit/s / stable OBS playout.**
  1920/16 at 60 fps and the 30 fps profile stay available for weaker machines.
  The experimental 120 fps preset stays selectable but is never a default.
- The bitrate is an upper bound, not a promised actual video bitrate.
- Single source of truth on the desktop side: `airframe/server.py`
  `BASE_PROFILE` (and `DEFAULTS = dict(BASE_PROFILE)`).
  `airframe/desktop.py` `PRESETS["Качество · 60 fps"]` is `dict(BASE_PROFILE)`.
  The Android sender mirrors the same fallback values in `configure()`.
- A newly created window initialises the preset selector, the fps/resolution/
  bitrate controls and the room settings from the same session profile, so the
  values shown before the first edit are exactly the values a phone is asked to
  open with. Pressing **Apply** without editing sends that identical profile and
  changes nothing.
- The 10 s ack timer and the light 60/16/1920 profile are unchanged choices that
  keep old clients and weaker machines working.

Behaviour change vs. 0.4.0: the sender is no longer asked for 120 fps / 80
Mbit/s by default, and the window no longer shows hard-coded 30 fps / 10 Mbit/s
/ 1920 px while the session is already running a different profile.

## Request / acknowledgement semantics

Message shapes on the wire are unchanged, so old clients keep working:

```text
receiver -> server -> sender: {"type": "configure", "settings": {"fps", "bitrate", "maxSize"}, "requestId"}
sender  -> server -> receiver: {"type": "configured", "requestId", "applied": bool, "settings": {...}}
```

Session truth (`Room.request_profile` / `Room.acknowledge_profile`):

- A requested profile is validated and **saved for the next connection**
  immediately. While a phone is connected it only becomes *effective* after an
  acknowledgement for that exact request.
- `applied: true` -> the session adopts the settings the phone reports (the
  phone may clamp values; its report wins over the wish).
- `applied: false` -> the session keeps the **previously effective** profile; a
  rejection is never recorded as an applied profile.
- An acknowledgement for an unknown, stale or already-consumed `requestId`
  cannot change the session profile.
- Disconnecting the sender clears the pending request.

UI states (`Desktop.profile_ack_state`, for tests and diagnostics):

| state | meaning | UI message |
|---|---|---|
| `None` | nothing requested yet | "Профиль ещё не отправлен" |
| `pending` | command sent, waiting for the phone | "Ожидаем подтверждение от телефона…" |
| `saved` | no phone connected; stored for the next connection, **not applied now** | "Сохранено. Применится при подключении телефона." |
| `applied` | phone accepted the settings (`setParameters()` accepted, which does **not** prove 60 fps) | "Телефон подтвердил профиль: … Фактические fps зависят от телефона, сети и компьютера." |
| `rejected` | phone refused; previous profile stays shown in the fields | "Телефон отклонил профиль. Действует прежний профиль: …" |
| `timeout` | no ack within `PROFILE_ACK_TIMEOUT_MS` (10 s); the profile stays saved for the next connection and is **not** reported as applied | "Телефон не подтвердил профиль за 10 с. …" |

A matching late acknowledgement received after a timeout still reconciles the
profile honestly. Saved profiles live in memory for the running desktop app;
they are not persisted across desktop restarts.

## Playout modes

Playout modes are identified by stable values `stable`, `balanced`, `low`
(`QComboBox` item data). Select them by value, never by item position:

- `window.select_playout_mode("low")` (or `combo.findData(value)` + index);
- reordering the combo-box items must not change the selected mode;
- production handlers read `currentData()` and ignore the `None` data emitted
  while the combo is being rebuilt.

The OBS-stable buffer is a playout reserve, not the full phone-to-PC latency.
`stable` remains the session default.

## Verification

Headless contract (Docker, no PySide6):

```sh
export DOCKER_CONFIG=$PWD/.docker
sh scripts/test-docker.sh                                  # whole suite
docker exec af-test python -m unittest tests.test_signaling -v
```

Named stage-1 tests: `test_new_session_requests_quality_60_profile`,
`test_rejected_configure_keeps_previous_effective_profile`,
`test_applied_configure_adopts_acked_settings`,
`test_late_duplicate_ack_cannot_roll_back_profile` in `tests/test_signaling.py`.

Native Qt contract (offscreen; proves the UI contract only — not a physical
display, GPU acceleration, QR scanning, OBS capture or an Android device):

```sh
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v
```

Named stage-1 tests: `test_initial_controls_and_apply_match_session`,
`test_profile_ack_matches_effective_settings` (`tests_native/test_profile_ui.py`,
real signaling WebSocket + fake phone), and
`test_playout_mode_switch_uses_stable_value`.

Smoke checks (need a Qt-capable runtime; they are smoke checks, not acceptance
measurements): `scripts/smoke-quality.py` and `scripts/smoke-motion.py`. Both
write to `${AIRFRAME_ARTIFACTS_DIR:-artifacts/2026-10-30-stable60fps/smoke}`.
