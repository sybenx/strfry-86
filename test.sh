#!/usr/bin/env bash
# strfry-86 test suite: manifest/bundle freshness + plugin86.py accept/reject logic.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

FAILURES=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# --- manifest + bundle freshness --------------------------------------------

MANIFEST_BACKUP="$(mktemp)"
BUNDLE_BACKUP="$(mktemp)"
cp manifest.json "$MANIFEST_BACKUP"
cp strfry86-bundle.tar.gz "$BUNDLE_BACKUP"
python3 tools/make_bundle.py > /dev/null

if diff -q manifest.json "$MANIFEST_BACKUP" > /dev/null; then
    pass "manifest.json matches working tree"
else
    fail "manifest.json is stale — run tools/make_bundle.py and commit the result"
    cp "$MANIFEST_BACKUP" manifest.json
fi

if diff -q strfry86-bundle.tar.gz "$BUNDLE_BACKUP" > /dev/null; then
    pass "strfry86-bundle.tar.gz matches working tree"
else
    fail "strfry86-bundle.tar.gz is stale — run tools/make_bundle.py and commit the result"
    cp "$BUNDLE_BACKUP" strfry86-bundle.tar.gz
fi
rm -f "$MANIFEST_BACKUP" "$BUNDLE_BACKUP"

# --- committed bundle contents hash-match the manifest ----------------------

CHECK_BUNDLE_SCRIPT="$(mktemp)"
cat > "$CHECK_BUNDLE_SCRIPT" <<'PYEOF'
import hashlib
import json
import sys
import tarfile

bundle_path, manifest_path = sys.argv[1], sys.argv[2]

with open(manifest_path) as f:
    manifest = json.load(f)

with tarfile.open(bundle_path, "r:gz") as tar:
    names = set(tar.getnames())

    missing = sorted(rel for rel in manifest if rel not in names)
    if missing:
        print(f"bundle missing files listed in manifest: {missing}")
        sys.exit(1)
    if "manifest.json" not in names:
        print("bundle missing manifest.json")
        sys.exit(1)

    for rel_path, expected_sha in manifest.items():
        data = tar.extractfile(tar.getmember(rel_path)).read()
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            print(f"hash mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}")
            sys.exit(1)

    bundled_manifest_raw = tar.extractfile(tar.getmember("manifest.json")).read()

with open(manifest_path, "rb") as f:
    committed_manifest_raw = f.read()

if bundled_manifest_raw != committed_manifest_raw:
    print("bundled manifest.json differs from committed manifest.json")
    sys.exit(1)
PYEOF

if python3 "$CHECK_BUNDLE_SCRIPT" strfry86-bundle.tar.gz manifest.json; then
    pass "committed bundle's contents hash-match manifest.json"
else
    fail "committed bundle's contents do NOT hash-match manifest.json"
fi
rm -f "$CHECK_BUNDLE_SCRIPT"

# --- lib86/audit.py pure-function tests --------------------------------------

AUDIT_TEST_SCRIPT="$(mktemp)"
cat > "$AUDIT_TEST_SCRIPT" <<PYEOF
import sys
sys.path.insert(0, "$REPO_ROOT")
from lib86 import audit

failures = 0


def check(name, cond):
    global failures
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures += 1


ADMIN = "a" * 64
BANNED = "b" * 64
GHOST = "c" * 64
NORMAL = "d" * 64
NOW = 1700000000

# a pubkey with only a 10002 classifies as ghost; one with a 10002 plus a
# kind 1 does not.
relay_list_events = [
    {"pubkey": GHOST, "kind": 10002, "created_at": NOW, "tags": []},
    {"pubkey": NORMAL, "kind": 10002, "created_at": NOW, "tags": []},
]
footprint_events = [
    {"pubkey": GHOST, "kind": 10002, "created_at": NOW, "tags": []},
    {"pubkey": NORMAL, "kind": 10002, "created_at": NOW, "tags": []},
    {"pubkey": NORMAL, "kind": 1, "created_at": NOW, "tags": []},
]
notice = audit.build_ghosts_notice(relay_list_events, footprint_events, set(), ADMIN, "")
check(
    "pubkey with only a 10002 classifies as ghost",
    notice is not None and GHOST in notice["pubkeys"],
)
check(
    "pubkey with a 10002 plus a kind 1 does not classify as ghost",
    notice is not None and NORMAL not in notice["pubkeys"],
)

# admin and already-banned pubkeys are excluded from ghosts.
relay_list_events2 = [
    {"pubkey": ADMIN, "kind": 10002, "created_at": NOW, "tags": []},
    {"pubkey": BANNED, "kind": 10002, "created_at": NOW, "tags": []},
    {"pubkey": GHOST, "kind": 10002, "created_at": NOW, "tags": []},
]
notice2 = audit.build_ghosts_notice(relay_list_events2, relay_list_events2, {BANNED}, ADMIN, "")
check("admin pubkey is excluded from ghosts", notice2 is not None and ADMIN not in notice2["pubkeys"])
check("already-banned pubkey is excluded from ghosts", notice2 is not None and BANNED not in notice2["pubkeys"])
check("non-admin non-banned ghost is still included", notice2 is not None and GHOST in notice2["pubkeys"])

# normalize_relay_url collides wss://X.Y/, wss://x.y, and wss://x.y//.
check(
    "normalize_relay_url collides wss://X.Y/, wss://x.y, wss://x.y//",
    audit.normalize_relay_url("wss://X.Y/")
    == audit.normalize_relay_url("wss://x.y")
    == audit.normalize_relay_url("wss://x.y//")
    == "wss://x.y",
)

# >=10 same-hour relay lists from distinct authors produce a burst notice.
burst_events = [
    {"pubkey": f"{i:064x}", "kind": 10002, "created_at": NOW + i, "tags": []}
    for i in range(10)
]
burst_notices = audit.build_burst_notices(burst_events)
check(
    "10 same-hour relay lists from distinct authors produce a burst notice",
    len(burst_notices) == 1 and len(burst_notices[0]["pubkeys"]) == 10,
)

# 5 identical sorted r-tag sets produce a fingerprint notice.
fp_tags = [["r", "wss://a.example"], ["r", "wss://b.example"]]
fp_events = [
    {"pubkey": f"{i:064x}", "kind": 10002, "created_at": NOW, "tags": fp_tags}
    for i in range(5)
]
fp_notices = audit.build_fingerprint_notices(fp_events)
check(
    "5 identical sorted r-tag sets produce a fingerprint notice",
    len(fp_notices) == 1 and len(fp_notices[0]["pubkeys"]) == 5,
)

# malformed events (missing tags, tags not a list) are skipped without raising.
malformed = [
    {"pubkey": GHOST, "kind": 10002, "created_at": NOW},
    {"pubkey": GHOST, "kind": 10002, "created_at": NOW, "tags": "not-a-list"},
    "not-a-dict",
    {"pubkey": GHOST, "created_at": NOW, "tags": []},
]
try:
    audit.build_ghosts_notice(malformed, malformed, set(), ADMIN, "")
    audit.build_burst_notices(malformed)
    audit.build_fingerprint_notices(malformed)
    audit.build_purge_pending_notice(malformed)
    audit.build_activity(malformed, now_ts=NOW)
    check("malformed events are skipped without raising", True)
except Exception as e:
    check(f"malformed events are skipped without raising (raised {e!r})", False)

sys.exit(1 if failures else 0)
PYEOF

AUDIT_TEST_OUTPUT="$(python3 "$AUDIT_TEST_SCRIPT")"
echo "$AUDIT_TEST_OUTPUT"
AUDIT_FAIL_COUNT="$(echo "$AUDIT_TEST_OUTPUT" | grep -c '^FAIL:')"
FAILURES=$((FAILURES + AUDIT_FAIL_COUNT))
rm -f "$AUDIT_TEST_SCRIPT"

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

# Helper: assert a pubkey's stored report_type matches expectations.
cat > "$TESTDIR/check_report_type.py" <<'PYEOF'
import json
import sys

pubkey, expected, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
actual = data.get(pubkey, {}).get("report_type")
expected = None if expected == "__NONE__" else expected
sys.exit(0 if actual == expected else 1)
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

check_report_type() {
    python3 "$TESTDIR/check_report_type.py" "$1" "$2" "$TESTDIR/blacklist.json"
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

# 6. admin 1984 with a NIP-56 report type records report_type
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e6 "$ADMIN_HEX" 1984 "[[\"p\",\"$TARGET_HEX\",\"spam\"]]" "reported for spam" 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "admin 1984 report with type accepted"; else fail "admin 1984 report with type should be accepted, got: $OUT"; fi
if check_report_type "$TARGET_HEX" "spam"; then pass "admin 1984 records report_type from p tag"; else fail "report_type should have been recorded as 'spam'"; fi

# 7. admin 1984 without a report type records report_type as null
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e7 "$ADMIN_HEX" 1984 "[[\"p\",\"$TARGET_HEX\"]]" "reported" 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_report_type "$TARGET_HEX" "__NONE__"; then pass "admin 1984 without type records report_type as null"; else fail "report_type should be null when p tag has no type"; fi

# 8. admin 1984 note report (bare p tag, type on e tag) falls back to e tag's type
echo '{}' > "$TESTDIR/blacklist.json"
LINE="$(mkevent e8 "$ADMIN_HEX" 1984 "[[\"e\",\"someeventid\",\"malware\"],[\"p\",\"$TARGET_HEX\"]]" "" 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_report_type "$TARGET_HEX" "malware"; then pass "admin 1984 note report falls back to e tag's report_type"; else fail "report_type should fall back to the e tag's type ('malware') for a bare p tag"; fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
