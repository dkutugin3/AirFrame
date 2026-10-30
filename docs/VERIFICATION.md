# VERIFICATION — commands, evidence and what they do NOT prove

Status: branch `codex/stable-60fps-tdd` (development branch of the stable-60fps
plan; `pyproject.toml` version 0.5.0, not a release). Owner of this document:
plan §14 deliverable. Last reviewed against HEAD `34d1925`; run
`sh scripts/verify.sh` for fresh evidence instead of trusting one revision.

**Honesty rule.** This document lists only commands that exist in this branch and
separates what each one *proves* from what it *does not prove*. Headless tests,
synthetic localhost streams and Docker builds are not physical 60 FPS / OBS /
GPU / phone evidence. Old `artifacts/v0.x/` packages are previous releases, not
builds of this branch.

## Environment used for the evidence in `artifacts/2026-10-30-stable60fps/`

- macOS 26.5.1 arm64 host; Docker via OrbStack 29.8.2.
- Docker test image `scr-test`: Linux Python 3.13.18, aiortc 1.15.0, PyAV
  17.1.0; **no PySide6, no Qt/X11** (that is why `tests/` must stay headless).
- Host `tests_native/` run: `.venv` Python 3.14.7 with PySide6 6.12.0; extra
  pinned deps (`qrcode`) come from the workspace-local, git-ignored
  `.superpowers/native-deps` (`.venv` is never modified).
- No Windows machine, no OBS instance and no Android phone were attached during
  the runs recorded in this branch's artifacts.

Every `docker` command in this repository needs the sandbox-safe Docker config
inside the workspace:

```sh
export DOCKER_CONFIG="$PWD/.docker"
```

## 1. Command reference

### 1.1 One-command deterministic verification

```sh
sh scripts/verify.sh                 # Docker headless suite + native offscreen suite
sh scripts/verify.sh --docker-only   # headless suite only (Linux/pinned env)
sh scripts/verify.sh --native-only   # native Qt contract suite only
sh scripts/verify.sh --require-native  # missing/skipped native env becomes a failure
```

Writes `environment.txt`, per-step raw logs, `summary.txt`, `exit-code.txt` and a
`latest` symlink under `artifacts/2026-10-30-stable60fps/verify/<UTC-stamp>/`.
Exit codes: `0` all executed checks passed (recorded skips allowed), `1` a check
failed, `2` usage error. Skips are always recorded with the reason.

| Proves | Does not prove |
|---|---|
| Every deterministic headless test and the native offscreen UI contract pass on this host/HEAD, with raw logs. | Anything physical: display, GPU, OBS recording, QR scan, real phone, Windows. |

### 1.2 Headless Python suite (Docker, the acceptance command)

```sh
export DOCKER_CONFIG="$PWD/.docker"
sh scripts/test-docker.sh                     # whole discovery, tests/
docker exec af-test python -m unittest tests.test_signaling -v          # one module
docker exec af-test python -m unittest tests.test_playout -v            # one module
docker compose run --build --rm test          # clean rebuild of the test image
```

The persistent `af-test` container bind-mounts the workspace; `test-docker.sh`
creates it from the `scr-test` image if it is missing. Pinned image versions are
the point: the same interpreter/aiortc/PyAV as the Docker test stage.

| Proves | Does not prove |
|---|---|
| Protocol, playout/RTP math, receiver/decoder integration, real H.264 over WebRTC, telemetry validation, bench-source/metrics units pass in the pinned Linux environment. | Qt/GUI, GPU, hardware decode, OBS capture, physical display, Android, Windows, long-duration stability. |

### 1.3 Stage-0 measurement stand (bench)

```sh
sh scripts/test-bench.sh                      # stage-0 contracts only (26 tests)
sh scripts/test-bench.sh tests.test_bench_source -v
docker exec -w /app af-test python scripts/bench-stand.py \
    --frames 240 --width 1920 --height 1080 --fps 60 \
    --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics
docker exec -w /app af-test python scripts/bench-stand.py \
    --verify path/to/obs-recording.mp4 \
    --report artifacts/2026-10-30-stable60fps/obs-verify.json
```

`scripts/bench-stand.py` renders a machine-readable frame ID into a synthetic
H.264 source, decodes it back and checks ID coverage/PTS/motion; it can also
extract IDs from a real recording. It exits nonzero when IDs are lost,
duplicated or reordered. Details: `docs/MEASUREMENT.md`.

| Proves | Does not prove |
|---|---|
| The synthetic source/metrics toolkit is deterministic and can verify an OBS recording if one exists. | That an OBS recording was made, that frames were physically presented, or that any target machine sustains 60 FPS. |

### 1.4 Native Qt contract suite (offscreen)

```sh
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v
```

`tests_native/` is a separate suite (never imported by `tests/`, which has no
PySide6). It covers the profile contract, honest profile-ack states, stable-value
playout selection, the telemetry diagnostics surface, and the render
color/geometry/lifetime contracts committed so far. `setup-native-tests.sh` installs pinned
`qrcode` into `.superpowers/native-deps`; it never touches `.venv`.

| Proves | Does not prove |
|---|---|
| The Qt widget/profile/ack/telemetry/render contract behaves correctly offscreen on this host. | Physical display, GPU acceleration, actual paint timing, QR scanning with an Android camera, OBS window capture, Windows. `QT_QPA_PLATFORM=offscreen` is not a real window. |

### 1.5 Synthetic localhost smokes (need a Qt-capable host)

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
Qt surfaces, and write screenshots/JSON under
`${AIRFRAME_ARTIFACTS_DIR:-artifacts/2026-10-30-stable60fps/smoke}`.

| Proves | Does not prove |
|---|---|
| The end-to-end localhost pipeline and the current desktop UI structure work together (an offscreen run passed on this host; see `reports/profile-ui.md`). | Wi-Fi/network behaviour, Android source, OBS capture, physical display, hardware acceleration. `smoke-motion.py` accepts ≥55 fps for 10 s: that is a smoke check, **not** the plan's 60 FPS acceptance criterion. |

### 1.6 Desktop and portable builds (Docker)

```sh
sh scripts/build-desktop.sh                                   # wheel -> artifacts/desktop
sh scripts/build-portable.sh macos-arm64                      # -> artifacts/portable
sh scripts/build-portable.sh macos-x64
sh scripts/build-portable.sh windows-x64
sh scripts/build-release.sh 2026-10-30-stable60fps            # all packages + VERSIONS/SHA256SUMS
```

`ARTIFACTS_DIR` can keep a run from overwriting older evidence (the release
script does that automatically). `build-release.sh` also records `VERSIONS.txt`
(HEAD, branch, dirty-path count, versions) and `SHA256SUMS.txt` for the run.

| Proves | Does not prove |
|---|---|
| The Docker-defined wheel/portable packaging pipeline completes and produces checksummed artifacts for the recorded HEAD. | That the packaged app runs, renders, decodes or sustains 60 FPS on macOS/Windows. A Windows package built in Docker on macOS is **not** a Windows run. |

### 1.7 Android build and test loop (Docker only; host SDK install is forbidden)

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
| JVM unit tests, Android lint and a debug APK build in the pinned Android Docker image; build artifacts are checksummed under `artifacts/2026-10-30-stable60fps/android/`. | Installing/running the APK on a phone, permissions/MediaProjection flows, real encoder behaviour, thermal behaviour, real network, real 60 FPS. The debug APK is for testing only; it is not release-signed. |

## 2. Evidence layout

`artifacts/` is git-ignored; the current run's evidence lives in
`artifacts/2026-10-30-stable60fps/`:

```text
BASELINE.md                 stage-0 baseline (audit facts, environment, versions)
verify/<stamp>/             scripts/verify.sh logs, environment, summary, exit code
profile-ui/                 stage-1/stage-5a RED/GREEN logs (profile, ack, telemetry)
smoke/                      smoke-quality/motion screenshots + JSON
bench/                      reproducible bench source, intervals, stand reports
bench/profile-2560/         2560-px bench source variant
android/                    Android Docker builds, lint, unit-test XML, isolated APK+checksums
core/                       playout/RTP/H.264 RED/GREEN logs and measurements
native/                     native probe/render evidence (higher stages)
acceptance/                 acceptance-harness probe/dry-run logs (harness in progress)
lead/                       integration suite runs
```

`artifacts/v0.2/`, `v0.2.1/`, `v0.2.2/`, `v0.3.0/`, `macos-arm64/`,
`portable/` are **previous releases** (before this branch) and are not evidence
about the current code.

Each stage report in `.superpowers/sdd/stable-60fps-tdd/reports/` names the RED
failure it reproduced and the commands/logs behind every claim.

## 3. Test levels and the 120 FPS experimental status

- **Deterministic/headless (Docker):** virtual clocks, real data, no GUI. This is
  what `verify.sh` and `test-docker.sh` run.
- **Integration (native Qt/offscreen):** real Qt widgets/lifecycle, real
  signaling WebSocket; offscreen only.
- **Synthetic localhost smoke:** real WebRTC/H.264 between processes on one host.
- **Performance/physical (plan §13.B/C/D):** dedicated machine, real GPU, OBS
  recording, real phone. **Not available in this environment yet** — the
  acceptance harness is being built (task-15) and no physical run has been made.

**120 FPS is experimental and never the default.** The base profile of a new
session is 60 FPS / 2560 px long side / max 24 Mbit/s. The 120 fps preset remains
selectable, but the stable playout reserve is sized as
`ceil(max_delay * fps) + 3` frames: at 120 fps/stable that is 45 frames
(≈237 MiB of real 2560×1440 YUV420 planes). The playout queue has a documented
hard limit of **256 MiB for that queue only**; a profile/resolution combination
that does not fit is refused explicitly with a safe 60 FPS suggestion instead of
silently shrinking the reserve or resolution. A 120 FPS run still has no
physical acceptance evidence and must not be advertised as guaranteed.

## 4. What is NOT proven by any command in this document

- Physical output of ≥99.5% unique frames, p99 frame intervals, holds >50 ms,
  or zero stable underflows in an OBS recording (plan §13.B).
- PSNR/SSIM quality comparison of old vs new decode/render paths (§13.C).
- Behavior on a real Android phone, including the S26: start/stop, rotation,
  profile switching, short network drop, reconnect, lock, capture revocation,
  thermal/encoder limits, actual bitrate (§13.D).
- Hardware (VideoToolbox / MediaCodec) decode actually being engaged; a
  software fallback is not hardware-path evidence.
- QR scanning with a real camera; real Wi-Fi loss/reorder behaviour;
  Windows runtime; macOS notarization/signing; release-signed APK.
- Full phone→OBS latency (needs external timer filming and a recorded playout
  reserve figure).
- 120 FPS stability on any real display.

## 5. Not available on this branch yet (planned, do not cite as commands)

At the time of writing these stage tasks are still in progress and their
commands/scripts are **not committed**, so they are intentionally absent from
`scripts/verify.sh`:

- Full acceptance harness (60 FPS external/OBS capture + quality comparison) —
  plan §13.B/C, task-15 (bench-harness). `artifacts/2026-10-30-stable60fps/acceptance/`
  only contains probe/dry-run logs while the harness is being written.
- Real-window render/GPU verification command and hardware H.264 decode
  (macOS VideoToolbox) — plan §12–13, tasks 11–13 (native-gpu). The render
  *contract* tests are already in `tests_native/`, but a real-window/GPU
  verification command is not committed yet. Probe images under
  `artifacts/2026-10-30-stable60fps/native/` are preliminary, not acceptance.
- Receiver telemetry pass-through (new optional fields reaching the desktop from
  a real sender) — task-8, blocked on the receiver queue work.

When those land, add their exact command, what it proves and its evidence path to
this document and (if deterministic) to `scripts/verify.sh`.

## 6. Related documents

- `docs/IMPLEMENTATION_PLAN_TDD.md` — binding plan (stages, acceptance §13, delivery §14).
- `docs/MEASUREMENT.md` — stage-0 measurement stand and interval metrics.
- `docs/PROFILE_CONTRACT.md` — base profile, profile-ack states, playout mode values.
- `docs/TELEMETRY.md` — additive sender telemetry schema and diagnostics surface.
- `README.md` — user-facing overview, build/run instructions, honest status.
