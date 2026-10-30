#!/bin/sh
# Full Android CI build in Docker (never installs the Android SDK on the host).
# Output (APK + lint report) defaults to artifacts/android; set ARTIFACTS_DIR to keep
# per-run evidence without overwriting older builds, for example:
#   ARTIFACTS_DIR=artifacts/2026-10-30-stable60fps/android sh scripts/build-android.sh
set -eu
cd "$(dirname "$0")/.."
ARTIFACTS_DIR=${ARTIFACTS_DIR:-artifacts/android}
mkdir -p "$ARTIFACTS_DIR"
docker build --platform linux/amd64 -f android/Dockerfile --target export \
    --output "type=local,dest=$ARTIFACTS_DIR" android
