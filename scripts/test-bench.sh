#!/bin/sh
# Docker-backed runner for the stage-0 measurement stand contracts.
#
#   sh scripts/test-bench.sh                    # bench source + interval metrics tests
#   sh scripts/test-bench.sh -v                 # verbose (unittest -v)
#   sh scripts/test-bench.sh tests.test_bench_source -v
#
# The whole Python suite still runs with `sh scripts/test-docker.sh`. The container
# already exists in the prepared environment; if it does not, it is built/started here.
set -eu
cd "$(dirname "$0")/.."
export DOCKER_CONFIG="$PWD/.docker"
if ! docker inspect af-test >/dev/null 2>&1; then
  docker compose build test
  docker run -d --name af-test -v "$PWD":/app -w /app scr-test sleep infinity >/dev/null
fi
case "${1:-}" in
  -*) set -- tests.test_bench_source tests.test_metrics "$@" ;;
  '') set -- tests.test_bench_source tests.test_metrics ;;
esac
exec docker exec af-test python -m unittest "$@"
