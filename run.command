#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo 'Создайте окружение: python3 -m venv .venv && .venv/bin/pip install -e .'
    exit 1
fi
exec .venv/bin/python -m airframe
