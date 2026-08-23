#!/usr/bin/env bash
# Destroys the container AND its volume — every `up` starts from nothing.
set -euo pipefail
cd "$(dirname "$0")/.."   # test-relay/
docker compose down -v
