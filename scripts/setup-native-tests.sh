#!/bin/sh
# Prepare the host-side native Qt test environment WITHOUT modifying .venv or
# site-packages (plan rule). Extra pinned runtime deps are installed into a
# workspace-local, git-ignored directory and injected through PYTHONPATH.
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
DEPS="$PWD/.superpowers/native-deps"
CACHE="$PWD/.superpowers/pip-cache"
mkdir -p "$DEPS" "$CACHE"
PIP_CACHE_DIR="$CACHE" "$PY" -m pip install --disable-pip-version-check -q --target "$DEPS" 'qrcode>=8,<9'
echo "Native test deps ready: $DEPS"
echo "Run native suite: PYTHONPATH=\"$DEPS\" QT_QPA_PLATFORM=offscreen $PY -m unittest discover -s tests_native -v"
