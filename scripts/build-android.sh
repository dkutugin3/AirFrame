#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker build --platform linux/amd64 -f android/Dockerfile --target export \
    --output type=local,dest=artifacts/android android
