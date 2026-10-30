# VERIFICATION — commands, evidence and what they do NOT prove

Status: branch `codex/stable-60fps-tdd` (`pyproject.toml` version 0.5.0, not a
published release). **Runtime code is frozen at HEAD
`2b7a5cb8f9091a20a12d547f1e375ade0a2c2711`.** After the freeze, only documentation
and acceptance-tooling commits landed (e.g. `1769f73` records the final §13.B/§13.C
status); they do not change the app runtime. Run `sh scripts/verify.sh` for fresh
evidence instead of trusting a document revision.

**Honesty rule.** This document lists only commands that exist in this branch and
separates what each one *proves* from what it *does not prove*. Headless tests,
synthetic localhost streams, native offscreen/real-window contract checks and
Docker builds are not physical 60 FPS / OBS / phone evidence. The old
`artifacts/v0.x/` packages are previous releases, not builds of this branch.

## Environment used for the evidence in `artifacts/2026-10-30-stable60fps/`

- macOS 26.5.1 arm64 host (Apple M4), built-in 60 Hz display; Docker via OrbStack 29.8.2.
- Docker test image `scr-test`: Linux Python 3.13.18, aiortc 1.15.0, PyAV
  17.1.0; **no PySide6, no Qt/X11** — that is why `tests/` stays headless.
- Host `tests_native/`/native-script run: `.venv` Python 3.14.7, PySide6 6.12.0,
  aiortc 1.15.0, PyAV 17.1.0; the pinned `qrcode` runtime dep comes from the
  workspace-local, git-ignored `.superpowers/native-deps` (`.venv` is never
  modified).
- No Windows machine and no Android phone were attached during any run in this
  branch's artifacts. OBS Studio automation was unavailable without manual setup
  and the user's OBS configuration was not modified. The host was **shared**, not a
  dedicated benchmark machine.

Every `docker` command in this repository needs the sandbox-safe Docker config
inside the workspace:

```sh
export DOCKER_CONFIG="$PWD/.docker"
```

## 1. Command reference

### 1.1 One-command deterministic verification

```sh
sh scripts/verify.sh                    # Docker headless suite + native offscreen suite
sh scripts/verify.sh --docker-only      # headless suite only (pinned Linux env)
sh scripts/verify.sh --native-only      # native Qt contract suite only
sh scripts/verify.sh --require-native   # missing/skipped native env becomes a failure
```

Writes `environment.txt`, per-step raw logs, `summary.txt`, `exit-code.txt` and a
`latest` symlink under `artifacts/2026-10-30-stable60fps/verify/<UTC-stamp>/`.
Exit codes: `0` all executed checks passed (recorded skips allowed), `1` a check
failed, `2` usage error. Skips are always recorded with the reason. `verify.sh`
does **not** run the real-window/native-evidence scripts or acceptance harness — it
is deliberately the conservative deterministic subset.

| Proves | Does not prove |
|---|---|
| Every deterministic headless test and the native offscreen contract pass on this host/HEAD, with raw logs. | Anything physical: display, GPU acceleration, OBS recording, QR scan, real phone, Windows. |

### 1.2 Headless Python suite (Docker, the acceptance command)

```sh
export DOCKER_CONFIG="$PWD/.docker"
sh scripts/test-docker.sh                     # whole discovery, tests/
docker exec af-test python -m unittest tests.test_signaling -v          # one module
docker exec af-test python -m unittest tests.test_playout -v            # one module
docker exec af-test python -m unittest discover -s tests -p 'test_receiver.py' -v
docker compose run --build --rm test          # clean rebuild of the test image
```

At the runtime freeze (`2b7a5cb`) this suite was **Ran 168 tests, OK**
(`artifacts/2026-10-30-stable60fps/native/stage7-docker-receiver-wired.log`). The
persistent `af-test` container bind-mounts the workspace; `test-docker.sh` creates
it from the `scr-test` image if missing. Pinned image versions are the point: the
same interpreter/aiortc/PyAV as the Docker test stage.

It covers: signaling/auth/QR/reconnect, profile contract + ack semantics,
playout capacity/timeline, RTP loss deadline + PLI policy + real H.264 recovery,
stage-4 decoder-queue bounds/recovery, receiver/decoder integration with real
H.264 over WebRTC, telemetry validation and receiver pass-through, H.264 level
rewrite + High/Constrained-High capability negotiation (headless), and the bench
source/metrics + acceptance-metrics units.

| Proves | Does not prove |
|---|---|
| The functional/unit/integration contracts listed above pass in the pinned Linux environment. | Qt/GUI, GPU, hardware decode, OBS capture, physical display, Android app or phone, Windows, long-duration stability, real phone telemetry values. |

### 1.3 Stage-0 measurement stand (bench)

```sh
sh scripts/test-bench.sh                      # stage-0 contracts
docker exec -w /app af-test python scripts/bench-stand.py \
    --frames 240 --width 1920 --height 1080 --fps 60 \
    --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics
docker exec -w /app af-test python scripts/bench-stand.py \
    --verify path/to/obs-recording.mp4 \
    --report artifacts/2026-10-30-stable60fps/obs-verify.json
```

`scripts/bench-stand.py` renders a machine-readable frame ID into a synthetic
H.264 source, decodes it back and checks ID coverage/PTS/motion; it can also
extract IDs from a real recording and exits nonzero when IDs are lost, duplicated
or reordered. Details: `docs/MEASUREMENT.md`.

| Proves | Does not prove |
|---|---|
| The synthetic source/metrics toolkit is deterministic and can verify an OBS recording if one exists. | That an OBS recording was made, that frames were physically presented, or that any machine sustains 60 FPS. |

### 1.4 Native Qt contract suite (offscreen)

```sh
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v
```

At the runtime freeze this suite was **Ran 74 tests, OK**
(`artifacts/2026-10-30-stable60fps/native/stage7-native-receiver-wired.log`).
`tests_native/` is a separate suite (never imported by `tests/`, which has no
PySide6). It covers the profile/ack contract, stable-value playout selection,
telemetry diagnostics surface, render colour/geometry/lifetime/desktop-integration
contracts, and hardware-decode selection/fallback contracts.
`setup-native-tests.sh` installs pinned `qrcode` into `.superpowers/native-deps`; it
never touches `.venv` or site-packages.

| Proves | Does not prove |
|---|---|
| The Qt widget/profile/ack/telemetry/render/decoder-selection contracts behave correctly offscreen on this host. | Physical display, GPU acceleration, actual paint timing, QR scanning with a camera, OBS capture, Windows. `QT_QPA_PLATFORM=offscreen` is not a real window. |

### 1.5 Native real-window / hardware evidence scripts (not offscreen)

These are the stage-6/7 evidence scripts. The two renderer scripts open real windows
and capture screen content, so they need a GUI session and macOS Screen Recording
permission; their screen-capture portions can be unavailable on a shared desktop
(each report records `screen_capture_available` and the reason instead of pretending).
`verify-hwdecode-native.py` needs only the native host PyAV build, not a display.

```sh
# Stage 6: real window, Metal RHI + yuv_triplanar shader, colour vs ITU,
# BT.601+full software route, exact 1:1 physical output, old-BGRA vs new-YUV CPU/interval
QT_LOGGING_RULES="qt.multimedia.video.texturehelper=true" \
    .venv/bin/python scripts/verify-render-native.py
# -> artifacts/2026-10-30-stable60fps/native/render-real-verification.json
#    render-real-exact-size.json/.png, render-real-colour-capture.png, render-real-run.log

# Stage 6b: the real LocalServer + Desktop pipeline, output path/diagnostics,
# integrated colour, exact physical window size, per-frame CPU/cadence
.venv/bin/python scripts/verify-desktop-native.py
# -> artifacts/2026-10-30-stable60fps/native/desktop-integration/desktop-integration.json

# Stage 7: VideoToolbox probe (is_hwaccel, videotoolbox_vld), decode equivalence
# vs software, measured wall/CPU benefit, selection and fallback contracts
.venv/bin/python scripts/verify-hwdecode-native.py
# -> artifacts/2026-10-30-stable60fps/native/stage7-videotoolbox-evidence.json
```

Observed on this host (frozen line): output path
`qt-multimedia-rhi/Metal (yuv_triplanar shader, 1 YUV copy/frame)` with
`QVideoSink.rhi().backendName() == "Metal"`; 24/24 colour patches within ±2 ITU-R
codes on the RHI path; BT.601+full routed to software (≤1 code off, reason
visible); exact output 2560×1440 stream → 1280×720 logical = **2560×1440 physical**,
1:1 checkerboard, no silent downscale; process CPU ≈1.5–2.1 ms per delivered frame
at 60 Hz on the shared host; VideoToolbox engaged (`is_hwaccel=True`,
`videotoolbox_vld`) with identical frames/PTS/sizes and a measured ≈3.1 vs ≈0.3
ms/frame wall (CPU 0.55 vs 1.2 ms/frame).

| Proves | Does not prove |
|---|---|
| On this macOS host and pinned stack: the real GPU renderer path is genuinely active, colour/exact-size contracts hold, and VideoToolbox decode really engages and is pixel/PTS-equivalent to software. | Physical 60 FPS in an OBS recording, display-refresh cadence, Windows/D3D11VA, real Android streams, zero-copy (none is claimed), or that hardware decode is faster (it is slower wall-time here; `auto` stays software). |

### 1.6 Synthetic localhost smokes (need a Qt-capable host)

```sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python scripts/smoke-quality.py
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python scripts/smoke-motion.py
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python scripts/smoke-desktop.py
```

They drive real signaling + WebRTC + real H.264 between a local synthetic sender
and the desktop app, exercise the Apply/ack path, playout mode switching and the
Qt surfaces. `smoke-quality.py`/`smoke-motion.py` write under
`${AIRFRAME_ARTIFACTS_DIR:-artifacts/2026-10-30-stable60fps/smoke}`;
`smoke-desktop.py` writes `artifacts/screenshots/`.

| Proves | Does not prove |
|---|---|
| The localhost end-to-end pipeline and current desktop UI structure work together. | Wi-Fi/network behaviour, Android source, OBS capture, physical display, hardware acceleration. `smoke-motion.py` accepts ≥55 fps for 10 s: a smoke check, **not** the plan's 60 FPS acceptance criterion. |

### 1.7 Acceptance harness — plan §13.B / §13.C (native, optional)

```sh
sh scripts/setup-native-tests.sh
export PYTHONPATH="$PWD/.superpowers/native-deps"

# short §13.B dry run (validates that the external recorder captures the final renderer)
.venv/bin/python scripts/acceptance-60fps.py --duration 15 --warmup 5 \
    --width 1920 --height 1080 \
    --out artifacts/2026-10-30-stable60fps/acceptance/dry-run-final-output/

# final 10-minute §13.B run (exclusive display/CPU; plan §13.B)
.venv/bin/python scripts/acceptance-60fps.py --duration 600 --warmup 10 \
    --width 1920 --height 1080 \
    --out artifacts/2026-10-30-stable60fps/acceptance/final-60fps/

# §13.C quality comparison (ID-aligned; tolerances pre-registered in acceptance_metrics.py)
.venv/bin/python scripts/quality-compare.py \
    --capture <capture.mp4> --baseline <baseline.mp4> \
    --out artifacts/2026-10-30-stable60fps/acceptance/quality-final-vs-bgra

# analyse an existing recording without capturing live
.venv/bin/python scripts/acceptance-60fps.py --recording path/to/recording.mp4 \
    --nominal-seconds 600 --out artifacts/2026-10-30-stable60fps/acceptance/obs-analysis
```

The §13.B driver starts the real receiver + clean OBS window, a separate-process
synthetic "phone" over the real localhost signaling/WebRTC/H.264 path, records the
composable OBS window **externally** (ffmpeg/AVFoundation screen capture; OBS
automation and `screencapture` video were probed and are unavailable on this stand),
decodes bench frame IDs from the recording and reports coverage/interval/hold/
underflow/CPU/RSS numbers. It **fails closed** (exit 2, reason recorded, no
app-side counter substituted) when the OBS window is not on the active display
Space, another app is frontmost, the probe has no decodable IDs, >2 % of captured
frames lack IDs, or the recorder fails.

**Final status for this run: the §13.B 10-minute run and the new-output-path §13.C
comparison were NOT performed.** The user's own full-screen app occupied the only
display Space, no agent can switch macOS Spaces (Accessibility denied), and the user
chose to skip physical capture. Authoritative record:
`artifacts/2026-10-30-stable60fps/acceptance/FINAL-STATUS.md` (also
`docs/ACCEPTANCE.md` §6, `acceptance/dry-run-stage6-STATUS.md`). The only valid
external-capture evidence is the **older BGRA-path** dry run
(`acceptance/dry-run/`: 897/899 = 99.78 % IDs, p99 16.67 ms, 0 holds >50 ms,
0 underflows) — **it predates the stage-6 renderer and must not be cited as
new-output-path evidence**; `acceptance/quality-dry-run/` grades the same old
recording. New-path evidence in this run is native/contract only (§1.5).

| Proves (when actually run on a free display) | Does not prove |
|---|---|
| Whether the final output path physically delivers the frames: unique-ID ratio, p99 intervals, holds, underflows, CPU/RSS, and §13.C ID-aligned PSNR/SSIM/colour/range of the final path vs a baseline. | For this run: nothing — the runs were not performed. Even when run: Wi-Fi/phone path, Windows, hardware decode benefit, long-duration 120 FPS. |

### 1.8 Desktop and portable builds + release assembly (Docker)

```sh
sh scripts/build-desktop.sh                                   # wheel -> $ARTIFACTS_DIR/desktop
sh scripts/build-portable.sh macos-arm64                      # -> $ARTIFACTS_DIR/portable
sh scripts/build-portable.sh macos-x64
sh scripts/build-portable.sh windows-x64
sh scripts/build-release.sh 2026-10-30-stable60fps            # all packages + VERSIONS/SHA256SUMS
```

`ARTIFACTS_DIR` keeps a run from overwriting older evidence. `build-release.sh`
defaults to `artifacts/<YYYY-MM-DD>-stable60fps/`, exports one `packages/`
directory for the wheel/portable/APK outputs and writes `VERSIONS.txt`
(HEAD/branch/dirty count, Python/Android/aiortc pins) and `SHA256SUMS.txt` over
the run folder. Because `artifacts/` is git-ignored, release evidence is the
run folder plus those checksum/version files, not a git commit.

| Proves | Does not prove |
|---|---|
| The Docker-defined wheel/portable/APK packaging pipeline completes and produces checksummed artifacts for the recorded HEAD. | That the packaged app runs, renders, decodes or sustains 60 FPS on macOS/Windows. A Windows package built in Docker on macOS is **not** a Windows run. |

### 1.9 Android build and test loop (Docker only; host SDK install is forbidden)

Full CI build (unit tests + lint + APK):

```sh
export DOCKER_CONFIG="$PWD/.docker"
ARTIFACTS_DIR=artifacts/2026-10-30-stable60fps/android sh scripts/build-android.sh
```

Fast TDD loop with a long-lived SDK container (details in `android/Dockerfile`):

```sh
docker build --platform linux/amd64 --target sdk -t af-android-sdk android
docker run -d --name af-android --user root \
    -v "$PWD/android":/project -w /project af-android-sdk sleep infinity
docker exec af-android gradle --no-daemon :app:testDebugUnitTest
docker exec af-android gradle --no-daemon :app:lintDebug :app:assembleDebug
```

| Proves | Does not prove |
|---|---|
| JVM unit tests, Android lint and a debug APK build in the pinned Android Docker image; evidence (APK, its sha256, unit-test XML, lint) under `artifacts/.../android/isolated/`. | Installing/running the APK on a phone, permissions/MediaProjection flows, real encoder behaviour (incl. H.264 High), thermal behaviour, real network, real 60 FPS. The debug APK is for testing only and is not release-signed. |

## 2. Evidence layout

`artifacts/` is git-ignored; this run's evidence lives in
`artifacts/2026-10-30-stable60fps/`:

```text
BASELINE.md                 stage-0 baseline (audit facts, environment, versions)
verify/<stamp>/             scripts/verify.sh logs, environment, summary, exit code
profile-ui/                 stage-1/5a/5b RED/GREEN logs (profile, ack, telemetry)
smoke/                      smoke-quality/motion screenshots + JSON
bench/                      reproducible bench source, intervals, stand reports
android/                    Android Docker builds/lint/unit-test XML; isolated/ has the APK + sha256
packages/                   release packaging output (wheel, portable archives, APK)
acceptance/                 §13.B/§13.C harness evidence:
  FINAL-STATUS.md             authoritative final status for this run (not performed, reasons)
  dry-run/                    only valid external capture: OLD BGRA-path dry run (not new path)
  quality-dry-run/            §13.C analysis on that old BGRA recording only
  dry-run-stage6*/            preserved fail-closed attempts on the new renderer output
  display-occupancy*.log      display-Space occupancy evidence
native/                     stage-6/7 native evidence:
  render-real-*.json/.png     real-window Metal RHI colour/exact-size/CPU comparison
  desktop-integration/        integrated LocalServer+Desktop evidence
  stage7-videotoolbox-evidence.json  hardware-decode probe/equivalence/benchmark/fallback
  stage7-*.log, stage6b-*.log suites and smoke runs at the freeze
core/                       playout/RTP/H.264/High-profile logs + stage9a-hevc-analysis.txt
lead/                       integration suite/answer-level runs
```

`artifacts/v0.2/`, `v0.2.1/`, `v0.2.2/`, `v0.3.0/`, `macos-arm64/`,
`portable/` are **previous releases** (before this branch) and are not evidence
about this code. Each workstream report in
`.superpowers/sdd/stable-60fps-tdd/reports/` names the RED failure it reproduced
and the commands/logs behind every claim.

## 3. Test levels, 120 FPS and playout memory

- **Deterministic/headless (Docker):** virtual clocks, real data, no GUI — what
  `verify.sh`/`test-docker.sh` run.
- **Native Qt contract (offscreen):** real Qt lifecycle, offscreen only.
- **Native real-window evidence:** GPU renderer/colour/exact-size and
  VideoToolbox decode on this macOS host (§1.5); not physical presentation.
- **Synthetic localhost smoke:** real WebRTC/H.264 between processes.
- **Performance/physical (§13.B/C/D):** harness exists, but the final runs were
  **not performed** for this run (display Space occupied; user decision) — see
  `FINAL-STATUS.md`.

**120 FPS is experimental and never the default.** The base profile of a new
session is 60 FPS / 2560 px long side / max 24 Mbit/s. The 120 fps preset remains
selectable, but the stable playout reserve is `ceil(max_delay × fps) + 3` frames: at
120 fps/stable that is 45 frames (≈237 MiB of real 2560×1440 YUV420 planes). The
playout queue has a documented **256 MiB hard limit for that queue only**; a
profile/resolution combination that does not fit is refused explicitly with a safe
60 FPS suggestion instead of silently shrinking the reserve or resolution. No
120 FPS physical acceptance evidence exists.

## 4. What is NOT proven by any command here (this run)

- Physical 60 FPS through the final (stage-6 YUV/RHI + stage-7 decoder-opt-in)
  path and its OBS/external capture: **not performed** (plan §13.B); the earlier
  BGRA dry run is not new-path evidence.
- New-output-path §13.C quality comparison: **not performed** (only the old BGRA
  recording was graded).
- Real Android phone / §13.D (including H.264 High end-to-end, encoder SPS,
  rotation, reconnect, lock, thermals, actual bitrate): no phone was attached.
- HEVC/H.265 anywhere: **not implemented / not claimed** (missing-work matrix in
  `docs/RELEASE_NOTES.md`, evidence `core/stage9a-hevc-analysis.txt`).
- Windows application, Windows D3D11VA hardware decode and Windows Qt RHI: no
  Windows machine; a Docker Windows package is not a Windows run.
- Hardware decode *benefit*: measured **negative** on this pinned stack (CPU
  offload only, ≈10.4× slower wall); `auto` keeps software. Do not cite it as a
  speedup.
- Zero-copy / GPU-surface sharing: **not implemented / not claimed** (renderer
  copies YUV planes into `QVideoFrame`; PyAV downloads hardware frames to RAM).
- QR scanning with a real camera; real Wi-Fi loss/reorder behaviour; macOS
  notarization/signing; release-signed APK; OBS Studio application recording.
- Full phone→OBS latency; display-refresh 60 FPS/p99 intervals; long-duration
  stability; 120 FPS as a working profile.
- Phone audio (user-owned WIP, out of scope).

## 5. Related documents

- `docs/IMPLEMENTATION_PLAN_TDD.md` — binding plan (stages, acceptance §13, delivery §14).
- `docs/RELEASE_NOTES.md` — frozen-runtime summary, stage 6/7/9a details, status matrix.
- `docs/RENDERING.md` — stage-6 output path, fallback, exact OBS window.
- `docs/ACCEPTANCE.md` — §13.B/§13.C harness, requirements, run status (frozen line).
- `docs/MEASUREMENT.md` — stage-0 measurement stand and interval metrics.
- `docs/PROFILE_CONTRACT.md` — base profile, profile-ack states, playout mode values.
- `docs/TELEMETRY.md` — additive sender telemetry schema and diagnostics surface.
- `README.md` — user-facing overview, build/run instructions, honest status.
