# AirFrame sender telemetry contract (stage 5, Python side)

This document records the additive sender-telemetry schema accepted by the
desktop receiver and the honest diagnostics it drives. It is a behaviour
document; the binding plan is `docs/IMPLEMENTATION_PLAN_TDD.md`.

## Wire message (additive, old senders stay valid)

```text
{"type": "telemetry", "session": "<id>",
 "encodedFps": 60, "width": 2560, "height": 1440, "limitation": "none",
 ... optional stage-5 fields ...}
```

Legacy required fields stay required and unchanged: `session`, `encodedFps`,
`width`, `height`, `limitation` (`none|cpu|bandwidth|other|unknown`).

Optional fields (`airframe/server.py`, `telemetry_valid`), all bounded:

| field | type / bounds | meaning |
|---|---|---|
| `captureFps` | number 0..1000 | frames the screen capture produced |
| `maxCaptureIntervalMs` | number 0..60000 | worst capture interval in the window |
| `pacerFps` | number 0..1000 | frames accepted by the frame pacer |
| `framesEncoded` | int >= 0, cumulative | encoder total for the session |
| `framesSent` | int >= 0, cumulative | packets/frames sent for the session |
| `videoBitrateKbps` | number 0..1000000 | measured video bitrate (not the cap) |
| `avgEncodeTimeMs` | number 0..60000 | window average encode time, **never a p95** |
| `codec` | str <= 32 | actual negotiated codec |
| `codecProfile` | str <= 128 | actual codec profile/level |
| `encoderImplementation` | str <= 128 | encoder name, only if the sender really knows it |
| `hardwareEncoder` | `"unknown"` only | no trustworthy hw-status source yet |
| `thermalStatus` | enum `unknown,none,light,moderate,severe,critical,emergency,shutdown` | temperature state where supported |
| `captureWidth` / `captureHeight` | int 1..16384 | captured frame size |
| `telemetryWindowMs` | number 0..60000 | measurement window for the reported averages |
| `sampledMonotonicMs` | int >= 0 | sender monotonic timestamp of the sample |

Rules: **absence means unknown; 0 is a measured zero.** NaN/Infinity, wrong
types, sub-types (bool where int is expected), out-of-range values and unknown
enum values are rejected. Unknown extra keys are ignored for forward
compatibility.

## Derived rates (`airframe/server.py`, `TelemetrySampler`)

`framesEncoded`/`framesSent` are cumulative. Rates are computed only from a real
positive time span:

- first sample -> unknown (`first-sample`), never 0;
- equal `sampledMonotonicMs` -> unknown (`equal-timestamps`);
- counters decreasing -> counter reset / new session (`counter-reset`);
- timestamps absent but `telemetryWindowMs > 0` -> window average;
- no counters -> `no-counters` (legacy sender).

## Diagnostics surface (`airframe/desktop.py`)

The main window detail line and the diagnostics journal (exportable as the
report) show:

- measurement chain: `захват ... FPS (макс. пауза ... мс) · pacer ... FPS ·
  кодировщик ... FPS · отправлено ... FPS · приём/декод ... FPS`;
- actual video bitrate and the **window-average** encode time (the text never
  calls it a p95; aggregated counters cannot prove a per-frame p95);
- actual codec/profile/frame size/limitation, encoder implementation, hardware
  encoder status (`unknown` -> "неизвестно"), thermal status (`unknown` ->
  "неизвестно", never 0);
- cumulative counter totals;
- bottleneck location: `захват не дал N FPS` / `кодировщик не успевает за N
  FPS` / `транспорт/ПК не успевают за N FPS` / `ограничений не видно` /
  `недостаточно данных`.

Journal lines are written when the bottleneck changes and at most once per 5 s,
so the export stays bounded.

The receiver's pass-through of the new optional fields (`receiver.sender_stats`)
is a separate task because `receiver.py` is owned by the core pipeline until
stage 4 lands; until then the diagnostics honestly report those fields as
unknown when the phone sends them.

## Verification

Headless (Docker):

```sh
export DOCKER_CONFIG=$PWD/.docker
docker exec af-test python -m unittest tests.test_signaling -v   # validation + derivations
sh scripts/test-docker.sh                                        # whole suite
```

Native Qt surface (offscreen; UI contract only):

```sh
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v
```

Named tests: `TelemetryValidationTests`, `TelemetryDiagnosticsTests` in
`tests/test_signaling.py`; `TelemetrySurfaceTests` in
`tests_native/test_telemetry_ui.py`.
