#!/bin/sh
# Build every release artifact for one run into one folder, then record the exact
# versions and checksums (plan §14: code, tests, docs + a separate artifact folder
# with APK, app packages, checksums, versions and reports; old runs are never
# overwritten because the run folder defaults to a date-stamped name).
#
# Usage: scripts/build-release.sh [run-folder-name]
#   default: artifacts/<YYYY-MM-DD>-stable60fps
set -eu
cd "$(dirname "$0")/.."

RUN="${1:-$(date +%Y-%m-%d)-stable60fps}"
OUT="$PWD/artifacts/$RUN"
mkdir -p "$OUT/packages"
export ARTIFACTS_DIR="$OUT/packages"
export DOCKER_CONFIG="${DOCKER_CONFIG:-$PWD/.docker}"

echo "== desktop wheel export =="
sh scripts/build-desktop.sh
echo "== portable packages =="
for target in macos-arm64 macos-x64 windows-x64; do
    echo "-- $target"
    sh scripts/build-portable.sh "$target"
done
echo "== Android APK =="
sh scripts/build-android.sh

echo "== versions =="
{
    echo "run: $RUN"
    echo "generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git HEAD: $(git rev-parse HEAD)"
    echo "git branch: $(git branch --show-current)"
    echo "git status: $(git status --porcelain | wc -l | tr -d ' ') dirty paths"
    echo "python (pyproject): $(grep -E '^version' pyproject.toml | head -1)"
    echo "android (build.gradle): $(grep -E 'versionName' android/app/build.gradle | head -1 | tr -d " '")"
    echo "aiortc pin: $(grep -oE 'aiortc==[0-9.]+' pyproject.toml | head -1)"
} > "$OUT/VERSIONS.txt"
cat "$OUT/VERSIONS.txt"

echo "== checksums =="
( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt -print0 \
    | xargs -0 -n1 shasum -a 256 > SHA256SUMS.txt )
echo "release artifacts: $OUT"
