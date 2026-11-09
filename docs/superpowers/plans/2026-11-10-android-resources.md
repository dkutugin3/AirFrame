# Android sender resource usage

**Goal:** Remove unnecessary phone work and resource leaks while preserving the selected
resolution, FPS, codec compatibility and AAC audio quality. Target phone: ROG Phone 6.

**Approach:** Keep the existing shared-EGL texture capture and hardware-first WebRTC encoder.
Tie capture and performance locks to a connected media session, not an outstanding offer.
Own playback-capture resources per worker; stop on recording failures and reuse packet memory.
Lowering the video profile is an optional user choice, not an automatic change in this patch.

## Tasks

- [x] Reproduce capture-during-negotiation/disconnection using CaptureGate tests; require both
  receiver intent and connected media before capturing. Cover reconnect and stale connected events.
- [x] Apply the gate to projection, audio, CPU/Wi-Fi locks and statistics scheduling. Preserve
  the same MediaProjection grant through pause/resume; retry audio once projection exists.
- [x] Isolate playback audio per worker, release partial startup, terminate failed/zero reads,
  prevent old teardown from touching a replacement, eliminate intermediate packet allocations,
  and safely release DataChannel observers/native handles. Add deterministic regression tests.
- [x] Run the complete Android JVM suite, lint and APK build, plus desktop protocol tests if
  the wire format changes (it should not). Independently review the integrated patch.
- [x] Record results and provide the APK and recommended ROG Phone 6 settings. Do not claim
  battery/CPU/GPU/FPS savings without a physical-device comparison.

## Review focus

Connected-before-audio-channel ordering; transient ICE recovery before a delayed restart;
stale callbacks from disposed peers; stop during audio startup/blocking read; channel disposal
while a worker is stopping; Android 14 single projection grant; screen-off idle reconnection.

## Decisions

No automatic FPS/size reduction, hardware-only encoder, HEVC switch or display-refresh override.
Surface.setFrameRate does not throttle this capture producer. Real phone power and compositor
cost require device measurements; unit tests cover lifecycle and resource contracts only.

## Verification

- Capture negotiation regression: original policy raised AssertionError; connected gate passed.
- PlaybackAudio regression tests: 8 failing assertions before implementation, then 8 passing.
- Final `gradle --no-daemon --no-watch-fs :app:testDebugUnitTest :app:lintDebug :app:assembleDebug`:
  BUILD SUCCESSFUL; 50 tests, no failures/errors/skips; lint has no errors and two existing warnings.
- Receiver `tests.test_receiver.DataChannelAudioTests`: passed actual AAC-to-PCM decode.
- Independent integrated review: no actionable findings. Projection startup errors are guarded.
- Final APK signature verified; APK DEX contains both new audio classes. SHA-256:
  `03e0d97604102280c709359539ac2eb0e9c04a6af3f208f7d5c2570bc047bc4f`.
- Evidence and development APK: `artifacts/2026-11-10-android-performance/` (ignored build output).
  Android package version remains 0.6.4/code 12; this is a development build for device comparison.
- No physical-device battery, thermal, GPU/CPU or sound test was performed.
