# ACCEPTANCE — final-acceptance harness for plan §13.B (60 FPS) and §13.C (quality)

Owner: bench-harness (task-15). Status: harness implemented, short dry run executed against
the current BGRA path; physical external capture **is available on this macOS host** (details below).

Files:
- `scripts/acceptance-60fps.py` — native §13.B driver (real app + real WebRTC + external capture).
- `scripts/acceptance-sender.py` — the synthetic "phone" process (H.264 `BenchVideoTrack` source).
- `scripts/quality-compare.py` — §13.C quality comparison (PSNR/SSIM/colour/range/crops).
- `scripts/acceptance_metrics.py` — headless analysis core (coverage, intervals, holds,
  fail-closed evidence check, PSNR/SSIM, colour/range patches, PNG crops, tolerances).
- `tests/test_acceptance_metrics.py` — 27 headless Docker tests for the analysis core
  (ID extraction from locally encoded MP4 fixtures, interval/statistics math, fail-closed
  evidence, PSNR/SSIM, colour/matrix/range, crops, tolerance registration).

## 1. Why this exists

Plan §13.B requires evidence that **≥99.5 % of the expected unique moving-source frames
appear in an external recording**, p99 inter-new-frame interval ≤ 2 source periods, no
unexplained image holds > 50 ms, and zero underflows; plan §13.C requires an ID-aligned
quality comparison against a lossless source with colour/matrix/range checks and saved
crops. `paintEvent` counters and Qt notifications are **not** accepted as proof of physical
output, so the §13.B harness records the real on-screen OBS window from outside the app and
proves from the recorded pixels (machine-readable frame IDs) which frames were shown.

## 2. Requirements on this machine

| Requirement | Status on this host (2026-10-30) |
|---|---|
| GUI display session | available (macOS 26.5.1, built-in 60 Hz display) |
| Host Python + PySide6 + aiortc + PyAV | `.venv` (Python 3.14.7, PySide6 6.12.0, aiortc 1.15.0, PyAV 17.1.0) |
| `qrcode` runtime dep (not in `.venv`, must not edit it) | installed to `.superpowers/native-deps` by `sh scripts/setup-native-tests.sh`; run with `PYTHONPATH="$PWD/.superpowers/native-deps"` |
| External screen recorder | **host ffmpeg** (`/opt/homebrew/bin/ffmpeg`, avfoundation "Capture screen 0") |
| Screen Recording permission | effective for this process path (verified: a real window's bench ID was decoded from an external recording) |
| OBS.app | installed (32.2.2) but **not automatable without manual setup**: `obs-websocket` server is disabled in the user config and there is no dedicated AirFrame scene collection. The harness does **not** modify the user's OBS config; it records this as "unavailable" and falls through. |
| macOS `screencapture -l` video | **unavailable**: `screencapture -v ...` fails on this host even for screenshots that succeed (`Failed to save to final location`); probed and recorded by the harness |

Capture backends are probed in order (OBS automation → `screencapture` → ffmpeg/avfoundation)
and every outcome is stored in the report (`capture.backend.probes`). The harness then records
a short **probe** of the OBS window and must decode bench frame IDs from it; otherwise it
**fails closed** (exit code 2, `failClosedReason` in the report, no app-side counters used as a
substitute for capture evidence).

## 3. Running §13.B

```sh
cd <repo>
sh scripts/setup-native-tests.sh          # once: installs qrcode into .superpowers/native-deps
export PYTHONPATH="$PWD/.superpowers/native-deps"
export DOCKER_CONFIG="$PWD/.docker"       # not needed for the native run, needed for Docker tests

# real acceptance: 10 min measured after a 10 s warm-up (plan §13.B)
.venv/bin/python scripts/acceptance-60fps.py --duration 600 --warmup 10 \
    --fps 60 --width 1920 --height 1080 --out artifacts/2026-10-30-stable60fps/acceptance/final

# short dry run (baseline now, and after stages 6-7)
.venv/bin/python scripts/acceptance-60fps.py --duration 15 --warmup 10 \
    --fps 60 --width 1920 --height 1080 --out artifacts/2026-10-30-stable60fps/acceptance/dry-run

# analyse an existing OBS/ffmpeg recording without capturing live (headless, no Qt needed)
.venv/bin/python scripts/acceptance-60fps.py \
    --recording path/to/obs-recording.mp4 --nominal-seconds 600 \
    --out artifacts/2026-10-30-stable60fps/acceptance/obs-analysis
```

What the driver does:
1. starts the real receiver (`LocalServer` + `Desktop`) with the base profile settings,
   shows only the clean `AirFrame — OBS` window (window stays on top; main window hidden);
2. starts `scripts/acceptance-sender.py` as a **separate process** (the "phone") streaming
   `BenchVideoTrack` over the real localhost signaling + WebRTC + H.264 path; the sender's
   test encoder is configured for real time (`libx264 ultrafast`; a real phone uses a hardware
   encoder — this is recorded in the report);
3. warms up (`--warmup`, default 10 s), then records the OBS window's **content rectangle**
   from the composited screen with ffmpeg/avfoundation (`-fps_mode passthrough` so recorder
   frame timestamps are not synthesised);
4. probes capture evidence (decodable bench IDs in a short recording) and fails closed if absent;
5. analyses the recording: frame IDs, coverage, repeats, drops, inter-new-frame intervals,
   image holds; collects app-side receiver/playout/queue telemetry, app CPU/RSS and source/
   recorder CPU;
6. evaluates the plan §13.B thresholds and writes the report, raw recording, probe recording,
   `app-stats.json`, ffmpeg logs.

Exit codes: `0` report written (and for `--require-pass`, thresholds met); `1` only with
`--require-pass` when thresholds fail; `2` capture evidence unavailable/invalid (fail closed).

Report highlights (`acceptance-60fps-report.json`): `capture.backend` (all probes + exact
reasons), `capture.window` (content rect in physical pixels), `capture.recording` (path/sha256/
size), `recordingAnalysis` (counts, `observedUniquePercent`, `repeats`, `dropsWithinObserved`,
`intervalStatsMs`, `holds`), `appStats` (fps/convertedFps/bitrate/playout queue+age/
underflows/skipped/gaps/conversion), `processUsage` (app process, remote source, recorder),
`actual` (codec, negotiated codec, decoder, frame sizes, receive bitrate), `thresholds`
(plan targets + per-check pass/fail), `appLog` (receiver journal tail).

Defaults: source 1920×1080 @ 60 FPS, OBS window content = source/2 points so the window
backing store is **1:1 with the decoded frame** (the profile-resolution case), capture output
1280×720 for evidence size (`--capture-width/--capture-height` to change; use
`--capture-width 1920 --capture-height 1080` for a native-resolution quality capture).
Window geometry is recorded; GUI benchmarks must run one at a time (plan §13.B).

## 4. Running §13.C

```sh
# baseline dry run: grade the external capture of §13.B against the lossless bench source
.venv/bin/python scripts/quality-compare.py \
    --capture artifacts/2026-10-30-stable60fps/acceptance/dry-run/acceptance-60fps-capture.mp4 \
    --out artifacts/2026-10-30-stable60fps/acceptance/quality-dry-run

# compare an old and a new recording, ID-aligned, in one report
.venv/bin/python scripts/quality-compare.py --capture new.mp4 --baseline old.mp4 \
    --out artifacts/2026-10-30-stable60fps/acceptance/quality-vs-old

# pinned bitrate for the coding-loss baseline (default 8000 kbps)
.venv/bin/python scripts/quality-compare.py --capture capture.mp4 --bitrate-kbps 24000 --out ...
```

The report separates **coding loss** (lossless source → pinned-bitrate H.264 → decode;
strict tolerances) from **capture/render-path loss** (lossless source → app render → external
screen capture; looser tolerances) and optionally grades old vs new recordings pairwise on
ID-aligned frames. Per frame: PSNR(Y), SSIM(Y), colour-patch deltas, black/white levels,
U/V-swap and range checks; identical crops of fine text, colour bars, gradient and motion are
saved for both sides (`crops-coding/`, `crops-capture/`). Frames are aligned by bench frame ID,
never by position. Tolerances are defined in `scripts/acceptance_metrics.py` **before** any
comparison and copied into every report with `tolerancesDefinedBeforeComparison: true`.

Tolerances (2026-10-30.1, fixed before the run — see the module for the authoritative values):

| Check | coding | capture |
|---|---|---|
| PSNR(Y) min | 32 dB | 20 dB |
| SSIM(Y) min | 0.97 | 0.85 |
| max colour-patch RGB delta | 8 | 48 |
| neutral patch delta | 6 | 28 |
| black level delta / max absolute | 4 / 32 | 20 / 64 |
| white level delta / min absolute | 6 / 200 | 24 / 168 |

## 5. Dry-run result on this machine (current BGRA path, 2026-10-30)

`--duration 15 --warmup 10 --width 1920 --height 1080` (source at profile resolution, 1:1 OBS
window backing store, external recorder output 1280×720):

| §13.B metric | Result | Threshold |
|---|---|---|
| external capture evidence | valid, decodable IDs | required |
| unique IDs | **897 / 899 = 99.78 %** | ≥ 99.5 % |
| repeats / drops within observed range | 1 / 3 | reported separately |
| inter-new-frame interval p50/p95/p99/max | 16.67 / 16.67 / 16.67 / 33.33 ms | p99 ≤ 33.3 ms |
| unexplained holds > 50 ms | 0 (max hold 16.67 ms) | 0 |
| playout underflows (max cumulative) | 0 | 0 |
| app converted FPS (mean) | 60.01 | ≥ 60 (source) |
| app process CPU / peak RSS | 44 % mean (52 % max) / 306 MiB peak | informational |
| remote source CPU / external recorder CPU | 99.6 % / 47.3 % | informational |
| codec / decoder / capture size / receive bitrate | h264 / aiortc 1.15.0 software (FFmpeg) / 1280×720 / ~2.2–2.7 Mbps | informational |

**All §13.B checks passed on the 15 s dry run.** A 10-minute run is still required for the
final acceptance record; the dry run also establishes this BGRA baseline for comparing the
stage 6–7 output path.

§13.C dry run on the same recording (40 ID-aligned frames, analysis 640×360, pinned coding
baseline 8000 kbps): coding floor **PSNR 57.5 dB mean / SSIM 0.9993**, capture/render path
**27.5 dB mean / SSIM 0.9252**, colour patch delta 23.3, no U/V swap, both within the
pre-registered capture tolerances. Crops are saved for visual review.

## 6. What this harness does NOT prove

- It records the **composited screen pixels** of the AirFrame OBS window. That is physical
  output evidence for this machine, but it is not an OBS-application recording unless OBS
  actually recorded (the report states the exact capture backend). An OBS recording can be
  analysed with `--recording`.
- The recorder pipeline (AVFoundation capture → crop/scale → H.264) contributes to the
  measured intervals and to capture-path quality loss; the report labels app-side telemetry
  separately and never uses it as a substitute for the recording.
- No GPU-acceleration, Windows, or Android/Wi-Fi claim. The sender is a local synthetic
  source, not a phone; plan §13.D (real Android) is separate.
- PSNR/SSIM are necessary but not sufficient for quality: the saved crops must be inspected;
  the report says so.
- The 20-bit frame ID wraps after ≈4.8 h at 60 FPS; acceptance runs are shorter (or restart).
- Screen colour accuracy is only measured in the recorded pixel values; display colour
  management is part of the capture category by design and its tolerances are looser.
- OBS automation is not implemented: the user's OBS configuration is never modified.
