#!/bin/sh
# Build a portable desktop package for one target. Output dir is overridable so
# a new run never overwrites evidence from an older release (plan §14).
set -eu
cd "$(dirname "$0")/.."
DESKTOP_TARGET="${1:-macos-arm64}"
DEST="${ARTIFACTS_DIR:-artifacts}/portable"
docker build --target portable-export --build-arg "DESKTOP_TARGET=$DESKTOP_TARGET" \
    --output "type=local,dest=$DEST" .
