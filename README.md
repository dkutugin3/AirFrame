# AirFrame

**English** · [Русский](#русский)

## English

Android screen mirroring over Wi-Fi into a **standalone desktop app for Windows and macOS** with a clean window for OBS capture. Own Android APK; no ADB, root or developer mode required.

> **Released line: `main` at `ad353b8`, published as v0.5.1** (`pyproject.toml` 0.5.1).
> Deterministic and native suites at the release revision: **Docker 187 OK, native 81 OK**
> (`artifacts/2026-10-30-stable60fps/verify/20261030T131718Z/`). **Physical 60 FPS in OBS, the §13.C
> quality rerun for the new output path and a real phone were not verified in this run** (details in
> [“Final acceptance of this run”](#final-acceptance-of-this-run) and
> [“What is verified and what is not”](#what-is-verified-and-what-is-not)). Hardware decoding is
> verified on macOS only and gives no wall-time win on the pinned stack; HEVC is not implemented.

### Profile and acknowledgement

- Settings are split into “Connection”, “Video” and “Sound & OBS” tabs. In a small window the pages scroll vertically; closed dropdowns and sliders do not change values from the mouse wheel. Detailed statistics live in the “Diagnostics and help” window.
- Base profile of a new session: **60 FPS / 2560 px / 24 Mbit/s max / OBS-stable**. The bitrate is an upper bound, not a promised actual rate.
- Presets: “Quality · 60 fps” (2560 px/24 Mbit/s, base), “Smoothness · 60 fps” (1920 px/16 Mbit/s), “Stability · 30 fps” (1920 px/10 Mbit/s), “Maximum · 120 fps” (2560 px/80 Mbit/s, experimental), “Custom”. The resolution caps the **long side**, preserving aspect.
- Profile acknowledgement: `saved` (no phone — stored for the next connection), `applied` (the phone accepted), `rejected` (the phone refused — the previous effective profile is shown), `timeout` (no answer for 10 s; the profile is stored but not counted as applied). Details: `docs/PROFILE_CONTRACT.md`.

### Smooth playback and memory

- The playout queue holds full decoded frames and releases them by their original timestamps. Capacity = `ceil(max reserve × FPS) + 3`; the byte budget uses the actual decoded planes (stride included), not a `width × height × 1.5` formula.
- The hard memory limit is **256 MiB for the playout queue only** (2560×1440 YUV420 planes are 5,529,600 bytes/frame; a 120 FPS/stable reserve of 45 frames ≈ 237 MiB). Other queues (RTP, handoff to the decoder, the renderer mailbox) have their own bounds. Used memory is visible in metrics, not hidden in shared statistics.
- A profile/resolution combination that does not fit is **rejected explicitly** with a safe 60 FPS suggestion; the reserve and resolution are never silently reduced. Under overload, overdue full frames are skipped; a backlog is never replayed sped-up.
- 120 FPS remains experimental: physical stability on a real display/OBS was not verified.

### Bounded wait for lost packets

- An RTP gap is proven only when the next packet arrives; the wait is capped at 150 ms and does not depend on the 2048-packet capacity or the playout queue. Lossless network silence (e.g. 225 ms) causes no false recovery.
- After an unrecoverable gap the session waits for a fresh key frame (IDR + SPS/PPS for H.264), requesting it: first PLI immediately, retries at most every 500 ms until recovery. Packets of a departed epoch are ignored.

### Rendering and the OBS window

- Decoded YUV frames pass through a “latest ready frame” mailbox into `QVideoWidget`/`QVideoSink` with a **QRhi** renderer (Metal on macOS, `yuv_triplanar` shader); the GPU does YUV→RGB and scaling. The mandatory per-frame BGRA conversion is gone from the main path.
- One **YUV copy per frame** remains (PySide6 6.12 cannot wrap external PyAV memory in a `QVideoFrame`). **There is no zero-copy, and none is claimed.**
- The actual output path is reported honestly: `qt-multimedia-rhi/Metal (yuv_triplanar shader, 1 YUV copy/frame)` only when `QVideoSink.rhi()` really returned a backend; otherwise the software path (`PyAV/swscale YUV→BGRA + QPainter`) runs with a visible fallback reason.
- Colour is verified on a real window: BT.601 limited, BT.709 limited and BT.709 full within ±2 ITU-R codes; BT.601 full diverges by up to 26 codes on the pinned Qt RHI, so it explicitly falls back to software conversion (also correct, reason visible).
- The `AirFrame — OBS` window: frameless, the “Exact OBS window size: 1:1 in physical pixels” option is on by default; diagnostics show the logical/physical size, the scale and the actual output path.
- Hidden/closed windows do no per-frame work; the preview and the OBS window share one stream. Details and limits: `docs/RENDERING.md`.
- **Limit:** Windows/D3D11VA was not verified; recording OBS Studio as an app is not automated on this bench, and the physical §13.B run of the new path was not executed in this run (see below).

### Hardware H.264 decoding (stage 7, macOS, opt-in)

- `airframe/hwdecode.py` implements `software` / `auto` / `hardware` selection on the pinned PyAV 17.1.0 / aiortc 1.15.0 (aiortc requires `av>=14,<18` — compatible).
- **VideoToolbox really engages** on this Mac: `is_hwaccel == True`, FFmpeg logs `pix_fmt: videotoolbox_vld`; frames arrive as `nv12` (software: `yuv420p`) and are downloaded by PyAV into system memory. This is **not zero-copy**.
- Decoded pixels/PTS/sizes are identical to the software reference (0 RGB difference after the same download path).
- Measured at 2560×1440@60 (no warm-up): **VideoToolbox ≈3.1 ms/frame wall vs software ≈0.3 ms/frame (~10.4× slower in time), CPU 0.55 vs 1.2 ms/frame**. CPU is offloaded; there is no time win on the pinned stack.
- Therefore **`auto` keeps the verified software path** and reports the measured reason; `AIRFRAME_HW_DECODE=hardware` (or `select_decoder_factory(MODE_HARDWARE)`) is the explicit verified opt-in.
- On failure — a fresh software decoder, waiting for an IDR, a visible reason, **no retry loop**. Visible stat keys: `hwDecodeRequested`, `hwDecodeMode`, `hwDecodeVerified`, `hwDecodeReason`.
- **Limit:** Windows/D3D11VA was not verified; no hardware-path win is claimed.

### Codecs: H.264 High and HEVC

- The desktop accepts H.264 **High** (`64001f`) and **Constrained High** (`640c1f`) through a process-local capability window only around `setRemoteDescription(offer)`; after negotiation the registry returns to stock aiortc. Baseline/Constrained-Baseline fallback is preserved: a Baseline-only offer gets an answer with exactly the two proposed payload types.
- The answer level is recomputed for the profile (`airframe/h264.py`): e.g. `640c33`/`640033` at 2560@60, `640c1f` at 1280@30. A real libx264-High stream (SPS `profile_idc == 100`) negotiates and decodes end-to-end on the desktop.
- **Android H.264 High end-to-end is unverified** (no phone): whether the pinned Android WebRTC advertises High and whether MediaCodec emits a High bitstream is unknown. Baseline remains the safe default.
- **HEVC/H.265 is not implemented and not claimed.** Why (briefly): aiortc 1.15.0 has no H.265 RTP depacketizer and no decoder/encoder mapping (H.264/VP8 only), the SDP matcher knows only H.264 rules, stages 3–4 would need VPS/SPS/PPS epoch rules, and HEVC support in the Android WebRTC factory cannot be checked without a device. A pinned aiortc/WebRTC patch or fork plus on-device verification is required; full analysis in `docs/RELEASE_NOTES.md`, evidence in `artifacts/2026-10-30-stable60fps/core/stage9a-hevc-analysis.txt`.

### Telemetry and diagnostics

- The phone can report: capture FPS, max capture interval, pacer FPS, cumulative `framesEncoded`/`framesSent`, the actual video bitrate, mean encode time over a window, the actual codec/profile, capture sizes, the limitation reason and thermal status. New fields are optional: an old message without them stays valid.
- Rules: a missing field is “unknown”, `0` is a measured zero; NaN/∞ and wrong types are rejected. Windowed counters give a mean encode time — **not** a per-frame p95. Hardware encoder status is accepted only as `unknown` until a trustworthy source exists.
- Diagnostics, in the window and the log, show the “capture → pacer → encoder → sent → receive/decode” chain, the actual bitrate, mean encode time, codec/profile/size, thermals, the bottleneck and the actual output path. Missing values are honestly shown as “unknown”. Details: `docs/TELEMETRY.md`.

### Final acceptance of this run

- **Not performed:** the final 10-minute §13.B run with external/OBS recording and the §13.C quality comparison for the **new** output path. Reason: the active display Space was occupied by the user's fullscreen app, agents cannot switch macOS Spaces (no Accessibility), and the user chose not to free the display. The acceptance harness **correctly refused** to record the wrong window (fail-closed, exit code 2, reason recorded); attempts and exact causes are in `artifacts/2026-10-30-stable60fps/acceptance/FINAL-STATUS.md` (also `docs/ACCEPTANCE.md` §6).
- **The only valid external-recording evidence** is the older **BGRA** run (`acceptance/dry-run/`: 897/899 = 99.78% unique IDs, p99 16.67 ms, 0 holds >50 ms, 0 underflows). It **predates the new YUV/RHI renderer and is not evidence for the new path**; `acceptance/quality-dry-run/` evaluates only that old recording.
- Evidence for the new path in this run is **native, not physical**: render contracts, colour on the real Metal RHI (24/24 patches within ±2 ITU-R codes), the exact 1:1 size, CPU/cadence (`artifacts/2026-10-30-stable60fps/native/render-real-*`, `native/desktop-integration/`). This is not an OBS recording and not proof of physical presentation.
- Summary: the line is verified at the deterministic/integration/native level; **physical 60 FPS in OBS, §13.C for the new path and all real-phone runs were not verified in this run**. No threshold claims (≥99.5% IDs, p99 ≤ 2 periods, 0 holds, 0 underflows) are made for the new path.
- Commands to finish on a free display/dedicated machine are in `docs/ACCEPTANCE.md` §6 and `FINAL-STATUS.md` (short dry run → 10-minute §13.B → §13.C new path vs the BGRA baseline).

### Features

- Android MediaProjection system consent; foreground service with a stop button.
- H.264 with hardware encoding on the phone where supported; VP8 as fallback; the desktop accepts H.264 High/Constrained High with a Baseline fallback.
- YUV rendering through Qt RHI (Metal on macOS) with a software fallback; an exact 1:1 physical-pixel OBS window.
- Optional VideoToolbox hardware decoding on macOS (measured: no time win, CPU offload only).
- WebRTC congestion control, retransmission of lost packets, a built-in jitter buffer, time-bounded loss recovery with key-frame requests.
- Up to eight signalling/media recovery attempts with backoff up to 15 s.
- Receive statistics: FPS, bitrate including WebRTC overhead, jitter, loss, RTP/assembly pauses, playout queue and underflows, decoder queue depths, hardware-decoder status; phone telemetry.
- The pairing code is valid for 10 minutes; failed attempts are rate-limited; reconnects use an in-memory random token.
- A clean black **AirFrame — OBS** window without buttons, codes or statistics; one decoded image for both windows; only the latest ready frame in the mailbox.
- Phone audio is transmitted to the computer and played on the default output (Android 10+, checkbox); the phone's media volume is adjustable from the computer with two-way sync.

### Requirements

- Phone: Android 8.0+ (API 26); the system screen permission is enough for the first connection.
- Computer: Windows 10 1809+ / Windows 11 x64, or macOS with a supported PySide6. Windows N needs the Media Feature Pack for Qt Multimedia. Building from source requires Python 3.11+.
- Docker for Python/Android builds and checks (no Android SDK, Java or Gradle on the host).
- One local network without client isolation. 5/6 GHz and Ethernet for the computer are recommended. Allow the app on the LAN in your firewall: TCP 8765 for signalling and dynamic UDP ports for WebRTC video.

### Build and verification

All Python/Android checks and builds run through Docker; no Android SDK is installed on the host.
Docker commands in this environment need the local config (`export DOCKER_CONFIG="$PWD/.docker"`).
What each command proves — and what it does not — is detailed in `docs/VERIFICATION.md`.

```sh
# one conservative check: Docker suite + native Qt contracts (offscreen)
sh scripts/verify.sh

# deterministic Python suite in Docker (pinned Python 3.13/aiortc/PyAV)
sh scripts/test-docker.sh
docker exec af-test python -m unittest tests.test_signaling -v          # one module
docker exec af-test python -m unittest discover -s tests -p 'test_receiver.py' -v

# stage-0 measurement bench
sh scripts/test-bench.sh
docker exec -w /app af-test python scripts/bench-stand.py --frames 240 --fps 60 \
    --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics

# native Qt contracts (offscreen; host Qt needed, .venv untouched)
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v

# native evidence: render/window needs a real display and screen-recording permission;
# VideoToolbox needs only native PyAV on the host (no display)
.venv/bin/python scripts/verify-render-native.py
.venv/bin/python scripts/verify-desktop-native.py
.venv/bin/python scripts/verify-hwdecode-native.py

# §13.B/§13.C acceptance harness (needs a free display; not run this time)
.venv/bin/python scripts/acceptance-60fps.py --duration 15 --warmup 5 \
    --width 1920 --height 1080 \
    --out artifacts/2026-10-30-stable60fps/acceptance/dry-run-final-output/
.venv/bin/python scripts/quality-compare.py --capture <capture.mp4> --baseline <baseline.mp4> \
    --out artifacts/2026-10-30-stable60fps/acceptance/quality-final-vs-bgra

# desktop/portable/Android builds and the full release set
sh scripts/build-desktop.sh
sh scripts/build-portable.sh macos-arm64      # also macos-x64, windows-x64
ARTIFACTS_DIR=artifacts/2026-10-30-stable60fps/android sh scripts/build-android.sh
sh scripts/build-release.sh 2026-10-30-stable60fps   # packages + VERSIONS.txt + SHA256SUMS.txt
```

The development Android loop (without an SDK on the host) is described in `android/Dockerfile`: a `--target sdk` image, a long-lived `af-android` container, then `gradle --no-daemon :app:testDebugUnitTest` / `:app:lintDebug :app:assembleDebug`.

Run artifacts are stored under `artifacts/2026-10-30-stable60fps/` (not in git):
`BASELINE.md`, `verify/`, `profile-ui/`, `smoke/`, `bench/`, `android/` (+ `isolated/` APK and
sha256), `packages/` (release wheel/portable/APK), `acceptance/` (including `FINAL-STATUS.md`),
`native/` (render/VideoToolbox evidence), `core/`, `lead/`. The older `artifacts/v0.2…v0.3.0/`
are builds of past releases, not of the current code.

### Running the desktop app

Build the wheel in Docker (`sh scripts/build-desktop.sh` → `artifacts/desktop/airframe-*.whl`), then on macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install artifacts/desktop/airframe-*.whl
.venv/bin/python -m airframe
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install artifacts\desktop\airframe-*.whl
.venv\Scripts\python.exe -m airframe
```

Alternatively, unpack the portable package (`AirFrame.app` / `AirFrame.exe`) in full and launch it —
no installed Python is required. For development: `python -m pip install -e .`.
A different port: `python -m airframe --port 8877`. Portable packages are unsigned; the Windows
package must be verified on real Windows (a Docker build on macOS is not such a test).
The Windows package ships “Проверить Windows.cmd” (Verify Windows): it checks the libraries, the
UI, test-frame output and the `AirFrame.exe` launch, saving a report and screenshots to
`%LOCALAPPDATA%\AirFrame\verification`. `--port` and `--renderer` can be passed straight to
`AirFrame.exe`; startup errors go to `%LOCALAPPDATA%\AirFrame\startup.log`.
The default decoder is software; enable the verified VideoToolbox path with
`AIRFRAME_HW_DECODE=hardware` (see the limits above).

### Connecting

1. Install the APK on the phone, allowing installation from your chosen source.
2. Start AirFrame on the computer. The UI shows the computer's IPv4 addresses and a code.
3. On the phone, enter the address with port, e.g. `192.168.1.10:8765`, and the eight-digit code (or scan the QR).
4. Press “Start streaming” and confirm the Android system dialog. On Android 14+ you can pick a single app or the whole screen.
5. On the computer, choose a profile and press “Apply profile”. Capture permission is not re-requested on quality changes or network recovery.

Capture cannot auto-resume after the permission is revoked, the screen locks, or the Android process is killed — Android requires fresh consent in those cases. Protected/DRM windows may show black.

If the computer has VPN/virtual adapters, pick its address on the phone's network. Guest Wi-Fi, client isolation and firewalls can block the connection. With persistent loss, first pick 30 fps or 1920 px, then lower the bitrate. Do not disable firewall rules wholesale.

### OBS

1. In AirFrame press **“Window for OBS”** or use the “Streaming → Window for OBS” menu item.
2. In OBS add a **“Window Capture”** source with the **AirFrame — OBS** window. On macOS use the corresponding macOS/window capture source and allow OBS screen recording in System Settings.
3. Keep the exact-size option (1:1 in physical pixels) enabled so OBS does no hidden downscale/upscale. Set the OBS rate to 30 or 60 fps to match the profile.
4. Keep the output window open and unminimised: the OS may throttle hidden windows. Settings can be restored from the main window.

When the phone disconnects, the clean window goes black; service messages never enter the OBS video frame. Recording and long OBS sessions on each target machine must be confirmed by real tests — none were run in this pass.

### Architecture and security limits

```text
Android: MediaProjection → SurfaceTexture → WebRTC encoder
                         ↓ DTLS-SRTP / UDP, local network
Desktop: aiortc → PyAV decoder (opt. VideoToolbox) → playout/queues
         → latest-frame mailbox → Qt RHI YUV (Metal) or software → window → OBS

SDP/ICE signalling: Android ← WebSocket → local aiohttp inside Qt
```

No STUN/TURN or external services: the connection is direct on the LAN. The media stream is
WebRTC-encrypted. In this version **signalling runs over plain HTTP/WebSocket**; the code and SDP
are not protected from LAN eavesdropping. Use a trusted home/studio network; an untrusted network
requires TLS plus computer-certificate verification in the APK. The code and resume token are never
written to disk; the log never contains them. The local API token is random; the native receiver
joins the room in-process.

### What is verified and what is not

| Item | Status on this line (runtime `ad353b8`) |
|---|---|
| Headless Python tests in Docker (protocol, profile/ack, playout, RTP recovery, decoder queues, real H.264 over WebRTC, telemetry, H.264 High/level, bench/metrics) | **Verified**: 187 tests OK at the release revision (`verify/20261030T131718Z/docker-suite.log`). |
| Native Qt contracts (profile/ack/telemetry/render/decoder choice) | **Verified offscreen**: 81 tests OK (`verify/20261030T131718Z/native-suite.log`); offscreen ≠ real display/acceleration. |
| Rendering on a real macOS window: Metal RHI + `yuv_triplanar`, colour 24/24 within ±2 ITU-R, BT.601+full → software, exact 1:1 size | **Verified natively** on this Mac (`native/render-real-*`); not a physical presentation or OBS recording. |
| Hardware decoding on macOS: VideoToolbox really on, pixels/PTS identical to software | **Verified natively** (`native/stage7-videotoolbox-evidence.json`); no time win (≈3.1 vs ≈0.3 ms/frame), `auto` = software. |
| H.264 High/Constrained High on desktop (real libx264-High stream, answer level `640c33`/`640033`) | **Verified headless**; Android High end-to-end — no. |
| HEVC/H.265 | **Not implemented and not claimed** (missing-work matrix in `docs/RELEASE_NOTES.md`). |
| Phone audio over the data channel (AAC capture/decode chain) | **Verified by a deterministic decode test + Docker/native suites**; sound on a real phone — not verified. |
| Telemetry: validation, pass-through via `receiver.py`, diagnostics | **Verified headless**; real phone values — no. |
| Android: APK/unit tests/lint in Docker | **Verified in Docker**; install/run on a phone, H.264 High, thermals — no. |
| Physical 60 FPS in OBS, interval p99, holds, underflows for the new path | **Not run** in this pass (display occupied, harness fail-closed; `acceptance/FINAL-STATUS.md`). The old BGRA dry run is not evidence for the new path. |
| §13.C quality of the new path (PSNR/SSIM/colour/range) | **Not run**; `quality-dry-run` evaluates only the old BGRA recording. |
| Windows app, D3D11VA, Windows Qt RHI | **Not verified** (a Docker build ≠ a Windows run); the package ships a self-verification script. |
| Real phone (incl. S26): rotation, reconnect, lock, thermals, latency | **Not verified** (no phone). |
| Zero-copy (render or decoder) | **None and none claimed**: 1 YUV copy/frame in the renderer; PyAV downloads decoded frames. |
| QR scanning with the phone camera; recording OBS Studio as an app | **Not verified** (QR rendering was not scanned; OBS automation unavailable). |
| 120 FPS as a working profile | **Experimental**, not physically confirmed. |

Stable 60 FPS cannot be guaranteed before real tests on the target phone, computer and OBS:
network, thermals and the phone's encoder can cap the actual rate. A static screen may be captured
less often — that is not evidence of healthy 60 FPS.

### Previous releases

- **0.5.1** — tabbed settings, wheel-safe controls, app icons, Windows self-verification (`artifacts/2026-10-30-stable60fps/packages-051/`).
- **0.5.0** — phone audio to the computer, the stable-60FPS TDD line: profile contract, bounded queues/RTP recovery, RHI rendering, VideoToolbox, telemetry, adaptive quality (`packages-finish/`).
- **0.4.0** — maximum presets and 120 fps experiment, phone-volume slider, QR-scanner orientation fix, exit-segfault fix.
- **0.3.0** — stable/balanced/low playout modes, burst tolerance, diagnostics panel (`artifacts/v0.3.0/`).
- **0.2.2** — incoming RTP buffer up to 2048 packets; large H.264 frames intact at zero loss (`artifacts/v0.2.2/`).
- **0.2.1** — decode/prepare split before Qt; latest-ready-frame only (`artifacts/v0.2.1/`).
- **0.2** — QR pairing, profile acknowledgement, frame pacer, Android statistics (`artifacts/v0.2/`).

Packages listed under older tags are built from older code and do not contain the current branch's fixes.

### Documents

- `docs/RELEASE_NOTES.md` — what is in the released runtime line, acceptance status, unverified matrix.
- `docs/VERIFICATION.md` — every verification command: what it proves and what it does not.
- `docs/RENDERING.md` — the output path, GPU/software fallback, the exact OBS window.
- `docs/ACCEPTANCE.md` — the §13.B/§13.C harness and the final-run status.
- `docs/PROFILE_CONTRACT.md` — base profile, acknowledgements, playout modes.
- `docs/TELEMETRY.md` — the telemetry schema and diagnostics.
- `docs/MEASUREMENT.md` — the measurement bench and interval metrics.
- `docs/IMPLEMENTATION_PLAN_TDD.md` — the work plan and acceptance criteria.

External docs: [MediaProjection](https://developer.android.com/media/grow/media-projection), [WebRTC Android SDK](https://github.com/webrtc-sdk/android), [Qt for Python](https://doc.qt.io/qtforpython-6/), [aiohttp](https://docs.aiohttp.org/en/stable/).

---

## Русский

Передача экрана Android по Wi-Fi в **отдельное настольное приложение для Windows и macOS** с чистым окном для захвата в OBS. Собственный Android APK, без ADB, root и режима разработчика.

> **Выпущенная линия: `main` на коммите `ad353b8`, опубликована как v0.5.1** (`pyproject.toml` 0.5.1).
> Детерминированный и нативный наборы на ревизции релиза: **Docker 187 OK, native 81 OK**
> (`artifacts/2026-10-30-stable60fps/verify/20261030T131718Z/`). **Физические 60 FPS в OBS, §13.C-прогон
> качества нового пути и реальный телефон в этом прогоне не проверялись** (подробно — в разделе
> [«Финальная приёмка этого прогона»](#финальная-приёмка-этого-прогона) и
> [«Что проверено и что нет»](#что-проверено-и-что-нет)). Аппаратное декодирование проверено только
> на macOS и не даёт выигрыша по времени на закреплённых версиях; HEVC не реализован.

### Профиль и подтверждение

- Настройки разделены на вкладки «Подключение», «Видео» и «Звук и OBS». В небольшом окне страницы прокручиваются по вертикали; закрытые выпадающие списки и ползунки не меняют значения от колёсика. Подробная статистика доступна в окне «Диагностика и помощь».
- Базовый профиль нового сеанса: **60 FPS / 2560 px / максимум 24 Мбит/с / стабильность OBS**. Битрейт — верхняя граница, а не обещанная фактическая скорость.
- Профили: «Качество · 60 fps» (2560 px/24 Мбит/с, базовый), «Плавность · 60 fps» (1920 px/16 Мбит/с), «Стабильность · 30 fps» (1920 px/10 Мбит/с), «Максимум · 120 fps» (2560 px/80 Мбит/с, экспериментальный), «Свой профиль». Разрешение ограничивает **длинную сторону** с сохранением пропорций.
- Подтверждение профиля: `saved` (телефона нет — сохранено для следующего подключения), `applied` (телефон принял настройки), `rejected` (телефон отказал — показывается прежний действующий профиль), `timeout` (телефон не ответил за 10 с; профиль сохранён, но не считается применённым). Подробности: `docs/PROFILE_CONTRACT.md`.

### Плавный вывод и память

- Очередь playout хранит полные декодированные кадры и выдаёт их по исходным временным меткам. Ёмкость = `ceil(максимальный запас × FPS) + 3`; бюджет байтов считается по фактическим декодированным плоскостям (с учётом stride), а не по формуле `width × height × 1.5`.
- Жёсткий предел памяти — **256 МиБ только для очереди playout** (плоскости 2560×1440 YUV420 — 5 529 600 байт на кадр; запас 120 FPS/стабильность = 45 кадров ≈ 237 МиБ). Другие очереди (RTP, передача декодеру, mailbox рендерера) имеют собственные ограничения. Занятая память видна в метриках, а не спрятана в общей статистике.
- Непомещающаяся комбинация профиля/разрешения **явно отклоняется** с предложением безопасных 60 FPS; запас и разрешение молча не уменьшаются. При перегрузке просроченные полные кадры пропускаются, ускоренного проигрывания накопленного нет.
- 120 FPS остаётся экспериментальным: физическая устойчивость на реальном экране/OBS не проверялась.

### Ограничение ожидания потерянных пакетов

- Разрыв RTP считается доказанным только после прибытия следующего пакета; ожидание ограничено 150 мс и не зависит от ёмкости 2048 пакетов и очереди playout. Безпотерьная тишина в сети (например, 225 мс) ложного восстановления не вызывает.
- После невосстановимого разрыва сеанс ждёт свежий ключевой кадр (IDR + SPS/PPS для H.264), запрашивая его: первый PLI сразу, повтор не чаще раза в 500 мс, до восстановления. Пакеты покинутой эпохи игнорируются.

### Рендеринг и окно OBS

- Декодированные YUV-кадры проходят через mailbox «последний готовый кадр» в `QVideoWidget`/`QVideoSink` с **QRhi**-рендерером (на macOS — Metal и шейдер `yuv_triplanar`); YUV→RGB и масштабирование делает GPU. Обязательная покадровая конвертация в BGRA из основного пути удалена.
- Остаётся **один YUV-copy на кадр** (PySide6 6.12 не позволяет обернуть внешнюю память PyAV в `QVideoFrame`). **Zero-copy нет и не заявляется.**
- Реальный путь вывода показывается честно: `qt-multimedia-rhi/Metal (yuv_triplanar shader, 1 YUV copy/frame)` только если `QVideoSink.rhi()` действительно вернул backend; иначе используется software-путь (`PyAV/swscale YUV→BGRA + QPainter`) с видимой причиной fallback.
- Цвет проверен на реальном окне: BT.601 limited, BT.709 limited и BT.709 full — в пределах ±2 кодов ITU-R; BT.601 full на закреплённом Qt RHI расходится до 26 кодов, поэтому явно уходит в software-конвертацию (тоже корректно, причина видна).
- Окно `AirFrame — OBS`: без рамки, опция «Точный размер окна OBS: 1:1 по физическим пикселям» включена по умолчанию; диагностика показывает логический/физический размер, масштаб и фактический путь вывода.
- Скрытые/закрытые окна не работают покадрово; предпросмотр и OBS-окно используют один поток. Подробности и ограничения: `docs/RENDERING.md`.
- **Ограничение:** Windows/D3D11VA не проверялся; запись OBS Studio как приложения на этом стенде не автоматизируется, а физический §13.B-прогон нового пути в этом прогоне не выполнен (см. ниже).

### Аппаратное декодирование H.264 (этап 7, macOS, opt-in)

- `airframe/hwdecode.py` реализует выбор `software` / `auto` / `hardware` на закреплённых PyAV 17.1.0 / aiortc 1.15.0 (aiortc требует `av>=14,<18` — совместимо).
- **VideoToolbox действительно включается** на этом Mac: `is_hwaccel == True`, FFmpeg пишет `pix_fmt: videotoolbox_vld`; кадры приходят как `nv12` (software — `yuv420p`) и скачиваются PyAV в системную память. Это **не zero-copy**.
- Декодированные пиксели/PTS/размеры идентичны software-эталону (0 разницы в RGB после одинакового пути скачивания).
- Измерено на 2560×1440@60 (без прогрева): **VideoToolbox ≈3,1 мс/кадр wall против software ≈0,3 мс/кадр (в ~10,4 раза медленнее по времени), CPU 0,55 против 1,2 мс/кадр**. Разгрузка CPU есть, выигрыша по времени на закреплённом стеке нет.
- Поэтому **`auto` оставляет проверенный software** и пишет измеренную причину; `AIRFRAME_HW_DECODE=hardware` (или `select_decoder_factory(MODE_HARDWARE)`) — явный проверенный opt-in.
- При отказе/сбое — fresh software-декодер, ожидание IDR, видимая причина, **без цикла повторных попыток**. Видимые ключи статистики: `hwDecodeRequested`, `hwDecodeMode`, `hwDecodeVerified`, `hwDecodeReason`.
- **Ограничение:** Windows/D3D11VA не проверялся; выигрыш от аппаратного пути не заявляется.

### Кодеки: H.264 High и HEVC

- Desktop принимает H.264 **High** (`64001f`) и **Constrained High** (`640c1f`) через процессно-локальное окно возможностей только вокруг `setRemoteDescription(offer)`; после согласования реестр возвращается к стоковому aiortc. Baseline/Constrained-Baseline fallback сохраняется: оффер только с Baseline получает ответ ровно с предложенными двумя payload type.
- Уровень в answer пересчитывается под профиль (`airframe/h264.py`): например `640c33`/`640033` на 2560@60, `640c1f` на 1280@30. Реальный libx264-High поток (SPS `profile_idc == 100`) согласуется и декодируется end-to-end на desktop.
- **Android H.264 High end-to-end не проверен** (нет телефона): неизвестно, объявляет ли закреплённый Android WebRTC High и отдаёт ли MediaCodec High-битстрим. Baseline остаётся безопасным вариантом по умолчанию.
- **HEVC/H.265 не реализован и не заявляется.** Почему (кратко): в aiortc 1.15.0 нет H.265 RTP-депакетизатора и маппинга декодера/энкодера (только H.264/VP8), SDP-матчер знает только H.264-правила, этапам 3–4 понадобились бы правила эпох VPS/SPS/PPS, а поддержка HEVC в Android WebRTC factory без телефона не проверяема. Нужен закреплённый aiortc/WebRTC-патч/форк и проверка на устройстве; полный разбор — в `docs/RELEASE_NOTES.md`, evidence `artifacts/2026-10-30-stable60fps/core/stage9a-hevc-analysis.txt`.

### Телеметрия и диагностика

- Телефон может присылать: FPS захвата, максимальный интервал захвата, FPS pacer, суммарные `framesEncoded`/`framesSent`, фактический видеобитрейт, среднее время кодирования за окно, фактический кодек/профиль, размеры захвата, причину ограничения, температурный статус. Новые поля необязательные: старое сообщение без них остаётся валидным.
- Правила: отсутствие поля — «неизвестно», `0` — измеренный ноль; NaN/∞ и неверные типы отклоняются. Агрегированные счётчики дают среднее время кодирования за окно — это **не** p95 отдельного кадра. Аппаратный статус кодировщика принимается только как `unknown`, пока нет достоверного источника.
- Диагностика в окне и в журнале показывает цепочку «захват → pacer → кодировщик → отправлено → приём/декод», фактический битрейт, среднее кодирование, кодек/профиль/размер, температуру, узкое место и фактический путь вывода. Отсутствующие значения честно отображаются как «неизвестно». Подробности: `docs/TELEMETRY.md`.

### Финальная приёмка этого прогона

- **Не выполнено:** финальный 10-минутный прогон §13.B с внешней/OBS-записью и §13.C-сравнение качества для **нового** пути вывода. Причина: активное пространство дисплея было занято полноэкранным приложением пользователя, агенты не могут переключать Spaces macOS (нет Accessibility), пользователь решил не освобождать дисплей. Приёмочный harness **корректно отказался** писать не то окно (fail-closed, код 2, причина зафиксирована); попытки и точные причины — в `artifacts/2026-10-30-stable60fps/acceptance/FINAL-STATUS.md` (также `docs/ACCEPTANCE.md` §6).
- **Единственное валидное evidence внешней записи** — старый **BGRA**-прогон (`acceptance/dry-run/`: 897/899 = 99,78 % уникальных ID, p99 16,67 мс, 0 удержаний >50 мс, 0 опустошений). Он **предшествует новому YUV/RHI-рендереру и не является доказательством для нового пути**; `acceptance/quality-dry-run/` оценивает только ту старую запись.
- Evidence нового пути в этом прогоне — **нативное, не физическое**: контракты рендера, цвет на реальном Metal RHI (24/24 патча ±2 кода ITU-R), точный размер 1:1, CPU/каденция (`artifacts/2026-10-30-stable60fps/native/render-real-*`, `native/desktop-integration/`). Это не запись OBS и не доказательство физического показа.
- Итог: линия проверена на детерминированном/интеграционном/нативном уровне; **физические 60 FPS в OBS, §13.C для нового пути и все реальные телефонные прогоны в этом прогоне не проверены**. Пороговых заявлений (≥99,5 % ID, p99 ≤ 2 периодов, 0 удержаний, 0 опустошений) для нового пути нет.
- Команды для завершения на свободном дисплее/выделенной машине — в `docs/ACCEPTANCE.md` §6 и `FINAL-STATUS.md` (короткий dry run → 10-минутный §13.B → §13.C новый путь против BGRA-базлайна).

### Возможности

- Системное разрешение захвата Android MediaProjection; foreground service с кнопкой остановки.
- H.264 с аппаратным кодированием на телефоне, если он его поддерживает; VP8 как запасной вариант; desktop принимает H.264 High/Constrained High с fallback на Baseline.
- YUV-рендеринг через Qt RHI (Metal на macOS) с software-fallback, точное окно OBS 1:1 по физическим пикселям.
- Опциональное аппаратное декодирование VideoToolbox на macOS (измерено: без выигрыша по времени, только разгрузка CPU).
- Управление перегрузкой WebRTC, повторная передача потерянных пакетов, встроенный jitter buffer, ограниченное по времени восстановление после потерь с запросом ключевого кадра.
- До восьми попыток восстановления сигнализации и медиасеанса с увеличением интервала до 15 с.
- Статистика приёма: FPS, битрейт с накладными расходами WebRTC, джиттер, потери, паузы RTP/сборки, очередь playout и опустошения, глубины очередей декодера, аппаратный статус декодера; телеметрия телефона.
- Код подключения действует 10 минут; ограничены неудачные попытки; для переподключения используется случайный токен в памяти.
- Чистое чёрное окно **AirFrame — OBS** без кнопок, кодов и статистики; одно декодированное изображение на оба окна, только последний готовый кадр в mailbox.
- Звук телефона передаётся на компьютер и выводится на устройство по умолчанию (Android 10+, чекбокс); громкость мультимедиа телефона регулируется с компьютера с двусторонней синхронизацией.

### Требования

- Телефон: Android 8.0+ (API 26); для первого подключения достаточно системного разрешения экрана.
- Компьютер: Windows 10 1809+ / Windows 11 x64 либо macOS с поддерживаемой версией PySide6. В Windows N нужен Media Feature Pack для Qt Multimedia. Для установки из исходников — Python 3.11+.
- Docker для сборок и проверок Python/Android (на хосте не нужны Android SDK, Java и Gradle).
- Одна локальная сеть без изоляции клиентов. Рекомендуются 5/6 ГГц и Ethernet для компьютера. Разрешите приложению локальную сеть в брандмауэре: TCP 8765 для согласования и динамические UDP-порты WebRTC для видео.

### Сборка и проверки

Все Python/Android-проверки и сборки идут через Docker; на хосте Android SDK не ставится.
Командам Docker в этой среде нужен локальный конфиг (`export DOCKER_CONFIG="$PWD/.docker"`).
Подробно — что доказывает каждая команда и что нет — в `docs/VERIFICATION.md`.

```sh
# одна консервативная проверка: Docker-suite + нативные Qt-контракты (offscreen)
sh scripts/verify.sh

# детерминированный Python-набор в Docker (pinned Python 3.13/aiortc/PyAV)
sh scripts/test-docker.sh
docker exec af-test python -m unittest tests.test_signaling -v          # один модуль
docker exec af-test python -m unittest discover -s tests -p 'test_receiver.py' -v

# измерительный стенд этапа 0
sh scripts/test-bench.sh
docker exec -w /app af-test python scripts/bench-stand.py --frames 240 --fps 60 \
    --artifacts artifacts/2026-10-30-stable60fps/bench --diagnostics

# нативные Qt-контракты (offscreen; нужен Qt на хосте, .venv не изменяется)
sh scripts/setup-native-tests.sh
PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m unittest discover -s tests_native -v

# нативное evidence: рендер/окно — реальный дисплей и разрешение записи экрана;
# VideoToolbox — только нативный PyAV на хосте (дисплей не нужен)
.venv/bin/python scripts/verify-render-native.py
.venv/bin/python scripts/verify-desktop-native.py
.venv/bin/python scripts/verify-hwdecode-native.py

# приёмочный harness §13.B/§13.C (нужен свободный дисплей; в этом прогоне не выполнялся)
.venv/bin/python scripts/acceptance-60fps.py --duration 15 --warmup 5 \
    --width 1920 --height 1080 \
    --out artifacts/2026-10-30-stable60fps/acceptance/dry-run-final-output/
.venv/bin/python scripts/quality-compare.py --capture <capture.mp4> --baseline <baseline.mp4> \
    --out artifacts/2026-10-30-stable60fps/acceptance/quality-final-vs-bgra

# сборки desktop/portable/Android и полный релизный набор
sh scripts/build-desktop.sh
sh scripts/build-portable.sh macos-arm64      # также macos-x64, windows-x64
ARTIFACTS_DIR=artifacts/2026-10-30-stable60fps/android sh scripts/build-android.sh
sh scripts/build-release.sh 2026-10-30-stable60fps   # пакеты + VERSIONS.txt + SHA256SUMS.txt
```

Android-цикл для разработки (без установки SDK на хост) описан в `android/Dockerfile`: образ `--target sdk`, долгоживущий контейнер `af-android`, затем `gradle --no-daemon :app:testDebugUnitTest` / `:app:lintDebug :app:assembleDebug`.

Артефакты прогона складываются в `artifacts/2026-10-30-stable60fps/` (папка в git не входит):
`BASELINE.md`, `verify/`, `profile-ui/`, `smoke/`, `bench/`, `android/` (+ `isolated/` APK и
sha256), `packages/` (релизные wheel/portable/APK), `acceptance/` (включая `FINAL-STATUS.md`),
`native/` (evidence рендера/VideoToolbox), `core/`, `lead/`. Старые `artifacts/v0.2…v0.3.0/` —
сборки прошлых выпусков, а не текущего кода.

### Запуск настольного приложения

Соберите wheel в Docker (`sh scripts/build-desktop.sh` → `artifacts/desktop/airframe-*.whl`), затем на macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install artifacts/desktop/airframe-*.whl
.venv/bin/python -m airframe
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install artifacts\desktop\airframe-*.whl
.venv\Scripts\python.exe -m airframe
```

Либо распакуйте portable-пакет (`AirFrame.app` / `AirFrame.exe`) целиком и запустите приложение —
установленный Python не требуется. Для разработки: `python -m pip install -e .`.
Другой порт: `python -m airframe --port 8877`. Portable-пакеты не подписаны; Windows-пакет
обязательно проверить на реальной Windows (сборка в Docker на macOS таким испытанием не является).
В Windows-пакет включён `Проверить Windows.cmd`: он проверяет библиотеки, интерфейс, вывод тестовых кадров и запуск `AirFrame.exe`, сохраняет отчёт и снимки в `%LOCALAPPDATA%\AirFrame\verification`. Параметры `--port` и `--renderer` можно передавать напрямую `AirFrame.exe`; ошибки запуска записываются в `%LOCALAPPDATA%\AirFrame\startup.log`.
Декодер по умолчанию программный; включить проверенный VideoToolbox можно переменной
`AIRFRAME_HW_DECODE=hardware` (см. ограничения выше).

### Подключение

1. Установите APK на телефон, разрешив установку из выбранного источника.
2. Запустите AirFrame на компьютере. В интерфейсе появятся IPv4-адреса компьютера и код.
3. На телефоне введите адрес с портом, например `192.168.1.10:8765`, и восемь цифр кода (или отсканируйте QR).
4. Нажмите «Начать трансляцию» и подтвердите системный диалог Android. На Android 14+ можно выбрать отдельное приложение или весь экран.
5. На компьютере выберите профиль и нажмите «Применить профиль». Разрешение захвата не запрашивается заново при смене качества и восстановлении сетевого соединения.

Захват нельзя автоматически возобновить после отзыва разрешения, блокировки экрана или уничтожения процесса Android. В этих случаях Android требует нового согласия. Защищённые окна/DRM могут отображаться чёрными.

Если компьютер имеет VPN/виртуальные адаптеры, выберите его адрес в сети телефона. Гостевая Wi-Fi сеть, client isolation и брандмауэр могут препятствовать соединению. При постоянных потерях сначала выберите 30 fps или 1920 px, затем снизьте битрейт. Не выключайте ограничения брандмауэра целиком.

### OBS

1. В AirFrame нажмите **«Окно для OBS»** или выберите пункт меню «Трансляция → Окно для OBS».
2. В OBS добавьте источник **«Захват окна»** с окном **AirFrame — OBS**. На macOS используйте соответствующий источник захвата macOS/окна и разрешите OBS запись экрана в настройках системы.
3. Оставьте включённой опцию точного размера окна (1:1 по физическим пикселям), чтобы OBS не делал скрытый downscale/upscale. Задайте частоту OBS 30 или 60 fps под профиль.
4. Оставляйте окно вывода открытым и не сворачивайте его: ОС может ограничивать отрисовку скрытых окон. Настройки можно вернуть кнопкой в основном окне.

При отключении телефона чистое окно становится чёрным; служебные сообщения в видеокадр OBS не попадают. Запись и длительную работу в OBS на каждом целевом устройстве необходимо подтвердить реальными испытаниями — в этом прогоне они не выполнялись.

### Архитектура и ограничения безопасности

```text
Android: MediaProjection → SurfaceTexture → WebRTC encoder
                         ↓ DTLS-SRTP / UDP, локальная сеть
Desktop: aiortc → PyAV декодер (опц. VideoToolbox) → playout/очереди
         → mailbox последнего кадра → Qt RHI YUV (Metal) или software → окно → OBS

Согласование SDP/ICE: Android ← WebSocket → локальный aiohttp внутри Qt
```

STUN/TURN и внешние сервисы отсутствуют: соединение работает напрямую в LAN. Медиапоток шифруется
WebRTC. В этой версии **сигнализация идёт по обычному HTTP/WebSocket**; код и SDP не защищены от
прослушивания самой локальной сети. Используйте доверенную домашнюю/студийную сеть; для недоверенной
сети требуется TLS и проверка сертификата компьютера в APK. Код и resume-токен не сохраняются на
диск; журнал не содержит их. Локальный токен API случайный, нативный приёмник подключён к комнате
внутри процесса.

### Что проверено и что нет

| Что | Статус на этой линии (runtime `ad353b8`) |
|---|---|
| Headless Python-тесты в Docker (протокол, профиль/ack, playout, RTP-восстановление, очереди декодера, реальный H.264 через WebRTC, телеметрия, H.264 High/level, bench/метрики) | **Проверено**: 187 тестов OK на ревизции релиза (`verify/20261030T131718Z/docker-suite.log`). |
| Нативные Qt-контракты (профиль/ack/телеметрия/рендер/выбор декодера) | **Проверено offscreen**: 81 тест OK (`verify/20261030T131718Z/native-suite.log`); offscreen ≠ реальный экран/ускорение. |
| Рендеринг на реальном окне macOS: Metal RHI + `yuv_triplanar`, цвет 24/24 в ±2 ITU-R, BT.601+full → software, точный размер 1:1 | **Проверено нативно** на этом Mac (`native/render-real-*`); это не физический показ и не запись OBS. |
| Аппаратное декодирование на macOS: VideoToolbox реально включён, пиксели/PTS идентичны software | **Проверено нативно** (`native/stage7-videotoolbox-evidence.json`); выигрыша по времени нет (≈3,1 vs ≈0,3 мс/кадр), `auto` = software. |
| H.264 High/Constrained High на desktop (реальный libx264-High поток, уровень answer `640c33`/`640033`) | **Проверено headless**; Android High end-to-end — нет. |
| HEVC/H.265 | **Не реализовано и не заявляется** (missing-work matrix в `docs/RELEASE_NOTES.md`). |
| Звук телефона через data channel (цепочка AAC-захвата/декодирования) | **Проверено детерминированным тестом декодирования + Docker/native-наборами**; звук на реальном телефоне — не проверен. |
| Телеметрия: валидация, проход через `receiver.py`, диагностика | **Проверено headless**; реальные значения с телефона — нет. |
| Android: APK/unit tests/lint в Docker | **Проверено в Docker**; установка/работа на телефоне, H.264 High, нагрев — нет. |
| Физические 60 FPS в OBS, p99 интервалов, удержания, опустошения для нового пути | **Не выполнялось** в этом прогоне (дисплей занят, harness fail-closed; `acceptance/FINAL-STATUS.md`). Старый BGRA dry run — не evidence нового пути. |
| §13.C качество нового пути (PSNR/SSIM/цвет/range) | **Не выполнялось**; `quality-dry-run` оценивает только старую BGRA-запись. |
| Windows-приложение, D3D11VA, Windows Qt RHI | **Не проверялось** (сборка в Docker ≠ запуск на Windows); в пакет встроена самопроверка. |
| Реальный телефон (включая S26): поворот, переподключение, блокировка, нагрев, задержка | **Не проверялось** (нет телефона). |
| Zero-copy (рендер или декодер) | **Нет и не заявляется**: 1 YUV-copy/кадр в рендере, PyAV скачивает декодированные кадры. |
| QR-сканирование камерой телефона; запись OBS Studio как приложения | **Не проверялось** (QR-рендеринг не сканировался; OBS-автоматизация недоступна). |
| 120 FPS как рабочий профиль | **Экспериментально**, физически не подтверждено. |

Гарантировать стабильные 60 FPS до реальных испытаний на целевом телефоне, компьютере и в OBS
нельзя: сеть, нагрев и кодировщик телефона могут ограничить фактическую частоту. Статичный экран
может захватываться реже — это не признак исправности 60 FPS.

### История предыдущих выпусков

- **0.5.1** — вкладки настроек, защита от колёсика, иконки приложения, Windows-самопроверка (`artifacts/2026-10-30-stable60fps/packages-051/`).
- **0.5.0** — звук телефона на ПК, линия стабильных 60 FPS по TDD: контракт профиля, ограниченные очереди/RTP-восстановление, RHI-рендеринг, VideoToolbox, телеметрия, адаптивное качество (`packages-finish/`).
- **0.4.0** — пресеты «Максимум» и экспериментальные 120 fps, ползунок громкости телефона, фикс ориентации QR-сканера, фикс сегфолта при выходе.
- **0.3.0** — режимы стабильности/баланса/минимума, устойчивость к паузам, панель диагностики (`artifacts/v0.3.0/`).
- **0.2.2** — буфер входящих RTP-пакетов до 2048, крупные H.264-кадры без обрезки при нулевых потерях (`artifacts/v0.2.2/`).
- **0.2.1** — разделение декодирования и подготовки к Qt, только последний готовый кадр (`artifacts/v0.2.1/`).
- **0.2** — QR-подключение, подтверждение профиля, ограничитель кадров, статистика Android (`artifacts/v0.2/`).

Пакеты старых тегов собраны из более старого кода и не содержат исправлений текущей линии.

### Документы

- `docs/RELEASE_NOTES.md` — что вошло в выпущенную runtime-линию, статус приёмки, unverified-матрица.
- `docs/VERIFICATION.md` — все команды проверки: что доказывают и что нет.
- `docs/RENDERING.md` — путь вывода, GPU/software fallback, точное окно OBS.
- `docs/ACCEPTANCE.md` — harness §13.B/§13.C и статус финального прогона.
- `docs/PROFILE_CONTRACT.md` — базовый профиль, подтверждения, режимы вывода.
- `docs/TELEMETRY.md` — схема телеметрии и диагностика.
- `docs/MEASUREMENT.md` — измерительный стенд и метрики интервалов.
- `docs/IMPLEMENTATION_PLAN_TDD.md` — план работ и критерии приёмки.

Внешние документы: [MediaProjection](https://developer.android.com/media/grow/media-projection), [WebRTC Android SDK](https://github.com/webrtc-sdk/android), [Qt for Python](https://doc.qt.io/qtforpython-6/), [aiohttp](https://docs.aiohttp.org/en/stable/).
