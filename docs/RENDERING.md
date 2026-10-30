# Video output path (stages 6): GPU YUV rendering, fallback and OBS window

This document describes how AirFrame turns decoded H.264 frames into the desktop
video window and the clean `AirFrame — OBS` window, what is proven where, and
which platforms remain unverified.

## Pipeline

`airframe/receiver.py` decodes with aiortc/PyAV and hands each `av.VideoFrame` to
`Frames.put` (`airframe/desktop.py`) from the decoded-frame worker thread.

`Frames` is a latest-ready-frame mailbox:

- it stores the newest YUV frame and bumps a serial number;
- it prepares the frame for every **active** output window on the worker thread
  (hidden or closed windows are skipped, so they cost nothing);
- it emits one coalesced GUI notification per batch; the GUI presents only the
  newest prepared frame per window, so notifications never accumulate;
- a prepared frame pins its PyAV source until the next prepared frame replaces
  it, so a mailbox replacement, window close or session switch can never free a
  frame that a renderer is still using;
- `put(None)` is the session-completion signal and clears both surfaces.

While both windows are open the preview presents every second frame
(`Frames.set_preview_stride`) instead of scaling two large windows at 60 Hz.

## Renderers (`airframe/render.py`)

Both implementations sit behind one small interface: `prepare()` runs on the
worker thread, `present()`/`refresh()`/`clear()`/`close()` run on the GUI
thread; `wants_frame()` tells the mailbox whether the window is active.

### QtVideoRenderer — main path

- Copies the PyAV `yuv420p`/`nv12` planes (both strides honoured) into a
  `QVideoFrame` carrying the stream's matrix (BT.601/BT.709), range
  (limited/full), rotation (PyAV reports the display matrix counter-clockwise,
  Qt wants clockwise) and size.
- Hands that frame to the `QVideoSink` of a `QVideoWidget`. On a real window Qt
  renders it through **QRhi** (Metal on macOS) with its `yuv_triplanar` shader:
  the YUV→RGB conversion and scaling happen on the GPU.
- The mandatory per-frame `frame.reformat(format="bgra")` is gone from this
  path: one 1.5 bytes/pixel YUV copy remains (PySide6 6.12 cannot construct a
  `QVideoFrame` from external memory, so this is **not** zero-copy), and no
  4 bytes/pixel BGRA swscale conversion is done per frame.
- The renderer reports the *actual* path: `qt-multimedia-rhi/Metal` only when
  `QVideoSink.rhi()` really returns a backend; on a platform plugin without RHI
  it says so honestly instead of pretending to be accelerated.

### SoftwareVideoRenderer — honest fallback

Used when no GPU/RHI path is available (`create_renderer("gpu"/"auto")` with a
failed probe, e.g. Qt's `offscreen` platform). It keeps the exact previous
conversion (PyAV/swscale YUV→BGRA + `QImage` + `QPainter`), so it cannot look
different from the old app, and it reports
`software (PyAV/swscale YUV->BGRA + QPainter)`.

### Colour handling

The default for streams whose SPS carries no colour description is BT.601
limited range, which is what the old swscale path effectively used; this keeps
metadata-less Android streams colour-identical to the previous app.

The pinned Qt 6.12.0 RHI conversion was measured against ITU-R BT.601/BT.709
reference tables in a real window (`scripts/verify-render-native.py`):

- BT.601 limited, BT.709 limited, BT.709 full: within 2 code values (GPU path
  tolerance fixed at 2 in `tests_native/render_fixtures.py`).
- BT.601 full: up to 26 code values away from the ITU reference on the pinned
  Qt RHI, so this combination is **not** used on the GPU path. The same
  renderer routes it through the software conversion and reports
  `software colour conversion: bt601/full is not verified on the GPU path`;
  the captured result is within 1 code value of the ITU reference.

Other limitations:

- Qt's CPU conversion (`QVideoFrame.toImage`) always applies BT.601 limited
  range regardless of the frame's declared metadata, so offscreen tests check
  the declared metadata and the exact plane bytes; real-window shader colour is
  proven by the verification script.
- Qt rounds odd frame heights down for 4:2:0 chroma (65x49 → 24 chroma rows);
  the copy itself is exact, and the Android sender does not produce odd sizes.

## Exact output size (no silent downscale)

`QCheckBox "Точный размер окна OBS: 1:1 по физическим пикселям"` (default on)
sizes the frameless `AirFrame — OBS` window so that its **device pixels** equal
the stream pixels: a 2560x1440 stream becomes a 1280x720 logical window on a 2x
display, i.e. exactly 2560x1440 physical pixels. The diagnostics show the
window's logical size, the physical pixel size, the current scale (1.00) and the
actual output path, so a hidden downscale cannot go unnoticed. With the option
off the window stays freely resizable at 1280x720.

## Diagnostics

`AirFrame — Диагностика` starts with the actual output path of both windows,
the frame/window physical pixel sizes, the current output scale and every
fallback reason (GPU unavailable, unverified colour combination, ...).

## How this was verified

- `PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests_native -v` — renderer and
  desktop-integration contracts (offscreen only proves the contract).
- `.venv/bin/python scripts/verify-render-native.py` — real window: Metal RHI +
  `yuv_triplanar` shader, 24/24 colour patches within ±2 of the ITU reference,
  BT.601+full routed to software and colour-correct, 1:1 exact physical output,
  and old-BGRA vs new-YUV CPU/interval comparison.
- `.venv/bin/python scripts/verify-desktop-native.py` — the real
  `LocalServer`+`Desktop` pipeline: actual output path/RHI in diagnostics,
  integrated colour per combination, exact physical window size, per-frame CPU
  and presentation cadence, software-fallback diagnostics.

Artifacts live in `artifacts/2026-10-30-stable60fps/native/`.

## Unverified

- **Windows**: no Windows machine on this stand; the D3D11VA/RHI path was not
  run.
- **OBS Studio recording**: OBS automation is unavailable on this stand (Lead
  instruction), so a real OBS window-capture recording was not produced here;
  the content evidence is the window's composited screen capture (when the
  shared desktop allows it) plus the renderer-output contract. The §13.B
  external-capture acceptance belongs to the bench harness.
- **Display-refresh 60 FPS**: the scripts record delivery/presentation cadence
  and CPU, not a display-refresh measurement on a dedicated bench.
- **Real Android stream through the full app**: the integration contract uses
  synthetic YUV frames; the real-phone run remains part of final acceptance.
