# MEASUREMENT — reproducible stage-0 bench stand

Status: stage 0 (plan §3) deliverable. Owner: bench-harness.
Baseline evidence: `artifacts/2026-10-30-stable60fps/BASELINE.md`.
Code: `scripts/bench_source.py`, `scripts/bench_metrics.py`, `scripts/bench-stand.py`.
Tests: `tests/test_bench_source.py`, `tests/test_metrics.py` (headless, Docker-safe, no PySide6).

This stand exists so that later stages (60 FPS stability, decoder queues, Android
pacing, GPU/OBS output) can be judged against **moving, uniquely numbered frames on a
virtual clock** instead of a repeated still image or an aggregate FPS number. It is a
synthetic source + metrics toolkit; it is not a benchmark result on its own.

## 1. Commands

All Python runs are Docker-backed. `export DOCKER_CONFIG=$PWD/.docker` is required
by the sandbox for every `docker` command.

```sh
# whole Python suite (the acceptance command)
sh scripts/test-docker.sh

# only the stage-0 bench contracts (26 tests: 13 source + 13 metrics)
sh scripts/test-bench.sh
docker exec af-test python -m unittest tests.test_bench_source -v   # one module
docker exec af-test python -m unittest tests.test_metrics -v

# generate the reproducible H.264 source and verify every frame ID round-trips
docker exec -w /app af-test python scripts/bench-stand.py \
    --frames 240 --width 1920 --height 1080 --fps 60 \
    --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics

# base profile resolution (2560 px long side) — shorter clip, same verification
docker exec -w /app af-test python scripts/bench-stand.py \
    --frames 60 --width 2560 --height 1440 --fps 60 --crf 22 \
    --artifacts artifacts/2026-10-30-stable60fps/bench/profile-2560

# check an existing recording (e.g. an OBS capture of a live run)
docker exec -w /app af-test python scripts/bench-stand.py \
    --verify path/to/obs-recording.mp4 --report artifacts/2026-10-30-stable60fps/obs-verify.json
```

`scripts/bench-stand.py` exits non-zero when a generated artifact loses/duplicates/reorders
an ID, fails PTS agreement, has a frame with an unreadable ID, or contains identical
consecutive frames.

## 2. The reproducible source

`scripts/bench_source.py` — `BenchScene(width, height, fps)` renders frame `id`
deterministically; `render_luma(id)` returns the Y plane only; `render(id)` returns a
populated `av.VideoFrame`; `expected_pts(id)` is the PTS.

* **Machine-readable frame ID.** A 30-cell band across the top `5.5%` of the frame:
  6 preamble cells (`1,0,1,0,1,0`), 20 data bits MSB-first, 4 parity bits. A frame ID is
  20 bits (`0 … 1 048 575`), so a 120 FPS run wraps after ≈2.4 h — restart or use a new
  `start_id` for longer runs. The preamble gives the extractor a per-frame black/white
  reference, so the threshold survives H.264 contrast changes and OBS scaling.
* **Extraction.** `decode_frame_id(frame)` (any yuv420p `av.VideoFrame`, decoder line
  stride handled) or `decode_luma_frame_id(luma, width, height)`. Cells are sampled at
  fixed *relative* positions (centre 40% of each cell), so a scaled recording still
  decodes. Parity mismatch or contrast below 40 raises `FrameIdDecodeError`; the
  stand never counts an unreadable ID as present.
* **PTS.** `pts = round(id * 90000 / fps)` on a `1/90000` `time_base`; decoded PTS and
  the ID therefore always agree. The stand report checks `|decodedPts - id/fps|` per frame.
* **Real motion, no repeated stills.** Every frame contains a scrolling striped band,
  a fast mover with a shadow, a moving thin line, a static vertical gradient background,
  a 1 px line grid, an eight-bar colour scale and fine decimal text (`FRAME 000123`,
  `PTS 002050 MS`, `ID=000123 PTS=002050`). The decimal counter is the human-readable
  companion to the binary band; the tests require consecutive frames to differ in
  >1% of luma pixels and the stand counts identical consecutive pairs separately.
* **Reuse as an aiortc source.** `BenchVideoTrack(scene, clock=None)` is a
  `VideoStreamTrack`; `recv()` paces frames on an injectable clock:

```python
from scripts.bench_source import BenchScene, BenchVideoTrack, VirtualClock

clock = VirtualClock()                      # deterministic virtual time for tests
track = BenchVideoTrack(BenchScene(1920, 1080, 60), clock=clock)   # clock=None = real time
peer.addTrack(track)                        # H.264 is negotiated normally (aiortc 1.15.0)
```

`tests/test_bench_source.py` proves the track feeds a **real** aiortc H.264
encode/RTP/decode pipeline and that the decoded frames still carry IDs `0,1,2,3`.

## 3. Interval metrics and the diagnostic stream

`scripts/bench_metrics.py` — `IntervalMetrics(clock=..., max_samples=4096,
max_pending_frames=1024, event_writer=None)`.

The plan's observable chain is `assembly -> decode_start -> decode_end -> output_ready
-> renderer_handoff -> render_complete` (`EVENT_NAMES`). Adjacent pairs form the five
intervals in `INTERVALS`. `mark(name, frame=..., pts=...)` records an event at
`clock.time()`; `snapshot()` returns the bounded aggregate; `reset()` starts a new
session (counters zeroed); `write_json(path)` writes the snapshot.

* **Aggregation.** Each interval reports `count`, `retained`, `p50Ms`, `p95Ms`,
  `p99Ms`, `minMs`, `maxMs`, `meanMs`. Percentiles are **nearest-rank over the retained
  window** (the last `max_samples` values), not a fabricated full-history quantile;
  `count` and `retained` make that distinction explicit. Memory is bounded (fixed-size
  deques, capped pending-frame map, scalar counters).
* **Counters.** `invalidTimestamps` (NaN/±Infinity), `unknownEvents`,
  `negativeIntervals` (clock went backwards: counted, never aggregated),
  `missingIntervals` + `missingByInterval` (incomplete stage pair at `render_complete`),
  `pendingEvicted` (oldest pending frames dropped at the cap), plus `events`,
  `eventsByName`, `session`, `pendingFrames`.
* **Ordering.** Intervals are computed whenever both endpoints are present, so marking
  order does not matter: `interval = end_ts - start_ts`. Equal timestamps produce a
  legitimate `0.0` sample.
* **Diagnostics (opt-in only).** `DiagnosticEventWriter(path, max_events=None)` writes
  one JSON object per event (`seq`, `name`, `ts`, `frame`, `pts`) and is bounded by
  `max_events`; `close()` reports `{written, dropped, path}`. Without a writer no
  detailed stream is kept, so memory stays bounded. Use it for post-mortems, not for
  long acceptance runs.

## 4. Artifacts and how to read them

Under `artifacts/2026-10-30-stable60fps/bench/` (git-ignored run evidence):

| File | Meaning |
|---|---|
| `bench-source-1920x1080-60fps.mp4` | 240-frame (4 s) H.264 source, 20-bit IDs, 1/90000 PTS |
| `profile-2560/bench-source-2560x1440-60fps.mp4` | same source at the base-profile long side |
| `frame-0123.png` | grayscale still of decoded frame 123 (visual check of the scene) |
| `stand-report.json` | full report: environment, scene, artifact sha256, ID coverage, PTS, motion, intervals, diagnostics |
| `intervals.json` | bounded interval snapshot of that run |
| `diagnostics.jsonl` | 1440 diagnostic events of that run (bounded stream) |
| `verify-stand-recording.json` | `--verify` report over the same recording |
| `bench-run.log`, `verify-stand.log` | console output/PASS lines |

Key report fields: `verification.decodedFrames` / `uniqueIds` / `missingIds` /
`duplicates` / `orderViolations` / `framesWithoutDecodableId`, `verification.pts.maxMs`,
`verification.motion.identicalConsecutivePairs`. The recorded 1920×1080/240-frame run
decodes 240/240 unique IDs in order, 0.0 ms max PTS error and 0 identical pairs.

## 5. What this stand does NOT prove

* **Not physical presentation.** `paintEvent`, decoded-frame handoff and the stand's
  headless consumer are not proof that a frame reached a physical display or OBS. Those
  claims need real Qt/GPU/OBS runs (plan §13.B) and, for phone→OBS latency, external
  filming of a shared clock.
* **Not an absolute-latency measurement.** Phone and PC clocks are not synchronised;
  never subtract them as absolute delays. The stand's clock only measures the stages of
  one process (or the injected virtual clock).
* **Not a network/Android measurement.** `BenchVideoTrack` over localhost exercises the
  real H.264/SDP/RTP code path but says nothing about Wi-Fi, jitter, thermal state or a
  physical phone; that is stage §13.D with Android telemetry.
* **Not a codec-quality or GPU-acceleration result.** libx264/x264 defaults are used for
  a reproducible source; no quality comparison (PSNR/SSIM) or hardware decode is implied.
* **Machine-specific numbers.** The interval values in `intervals.json` are observations
  of this container on this host (macOS 26.5.1 / Apple M4 / OrbStack), recorded on
  battery (`BASELINE.md` §3). They are not thresholds and are not transferable. For the
  final performance run, pin AC power and the OS power mode first and record them.
* **Extractor limits.** The ID band must be fully visible in the recording (no cropping)
  and the frame yuv420p; parity catches a corrupted cell, not arbitrary transmission
  noise inside a cell. The 20-bit ID wraps after ≈2.4 h at 120 FPS / ≈4.8 h at 60 FPS —
  restart or use a distinct `start_id` for longer runs.
* **`output_ready`/`renderer_handoff`/`render_complete` in the file stand** refer to the
  stand's own consumer; in a live Qt run they must be emitted by that integration and
  are still only an API-boundary observation, not physical output.

## 6. Using the stand in later stages

1. **Stable playout (stage 2) / transport (stage 3) / decoder queues (stage 4):** pull
   frames from `BenchVideoTrack` on a `VirtualClock`, push them through the real
   `Receiver`/`Playout` path, assert unique IDs are preserved in order; record
   `IntervalMetrics` for the stages the integration can observe. Never assert OS sleep
   accuracy.
2. **Android pacer / telemetry (stage 5):** synthetic source is Python-only; use the
   generated IDs to prove the PC side does not invent or repeat frames. Android pacer
   correctness stays in Android unit tests.
3. **GPU/OBS output (stages 6–7) and quality (8–9):** feed `BenchVideoTrack` at the
   agreed FPS/resolution, record with OBS, then run `scripts/bench-stand.py --verify` on
   the recording: unique-ID coverage, duplicates/missing, order violations and identical
   consecutive frames are reported from the real capture. Pair that with frame stills
   for quality (e.g. `frame-0123.png` style luma export) and with CPU/GPU/RSS telemetry.
