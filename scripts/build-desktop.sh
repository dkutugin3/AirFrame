#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker build --target export --output type=local,dest=artifacts/desktop .
