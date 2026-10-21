#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
DESKTOP_TARGET="${1:-macos-arm64}"
docker build --target portable-export --build-arg "DESKTOP_TARGET=$DESKTOP_TARGET" \
    --output type=local,dest=artifacts/portable .
