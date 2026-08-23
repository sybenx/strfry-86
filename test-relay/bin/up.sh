#!/usr/bin/env bash
# Brings up the throwaway relay and installs Eighty-Six into it through the
# real strfry-86-updater.py path (offline-bundle mode), so every run also
# exercises the installer. See test-relay/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."          # test-relay/
REPO_ROOT="$(cd .. && pwd)"

# The container writes into its /etc/strfry.conf (patches writePolicy.plugin
# on install); mount a disposable copy so the committed template never
# picks up local edits.
mkdir -p .runtime
cp strfry.conf .runtime/strfry.conf

docker compose up -d --wait

# Build the install bundle from a throwaway COPY of the tree, never the
# real repo — this must never touch manifest.json / strfry86-bundle.tar.gz
# in the actual working directory. Copies whatever is on disk right now,
# uncommitted changes included, so the installer test reflects the tree
# you're actually looking at.
TMP_TREE="$(mktemp -d)"
trap 'rm -rf "$TMP_TREE"' EXIT
rsync -a --exclude .git --exclude test-relay --exclude __pycache__ "$REPO_ROOT"/ "$TMP_TREE"/
python3 "$TMP_TREE/tools/make_bundle.py"

docker exec strfry86-test mkdir -p /config/strfry86
docker cp "$TMP_TREE/strfry-86-updater.py" strfry86-test:/config/strfry86/strfry-86-updater.py
docker cp "$TMP_TREE/strfry86-bundle.tar.gz" strfry86-test:/config/strfry86/strfry86-bundle.tar.gz

ADMIN_HEX="$(python3 seed/admin_key.py)"
echo "running the real installer inside the container (admin pubkey: $ADMIN_HEX)..."
# Piped stdin -> ensure_config() sees a non-tty, so contact_appeal is
# skipped automatically and only the one admin-pubkey prompt needs an answer.
printf '%s\n' "$ADMIN_HEX" | docker exec -i strfry86-test python3 /config/strfry86/strfry-86-updater.py

echo
echo "up. admin UI: http://127.0.0.1:18686   relay ws: ws://127.0.0.1:17777"
echo "next: bin/seed.sh"
