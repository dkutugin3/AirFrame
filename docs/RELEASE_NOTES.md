# RELEASE NOTES — AirFrame 0.6.5

Released: 2026-11-10. Focus: lower unnecessary Android resource usage and reliable audio cleanup.

- **Capture and power:** screen capture starts after ICE/DTLS connects, pauses during media
  disconnection/retry, and resumes the same projection grant after recovery. CPU/Wi-Fi performance
  locks and statistics polling now follow active media instead of keeping a waiting phone awake.
- **Audio lifecycle:** each worker owns its recorder, AAC encoder and transport. Failed startup,
  terminal/empty recorder reads and stopped sessions release resources without a hot retry loop.
  A quick audio off/on safely waits for old cleanup; stale workers cannot release a replacement.
- **Audio memory:** packets reuse worker-owned storage and avoid an intermediate payload copy.
  The native data-channel observer and reference are explicitly released, with sends serialized
  against disposal. AAC format and compatibility with receiver 0.6.4 are preserved.
- **Quality:** selected FPS, size and bitrate are preserved. For ROG Phone 6, the existing
  “Плавность · 60 fps” profile (1920 px, 16 Mbit/s) is a suggested starting point.
- **Versions:** Android version code 13; Python package/runtime and desktop footer use 0.6.5.

Verification: 50 Android JVM tests, APK build and lint; desktop AAC-to-PCM compatibility test.
Full desktop package checks are recorded with the release artifacts. Phone battery, thermals,
actual encoder selection and FPS remain unmeasured on physical Android hardware. Native Windows
GPU performance remains unverified. See [ANDROID_PERFORMANCE.md](ANDROID_PERFORMANCE.md).

---

# RELEASE NOTES — AirFrame 0.6.4

Released: 2026-11-07. Version 0.6.3 was staged but not published; 0.6.4 includes the x86 test-fixture correction without rewriting that tag. Focus: reliable HEVC, Windows receive path and less CPU work per frame.

- **x86 verification:** frame-ID fixtures now measure visible pixels rather than stride padding; the x86 runner exposed architecture-dependent false frame-loss assertions. Production decode was not losing those frames.
- **Windows:** explicit hardware mode now selects D3D11VA instead of macOS VideoToolbox; failures retain the software fallback and diagnostics report the actual mode. Automatic mode remains software until a native Windows benchmark justifies changing it. Set `AIRFRAME_HW_DECODE=hardware` before launch to opt in; this is not a zero-copy path.
- **Rendering:** equivalent preview/OBS outputs share one owned Qt YUV buffer per source frame. Unsupported colour conversion is prepared off the GUI thread. A reproducible host microbenchmark for two 2560×1440 outputs reduced median preparation CPU time from 0.223 to 0.103 ms/frame (about 54%). This measures preparation only, not end-to-end FPS, latency or Windows GPU performance.
- **HEVC:** fixed the decoder registry used by the actual Receiver, RFC 7798 FU packetization, and recovery gating after loss/overload. H.265 now has an end-to-end test through the production bounded decoder and playout.
- **Android:** reconnect pauses/resumes one virtual display without reusing a consumed projection grant; preserves HEVC preference and resets the profile acknowledgement id on a fresh peer. Requests optional playback-audio permission on a fresh installation.
- **Adaptation:** a first absolute monotonic timestamp, telemetry gaps, unknown status and cached sender samples no longer trigger a spurious resolution downgrade.
- **Packaging/connectivity:** native PyInstaller builds collect application icons; LAN address discovery still falls back to the active route when hostname resolution fails. Android builds use a locked Gradle cache at the actual configured path.

Verification and remaining platform boundaries: see [REVIEW_0.6.4.md](REVIEW_0.6.4.md). No physical Windows PC or Android phone was available for this release; no claim of maximum throughput or physical 60/120 FPS is made.

---

# RELEASE NOTES — AirFrame 0.6.2 (runtime `815cadc` + release commit)

## Current line: 0.6.0 → 0.6.2

- **0.6.0/0.6.1 — HEVC/H.265 (stage 9b).** The desktop receives H.265 RTP
  (RFC 7798) on the pinned aiortc 1.15.0 via a guarded runtime registration;
  the phone prefers its hardware HEVC encoder when "Предпочесть HEVC/H.265" is
  checked, H.264 stays the negotiated fallback. 0.6.1 keeps `preferHevc` in the
  phone's ack (the checkbox no longer bounces) and shows the negotiated codec
  in the desktop footer with offer/answer codec logging.
- **0.6.2 — phone load.** Screen capture (and the high-perf WifiLock) runs only
  while a peer session is live — it no longer burns display-rate GPU while the
  service waits for a receiver (`CaptureGate`); `getStats` telemetry drops from
  1 s to 2 s once a session is 30 s old; audio capture wakes its thread half as
  often (8192-byte reads split safely across MediaCodec input buffers). The
  desktop adaptive controller accumulates streaks in wall-clock seconds, so the
  calmer telemetry does not slow adaptation. Verified: Java unit tests + APK
  build in Docker, `tests/` 201 OK, `tests_native/` 91 OK. Real-phone battery
  and thermal effect is **unverified** — measure on device.

Suites at the 0.6.2 revision: `tests/` **Ran 201 tests, OK**;
`tests_native/` (offscreen Qt) **Ran 91 tests, OK**; Android
`:app:testDebugUnitTest` + `:app:assembleDebug` — BUILD SUCCESSFUL.

---

## Historical frozen-runtime notes (0.5.0-era evidence, kept for the record)

Status: **release v0.5.0**. `pyproject.toml` version 0.5.0; the released runtime is
commit `a981886` (stage-8 adaptive quality) plus docs-only commits; the finish pass
`60cbcee` (review fixes I3/C1/I2/I6/I13) is part of the runtime. Published artifacts
are rebuilt from this revision in `artifacts/2026-10-30-stable60fps/packages-finish/`.
Everything below this marker describes that 0.5.0-era run; in particular the
"HEVC not implemented" statements were superseded by 0.6.0.

Suites at the release revision (raw logs):

- Deterministic headless Docker suite: **Ran 187 tests, OK** —
  `artifacts/2026-10-30-stable60fps/verify/20261030T131718Z/docker-suite.log`
- Native Qt suite (offscreen): **Ran 81 tests, OK** —
  `artifacts/2026-10-30-stable60fps/verify/20261030T131718Z/native-suite.log`

Re-run `sh scripts/verify.sh` for fresh evidence on the checked-out HEAD, and see
`docs/VERIFICATION.md` for every command with what it proves and does not prove.

## What is in the frozen runtime line

Plan stages 0–7 and the desktop half of stage 9a are committed. In short:

- **Profile contract (stage 1):** new sessions start at 60 FPS / 2560 px long side /
  max 24 Mbit/s / OBS-stable playout; the window is initialised from the session
  settings; profile acknowledgement is honest (`saved` / `applied` / `rejected` /
  `timeout`, rejection is never shown as applied); mobile preset selection uses the
  stable value, not the item index. 1920 px/16 Mbit/s and 30 FPS remain available;
  120 FPS is experimental and never a default. Details:
  `docs/PROFILE_CONTRACT.md`.
- **Playout (stage 2):** modes stable (250–350 ms reserve), balanced (120–200 ms),
  minimum latency (no reserve); frame reserve `ceil(max_delay × fps) + 3`; byte
  budget from real decoded planes with a documented **256 MiB playout-only** hard
  limit; an over-budget profile combination is refused explicitly with a safe 60 FPS
  suggestion instead of a silent shrink; no accelerated playback of a backlog.
- **RTP loss recovery (stage 3):** a gap is proven only when a later packet arrives;
  waiting is bounded at **150 ms** (was effectively 3.4 s at buffer capacity), then
  a keyframe is requested — first PLI immediately, repeats at most one per 500 ms,
  stopping after recovery; late packets of the abandoned epoch are ignored; a
  lossless 225 ms pause causes no false recovery.
- **Decoder queues (stage 4):** bounded pre-decoder and decoded-frame handoff
  queues with age/byte limits, controlled IDR recovery on overload, stale-epoch
  filtering before playout/Qt, bounded cleanup, visible depth/age/decode metrics;
  a narrow pinned-aiortc integration layer. See `.superpowers/sdd/stable-60fps-tdd/reports/core-pipeline.md`.
- **Telemetry (stage 5):** additive optional sender measurements are validated
  (bounded, finite, typed; absence = unknown, 0 = measured zero), passed through
  `receiver.py` into `on_stats`, and surfaced in the desktop diagnostics with the
  capture / pacer / encoder / sent / received chain and the
  capture-vs-encoder-vs-transport diagnosis. Window-average encode time is never
  presented as a per-frame p95. Details: `docs/TELEMETRY.md`.
- **Rendering (stage 6):** see below; details in `docs/RENDERING.md`.
- **Hardware decode (stage 7):** see below; evidence in
  `artifacts/2026-10-30-stable60fps/native/stage7-videotoolbox-evidence.json`.
- **H.264 High receive (stage 9a):** see below; HEVC is **not implemented**.

## Stage 6 rendering — YUV/RHI path, honest fallback, exact OBS window

- Decoded YUV frames go through a latest-ready-frame mailbox to a
  `QVideoWidget`/`QVideoSink` **QRhi** renderer; on macOS this is the Metal
  backend with Qt's `yuv_triplanar` shader (GPU YUV→RGB and scaling). The mandatory
  per-frame `frame.reformat(format="bgra")` is gone from the main path.
- **One 1.5 bytes/pixel YUV copy per frame remains** (PySide6 6.12 cannot wrap
  external PyAV memory in a `QVideoFrame`). This is **not zero-copy** and no
  zero-copy claim is made.
- The actual output path is reported from `QVideoSink.rhi()`: it is called
  `qt-multimedia-rhi/Metal (yuv_triplanar shader, 1 YUV copy/frame)` only when the
  RHI backend really exists. When no GPU/RHI path is available the software
  renderer (`PyAV/swscale YUV→BGRA + QPainter`) is used and the fallback reason is
  visible in diagnostics.
- BT.601 limited, BT.709 limited and BT.709 full were verified within ±2 ITU-R
  code values on the real Metal RHI path; BT.601 full measured up to 26 codes off
  on the pinned Qt RHI and is therefore routed through the software conversion
  (also colour-correct, reason visible).
- The frameless `AirFrame — OBS` window has an exact-size option (default on):
  physical device pixels equal stream pixels (a 2560×1440 stream is a 1280×720
  logical window at DPR 2 = 2560×1440 physical), so OBS does not silently
  downscale-then-upscale.
- Hidden/closed windows do no per-frame work; both windows share one decoded frame
  pipeline. Native evidence: `scripts/verify-render-native.py`,
  `scripts/verify-desktop-native.py`,
  `artifacts/2026-10-30-stable60fps/native/render-real-*`,
  `artifacts/2026-10-30-stable60fps/native/desktop-integration/`.

## Stage 7 hardware H.264 decode — verified on macOS, opt-in only

- `airframe/hwdecode.py` provides `software` / `auto` / `hardware` selection with the
  pinned PyAV 17.1.0 / aiortc 1.15.0 (aiortc requires `av>=14,<18`).
- **VideoToolbox genuinely engages** on this Mac: `is_hwaccel == True` and FFmpeg
  logs `pix_fmt: videotoolbox_vld` on a real libx264 H.264 stream. Frames come back
  as `nv12` (software: `yuv420p`) and are downloaded to system memory — **not
  zero-copy**.
- Decoded pixels/PTS/sizes are identical to the software reference (0 code-value
  difference in RGB after the same download path, same frame count/PTS/sizes).
- Measured on the shared stand (2560×1440@60, warm-up excluded):
  **VideoToolbox ≈3.1 ms/frame wall vs software ≈0.3 ms/frame (10.4× slower wall),
  process CPU 0.55 vs 1.2 ms/frame (0.44×)**. Hardware offloads CPU but is slower
  through PyAV's per-frame download on this pinned stack.
- Therefore **`auto` keeps the verified software decoder** and reports the measured
  reason. `AIRFRAME_HW_DECODE=hardware` (or `select_decoder_factory(MODE_HARDWARE)`)
  is the explicit, verified opt-in.
- Init/mid-session failure falls back to a fresh software decoder with an IDR
  restart point, a visible reason and **no hardware retry loop**.
- Visible stats keys: `hwDecodeRequested`, `hwDecodeMode`, `hwDecodeVerified`,
  `hwDecodeReason`.
- Evidence: `scripts/verify-hwdecode-native.py`,
  `artifacts/2026-10-30-stable60fps/native/stage7-videotoolbox-evidence.json`.
- **Windows/D3D11VA is unverified** (no Windows machine); no hardware-benefit claim
  beyond the measurement above.

## Stage 9a codecs — H.264 High receive verified on the desktop, HEVC not claimed

- `airframe/codec_capabilities.py` adds a **scoped, process-local** capability
  window used only around `setRemoteDescription(offer)`: H.264 High (`64001f`) and
  Constrained High (`640c1f`) receive entries are added for that negotiation and
  removed afterwards. Baseline/Constrained-Baseline fallback is preserved
  (a baseline-only offer still produces exactly the two offered payload types).
- Verified on the desktop: real SDP offers advertising High/Constrained High are
  answered with the offered payload type, profile/constraint bytes preserved and
  the level rewritten for the configured profile (`640c33` at 2560@60,
  `640c1f` at 1280@30); a real **libx264 High** bitstream (SPS `profile_idc == 100`)
  is negotiated and decoded end-to-end through aiortc/PyAV.
- **Android H.264 High end-to-end is unverified** (no phone): whether the pinned
  Android WebRTC factory advertises High and whether MediaCodec emits a High
  bitstream can only be checked on device; Baseline remains the safe default.
- **HEVC/H.265 is NOT implemented and NOT claimed.** Missing work (evidence
  `artifacts/2026-10-30-stable60fps/core/stage9a-hevc-analysis.txt`): aiortc 1.15.0
  has no H.265 RTP depacketizer (only VP8/H.264 branches), `get_decoder`/`get_encoder`
  map only H.264/VP8, its SDP matcher only knows H.264 profile rules (H.265 fmtp
  semantics differ), our stage-3/4 epoch rules would need VPS/SPS/PPS handling, and
  the Android WebRTC factory's HEVC capability/link to MediaCodec is unverified.
  Closing it needs a pinned aiortc/WebRTC extension or vetted fork plus real-phone
  verification.

## Final acceptance status (§13.B / §13.C / §13.D) for this run

- **Not performed:** the final 10-minute §13.B external/OBS-capture run and the
  new-output-path §13.C quality comparison. The active macOS display Space was
  occupied by the user's own full-screen application, no agent can switch Spaces
  (Accessibility permission denied), and the user chose to keep the display occupied
  and skip physical capture for this run. The acceptance harness **fails closed**
  (exit 2, exact reason, no app-side counter substituted); all preserved attempts
  with exact reasons are in the authoritative
  `artifacts/2026-10-30-stable60fps/acceptance/FINAL-STATUS.md`
  (also `acceptance/dry-run-stage6-STATUS.md`, `docs/ACCEPTANCE.md` §6).
- The only valid external-capture evidence in the run is the **older BGRA-path dry
  run** (`acceptance/dry-run/`: 897/899 = 99.78 % unique IDs, p99 16.67 ms, 0 holds
  > 50 ms, 0 underflows). It **predates the stage-6 YUV/RHI renderer and must not be
  used as evidence for the new output path**;
  `acceptance/quality-dry-run/` likewise grades only that BGRA recording.
- New-output-path evidence in this run is **native, not physical**: renderer
  contract/colour/exact-size verification on a real window
  (`artifacts/2026-10-30-stable60fps/native/render-real-*`,
  `native/desktop-integration/`). This is not OBS-capture or
  physical-presentation evidence.
- Plain-language result: **this branch is functionally verified at the
  deterministic/integration/native-contract level; physical 60 FPS in OBS, the
  new-path §13.C quality comparison and all real-phone runs remain unverified for
  this run.** No threshold claim (≥99.5 % IDs, p99 ≤ 2 source periods, 0 holds,
  0 underflows) is made for the new output path.
- To complete the physical runs on a free display/exclusive machine, use the
  commands in `docs/ACCEPTANCE.md` §6 / `acceptance/FINAL-STATUS.md`
  (short dry run → 10-minute §13.B → §13.C new path vs BGRA baseline).

## Unverified on this run (do not treat as results)

| Area | Status |
|---|---|
| Physical 60 FPS / OBS or external capture of the final (stage-6/7) output path | **NOT PERFORMED** (display Space occupied; harness failed closed) |
| §13.C quality comparison of the new output path | **NOT PERFORMED** (only the old BGRA recording was graded) |
| Real Android phone (§13.D), incl. H.264 High end-to-end, encoder SPS, rotation, reconnect, thermals, latency | **UNVERIFIED** (no phone) |
| HEVC / H.265 | **NOT IMPLEMENTED / NOT CLAIMED** (missing-work matrix above) |
| Windows application / D3D11VA hardware decode / Windows Qt RHI | **UNVERIFIED** (no Windows machine; Docker packaging is not a Windows run) |
| Hardware decode net benefit | **MEASURED, NOT PRESENT** on this pinned stack (CPU offload only, slower wall time); `auto` stays software |
| Zero-copy / GPU-surface sharing (renderer or decoder) | **NOT CLAIMED / NOT IMPLEMENTED** (1 YUV copy/frame renderer; PyAV downloads decoded frames) |
| OBS Studio application recording | **NOT PRODUCED** (automation unavailable without manual setup; OBS config not modified) |
| Display-refresh 60 FPS, p99 frame intervals, long-duration stability | **NOT MEASURED** (requires the §13.B bench run) |
| Full phone→OBS latency | **NOT MEASURED** (needs external timer filming) |
| 120 FPS as a working profile | **EXPERIMENTAL**, no physical acceptance evidence |
| Phone audio | **USER-OWNED WIP, OUT OF SCOPE** (not verified) |

## Artifacts and release assembly

Run evidence lives in `artifacts/2026-10-30-stable60fps/` (git-ignored), see the
layout in `docs/VERIFICATION.md` §2. Release packages are assembled from the
frozen runtime with:

```sh
export DOCKER_CONFIG="$PWD/.docker"
sh scripts/build-release.sh 2026-10-30-stable60fps
# ARTIFACTS_DIR overrides the per-package output dir for the individual build scripts
```

The release run folder contains `packages/` (wheel export, portable
`macos-arm64` / `macos-x64` / `windows-x64` archives, Android debug APK),
`VERSIONS.txt` (git HEAD/branch, dirty-path count, Python/Android/aiortc pins) and
`SHA256SUMS.txt`. The Android APK is debug-signed for testing; Windows packages
built in Docker on macOS are **not** a Windows run. Previous-release folders
`artifacts/v0.2…v0.3.0/` are old code, not this branch.

## Documentation index

- `docs/VERIFICATION.md` — every command and what it proves / does not prove.
- `docs/RENDERING.md` — stage-6 output path, fallback, exact OBS window.
- `docs/ACCEPTANCE.md` — §13.B/§13.C harness and the final-run status.
- `docs/PROFILE_CONTRACT.md` — profile/ack/playout-mode contract.
- `docs/TELEMETRY.md` — sender telemetry schema and diagnostics.
- `docs/MEASUREMENT.md` — stage-0 measurement stand.
- `README.md` — user-facing overview and honest status.
