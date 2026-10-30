#!/bin/sh
# Build the desktop wheel export. Output dir is overridable so a new run never
# overwrites evidence from an older release (plan §14).
set -eu
cd "$(dirname "$0")/.."
DEST="${ARTIFACTS_DIR:-artifacts}/desktop"
docker build --target export --output "type=local,dest=$DEST" .
