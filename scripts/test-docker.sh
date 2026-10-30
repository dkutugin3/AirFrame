#!/bin/sh
# Docker-backed Python test runner for AirFrame (fast: bind-mounts the
# workspace into the prepared scr-test image).
set -eu
cd "$(dirname "$0")/.."
export DOCKER_CONFIG="$PWD/.docker"
if ! docker inspect af-test >/dev/null 2>&1; then
  docker run -d --name af-test -v "$PWD":/app -w /app scr-test sleep infinity >/dev/null
fi
exec docker exec af-test python -m unittest discover -s tests -v "$@"
