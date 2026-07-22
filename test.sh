#!/usr/bin/env bash
# strfry-86 test suite: manifest freshness + plugin86.py accept/reject logic.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

FAILURES=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# --- manifest freshness ----------------------------------------------------

MANIFEST_BACKUP="$(mktemp)"
cp manifest.json "$MANIFEST_BACKUP"
python3 tools/make_manifest.py > /dev/null
if diff -q manifest.json "$MANIFEST_BACKUP" > /dev/null; then
    pass "manifest.json matches working tree"
else
    fail "manifest.json is stale — run tools/make_manifest.py and commit the result"
    cp "$MANIFEST_BACKUP" manifest.json
fi
rm -f "$MANIFEST_BACKUP"

# --- plugin86.py sandbox ----------------------------------------------------

TESTDIR="$(mktemp -d)"
trap 'rm -rf "$TESTDIR"' EXIT

cp plugin86.py "$TESTDIR/"
cp -r lib86 "$TESTDIR/"
# server86.py is intentionally NOT copied: plugin86 tries to spawn it on
# startup, and without the file present that spawn harmlessly no-ops
# instead of binding a real port during the test run.

# Helper: build one JSONL event line from plain CLI args (avoids fragile
# nested shell/JSON quoting).
cat > "$TESTDIR/mkevent.py" <<'PYEOF'
import json
import sys

event_id, pubkey, kind, tags_json, content, created_at = sys.argv[1:7]
event = {
    "id": event_id,
    "pubkey": pubkey,
    "kind": int(kind),
    "tags": json.loads(tags_json),
    "content": content,
    "created_at": int(created_at),
}
print(json.dumps({"type": "new", "event": event}))
PYEOF

# Helper: assert the plugin's output line has the given action.
cat > "$TESTDIR/check_action.py" <<'PYEOF'
import json
import sys

out_line, expected = sys.argv[1], sys.argv[2]
obj = json.loads(out_line)
sys.exit(0 if obj.get("action") == expected else 1)
PYEOF

# Helper: assert whether a pubkey is present in blacklist.json.
cat > "$TESTDIR/is_banned.py" <<'PYEOF'
import json
import sys

pubkey, path = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
sys.exit(0 if pubkey in data else 1)
PYEOF

ADMIN_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
OTHER_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
BANNED_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
TARGET_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
THIRD_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

cat > "$TESTDIR/config.json" <<EOF
{"admin_pubkey_hex": "$ADMIN_HEX", "port": 8686, "bind": "0.0.0.0"}
EOF

mkevent() {
    python3 "$TESTDIR/mkevent.py" "$@"
}

run_plugin() {
    echo "$1" | python3 "$TESTDIR/plugin86.py" 2>"$TESTDIR/stderr.log"
}

check_action() {
    python3 "$TESTDIR/check_action.py" "$1" "$2"
}

is_banned_in_file() {
    python3 "$TESTDIR/is_banned.py" "$1" "$TESTDIR/blacklist.json"
}

# 1. normal event accepted
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e1 "$OTHER_HEX" 1 '[]' hello 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "normal event accepted"; else fail "normal event should be accepted, got: $OUT"; fi

# 2. banned author rejected
python3 - "$BANNED_HEX" "$TESTDIR/blacklist.json" <<'PYEOF'
import json, sys
pubkey, path = sys.argv[1], sys.argv[2]
json.dump({pubkey: {"banned_at": 1, "report_event_id": "r1", "reason": "spam"}}, open(path, "w"))
PYEOF
LINE="$(mkevent e2 "$BANNED_HEX" 1 '[]' x 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" reject; then pass "banned author rejected"; else fail "banned author should be rejected, got: $OUT"; fi

# 3. admin 1984 bans its p-tags
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e3 "$ADMIN_HEX" 1984 "[[\"p\",\"$TARGET_HEX\"]]" "reported for spam" 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "admin 1984 report accepted"; else fail "admin 1984 report should be accepted, got: $OUT"; fi
if is_banned_in_file "$TARGET_HEX"; then pass "admin 1984 bans its p-tags"; else fail "target pubkey should have been added to blacklist"; fi

# 4. non-admin 1984 does not ban
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e4 "$OTHER_HEX" 1984 "[[\"p\",\"$THIRD_HEX\"]]" reported 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "non-admin 1984 report accepted (not treated specially)"; else fail "non-admin 1984 should still be accepted, got: $OUT"; fi
if is_banned_in_file "$THIRD_HEX"; then fail "non-admin 1984 must NOT ban its p-tags"; else pass "non-admin 1984 does not ban"; fi

# 5. admin pubkey cannot be banned
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e5 "$ADMIN_HEX" 1984 "[[\"p\",\"$ADMIN_HEX\"]]" self 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "admin self-report event accepted"; else fail "admin self-report should still be accepted, got: $OUT"; fi
if is_banned_in_file "$ADMIN_HEX"; then fail "admin pubkey must never be banned"; else pass "admin pubkey cannot be banned"; fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
