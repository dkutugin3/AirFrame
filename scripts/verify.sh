#!/bin/sh
# Conservative deterministic verification for the AirFrame branch
# codex/stable-60fps-tdd (plan §14). It runs only checks that are fully
# reproducible on this host and writes raw logs + a pass/fail summary under
# artifacts/2026-10-30-stable60fps/verify/<UTC-stamp>/.
#
# It makes NO hardware claims: it does not run OBS, does not prove physical
# presentation, GPU acceleration, a real Android phone or a Windows run.
# Physical/long-duration acceptance is documented in docs/VERIFICATION.md.
#
# Usage:
#   sh scripts/verify.sh                 # Docker suite + native offscreen suite
#   sh scripts/verify.sh --docker-only   # deterministic headless suite only
#   sh scripts/verify.sh --native-only   # native Qt contract suite only
#   sh scripts/verify.sh --require-native  # a missing/skipped native env is a FAIL
#   sh scripts/verify.sh --help
#
# Exit codes: 0 = every executed check passed (skips allowed), 1 = a check
# failed, 2 = bad usage. Skips are always recorded with the exact reason.
set -u

cd "$(dirname "$0")/.." || exit 1

RUN_DIR="${VERIFY_DIR:-artifacts/2026-10-30-stable60fps/verify}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$RUN_DIR/$STAMP"
MODE=all
REQUIRE_NATIVE=0

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --docker-only) MODE=docker ;;
        --native-only) MODE=native ;;
        --require-native) REQUIRE_NATIVE=1 ;;
        *) echo "verify.sh: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$OUT" || exit 1
export DOCKER_CONFIG="${DOCKER_CONFIG:-$PWD/.docker}"
STATUS=0
SUMMARY="$OUT/summary.txt"
: > "$SUMMARY"

record() {
    printf '%s\n' "$*" | tee -a "$SUMMARY"
}

record "AirFrame verification: branch codex/stable-60fps-tdd"
record "run: $OUT"
record "date (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
record "mode: $MODE${REQUIRE_NATIVE:+ require_native=$REQUIRE_NATIVE}"
{
    echo "uname: $(uname -a)"
    echo "git HEAD: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "git branch: $(git branch --show-current 2>/dev/null || echo unknown)"
    echo "git dirty paths: $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    echo "host python: $([ -x .venv/bin/python ] && .venv/bin/python --version 2>&1 || command -v python3 || true)"
    echo "docker: $(docker --version 2>/dev/null || echo 'not available')"
} > "$OUT/environment.txt"
record "environment: $OUT/environment.txt"
record ""

# run_step NAME COMMAND... -> 0 pass, 1 fail; raw output in $OUT/NAME.log
run_step() {
    name=$1
    shift
    printf '== %s: %s\n' "$name" "$*" | tee -a "$SUMMARY"
    "$@" > "$OUT/$name.log" 2>&1
    code=$?
    if [ "$code" -eq 0 ]; then
        record "PASS $name"
        record ""
        return 0
    fi
    record "FAIL $name (exit $code) — see $OUT/$name.log"
    record ""
    return 1
}

skip_step() {
    record "SKIP $1 — $2"
    record ""
}

# --- prerequisite detection -------------------------------------------------
HAVE_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info > "$OUT/docker-info.log" 2>&1; then
    HAVE_DOCKER=1
else
    skip_step docker "docker CLI or daemon is unavailable (see $OUT/docker-info.log)"
fi

HAVE_NATIVE=0
NATIVE_REASON=""
if [ ! -x .venv/bin/python ]; then
    NATIVE_REASON=".venv/bin/python is missing"
elif ! QT_QPA_PLATFORM=offscreen .venv/bin/python -c 'import PySide6' >/dev/null 2>&1; then
    NATIVE_REASON="host .venv has no usable PySide6"
elif [ ! -f tests_native/qt_harness.py ]; then
    NATIVE_REASON="tests_native/ is missing"
else
    HAVE_NATIVE=1
fi

# --- deterministic headless suite (Docker, pinned Python 3.13) --------------
if [ "$MODE" != native ]; then
    if [ "$HAVE_DOCKER" -eq 1 ]; then
        run_step docker-suite sh scripts/test-docker.sh || STATUS=1
    elif [ "$MODE" = docker ]; then
        record "FAIL docker-suite — Docker is required for the deterministic headless suite"
        record ""
        STATUS=1
    fi
fi

# --- native Qt contract suite (offscreen; macOS/Linux host with Qt) ---------
if [ "$MODE" != docker ]; then
    if [ "$HAVE_NATIVE" -eq 1 ]; then
        if run_step native-setup sh scripts/setup-native-tests.sh \
            && PYTHONPATH="$PWD/.superpowers/native-deps" \
               QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests_native -v \
               > "$OUT/native-suite.log" 2>&1; then
            record "PASS native-suite — see $OUT/native-suite.log"
            record ""
        else
            [ -f "$OUT/native-suite.log" ] || cp "$OUT/native-setup.log" "$OUT/native-suite.log" 2>/dev/null || true
            record "FAIL native-suite — see $OUT/native-suite.log"
            record ""
            STATUS=1
        fi
    else
        if [ "$REQUIRE_NATIVE" -eq 1 ]; then
            record "FAIL native-suite — $NATIVE_REASON (--require-native)"
            record ""
            STATUS=1
        else
            skip_step native-suite "$NATIVE_REASON; offscreen Qt contract not executed"
        fi
    fi
fi

# --- result -----------------------------------------------------------------
if [ "$STATUS" -eq 0 ]; then
    record "RESULT: PASS (all executed checks passed; skipped steps are listed above)"
else
    record "RESULT: FAIL (see failing steps above)"
fi
printf '%s\n' "$STATUS" > "$OUT/exit-code.txt"
ln -sfn "$STAMP" "$RUN_DIR/latest" 2>/dev/null || true
record "logs: $OUT"
record ""
record "This run proves only the deterministic test/contract checks listed above."
record "It does NOT prove physical 60 FPS, OBS capture, GPU/hardware decode,"
record "real Android phone behaviour or a Windows run — see docs/VERIFICATION.md."
exit "$STATUS"
