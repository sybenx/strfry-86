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

# --- crypto tests (non-negotiable — this is the whole authorization system) -
#
# lib86/bip340.py has exactly one caller: NIP-98 signature verification in
# server86.py. There are no sessions, cookies, or tokens, so that one
# function is the only thing between a stranger and /api/unban, /api/ban,
# and /api/authors/scan. A verifier that wrongly returns True breaks nothing
# visible — it just opens the admin API to the internet — so unlike every
# other test above, this one covers a failure you would NOT notice by using
# the thing. See tests/README.md for fixture provenance.

CRYPTO_TEST_SCRIPT="$(mktemp)"
cat > "$CRYPTO_TEST_SCRIPT" <<'PYEOF'
import csv
import json
import os
import secrets
import sys

REPO_ROOT = sys.argv[1]
sys.path.insert(0, REPO_ROOT)

from lib86 import bech32, bip340  # noqa: E402
import server86  # noqa: E402

results = []  # (ok, name, detail)


def check(ok, name, detail=None):
    results.append((ok, name, detail))


# --- BIP-340 reference vectors (tests/bip340-vectors.csv, vendored verbatim)

vectors_path = os.path.join(REPO_ROOT, "tests", "bip340-vectors.csv")
applicable = 0
skipped = 0
mismatches = []
with open(vectors_path, newline="") as f:
    for row in csv.DictReader(f):
        msg_hex = row["message"]
        msg = bytes.fromhex(msg_hex) if msg_hex else b""
        if len(msg) != 32:
            # lib86/bip340.py verifies only fixed 32-byte messages (a Nostr
            # event id is always a sha256 hash) — these upstream vectors
            # (added 2022-12) test variable-length messages, a mode this
            # project deliberately does not implement. See tests/README.md.
            skipped += 1
            continue
        applicable += 1
        pubkey = bytes.fromhex(row["public key"])
        sig = bytes.fromhex(row["signature"])
        expected = row["verification result"].strip().upper() == "TRUE"
        try:
            actual = bip340.schnorr_verify(msg, pubkey, sig)
        except Exception as e:
            actual = f"raised {type(e).__name__}"
        if actual != expected:
            mismatches.append(
                f"vector {row['index']} ({row.get('comment', '')}): expected {expected}, got {actual}"
            )

if mismatches:
    check(False, "bip340 test vectors", f"{len(mismatches)} mismatch(es): " + "; ".join(mismatches))
else:
    check(True, f"bip340 test vectors ({applicable} applicable rows, {skipped} skipped: variable-length message)")


# --- NIP-98 fixture + mutations (tests/nip98-fixture.json) ------------------

with open(os.path.join(REPO_ROOT, "tests", "nip98-fixture.json")) as f:
    fixtures = json.load(f)

valid = fixtures["valid"]
admin = valid["pubkey"]
path = "/api/unban"
now = valid["created_at"]


def flip_hex_byte(hexstr, index=0):
    b = bytearray(bytes.fromhex(hexstr))
    b[index] ^= 0xFF
    return b.hex()


def with_field(event, **overrides):
    ev = dict(event)
    ev.update(overrides)
    return ev


ok, err = server86.verify_nip98(valid, admin, path, now=now)
check(ok, "nip98 fixture accepted unmodified", err)

reject_cases = [
    ("flipped sig byte", with_field(valid, sig=flip_hex_byte(valid["sig"])), admin, path, now),
    ("flipped id byte", with_field(valid, id=flip_hex_byte(valid["id"])), admin, path, now),
    ("wrong admin pubkey", valid, flip_hex_byte(admin), path, now),
    ("wrong kind", fixtures["wrong_kind"], admin, path, now),
    ("wrong method tag", fixtures["wrong_method"], admin, path, now),
    ("wrong u path", fixtures["wrong_path"], admin, path, now),
    ("now 120s before created_at (event from the future)", valid, admin, path, now - 120),
    ("now 120s after created_at (stale event)", valid, admin, path, now + 120),
]
for name, ev, adm, p, n in reject_cases:
    ok, err = server86.verify_nip98(ev, adm, p, now=n)
    check(not ok, f"nip98 rejects: {name}", None if not ok else "was wrongly accepted")


# --- kind-0 fixture + mutations (tests/kind0-fixture.json) ------------------
# bip340.py's second and last caller: verifying the raw kind-0 events a
# browser POSTs to /api/names after querying a third-party relay.

with open(os.path.join(REPO_ROOT, "tests", "kind0-fixture.json")) as f:
    kind0_fixtures = json.load(f)

k0_valid = kind0_fixtures["valid"]
k0_pubkey = k0_valid["pubkey"]

verified = server86.verify_kind0_event(k0_valid, {k0_pubkey}, {k0_pubkey})
check(verified is not None, "kind0 fixture accepted unmodified",
      None if verified is not None else "was wrongly rejected")

k0_reject_cases = [
    ("flipped sig byte", with_field(k0_valid, sig=flip_hex_byte(k0_valid["sig"])), {k0_pubkey}, {k0_pubkey}),
    ("flipped id byte", with_field(k0_valid, id=flip_hex_byte(k0_valid["id"])), {k0_pubkey}, {k0_pubkey}),
    ("tampered content", with_field(k0_valid, content=k0_valid["content"] + "x"), {k0_pubkey}, {k0_pubkey}),
    ("wrong kind", kind0_fixtures["wrong_kind"], {k0_pubkey}, {k0_pubkey}),
    ("pubkey not in queried", k0_valid, set(), {k0_pubkey}),
    ("pubkey not currently banned", k0_valid, {k0_pubkey}, set()),
]
for name, ev, queried, banned_pubkeys in k0_reject_cases:
    result = server86.verify_kind0_event(ev, queried, banned_pubkeys)
    check(result is None, f"kind0 rejects: {name}", None if result is None else "was wrongly accepted")


# --- bech32 round-trip / corruption (npub_encode / npub_decode) ------------

pairs_detail = []
for _ in range(5):
    hexkey = secrets.token_hex(32)
    npub = bech32.npub_encode(hexkey)
    back = bech32.npub_decode(npub)
    if back != hexkey:
        pairs_detail.append(f"{hexkey} -> {npub} -> {back}")
check(not pairs_detail, "bech32 round-trip (5 random pubkeys)", "; ".join(pairs_detail) or None)


def raises_value_error(fn, *a):
    try:
        fn(*a)
        return False
    except ValueError:
        return True
    except Exception as e:
        return f"raised {type(e).__name__} instead of ValueError"


sample_hex = secrets.token_hex(32)
sample_npub = bech32.npub_encode(sample_hex)

corrupted = sample_npub[:-1] + ("q" if sample_npub[-1] != "q" else "p")
result = raises_value_error(bech32.npub_decode, corrupted)
check(result is True, "bech32 rejects corrupted checksum", None if result is True else result)

wrong_hrp = bech32.bech32_encode("nsec", bech32.convertbits(list(bytes.fromhex(sample_hex)), 8, 5, True))
result = raises_value_error(bech32.npub_decode, wrong_hrp)
check(result is True, "bech32 rejects wrong HRP (nsec)", None if result is True else result)

truncated = sample_npub[:-4]
result = raises_value_error(bech32.npub_decode, truncated)
check(result is True, "bech32 rejects truncated npub", None if result is True else result)


# --- report ------------------------------------------------------------

for ok, name, detail in results:
    if ok:
        print(f"PASS: {name}")
    else:
        line = f"FAIL: {name}"
        if detail:
            line += f" ({detail})"
        print(line)
PYEOF

CRYPTO_OUTPUT="$(python3 "$CRYPTO_TEST_SCRIPT" "$REPO_ROOT")"
echo "$CRYPTO_OUTPUT"
CRYPTO_FAIL_COUNT="$(echo "$CRYPTO_OUTPUT" | grep -c '^FAIL: ')"
FAILURES=$((FAILURES + CRYPTO_FAIL_COUNT))
rm -f "$CRYPTO_TEST_SCRIPT"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
