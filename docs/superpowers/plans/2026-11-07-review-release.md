# AirFrame 0.6.3 review and release

**Goal:** Review the complete application, fix confirmed correctness/performance defects, and release portable packages with particular attention to Windows.
**Scope:** Existing Android → WebRTC → bounded decoder → Qt video path. No physical Windows PC or phone is available; native Windows GPU/FPS and phone acceptance must remain explicitly unverified.

- [x] Android: regress and fix projection grant reuse, HEVC persistence, and any confirmed lifecycle defects; run JVM tests and APK build.
- [x] Receive pipeline: exercise real Receiver for H.264/H.265; verify decoder registration, bounded queues, profile changes, and native Windows hardware selection/fallback.
- [x] Desktop: regress first monotonic timestamp and telemetry gaps; remove duplicate YUV bridge/conversion work for equivalent outputs and software conversion on GUI thread. Preserve colour and frame lifetime contracts.
- [x] Windows: audit fresh portable archive, launcher/runtime DLLs and plugins; fix confirmed build/verification defects. Run available cross-platform checks.
- [x] Integrate: run full headless and offscreen native suites, compare reproducible YUV preparation CPU benchmark, independent final diff review.
- [x] Release: synchronize 0.6.3 versions, record release notes and limitations, build desktop wheel/Windows/macOS archives and Android APK, checksum artifacts. Publication proceeds from the verified release commit through the existing GitHub repository; the published release and its checksums are the final publication record.

Constraints: keep 60 FPS/24 Mbps/2560 profile defaults; do not advertise hardware acceleration without evidence; retain old artifact runs. User requested implementation and release directly, so routine fixes and release operations proceed within that authorization.
