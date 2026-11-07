# AirFrame 0.6.4 code review

Date: 2026-11-07. Base: `1fb3fe2` (0.6.2). User scope: review the full application, particularly Windows, fix confirmed problems and release.

## Reviewed paths

Android pairing, permission/service lifecycle, frame pacing, encoder settings and audio; authenticated local signalling and profile acknowledgements; RTP depayload/reassembly and loss recovery; decoder selection, bounded queues and playout; Qt buffer ownership, colours, GUI handoff and two-window rendering; Windows launcher, embedded runtime, DLL/plugin payload, portable/PyInstaller packaging and platform verification scripts. Independent final review found no additional confirmed regression in the integrated patch.

The initial 0.6.3 draft was not published. Remote x86 Docker verification exposed tests averaging visible luma together with row padding. The corrected fixtures and a visible-pixel regression are included in 0.6.4; release-source tags are not rewritten.

## Confirmed fixes

| Area | Defect and resulting change |
| --- | --- |
| Android projection | `stopCapture()` ended the SDK projection; restarting reused a single-use Android 14+ grant. Owned `ScreenCapture` now detaches the virtual display surface while idle and reuses the same projection/display on resume. Disposal ends it once. |
| HEVC setting | Internal offer configuration omitted `preferHevc`; complete current settings now survive offers and acknowledgements. |
| Profile ack | A new peer retained the previous request id; reset it to match the server's connection profile request. |
| Android audio | RECORD_AUDIO was declared but never requested; optional runtime permission is now requested before projection. Denial still permits video. |
| Actual HEVC receiver | Factory captured the codec registry before H.265 registration; resolve the active decoder entry at use time. |
| RFC 7798 | Large-NAL fragmentation retained the original header and wrong FU type; replace the header and fragment only the body. |
| HEVC recovery | Recovery treated any HEVC frame as a keyframe; wait for a random-access frame and parameter sets. |
| Windows hardware API | Forced hardware mode selected VideoToolbox. Select D3D11VA on Windows and retain truthful software fallback. |
| Decoder diagnostics | UI statistics exposed selected hardware after fallback; report actual decoder mode. |
| Render work | Two outputs copied the same source twice. Share one per-put preparation between equivalent built-in renderers; custom renderer instances retain independent preparation. |
| GUI stalls | Colour fallback could run swscale during presentation. Complete conversion during preparation on the worker. |
| Quality adaptation | Initial monotonic offset, gaps, unknown or repeated cached samples could count as sustained limitation. Establish the initial time and require fresh observations. |
| LAN discovery | Hostname lookup failure skipped route probing. The two discovery mechanisms now fail independently. |
| PyInstaller | Missing application package data omitted icons. Collect `airframe` assets. |
| Android build cache | Gradle's real home did not match the cache mount; use an explicit home and locked mount. |

## Verification

- Full host headless suite: **216 tests passed**, including real local H.264 and HEVC production-receiver tests.
- Full native/offscreen Qt suite: **94 tests passed**, including frame ownership, colour/stride, profile/control, decoder-fallback and preparation regressions.
- All three portable builds completed; Docker runs the complete 216-test suite. The fresh Windows archive passed 29 offline composition/source/version checks; both macOS archives match the reviewed source, and the Apple Silicon bundled interpreter imported the application successfully. Android `assembleDebug`, `testDebugUnitTest`, and `lintDebug` completed successfully (49 executed Gradle tasks): **39 JVM tests, zero failures/errors/skips**. `aapt2` confirms versionName 0.6.4 / versionCode 12; `apksigner verify` passed. APK, JUnit XML, manifest/signature evidence and lint HTML are exported with the release artifacts.
- Desktop and receive-pipeline regression tests were observed failing before their fixes. Android lifecycle contracts have added JVM tests; the earlier Docker mutation build was cancelled during dependency setup, so no Android red-run claim is made. The complete suite is rerun during Docker wheel/portable builds.
- Host YUV preparation microbenchmark: two active Qt outputs, one 2560×1440 YUV420 source, 30 warmup frames and five runs of 240 frames. Median process CPU: **0.223 → 0.103 ms/source frame**. No physical display is exercised. Raw reports: `verify/render-before.json` and `verify/render-after.json` in the release artifact folder.

## Platform boundaries

This host is macOS. No native Windows PC or Android phone was available. Package inspection and cross-building do not validate Windows D3D11VA, QRhi colour rendering, actual Wi-Fi throughput, battery/thermal behaviour, phone reconnection, OBS recording or physical display cadence. Windows default decode remains software; explicit `AIRFRAME_HW_DECODE=hardware` chooses D3D11VA with software fallback but still downloads frames to host memory. Maximum end-to-end performance cannot be certified without target hardware and a moving-image acceptance run. The existing Windows verification command ships in the archive for that run.
