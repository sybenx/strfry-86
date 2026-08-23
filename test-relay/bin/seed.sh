#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."   # test-relay/
python3 seed/seed.py
