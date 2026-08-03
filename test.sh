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

# --- style block identical on every page ------------------------------------
# Silent failure this covers: one page's <style> edited in place. Nothing
# errors, no test notices, and that page quietly renders differently from the
# other eight — which CLAUDE.md calls a bug.

if python3 tools/stamp_style.py --check > /dev/null 2>&1; then
    pass "style block is byte-identical across every page"
else
    fail "style block drifted — edit tools/style86.css, then run tools/stamp_style.py"
fi

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

# 1b. NIP-16 ephemeral kinds must be ACCEPTED by the plugin so strfry can
# broadcast them to open subscriptions. Persistence is strfry's job
# (expiration=1 + ephemeralEventsLifetimeSeconds cron), not a write-policy reject
# — a reject would also suppress live fan-out.
LINE="$(mkevent e1b "$OTHER_HEX" 20000 '[]' typing 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "ephemeral kind 20000 accepted (broadcast path)"; else fail "ephemeral kind 20000 should be accepted so strfry can fan out, got: $OUT"; fi
LINE="$(mkevent e1c "$OTHER_HEX" 29999 '[]' presence 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" accept; then pass "ephemeral kind 29999 accepted (broadcast path)"; else fail "ephemeral kind 29999 should be accepted so strfry can fan out, got: $OUT"; fi
# Banned authors still lose, including ephemeral — ban is about the author,
# not the kind.
python3 - "$BANNED_HEX" "$TESTDIR/blacklist.json" <<'PYEOF'
import json, sys
pubkey, path = sys.argv[1], sys.argv[2]
json.dump({pubkey: {"banned_at": 1, "report_event_id": "r1", "reason": "spam"}}, open(path, "w"))
PYEOF
LINE="$(mkevent e1d "$BANNED_HEX" 22242 '[]' auth 1700000000)"
OUT="$(run_plugin "$LINE")"
if check_action "$OUT" reject; then pass "banned author ephemeral still rejected"; else fail "banned author ephemeral should still be rejected, got: $OUT"; fi
echo '{}' > "$TESTDIR/blacklist.json"

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


# Fixture u is https://relay.example/api/unban — bind origin like do_POST does.
_origin = "https://relay.example"
ok, err = server86.verify_nip98(valid, admin, path, now=now, expected_origin=_origin)
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
    ok, err = server86.verify_nip98(ev, adm, p, now=n, expected_origin=_origin)
    check(not ok, f"nip98 rejects: {name}", None if not ok else "was wrongly accepted")

# Origin binding: same path on a phishing host must fail even with a valid sig
# over that evil u (fixture is signed for relay.example; evil origin is wrong).
ok, err = server86.verify_nip98(valid, admin, path, now=now, expected_origin="https://evil.example")
check(not ok and err == "wrong u origin",
      "nip98 rejects: wrong u origin (phishing host)", err)

# Path-only was the old check — origin mismatch is the new defence. Same
# fixture against the correct origin still passes (above).
ok, err = server86.verify_nip98(valid, admin, path, now=now, expected_origin="https://relay.example:443")
# netloc differs (:443 explicit vs default) — treat as different origins
check(not ok, "nip98 rejects: origin netloc must match exactly (incl. port)", err)

# payload_body_hash: empty non-auth object is stable across languages
_empty_hash = server86.payload_body_hash({"auth": valid})
check(_empty_hash == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "payload_body_hash of {auth-only} is sha256 of '{}'")
_with_pubkeys = server86.payload_body_hash({"auth": valid, "pubkeys": ["aa" * 32]})
check(_with_pubkeys == server86.payload_body_hash({"pubkeys": ["aa" * 32]}),
      "payload_body_hash ignores the auth key")

# Require payload tag when payload_sha256 is supplied — fixture has no tag
ok, err = server86.verify_nip98(
    valid, admin, path, now=now, expected_origin=_origin,
    payload_sha256=_empty_hash,
)
check(not ok and err == "payload hash mismatch",
      "nip98 rejects: missing payload tag when body hash is required", err)

# Replay: first consume succeeds, second with same id fails
server86._nip98_used_ids.clear()
ok1, _ = server86.verify_nip98(
    valid, admin, path, now=now, expected_origin=_origin, consume_id=True)
ok2, err2 = server86.verify_nip98(
    valid, admin, path, now=now, expected_origin=_origin, consume_id=True)
check(ok1 and (not ok2) and err2 == "auth event already used",
      "nip98 rejects: replay of the same auth event id within TTL", err2)
server86._nip98_used_ids.clear()

# normalize_public_origin strips path and trailing slash
check(server86.normalize_public_origin("https://Relay.Example/") == "https://relay.example",
      "normalize_public_origin lowercases and strips trailing slash")
check(server86.normalize_public_origin("relay.example") == "https://relay.example",
      "normalize_public_origin defaults bare host to https")
check(server86.normalize_public_origin("") is None,
      "normalize_public_origin returns None for blank")


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

# --- async author-scan bounds (server86.py) ---------------------------------
# Every one of these is a silent failure if it stops holding: a bad `mode`
# must never fall back to a default, the request must never be able to
# supply a raw limit/kinds filter, and /api/names' widened accept-bound
# must only ever come from server-held state (banned ∪ author-scan cache).

BOUNDS_TEST_SCRIPT="$(mktemp)"
cat > "$BOUNDS_TEST_SCRIPT" <<'PYEOF'
import json
import os
import sys

REPO_ROOT = sys.argv[1]
sys.path.insert(0, REPO_ROOT)
import server86  # noqa: E402

results = []


def check(ok, name, detail=None):
    results.append((ok, name, detail))


# --- validate_authors_scan_mode -----------------------------------------

mode, err = server86.validate_authors_scan_mode({"mode": "recent"})
check(mode == "recent" and err is None, "validate_authors_scan_mode accepts 'recent'")

mode, err = server86.validate_authors_scan_mode({"mode": "full"})
check(mode == "full" and err is None, "validate_authors_scan_mode accepts 'full'")

mode, err = server86.validate_authors_scan_mode({})
check(mode is None and err is not None, "validate_authors_scan_mode rejects a missing mode")

mode, err = server86.validate_authors_scan_mode({"mode": "bogus"})
check(mode is None and err is not None, "validate_authors_scan_mode rejects an unrecognized mode name")

mode, err = server86.validate_authors_scan_mode({"mode": "recent", "limit": 250000})
check(mode is None and err is not None, "validate_authors_scan_mode rejects a request-supplied limit")

mode, err = server86.validate_authors_scan_mode({"mode": "recent", "kinds": [1, 2]})
check(mode is None and err is not None, "validate_authors_scan_mode rejects a request-supplied kinds array")


# --- validate_relay_url_input (POST /api/relay-url) ----------------------

value, err = server86.validate_relay_url_input({"relay_url": "wss://relay.example.com"})
check(value == "wss://relay.example.com" and err is None,
      "validate_relay_url_input accepts a wss:// URL")

value, err = server86.validate_relay_url_input({"relay_url": "  relay.example.com  "})
check(value == "relay.example.com" and err is None,
      "validate_relay_url_input accepts a bare hostname and trims whitespace")

value, err = server86.validate_relay_url_input({})
check(value is None and err is None,
      "validate_relay_url_input treats a missing relay_url as 'clear', not an error")

value, err = server86.validate_relay_url_input({"relay_url": "   "})
check(value is None and err is None,
      "validate_relay_url_input treats a blank/whitespace-only relay_url as 'clear', not an error")

value, err = server86.validate_relay_url_input({"relay_url": 12345})
check(value is None and err is not None,
      "validate_relay_url_input rejects a non-string relay_url")

value, err = server86.validate_relay_url_input({"relay_url": "://not a url"})
check(value is None and err is not None,
      "validate_relay_url_input rejects a string with no parseable hostname")


# --- authors_scan_pubkeys / /api/names widened accept-bound --------------

check(server86.authors_scan_pubkeys() == set(), "authors_scan_pubkeys is empty before any scan has run")

with open(os.path.join(REPO_ROOT, "tests", "kind0-fixture.json")) as f:
    kind0_fixture = json.load(f)
real_pubkey = kind0_fixture["valid"]["pubkey"]

with server86._scan_lock:
    server86._authors_cache = dict(server86._authors_cache)
    server86._authors_cache["authors"] = [{"pubkey": real_pubkey, "npub": "npub1fake", "count": 3}]

check(real_pubkey in server86.authors_scan_pubkeys(),
      "authors_scan_pubkeys reflects a pubkey present in the author-scan cache")

# The accept-bound is a union of banned pubkeys and author-scan-cache
# pubkeys, assembled exactly as the /api/names handler assembles it —
# never a value taken from the request. With no bans in this process,
# the only reason this validly-signed event should be accepted is that
# its pubkey is in the author-scan cache.
acceptable = set() | server86.authors_scan_pubkeys()
verified = server86.verify_kind0_event(kind0_fixture["valid"], {real_pubkey}, acceptable)
check(verified is not None,
      "verify_kind0_event accepts a validly-signed kind-0 for a pubkey that is only in the "
      "author-scan cache, not banned")


# --- Phase 2: /api/recipients, /api/subscribers (server86.py) -------------
# Every async scan (authors, recipients, subscribers) shares one code path
# (_run_scan_job/_start_scan_job/_scan_status) per CLAUDE.md's "one code
# path for all of them", so these checks double as coverage for that
# shared machinery via the two newer resources. Cache paths are redirected
# into a tempdir first so a test run never writes a *-cache.json into the
# repo itself.

import tempfile as _tempfile

_cache_tmpdir = _tempfile.mkdtemp()
server86.RECIPIENTS_CACHE_PATH = os.path.join(_cache_tmpdir, "recipients-cache.json")
server86.SUBSCRIBERS_CACHE_PATH = os.path.join(_cache_tmpdir, "subscribers-cache.json")

# relay_url now lives in config.json, not strfry.conf — redirect CONFIG_PATH
# too, same reasoning as the cache paths above: a test run must never write
# into the repo's own config.json.
server86.CONFIG_PATH = os.path.join(_cache_tmpdir, "config.json")
with open(server86.CONFIG_PATH, "w") as f:
    json.dump({"admin_pubkey_hex": "a" * 64}, f)

# never-scanned skeleton shape
_idle_job_fields = {"status": "idle", "started_at": None, "progress": None,
                     "total": None, "rate": None, "eta": None, "blocked_by": None}
check(server86.get_recipients_status() == {**server86._empty_recipients_result(), **_idle_job_fields},
      "get_recipients_status is the never-scanned skeleton before any scan has run")
check(server86.get_subscribers_status() == {**server86._empty_subscribers_result(), **_idle_job_fields},
      "get_subscribers_status is the never-scanned skeleton before any scan has run")

# single-flight, for both resources, mirroring the authors coverage above
import time as _time

_orig_compute_recipients = server86.compute_recipients
_recipients_calls = {"n": 0}


def _fake_compute_recipients(progress_cb=None):
    _recipients_calls["n"] += 1
    _time.sleep(0.2)
    return {"scanned_at": 111, "events_read": 1, "span_start": 1, "span_end": 1,
            "saturated": False, "warning": None, "recipients": []}


server86.compute_recipients = _fake_compute_recipients
s1 = server86.start_recipients_scan()
s2 = server86.start_recipients_scan()
check(s1["status"] == "running" and s2 == s1,
      "/api/recipients: a POST while one is running returns the SAME 202 shape, not a second scan")
check(_recipients_calls["n"] == 1, "/api/recipients: single-flight actually prevented a second compute call")
_time.sleep(0.4)
check(server86.get_recipients_status()["scanned_at"] == 111,
      "/api/recipients: scan completes, persists, and GET reflects it without scanning again")
check(os.path.exists(server86.RECIPIENTS_CACHE_PATH), "/api/recipients: result persisted to recipients-cache.json")
server86.compute_recipients = _orig_compute_recipients

_orig_compute_subscribers = server86.compute_subscribers
_subscribers_calls = {"n": 0}


def _fake_compute_subscribers(progress_cb=None):
    _subscribers_calls["n"] += 1
    _time.sleep(0.2)
    return {"scanned_at": 222, "relay_url": "wss://relay.example.com", "saturated": False,
            "warning": None, "subscribers": [], "general_subscribers": []}


server86.compute_subscribers = _fake_compute_subscribers
s1 = server86.start_subscribers_scan()
s2 = server86.start_subscribers_scan()
check(s1["status"] == "running" and s2 == s1,
      "/api/subscribers: a POST while one is running returns the SAME 202 shape, not a second scan")
check(_subscribers_calls["n"] == 1, "/api/subscribers: single-flight actually prevented a second compute call")
_time.sleep(0.4)
check(server86.get_subscribers_status()["scanned_at"] == 222,
      "/api/subscribers: scan completes, persists, and GET reflects it without scanning again")
server86.compute_subscribers = _orig_compute_subscribers

# recipients tally: bounded by the constant limit, tallies the `p` tag
# (recipient), never the event's own single-use `pubkey` (sender)
_orig_streaming = server86.run_strfry_scan_streaming
_orig_resolve_profiles = server86.resolve_profiles
server86.resolve_profiles = lambda pks: {}


def _streaming_recipients(filter_obj, on_event, timeout, on_progress=None):
    check(filter_obj == {"kinds": [1059], "limit": server86.RECIPIENT_SCAN_LIMIT},
          "compute_recipients scans exactly {kinds:[1059], limit:RECIPIENT_SCAN_LIMIT} — no wider filter")
    check(timeout == server86.AUTHOR_SCAN_DEADLINE,
          "compute_recipients uses AUTHOR_SCAN_DEADLINE, the async failure timeout, not SCAN_TIMEOUT")
    sender = "e" * 64
    recipient = "f" * 64
    on_event({"pubkey": sender, "created_at": 100, "tags": [["p", recipient]]})
    on_event({"pubkey": sender, "created_at": 200, "tags": [["p", recipient]]})
    return 2


server86.run_strfry_scan_streaming = _streaming_recipients
recipients_result = server86.compute_recipients()
check(recipients_result["recipients"] == [{"pubkey": "f" * 64, "npub": server86.bech32.npub_encode("f" * 64),
                                            "name": None, "nip05": None, "count": 2}],
      "compute_recipients tallies the `p` tag (recipient), never the gift wrap's own single-use pubkey")
check(recipients_result["span_start"] == 100 and recipients_result["span_end"] == 200,
      "compute_recipients reports the actual span of created_at seen")
server86.run_strfry_scan_streaming = _orig_streaming

# subscribers: host-only matching (rejects a lookalike suffix domain),
# kind 10050 vs 10002 kept separate, giftwrap_count cross-referenced from
# the (redirected) recipients cache
check(server86._hostname_of("wss://relay.example/") == "relay.example",
      "_hostname_of normalizes scheme+trailing-slash")
check(server86._hostname_of("relay.example") == "relay.example",
      "_hostname_of accepts a bare hostname with no scheme")
check(server86._hostname_of("wss://relay.example.evil.com") != server86._hostname_of("wss://relay.example"),
      "_hostname_of does not let a suffix-matching lookalike domain compare equal")
check(server86._hostname_of(None) is None and server86._hostname_of("") is None,
      "_hostname_of returns None for missing/empty input rather than ''")

_orig_run_strfry_count = server86.run_strfry_count

# relay_url: config.json-backed (POST /api/relay-url, GET /api/relay-url),
# replacing the old hand-edited strfry.conf `info.url` field entirely.
check(server86.get_relay_url() is None,
      "get_relay_url returns None before relay_url is ever set")

server86.set_relay_url("wss://relay.example.com")
check(server86.get_relay_url() == "wss://relay.example.com",
      "set_relay_url writes relay_url, and get_relay_url reads it straight back")
with open(server86.CONFIG_PATH) as f:
    _cfg_after_set = json.load(f)
check(_cfg_after_set.get("relay_url") == "wss://relay.example.com",
      "set_relay_url persists relay_url into config.json itself")
check(_cfg_after_set.get("admin_pubkey_hex") == "a" * 64,
      "set_relay_url leaves the rest of config.json (admin_pubkey_hex) untouched")

server86.set_relay_url(None)
check(server86.get_relay_url() is None,
      "set_relay_url(None) clears relay_url")
with open(server86.CONFIG_PATH) as f:
    check("relay_url" not in json.load(f),
          "set_relay_url(None) removes the key from config.json rather than writing an empty string")

_orig_get_relay_url = server86.get_relay_url

server86.get_relay_url = lambda: None
try:
    server86.compute_subscribers()
    check(False, "compute_subscribers raises — never a fake-empty result — when relay_url is unconfigured")
except RuntimeError as e:
    check("relay_url" in str(e), "compute_subscribers's precondition-failure message names relay_url")
    check("admin page" in str(e), "compute_subscribers's precondition-failure message states the fix")

dm_pubkey = "a" * 64
general_pubkey = "b" * 64
lookalike_pubkey = "c" * 64


def _streaming_subscribers(filter_obj, on_event, timeout, on_progress=None):
    kind = filter_obj["kinds"][0]
    check(filter_obj["limit"] == server86.SUBSCRIBER_SCAN_LIMIT,
          f"compute_subscribers bounds kind {kind} scan by SUBSCRIBER_SCAN_LIMIT")
    if kind == 10050:
        on_event({"pubkey": dm_pubkey, "created_at": 100, "tags": [["relay", "wss://relay.example.com/"]]})
        on_event({"pubkey": lookalike_pubkey, "created_at": 100, "tags": [["relay", "wss://relay.example.com.evil.com"]]})
        return 2
    else:
        on_event({"pubkey": general_pubkey, "created_at": 50, "tags": [["relay", "relay.example.com"]]})
        return 1


_subscriber_counts = {10050: 2, 10002: 1}


def _count_subscribers(filter_obj, timeout=None):
    return _subscriber_counts[filter_obj["kinds"][0]]


server86.get_relay_url = lambda: "wss://relay.example.com"
server86.run_strfry_scan_streaming = _streaming_subscribers
server86.run_strfry_count = _count_subscribers
server86._recipients_cache.clear()
server86._recipients_cache.update({"scanned_at": 999, "recipients": [{"pubkey": dm_pubkey, "count": 42}]})

subs_result = server86.compute_subscribers()
check([r["pubkey"] for r in subs_result["subscribers"]] == [dm_pubkey],
      "compute_subscribers (kind 10050) accepts the exact host and rejects the lookalike suffix domain")
check(subs_result["subscribers"][0]["giftwrap_count"] == 42,
      "compute_subscribers cross-references giftwrap_count from the recipients cache")
check([r["pubkey"] for r in subs_result["general_subscribers"]] == [general_pubkey],
      "compute_subscribers keeps kind 10002 (general_subscribers) separate from kind 10050 (subscribers)")
check(subs_result["general_subscribers"][0]["giftwrap_count"] is None,
      "compute_subscribers reports giftwrap_count null for a pubkey absent from the recipients cache")
check(subs_result["counted"] == {"10050": 2, "10002": 1},
      "compute_subscribers runs an exact index --count per kind, so saturation is a fact rather than an inference")

# Saturation: a subscriber-list FLOOR is unsafe in the direction that
# matters (missing subscribers means missing exemptions means MORE
# deletion), unlike a saturated recipient scan, which only deletes less.
server86.SUBSCRIBER_SCAN_LIMIT = 1


def _streaming_subscribers_saturated(filter_obj, on_event, timeout, on_progress=None):
    on_event({"pubkey": dm_pubkey, "created_at": 100, "tags": [["relay", "wss://relay.example.com/"]]})
    return filter_obj["limit"]


server86.run_strfry_scan_streaming = _streaming_subscribers_saturated
saturated_result = server86.compute_subscribers()
check(saturated_result["saturated"] is True,
      "compute_subscribers reports saturated when the bounded scan hit SUBSCRIBER_SCAN_LIMIT")
server86.SUBSCRIBER_SCAN_LIMIT = 50000

server86.run_strfry_scan_streaming = _orig_streaming
server86.run_strfry_count = _orig_run_strfry_count
server86.resolve_profiles = _orig_resolve_profiles


# --- Live layer: one shared poll tick, kind/ts/id only, deltas reset -----
_orig_scan = server86.run_strfry_scan
server86._live_since = 100
server86._live_seen_at_since = set()
server86._live_recent = []
server86._live_new_events = 0
server86._live_deletes = 0
server86._live_delta_baseline_at = None
server86.run_strfry_scan = lambda filter_obj, timeout=10: [
    {"id": "a" * 64, "kind": 1, "created_at": 100, "pubkey": "b" * 64, "content": "secret", "tags": [["p", "z"]]},
    {"id": "c" * 64, "kind": 5, "created_at": 101, "content": "x"},
]
server86._live_poll_tick()
check(server86._live_error is None and len(server86._live_recent) == 2,
      "_live_poll_tick folds new events into the shared ring buffer on a successful poll")
check(all(set(e.keys()) == {"id", "kind", "created_at"} for e in server86._live_recent),
      "_live_poll_tick strips every field except id/kind/created_at (no pubkey/content/tags)")
check("secret" not in json.dumps(server86._live_recent) and ("b" * 64) not in json.dumps(server86._live_recent),
      "the live ring buffer never leaks content or pubkey")
check(server86._live_new_events == 2 and server86._live_deletes == 1,
      "_live_poll_tick counts new_events and, separately, kind-5 deletes")
server86.run_strfry_scan = lambda filter_obj, timeout=10: (_ for _ in ()).throw(RuntimeError("no strfry"))
server86._live_poll_tick()
check(server86._live_error is not None and len(server86._live_recent) == 2,
      "_live_poll_tick on failure leaves the prior ring buffer untouched and just sets _live_error (failed poll ≠ empty result)")
server86.run_strfry_scan = _orig_scan

_orig_baseline_at = server86._live_current_baseline_at
server86._live_current_baseline_at = lambda: 12345
server86._live_new_events = 7
server86._live_deletes = 3
server86._live_since = 1
server86._live_poll_tick()
check(server86._live_delta_baseline_at == 12345 and server86._live_since == 12345,
      "a moved report baseline re-anchors the live poll cursor forward")
server86._live_current_baseline_at = _orig_baseline_at
server86._live_since = int(__import__("time").time())
server86._live_recent = []
server86._live_new_events = 0
server86._live_deletes = 0
server86._live_delta_baseline_at = None

# --- Console allowlist: CONSOLE_VERBS only, no per-verb flag table --------
_argv, _err = server86.validate_console_command("delete --filter '{}'")
check(_argv is None and _err and "refused" in _err,
      "console allowlist refuses delete")
_argv, _err = server86.validate_console_command("sync --dry-run")
check(_argv is None and _err and "refused" in _err,
      "console allowlist refuses sync (dropped from CONSOLE_VERBS)")
_argv, _err = server86.validate_console_command("scan '{}'")
check(_argv is not None and _err is None,
      "console allowlist accepts bare scan — CONSOLE_VERBS has no per-verb required-flag table")
_argv, _err = server86.validate_console_command("info")
check(_argv is not None and _err is None, "console allowlist accepts info")
_argv, _err = server86.validate_console_command("export")
check(_argv is not None and _err is None, "console allowlist accepts export")
# compact is Settings-only: confirm dialog, audit trail, fixed output path.
# A free console command would skip all three (WHY.md §5).
_argv, _err = server86.validate_console_command("compact data.mdb.compacted")
check(_argv is None and _err and "refused" in _err,
      "console allowlist refuses compact — Settings Compact is the only path")

# Global job lock applies to console too (CLAUDE.md): refuse while a scan holds it.
_prev_active = server86._active_scan["name"]
server86._active_scan["name"] = "authors"
_refusal = server86.acquire_console_slot()
check(_refusal is not None and _refusal.get("blocked_by") == "authors"
      and "refused" in (_refusal.get("error") or ""),
      "console refuses while authors holds the global job lock")
check(server86._active_scan["name"] == "authors",
      "a refused console acquire leaves the running job's lock untouched")
server86._active_scan["name"] = None
_refusal = server86.acquire_console_slot()
check(_refusal is None and server86._active_scan["name"] == "console",
      "console acquire takes the global lock when idle")
# A scan start while console holds the lock must see blocked_by=console
_blocked = server86._start_scan_job(
    "recipients", server86._recipients_job, lambda: None)
check(_blocked.get("blocked_by") == "console",
      "scan start is blocked while console holds the global lock")
server86.release_console_slot()
check(server86._active_scan["name"] is None,
      "console release clears the global lock")
server86._active_scan["name"] = _prev_active

# --- Memory ceilings: console capture cap + ranked list retention ----------
# Bare export/scan must never buffer an entire LMDB into server86. The pump
# keeps only CONSOLE_STDOUT_MAX bytes then kills the child.
_chunks, _kept, _hit = server86._pump_stream_capped(
    __import__("io").BytesIO(b"x" * (server86.CONSOLE_STDOUT_MAX + 50_000)),
    server86.CONSOLE_STDOUT_MAX,
    lambda: None,
)
check(_hit is True and _kept == server86.CONSOLE_STDOUT_MAX,
      "console stdout pump stops at CONSOLE_STDOUT_MAX and reports truncated")
check(sum(len(c) for c in _chunks) == server86.CONSOLE_STDOUT_MAX,
      "console stdout pump retains exactly the cap, never the overflow")
_kept_rows, _total, _omitted = server86._cap_list_rows(
    list(range(server86.CACHE_LIST_MAX + 123)), server86.CACHE_LIST_MAX,
)
check(len(_kept_rows) == server86.CACHE_LIST_MAX and _total == server86.CACHE_LIST_MAX + 123
      and _omitted == 123,
      "author/recipient list cap keeps head and names the omitted tail count")
_server86_src = open(os.path.join(REPO_ROOT, "server86.py")).read()
# RLIMIT_AS caps ADDRESS SPACE and is inherited by every strfry child. strfry
# mmaps its LMDB over dbParams.mapsize, which reserves far more address space
# than the database occupies, so any such cap makes every scan, walk and
# profile load die with `mdb_env_open: Out of memory` — and lowering the hard
# limit can't be undone by a child, so there is no per-spawn escape hatch.
check("setrlimit" not in _server86_src,
      "server86 never sets an rlimit — RLIMIT_AS would kill every strfry LMDB mmap")
check(not hasattr(server86, "MEMORY_HARD_BYTES"),
      "no MEMORY_HARD_BYTES address-space ceiling exists to be re-applied")
import resource as _resource
check(_resource.getrlimit(_resource.RLIMIT_AS)[1] == _resource.RLIM_INFINITY
      or "MEMORY_HARD_BYTES" not in _server86_src,
      "importing server86 leaves the inherited address-space hard limit alone")
check(server86.POST_BODY_MAX == 2 * 1024 * 1024,
      "POST body ceiling is 2MB")

# --- Reports cache: kind-1984 tallied by DISTINCT reporter per p-tag -----
_orig_scan = server86.run_strfry_scan
server86.run_strfry_scan = lambda filter_obj, timeout=10: [
    {"pubkey": "r" * 64, "tags": [["p", "x" * 64]]},
    {"pubkey": "r" * 64, "tags": [["p", "x" * 64]]},  # same reporter twice — counts once
    {"pubkey": "s" * 64, "tags": [["p", "x" * 64]]},
    {"pubkey": "r" * 64, "tags": [["p", "y" * 64]]},
]
_reports = server86.compute_reports()
server86.run_strfry_scan = _orig_scan
check(_reports["counts"].get("x" * 64) == 2 and _reports["counts"].get("y" * 64) == 1,
      "compute_reports tallies DISTINCT reporters per p-tag, not raw report events")

# --- Static routes: home landing + Bans moved -----------------------------
check(server86.STATIC_ROUTES["/"][0] == "home.html",
      "STATIC_ROUTES maps / to home.html (public landing)")
check(server86.STATIC_ROUTES["/bans"][0] == "bans.html",
      "STATIC_ROUTES maps /bans to bans.html")
for _path, _file in (("/stats", "stats.html"), ("/users", "users.html"),
                     ("/audit", "audit.html"), ("/settings", "settings.html")):
    check(server86.STATIC_ROUTES.get(_path, (None,))[0] == _file,
          f"STATIC_ROUTES maps {_path} to {_file}")
check("/home" not in server86.STATIC_ROUTES,
      "'/home' is not a static route — LEGACY_REDIRECTS sends it to /")

# --- Merged / moved pages: old paths redirect, they do not 404 or double-serve
# Landing moved from /home to /. Report became a section of Stats & Console
# and Authors became Users. A bookmark that 404s teaches the operator the
# feature was deleted.
for _old, _new in (("/home", "/"), ("/report", "/stats"),
                   ("/authors", "/users"), ("/userlist", "/users")):
    check(server86.LEGACY_REDIRECTS.get(_old) == _new,
          f"{_old} redirects to {_new} rather than serving or 404ing")
    check(_old not in server86.STATIC_ROUTES,
          f"{_old} is NOT also a static route — one page, one path")

# --- Users empty state: recent is the default radio ----------------------
import re as _re
_users_html = open(os.path.join(REPO_ROOT, "users.html")).read()
check(bool(_re.search(r'value="recent"\s+checked', _users_html)),
      "users.html offers recent as the default (checked) scan mode")


import shutil
import tempfile

# --- decision log: the only channel that can see a REJECT ----------------
# A blocked event never enters the database, so no scan can report one. If
# this reader silently returns nothing, the Result column quietly reads
# "unknown" forever and nobody notices the write policy stopped being visible.
_dec_dir = tempfile.mkdtemp()
_orig_dec, _orig_dec_prev = server86.DECISION_LOG_PATH, server86.DECISION_LOG_PREV_PATH
server86.DECISION_LOG_PATH = os.path.join(_dec_dir, "decisions.jsonl")
server86.DECISION_LOG_PREV_PATH = server86.DECISION_LOG_PATH + ".1"
try:
    _pk_ok = "aa" * 32
    _pk_bad = "bb" * 32
    with open(server86.DECISION_LOG_PREV_PATH, "w") as _fh:
        _fh.write(json.dumps({"at": 1, "id": "a" * 64, "pubkey": _pk_ok, "kind": 1,
                              "created_at": 1, "action": "accept", "msg": "",
                              "content": "hello"}) + "\n")
    with open(server86.DECISION_LOG_PATH, "w") as _fh:
        _fh.write(json.dumps({"at": 2, "id": "b" * 64, "pubkey": _pk_bad, "kind": 1,
                              "created_at": 2, "action": "reject",
                              "msg": "blocked: banned pubkey", "content": "spam"}) + "\n")
        _fh.write("this line is not json\n")

    _recs = server86.read_decisions()
    check(len(_recs) == 2,
          "read_decisions reads BOTH log segments and skips unparseable lines")
    check([r["action"] for r in _recs] == ["accept", "reject"],
          "read_decisions returns records oldest-first across the rotation boundary")

    _rows, _accepted, _blocked, _judged = server86._live_feed_state()
    check(_judged is True and _accepted == 1 and _blocked == 1,
          "the feed counts one accept and one block — a reject is visible nowhere else")
    _rejected = [r for r in _rows if r["result"] == "blocked"][0]
    check(_rejected["content"] == "spam" and _rejected["npub"].startswith("npub1"),
          "a blocked row still carries its content preview and an encoded npub")

    # The fallback: no decision log at all must NOT claim everything was
    # accepted. An event nothing judged is unknown, and rule 9 says unknown
    # renders as unknown.
    os.remove(server86.DECISION_LOG_PATH)
    os.remove(server86.DECISION_LOG_PREV_PATH)
    with server86._live_lock:
        server86._live_recent.append({"id": "c" * 64, "kind": 1, "created_at": 3})
    _rows, _accepted, _blocked, _judged = server86._live_feed_state()
    check(_judged is False and _accepted is None and _blocked is None,
          "with no decision log the feed reports nothing judged, rather than a made-up rate")
    check(all(r["result"] is None for r in _rows),
          "scan-fed rows render Result unknown — never 'accepted' for an event nothing judged")
    with server86._live_lock:
        server86._live_recent.clear()
finally:
    server86.DECISION_LOG_PATH, server86.DECISION_LOG_PREV_PATH = _orig_dec, _orig_dec_prev
    shutil.rmtree(_dec_dir, ignore_errors=True)


# --- strfry.conf: a field edit rewrites ONE line, and nothing else --------
# The whole reason Settings parses line-by-line instead of reserialising: an
# operator's comments, ordering and spacing must survive a visit to the page.
_CONF = """# my relay
db = "./strfry-db/"

relay {
    bind = "0.0.0.0"
    port = 7777          # HTTP port

    info {
        name = "My Relay"
    }

    writePolicy {
        plugin = "./plugin86.py"
    }
}
"""
_fields, _balanced = server86.parse_strfry_conf(_CONF)
_by_path = {f["path"]: f for f in _fields}
check(_balanced, "parse_strfry_conf reports balanced braces for a well-formed file")
check(_by_path["relay.info.name"]["value"] == "My Relay"
      and _by_path["relay.port"]["type"] == "int"
      and _by_path["db"]["section"] == "general",
      "parse_strfry_conf recovers dotted paths, values and types from nested blocks")

_new_text, _changed = server86.apply_strfry_conf_edits(_CONF, {"relay.port": "7778"})
check(_changed == ["relay.port"], "one edit reports exactly one changed path")
_before, _after = _CONF.split("\n"), _new_text.split("\n")
_diff = [i for i in range(len(_before)) if _before[i] != _after[i]]
check(len(_diff) == 1 and "7778" in _after[_diff[0]],
      "a field edit changes exactly ONE line of strfry.conf")
check("# HTTP port" in _after[_diff[0]] and "# my relay" in _new_text,
      "the edited line keeps its trailing comment, and the rest of the file keeps its own")

_, _unbalanced = server86.parse_strfry_conf("relay {\n  port = 1\n")
check(not _unbalanced, "parse_strfry_conf reports UNBALANCED braces rather than guessing")

_conf_dir = tempfile.mkdtemp()
_conf_file = os.path.join(_conf_dir, "strfry.conf")
_orig_cfg_for_conf = server86.CONFIG_PATH
# strfry.conf is resolved, not a constant: point config.json at a temp file
# so the write path exercises the same resolver the live server uses.
server86.CONFIG_PATH = os.path.join(_conf_dir, "config.json")
try:
    with open(_conf_file, "w") as _fh:
        _fh.write(_CONF)
    with open(server86.CONFIG_PATH, "w") as _fh:
        json.dump({"strfry_conf_path": _conf_file, "admin_pubkey_hex": "aa" * 32}, _fh)
    _ok, _err = server86.write_strfry_conf("relay {\n  port = 1\n")
    check(_ok is False and "brace" in (_err or ""),
          "write_strfry_conf REFUSES an unbalanced file")
    check(open(_conf_file).read() == _CONF,
          "a refused write leaves strfry.conf byte-identical — no partial write")
finally:
    server86.CONFIG_PATH = _orig_cfg_for_conf
    shutil.rmtree(_conf_dir, ignore_errors=True)


# --- config.json: a raw save can never lock the operator out -------------
_cfg_dir = tempfile.mkdtemp()
_orig_cfg_path = server86.CONFIG_PATH
server86.CONFIG_PATH = os.path.join(_cfg_dir, "config.json")
try:
    _good_cfg = {"admin_pubkey_hex": "cc" * 32, "relay_url": "wss://a.example",
                 "contact_appeal": "admin@a.example", "port": 8686, "bind": "0.0.0.0"}
    with open(server86.CONFIG_PATH, "w") as _fh:
        json.dump(_good_cfg, _fh)

    _ok, _err = server86.write_app_config(raw=json.dumps({"relay_url": "wss://b.example"}))
    check(_ok is False and "admin_pubkey_hex" in (_err or ""),
          "a raw config.json save without a valid admin_pubkey_hex is REFUSED")
    check(json.load(open(server86.CONFIG_PATH)) == _good_cfg,
          "the refused save left config.json untouched — the operator is still admin")

    _ok, _err = server86.write_app_config(edits={"relay_url": "wss://c.example",
                                                 "admin_pubkey_hex": "dd" * 32})
    _after_cfg = json.load(open(server86.CONFIG_PATH))
    check(_ok and _after_cfg["relay_url"] == "wss://c.example",
          "a field edit writes the field it names")
    check(_after_cfg["admin_pubkey_hex"] == "cc" * 32,
          "admin_pubkey_hex is NOT editable from Settings, even when the request asks")

    # Gift-wrap retention: default 30, clamp to [7, 30], filter is pure.
    check(server86.get_giftwrap_retention_days() == 30,
          "giftwrap retention defaults to 30 days when unset")
    _ok, _err = server86.write_app_config(edits={"giftwrap_retention_days": "3"})
    check(_ok and server86.get_giftwrap_retention_days() == 7,
          "giftwrap retention clamps below the floor up to 7 days")
    _ok, _err = server86.write_app_config(edits={"giftwrap_retention_days": "99"})
    check(_ok and server86.get_giftwrap_retention_days() == 30,
          "giftwrap retention clamps above the ceiling down to 30 days")
    _ok, _err = server86.write_app_config(edits={"giftwrap_retention_days": "14"})
    check(_ok and server86.get_giftwrap_retention_days() == 14,
          "giftwrap retention accepts a value inside the range")
    _filt = server86.giftwrap_retention_filter(now=1_700_000_000, days=14)
    check(_filt == {"kinds": [1059], "until": 1_700_000_000 - 14 * 86400},
          "giftwrap retention filter is kinds:[1059] until now−days")

    # Estimate: event count is exact; storage is proportional to db_bytes.
    _orig_count = server86.run_strfry_count
    _orig_report = server86.get_report_status
    _orig_db = server86._strfry_db_bytes
    server86.run_strfry_count = lambda filt, timeout=10: (
        100 if filt.get("until") else (1000 if filt == {} else 400)
    )
    server86.get_report_status = lambda: {
        "totals": {"total_events": 1000, "giftwrap_events": 400, "scanned_at": 1},
        "walk": {},
    }
    server86._strfry_db_bytes = lambda: (10_000_000, "/tmp/db")
    try:
        _est = server86.estimate_giftwrap_retention(14, now=1_700_000_000)
        check(_est["delete_events"] == 100 and _est["event_share"] == 0.1,
              "giftwrap retention estimate reports exact delete count and event share")
        check(_est["bytes_estimate"] == 1_000_000,
              "giftwrap retention storage estimate is delete/total × db_bytes")
        check(_est["days"] == 14 and "proportional" in (_est.get("estimate_note") or "").lower(),
              "giftwrap retention estimate labels storage as proportional, not measured")
    finally:
        server86.run_strfry_count = _orig_count
        server86.get_report_status = _orig_report
        server86._strfry_db_bytes = _orig_db
finally:
    server86.CONFIG_PATH = _orig_cfg_path
    shutil.rmtree(_cfg_dir, ignore_errors=True)


# strfry.conf: blank nips means strfry's default NIP list (upstream conf
# comment: "empty string to use default"). Settings must say so.
_nips_dir = tempfile.mkdtemp()
_orig_cfg_nips = server86.CONFIG_PATH
_nips_conf = os.path.join(_nips_dir, "strfry.conf")
with open(_nips_conf, "w") as _fh:
    _fh.write('relay {\n  info {\n    nips = ""\n  }\n}\n')
server86.CONFIG_PATH = os.path.join(_nips_dir, "config.json")
with open(server86.CONFIG_PATH, "w") as _fh:
    json.dump({"strfry_conf_path": _nips_conf, "admin_pubkey_hex": "aa" * 32}, _fh)
try:
    _payload = server86.get_settings_payload()
    _nips_fields = [f for f in _payload["relay"]["fields"] if f["path"] == "relay.info.nips"]
    check(len(_nips_fields) == 1 and "blank" in (_nips_fields[0].get("help") or "").lower(),
          "Settings labels blank nips as leave-blank-for-default")
    _ret = [f for f in _payload["app"]["fields"] if f["path"] == "giftwrap_retention_days"]
    check(len(_ret) == 1 and _ret[0]["type"] == "range"
          and _ret[0]["min"] == 7 and _ret[0]["max"] == 30,
          "Settings exposes giftwrap retention as a 7–30 day range control")
finally:
    server86.CONFIG_PATH = _orig_cfg_nips
    shutil.rmtree(_nips_dir, ignore_errors=True)


# --- Phase 3: POST /api/reason (server86.py + lib86/blacklist.py) --------
# blacklist.py's BASE_DIR is derived from the FILE's own path, not cwd, so
# it always resolves to the real repo's blacklist.json unless redirected —
# every check here does that first, so this test run can never touch the
# repo's own blacklist.json.

server86.blacklist.BLACKLIST_PATH = os.path.join(_cache_tmpdir, "blacklist.json")
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None

# validate_reason_request bounds
pubkeys, reason, mode, err = server86.validate_reason_request(
    {"pubkeys": ["a" * 64], "reason": "spam", "mode": "replace"})
check(pubkeys == ["a" * 64] and reason == "spam" and mode == "replace" and err is None,
      "validate_reason_request accepts a well-formed replace request")

_, _, _, err = server86.validate_reason_request({"pubkeys": ["not-hex"], "reason": "x", "mode": "replace"})
check(err is not None, "validate_reason_request rejects a malformed pubkey")

_, _, _, err = server86.validate_reason_request({"pubkeys": ["a" * 64], "reason": "x" * (server86.REASON_MAX_LEN + 1), "mode": "replace"})
check(err is not None, "validate_reason_request rejects a reason longer than REASON_MAX_LEN")

_, _, _, err = server86.validate_reason_request({"pubkeys": ["a" * 64], "reason": "x", "mode": "delete"})
check(err is not None, "validate_reason_request rejects a mode other than replace/append")

_, _, _, err = server86.validate_reason_request({"pubkeys": ["a" * 64], "reason": "", "mode": "replace"})
check(err is None, "validate_reason_request accepts an empty reason (clearing is valid)")

# blacklist.set_reasons: skip-not-banned, replace, append-onto-empty,
# append-onto-existing, and edits reason and nothing else
pk_untouched = "1" * 64
pk_empty = "2" * 64
pk_existing = "3" * 64
seed = {
    pk_empty: {"banned_at": 100, "report_event_id": None, "reason": "",
               "report_type": "manual", "name": None, "nip05": None, "name_checked_at": None},
    pk_existing: {"banned_at": 200, "report_event_id": "deadbeef", "reason": "first reason",
                  "report_type": "spam", "name": "alice", "nip05": None, "name_checked_at": None},
}
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump(seed, f)
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None

not_banned = "4" * 64
updated, skipped = server86.blacklist.set_reasons([not_banned, pk_empty, pk_existing], "spam report", "append", now=1)
check(skipped == [not_banned], "set_reasons SKIPS a pubkey that isn't currently banned, never creates one")
by_pk = {u["pubkey"]: u for u in updated}
check(by_pk[pk_empty]["old_reason"] == "" and by_pk[pk_empty]["new_reason"] == "spam report",
      "set_reasons append onto an EMPTY existing reason behaves as replace (no ' — ' joiner)")
check(by_pk[pk_existing]["old_reason"] == "first reason" and by_pk[pk_existing]["new_reason"] == "first reason — spam report",
      "set_reasons append onto a non-empty reason joins with ' — '")

on_disk = json.load(open(server86.blacklist.BLACKLIST_PATH))
check(on_disk[pk_existing]["reason"] == "first reason — spam report", "set_reasons persists the new reason to disk")
check(on_disk[pk_existing]["report_type"] == "spam" and on_disk[pk_existing]["report_event_id"] == "deadbeef"
      and on_disk[pk_existing]["banned_at"] == 200 and on_disk[pk_existing]["name"] == "alice",
      "set_reasons edits `reason` and NOTHING else — report_type/report_event_id/banned_at/name untouched")

updated2, _ = server86.blacklist.set_reasons([pk_existing], "overwritten", "replace", now=2)
check(updated2[0]["old_reason"] == "first reason — spam report" and updated2[0]["new_reason"] == "overwritten",
      "set_reasons replace mode overwrites regardless of what the existing reason was")

# reload-fresh-before-write: an external write between load and call must
# be what 'append' joins against, never a value cached before the call
server86.blacklist.load()  # populate the in-memory cache with "overwritten"
external = json.load(open(server86.blacklist.BLACKLIST_PATH))
external[pk_existing]["reason"] = "changed out from under the cache"
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump(external, f)
updated3, _ = server86.blacklist.set_reasons([pk_existing], "appended after race", "append", now=3)
check(updated3[0]["old_reason"] == "changed out from under the cache",
      "set_reasons reloads blacklist.json from disk before writing, so append joins the CURRENT reason, not a stale cache")

# --- Grok phase 1: lowercase keys + corrupt-safe reload + is_banned case --
# A mixed-case ban must enforce against the lowercase event pubkey. On-disk
# keys are always lowercase. Corrupt JSON must not wipe a good in-memory map.
_upper_pk = "AB" * 32
_lower_pk = _upper_pk.lower()
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({}, f)
ok_add = server86.blacklist.add(
    _upper_pk, banned_at=1, report_event_id=None, reason="case",
    report_type="manual", admin_pubkey_hex="00" * 32,
)
check(ok_add is True, "blacklist.add accepts mixed-case hex pubkey")
on_disk_case = json.load(open(server86.blacklist.BLACKLIST_PATH))
check(_lower_pk in on_disk_case and _upper_pk not in on_disk_case,
      "blacklist.add stores the key as lowercase only")
check(server86.blacklist.is_banned(_upper_pk) is True,
      "is_banned matches mixed-case lookup against a lowercase key")
check(server86.blacklist.is_banned(_lower_pk) is True,
      "is_banned matches lowercase lookup")
# Legacy uppercase key on disk is normalized on read into lowercase.
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({_upper_pk: {"banned_at": 2, "report_event_id": None, "reason": "legacy",
                           "report_type": "manual", "name": None, "nip05": None,
                           "name_checked_at": None}}, f)
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None
check(server86.blacklist.is_banned(_lower_pk) is True,
      "is_banned enforces a legacy uppercase key after normalize-on-read")
# Corrupt file: keep last good cache, do not fail open to empty.
server86.blacklist.load()
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    f.write("{not valid json")
server86.blacklist._cache_mtime = None  # force re-stat path
server86.blacklist._last_checked = None
server86.blacklist._refresh(force=True)
check(server86.blacklist.is_banned(_lower_pk) is True,
      "corrupt blacklist.json does not replace a good in-memory cache with {}")
check(server86.as_hex64(_upper_pk) == _lower_pk,
      "as_hex64 lowercases valid mixed-case hex")
check(server86.as_hex64("not-a-key") is None,
      "as_hex64 returns None for non-hex")
# Clean slate for subsequent tests that assume an empty/seeded banlist.
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({}, f)
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None


# --- Phase 4: POST /api/profile (server86.py) -----------------------------

# run_strfry_count: parses --count's numeric stdout, raises rather than
# parsing loosely on non-zero exit or non-numeric output
import subprocess as _subprocess


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


_orig_subprocess_run = _subprocess.run
_orig_require_strfry_bin = server86.require_strfry_bin
_orig_require_strfry_conf = server86.require_strfry_conf
_orig_get_relay_cwd = server86.get_relay_cwd
server86.require_strfry_bin = lambda: "/fake/strfry"
server86.require_strfry_conf = lambda: "/fake/strfry.conf"
server86.get_relay_cwd = lambda: "/tmp"

_subprocess.run = lambda argv, **kw: _FakeCompleted(0, b"42\n")
check(server86.run_strfry_count({"authors": ["a" * 64]}) == 42,
      "run_strfry_count parses --count's numeric stdout")

_subprocess.run = lambda argv, **kw: _FakeCompleted(0, b"not-a-number\n")
try:
    server86.run_strfry_count({"authors": ["a" * 64]})
    check(False, "run_strfry_count raises on non-numeric --count output")
except RuntimeError:
    check(True, "run_strfry_count raises on non-numeric --count output")

_subprocess.run = lambda argv, **kw: _FakeCompleted(1, b"")
try:
    server86.run_strfry_count({"authors": ["a" * 64]})
    check(False, "run_strfry_count raises on non-zero exit")
except RuntimeError:
    check(True, "run_strfry_count raises on non-zero exit")

_subprocess.run = _orig_subprocess_run
server86.require_strfry_bin = _orig_require_strfry_bin
server86.require_strfry_conf = _orig_require_strfry_conf
server86.get_relay_cwd = _orig_get_relay_cwd

# _report_type_for_target: same dual lookup as plugin86.py's hot path,
# scoped to the ONE p-tag naming the target pubkey in a multi-target report
target = "e" * 64
other = "f" * 64
check(server86._report_type_for_target([["p", target, "spam"]], target) == "spam",
      "_report_type_for_target reads the target's own p-tag third element")
check(server86._report_type_for_target([["e", "someid", "malware"], ["p", target]], target) == "malware",
      "_report_type_for_target falls back to the e-tag's third element when the p-tag has none")
check(server86._report_type_for_target([["p", other, "spam"], ["p", target]], target) is None,
      "_report_type_for_target never returns another p-tag's type for a DIFFERENT target pubkey")
check(server86._report_type_for_target([["p", target]], target) is None,
      "_report_type_for_target returns None when neither the p-tag nor any e/a tag carries a type")
check(server86._report_type_for_target(None, target) is None,
      "_report_type_for_target tolerates malformed (non-list) tags")

# build_profile_response: assembles ban status / name / scan-rank from
# in-memory state, with NO scan of its own — compute_profile is mocked out
# so this only exercises the assembly logic. blacklist.BLACKLIST_PATH is
# still redirected to _cache_tmpdir from the /api/reason tests above.
profile_target = "5" * 64
banned_target = "6" * 64
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({
        banned_target: {"banned_at": 500, "report_event_id": None, "reason": "spam",
                        "report_type": "manual", "name": None, "nip05": None, "name_checked_at": None},
    }, f)
# build_profile_response calls blacklist.load(), a non-forced refresh —
# unlike set_reasons() it does NOT bypass the once-per-second mtime-check
# throttle, so _last_checked must be cleared too or this reload is
# silently skipped this soon after the set_reasons() calls above.
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None

_orig_compute_profile = server86.compute_profile
_orig_resolve_profiles = server86.resolve_profiles
server86.compute_profile = lambda pk: {"total_events": 7, "kinds": {}, "kinds_window": 500,
                                        "kinds_saturated": False, "profile": None, "previews": [],
                                        "reports": [], "reports_saturated": False,
                                        "relay_list": None, "warning": None}
server86.resolve_profiles = lambda pks: {pks[0]: {"name": "alice", "nip05": "alice@example.com"}}

with server86._scan_lock:
    server86._authors_cache = dict(server86._authors_cache)
    server86._authors_cache["authors"] = [
        {"pubkey": "9" * 64, "count": 100},
        {"pubkey": profile_target, "count": 42},
    ]

resp = server86.build_profile_response(profile_target)
check(resp["pubkey"] == profile_target and resp["npub"] == server86.bech32.npub_encode(profile_target),
      "build_profile_response includes the requested pubkey and its npub")
check(resp["name"] == "alice" and resp["nip05"] == "alice@example.com",
      "build_profile_response resolves name/nip05 via resolve_profiles()")
check(resp["banned"] is False and resp["ban"] is None,
      "build_profile_response reports banned:false / ban:null for a non-banned pubkey")
check(resp["scan_rank"] == 2 and resp["scan_count"] == 42,
      "build_profile_response finds this pubkey's 1-indexed rank and count in the author-scan cache")
check(resp["total_events"] == 7, "build_profile_response merges compute_profile()'s scan fields into the response")
check(resp.get("relay_list") is None,
      "build_profile_response passes through relay_list from compute_profile (null when none)")

_parsed_relays = server86._parse_relay_list_event({
    "id": "ab" * 32,
    "created_at": 1700000000,
    "tags": [
        ["r", "wss://relay.example.com"],
        ["r", "wss://read.example.com", "read"],
        ["r", "wss://write.example.com", "write"],
        ["r", "wss://relay.example.com"],  # dedupe
        ["p", "not-a-relay"],
        ["r", ""],
    ],
})
check(_parsed_relays is not None and len(_parsed_relays["relays"]) == 3,
      "parse_relay_list_event keeps three distinct r-tag URLs and drops empties/dupes")
check(_parsed_relays["relays"][0] == {"url": "wss://relay.example.com"}
      and _parsed_relays["relays"][1] == {"url": "wss://read.example.com", "marker": "read"}
      and _parsed_relays["relays"][2] == {"url": "wss://write.example.com", "marker": "write"},
      "parse_relay_list_event preserves NIP-65 read/write markers")
check(server86._parse_relay_list_event({"tags": [["p", "x"]]}) is None,
      "parse_relay_list_event returns None when no r tags exist")

resp2 = server86.build_profile_response(banned_target)
check(resp2["banned"] is True and resp2["ban"]["reason"] == "spam" and resp2["ban"]["report_type"] == "manual",
      "build_profile_response includes the full blacklist entry when banned")
check(resp2["scan_rank"] is None and resp2["scan_count"] is None,
      "build_profile_response reports scan_rank/scan_count null for a pubkey absent from the author-scan cache")

server86.compute_profile = _orig_compute_profile
server86.resolve_profiles = _orig_resolve_profiles

# compute_profile: the reports-against list gets name/nip05 attached via a
# single batched LOCAL resolve_profiles() call over the DISTINCT reporters
# — never once per report row — and independent of the report scan itself,
# so a resolve_profiles failure loses only the names, never the reports.
reporter1 = "1" * 64
reporter2 = "2" * 64
profile_subject = "3" * 64

def _fake_scan_for_profile(filt, timeout=None):
    if filt.get("kinds") == [1984]:
        return [
            {"pubkey": reporter1, "kind": 1984, "content": "spam", "created_at": 10,
             "tags": [["p", profile_subject, "spam"]]},
            {"pubkey": reporter2, "kind": 1984, "content": "", "created_at": 20,
             "tags": [["p", profile_subject]]},
            {"pubkey": reporter1, "kind": 1984, "content": "again", "created_at": 30,
             "tags": [["p", profile_subject]]},
        ]
    return []

_orig_run_strfry_count = server86.run_strfry_count
_orig_run_strfry_scan = server86.run_strfry_scan
_orig_resolve_profiles = server86.resolve_profiles
server86.run_strfry_count = lambda *a, **kw: 0
server86.run_strfry_scan = _fake_scan_for_profile

resolve_calls = []
def _fake_resolve(pks):
    resolve_calls.append(list(pks))
    return {reporter1: {"name": "Alice", "nip05": "alice@example.com"}}
server86.resolve_profiles = _fake_resolve

result = server86.compute_profile(profile_subject)
by_reporter = {}
for r in result["reports"]:
    by_reporter.setdefault(r["reporter"], []).append(r)
check(len(resolve_calls) == 1 and sorted(resolve_calls[0]) == sorted([reporter1, reporter2]),
      "compute_profile resolves reporters' name/nip05 with ONE batched call over the DISTINCT reporter set")
check(all(r["name"] == "Alice" and r["nip05"] == "alice@example.com" for r in by_reporter[reporter1]),
      "compute_profile attaches a resolved reporter's name/nip05 to EVERY one of their report rows")
check(by_reporter[reporter2][0]["name"] is None and by_reporter[reporter2][0]["nip05"] is None,
      "compute_profile leaves name/nip05 null for a reporter resolve_profiles has nothing for")

def _raising_resolve(pks):
    raise RuntimeError("boom")
server86.resolve_profiles = _raising_resolve
result2 = server86.compute_profile(profile_subject)
check(len(result2["reports"]) == 3 and all(r["name"] is None and r["nip05"] is None for r in result2["reports"]),
      "compute_profile keeps the reports list intact (name/nip05 null) when resolve_profiles itself raises")

server86.run_strfry_count = _orig_run_strfry_count
server86.run_strfry_scan = _orig_run_strfry_scan
server86.resolve_profiles = _orig_resolve_profiles

# validate_profile_day_request: the client sends a calendar date, never a
# raw time window — [since, until) is computed HERE, server-side, as one
# UTC day, so the client can never hand this endpoint an arbitrary span.
day_pubkey = "7" * 64
pk, since, until, err = server86.validate_profile_day_request({"pubkey": day_pubkey, "date": "1970-01-02"})
check(pk == day_pubkey and since == 86400 and until == 86400 + 86400 and err is None,
      "validate_profile_day_request turns a calendar date into an exact [since, until) UTC-day pair")

_, _, _, err = server86.validate_profile_day_request({"pubkey": "not-hex", "date": "1970-01-02"})
check(err is not None, "validate_profile_day_request rejects a malformed pubkey")

_, _, _, err = server86.validate_profile_day_request({"pubkey": day_pubkey, "date": "01/02/1970"})
check(err is not None, "validate_profile_day_request rejects a date not in YYYY-MM-DD form")

_, _, _, err = server86.validate_profile_day_request({"pubkey": day_pubkey, "date": "2024-13-40"})
check(err is not None, "validate_profile_day_request rejects a calendar date that doesn't exist")

# compute_profile_day: same PROFILE_DAY_EVENTS_MAX bound and newest-first
# sort as compute_profile's own previews, plus an explicit `truncated` flag
# — a full day of events must never look like a complete one.
_orig_run_strfry_scan_day = server86.run_strfry_scan
scan_calls = []

def _fake_day_scan(filt, timeout=None):
    scan_calls.append(filt)
    return [
        {"kind": 1, "created_at": 100, "content": "first"},
        {"kind": 1, "created_at": 300, "content": "third"},
        {"kind": 7, "created_at": 200, "content": "x" * 400},
    ]

server86.run_strfry_scan = _fake_day_scan
day_result = server86.compute_profile_day(day_pubkey, 86400, 172800)
check(len(scan_calls) == 1
      and scan_calls[0]["authors"] == [day_pubkey]
      and scan_calls[0]["since"] == 86400 and scan_calls[0]["until"] == 172800
      and scan_calls[0]["limit"] == server86.PROFILE_DAY_EVENTS_MAX,
      "compute_profile_day scans with the exact [since, until) window and the PROFILE_DAY_EVENTS_MAX limit")
check([ev["created_at"] for ev in day_result["previews"]] == [300, 200, 100],
      "compute_profile_day sorts previews newest-first regardless of strfry's own return order")
check(len(day_result["previews"][1]["content"]) == 280,
      "compute_profile_day truncates preview content to 280 chars, same as compute_profile's previews")
check(day_result["truncated"] is False, "compute_profile_day: truncated is False when under PROFILE_DAY_EVENTS_MAX")

server86.run_strfry_scan = lambda filt, timeout=None: [
    {"kind": 1, "created_at": i, "content": ""} for i in range(server86.PROFILE_DAY_EVENTS_MAX)
]
day_result_full = server86.compute_profile_day(day_pubkey, 86400, 172800)
check(day_result_full["truncated"] is True,
      "compute_profile_day: truncated is True when the scan returns exactly PROFILE_DAY_EVENTS_MAX events")

server86.run_strfry_scan = _orig_run_strfry_scan_day

# validate_profile_new_request / compute_profile_new_events: the cheap
# alternative to a full compute_profile() recompute for the profile page's
# '>' button at the latest day — reads only events newer than a
# client-supplied `since`, and reports whether it read ALL of them
# (truncated: False) or hit PROFILE_EVENT_LIMIT (truncated: True), so the
# client knows whether an exact counter delta is safe to apply.
pk, since, err = server86.validate_profile_new_request({"pubkey": day_pubkey, "since": 500})
check(pk == day_pubkey and since == 500 and err is None,
      "validate_profile_new_request accepts a pubkey and a non-negative integer since")

_, _, err = server86.validate_profile_new_request({"pubkey": "not-hex", "since": 500})
check(err is not None, "validate_profile_new_request rejects a malformed pubkey")

_, _, err = server86.validate_profile_new_request({"pubkey": day_pubkey, "since": -1})
check(err is not None, "validate_profile_new_request rejects a negative since")

_, _, err = server86.validate_profile_new_request({"pubkey": day_pubkey, "since": "500"})
check(err is not None, "validate_profile_new_request rejects a since that isn't an int (no silent str->int coercion)")

_, _, err = server86.validate_profile_new_request({"pubkey": day_pubkey})
check(err is not None, "validate_profile_new_request rejects a missing since")

_orig_run_strfry_scan_new = server86.run_strfry_scan
new_scan_calls = []


def _fake_new_scan(filt, timeout=None):
    new_scan_calls.append(filt)
    return [
        {"kind": 1, "created_at": 500, "content": "a"},
        {"kind": 1, "created_at": 600, "content": "b"},
        {"kind": 7, "created_at": 550, "content": "+"},
    ]


server86.run_strfry_scan = _fake_new_scan
new_result = server86.compute_profile_new_events(day_pubkey, 500)
check(len(new_scan_calls) == 1
      and new_scan_calls[0]["authors"] == [day_pubkey]
      and new_scan_calls[0]["since"] == 500
      and "until" not in new_scan_calls[0]
      and new_scan_calls[0]["limit"] == server86.PROFILE_EVENT_LIMIT,
      "compute_profile_new_events scans with since (no until) and the PROFILE_EVENT_LIMIT bound")
check([ev["created_at"] for ev in new_result["previews"]] == [600, 550, 500],
      "compute_profile_new_events sorts previews newest-first regardless of strfry's own return order")
check(new_result["kinds_delta"] == {1: 2, 7: 1},
      "compute_profile_new_events tallies kinds_delta from exactly the events it read")
check(new_result["truncated"] is False,
      "compute_profile_new_events: truncated is False when under PROFILE_EVENT_LIMIT")

server86.run_strfry_scan = lambda filt, timeout=None: [
    {"kind": 1, "created_at": i, "content": ""} for i in range(server86.PROFILE_EVENT_LIMIT)
]
new_result_full = server86.compute_profile_new_events(day_pubkey, 500)
check(new_result_full["truncated"] is True,
      "compute_profile_new_events: truncated is True when the scan returns exactly PROFILE_EVENT_LIMIT events — "
      "the caller must not trust kinds_delta/len(previews) as an exact count in this case")

server86.run_strfry_scan = _orig_run_strfry_scan_new

# _build_event_preview: reply detection (kind-1-only) and note1 id encoding
# — shared by compute_profile and compute_profile_day so both previews
# lists carry the same fields the profile page's events list renders.
note_pubkey = "d" * 64
note_id = "e" * 64
preview_reply = server86._build_event_preview(
    {"kind": 1, "created_at": 1, "content": "hi", "id": note_id, "tags": [["e", "f" * 64]]})
check(preview_reply["reply"] is True, "_build_event_preview: a kind-1 event with an `e` tag is a reply")

preview_note = server86._build_event_preview(
    {"kind": 1, "created_at": 1, "content": "hi", "id": note_id, "tags": [["p", note_pubkey]]})
check(preview_note["reply"] is False, "_build_event_preview: a kind-1 event with no `e` tag is not a reply")

preview_other_kind = server86._build_event_preview(
    {"kind": 7, "created_at": 1, "content": "+", "id": note_id, "tags": [["e", "f" * 64]]})
check(preview_other_kind["reply"] is None,
      "_build_event_preview: reply is null for any kind other than 1 — 'reply vs note' doesn't apply")

check(preview_reply["note"] == server86.bech32.note_encode(note_id),
      "_build_event_preview: note is the event id encoded as note1... via bech32.note_encode")
preview_bad_id = server86._build_event_preview({"kind": 1, "created_at": 1, "content": "", "id": "not-hex", "tags": []})
check(preview_bad_id["note"] is None,
      "_build_event_preview: note is null when the event's id is missing or malformed")

_note_hrp, _note_data = server86.bech32.bech32_decode(server86.bech32.note_encode(note_id))
_note_roundtrip = server86.bech32.convertbits(_note_data, 5, 8, False)
check(_note_hrp == "note" and bytes(_note_roundtrip).hex() == note_id,
      "note_encode: produces a valid bech32 string with hrp 'note' that round-trips back to the same event id")


# --- Phase 5: POST /api/pubkeys/lookup (server86.py) ----------------------

# validate_pubkeys_lookup_request bounds
pubkeys, domain, err = server86.validate_pubkeys_lookup_request(
    {"pubkeys": ["a" * 64, "b" * 64], "domain": "example.com"})
check(pubkeys == ["a" * 64, "b" * 64] and domain == "example.com" and err is None,
      "validate_pubkeys_lookup_request accepts a well-formed request")

_, _, err = server86.validate_pubkeys_lookup_request({"pubkeys": [], "domain": "example.com"})
check(err is not None, "validate_pubkeys_lookup_request rejects an empty pubkeys list")

_, _, err = server86.validate_pubkeys_lookup_request({"pubkeys": ["not-hex"], "domain": "example.com"})
check(err is not None, "validate_pubkeys_lookup_request rejects a malformed pubkey rather than skipping it")

_, _, err = server86.validate_pubkeys_lookup_request(
    {"pubkeys": ["a" * 64] * (server86.DOMAIN_LOOKUP_MAX + 1), "domain": "example.com"})
check(err is not None, "validate_pubkeys_lookup_request rejects a body over DOMAIN_LOOKUP_MAX outright, never truncates")

pubkeys_at_max, _, err = server86.validate_pubkeys_lookup_request(
    {"pubkeys": ["a" * 64] * server86.DOMAIN_LOOKUP_MAX, "domain": "example.com"})
check(err is None and len(pubkeys_at_max) == server86.DOMAIN_LOOKUP_MAX,
      "validate_pubkeys_lookup_request accepts a body at exactly DOMAIN_LOOKUP_MAX")

_, _, err = server86.validate_pubkeys_lookup_request({"pubkeys": ["a" * 64], "domain": ""})
check(err is not None, "validate_pubkeys_lookup_request rejects an empty domain")

# compute_pubkeys_lookup: claims_domain cross-check, ban status, scan_count
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({}, f)
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None

pk_claims = "7" * 64
pk_stale = "8" * 64
pk_no_nip05 = "9" * 64

_orig_resolve_profiles2 = server86.resolve_profiles
server86.resolve_profiles = lambda pks: {
    pk_claims: {"name": "alice", "nip05": "alice@example.com"},
    pk_stale: {"name": "bob", "nip05": "bob@other-domain.com"},
    pk_no_nip05: {"name": "carol", "nip05": None},
}

with server86._scan_lock:
    server86._authors_cache = dict(server86._authors_cache)
    server86._authors_cache["authors"] = [{"pubkey": pk_claims, "count": 17}]

result = server86.compute_pubkeys_lookup([pk_claims, pk_stale, pk_no_nip05], "example.com")
by_pk = {r["pubkey"]: r for r in result["results"]}
check(result["domain"] == "example.com", "compute_pubkeys_lookup echoes the domain verbatim")
check(by_pk[pk_claims]["claims_domain"] is True,
      "compute_pubkeys_lookup sets claims_domain True when the kind-0 nip05 ends in @<domain>")
check(by_pk[pk_stale]["claims_domain"] is False,
      "compute_pubkeys_lookup sets claims_domain False for a nip05 claiming a DIFFERENT domain (stale roster entry)")
check(by_pk[pk_no_nip05]["claims_domain"] is False,
      "compute_pubkeys_lookup sets claims_domain False when there is no nip05 at all")
check(by_pk[pk_claims]["scan_count"] == 17,
      "compute_pubkeys_lookup reports scan_count from the author-scan cache when the pubkey appears there")
check(by_pk[pk_stale]["scan_count"] is None,
      "compute_pubkeys_lookup reports scan_count null for a pubkey absent from the author-scan cache")
check(all(r["banned"] is False for r in result["results"]),
      "compute_pubkeys_lookup reports banned:false for pubkeys with no blacklist entry")

# a pubkey banned WITH a stored name should win over local resolution,
# exactly as resolve_profiles() already guarantees elsewhere
pk_banned = "a" * 63 + "1"
with open(server86.blacklist.BLACKLIST_PATH, "w") as f:
    json.dump({pk_banned: {"banned_at": 1, "report_event_id": None, "reason": "spam",
                           "report_type": "manual", "name": "eve", "nip05": "eve@example.com",
                           "name_checked_at": 1}}, f)
server86.blacklist._cache = {}
server86.blacklist._cache_mtime = None
server86.blacklist._last_checked = None
server86.resolve_profiles = _orig_resolve_profiles2  # use the real one now — it reads the blacklist directly

result2 = server86.compute_pubkeys_lookup([pk_banned], "example.com")
row = result2["results"][0]
check(row["banned"] is True and row["ban_reason"] == "spam",
      "compute_pubkeys_lookup reports banned:true and ban_reason for a banned pubkey")
check(row["name"] == "eve" and row["claims_domain"] is True,
      "compute_pubkeys_lookup resolves name/nip05 for a banned pubkey from blacklist.json, not a fresh scan")

server86.resolve_profiles = _orig_resolve_profiles2


# --- report endpoints (server86.py: /api/report/totals, /api/report/walk) -
# Cache path redirected into the same tempdir Phase 2 uses, for the same
# reason: a test run must never write a *-cache.json into the repo.

server86.REPORT_CACHE_PATH = os.path.join(_cache_tmpdir, "report-cache.json")
server86._report_cache = server86._load_report_cache()

report_status = server86.get_report_status()
check(report_status["status"] == "idle" and report_status["job"] is None
      and report_status["totals"] is None and report_status["walk"] is None,
      "GET /api/report is the never-scanned skeleton before any report scan has run, and never scans to produce it")

# gap_events / gap_share: three counts, no fourth call — the fourth figure
# is pure arithmetic on the other three.
_orig_run_strfry_count = server86.run_strfry_count
_totals_canned = [2628121, 1702655, 909056]  # total, giftwrap, allowlist — reference-relay-shaped
_totals_calls = {"n": 0}


def _fake_run_strfry_count(filter_obj, timeout=None):
    v = _totals_canned[_totals_calls["n"]]
    _totals_calls["n"] += 1
    return v


server86.run_strfry_count = _fake_run_strfry_count
totals_record = server86.compute_report_totals()
server86.run_strfry_count = _orig_run_strfry_count

check(_totals_calls["n"] == 3, "compute_report_totals makes exactly three scan --count calls, never a fourth")
expected_gap = 2628121 - 909056 - 1702655
check(totals_record["gap_events"] == expected_gap,
      "compute_report_totals: gap_events = total_events - allowlist_events - giftwrap_events")
check(abs(totals_record["gap_share"] - expected_gap / (2628121 - 1702655)) < 1e-9,
      "compute_report_totals: gap_share is gap_events / non-giftwrap total, not gap_events / total_events")

# compute_report_walk: the one unlimited scan. Two non-giftwrap authors
# sharing a REPORT_AUTHOR_KEY_BYTES-byte pubkey PREFIX must collapse to one
# distinct author (that's the memory-bounding trade this endpoint makes);
# gift-wrap authors must never enter that set at all, since their count is
# already known exactly from the tally and storing them would defeat the
# whole point of skipping kind 1059.
shared_prefix = "ab" * server86.REPORT_AUTHOR_KEY_BYTES
pk_a = shared_prefix + "11" * (32 - server86.REPORT_AUTHOR_KEY_BYTES)
pk_b = shared_prefix + "22" * (32 - server86.REPORT_AUTHOR_KEY_BYTES)


def _streaming_walk(filter_obj, on_event, timeout, on_progress=None):
    check(filter_obj == {}, "compute_report_walk streams the genuinely unbounded filter '{}' — the one sanctioned exception to the bounding rule")
    # NIP-40 expiration tags exercise the walk's expired-event tally: a
    # past timestamp on a plain event counts; a past timestamp on a GIFT
    # WRAP also counts (the check runs before the kind-1059 early return);
    # a future timestamp and a malformed value do not.
    on_event({"kind": 1, "pubkey": pk_a, "tags": [["expiration", "1"]]})
    on_event({"kind": 1, "pubkey": pk_b, "tags": [["expiration", "9999999999"]]})
    on_event({"kind": 1059, "pubkey": "c" * 64, "tags": [["expiration", "1"]]})
    on_event({"kind": 1059, "pubkey": "d" * 64})
    on_event({"kind": 99999, "pubkey": pk_a, "tags": [["expiration", "soon"]]})
    on_event({"kind": 20000, "pubkey": pk_a})  # NIP-16 ephemeral leftover
    on_event({"kind": 22242, "pubkey": pk_b})
    return 7


server86.run_strfry_count = lambda filter_obj, timeout=None: 5
server86.run_strfry_scan_streaming = _streaming_walk
walk_result = server86.compute_report_walk()
server86.run_strfry_scan_streaming = _orig_streaming
server86.run_strfry_count = _orig_run_strfry_count

check(walk_result["walk"]["distinct_authors_nongiftwrap"] == 1,
      "compute_report_walk: two authors sharing a REPORT_AUTHOR_KEY_BYTES-byte prefix collapse to ONE distinct author")
check(walk_result["walk"]["distinct_authors_giftwrap"] == 2,
      "compute_report_walk: gift-wrap distinct-author count is the gift-wrap EVENT count (structural, one key per message), never a stored set")
check(walk_result["walk"]["distinct_authors"] == 3,
      "compute_report_walk: distinct_authors sums the measured non-giftwrap set and the structural giftwrap count")
check(99999 in walk_result["walk"]["unlisted_kinds"] and walk_result["walk"]["unlisted_kinds"][99999] == 1,
      "compute_report_walk: unlisted_kinds reports a kind that is neither in AUTHOR_SCAN_KINDS nor 1059")
check(1059 not in walk_result["walk"]["unlisted_kinds"] and 1 not in walk_result["walk"]["unlisted_kinds"],
      "compute_report_walk: unlisted_kinds excludes gift wraps and allowlisted kinds")
# 99999 + two NIP-16 ephemeral kinds (20000, 22242) sit outside the allowlist.
check(walk_result["walk"]["unlisted_total"] == 3 and walk_result["walk"]["unlisted_kind_count"] == 3,
      "compute_report_walk: unlisted_total/unlisted_kind_count summarise unlisted_kinds so the page needs no client-side arithmetic")
check(walk_result["walk"]["expired_events"] == 2,
      "compute_report_walk: expired_events counts NIP-40 past-expiration events (incl. an expired gift wrap), skipping future and malformed expirations")
check(walk_result["walk"]["ephemeral_events"] == 2,
      "compute_report_walk: ephemeral_events counts kinds 20000–29999 still on disk")

# --- ephemeral kinds filter + estimate --------------------------------------
_eph_filt = server86.ephemeral_kinds_filter()
check(_eph_filt["kinds"][0] == 20000 and _eph_filt["kinds"][-1] == 29999
      and len(_eph_filt["kinds"]) == 10000,
      "ephemeral_kinds_filter is the full NIP-16 range 20000–29999 (no kind-range in NIP-01)")
check(server86.is_ephemeral_kind(20000) and server86.is_ephemeral_kind(29999)
      and not server86.is_ephemeral_kind(19999) and not server86.is_ephemeral_kind(30000),
      "is_ephemeral_kind bounds the NIP-16 range inclusively")
_orig_count_eph = server86.run_strfry_count
_orig_db_eph = server86._strfry_db_bytes
_eph_calls = []
def _fake_eph_count(filter_obj, timeout=None):
    _eph_calls.append(filter_obj)
    if filter_obj == {}:
        return 1000
    return 42
server86.run_strfry_count = _fake_eph_count
server86._strfry_db_bytes = lambda: (10 * 1024 * 1024, None)
_eph_est = server86.estimate_ephemeral_storage()
server86.run_strfry_count = _orig_count_eph
server86._strfry_db_bytes = _orig_db_eph
check(_eph_est["ephemeral_events"] == 42 and _eph_est["total_events"] == 1000,
      "estimate_ephemeral_storage counts kinds 20000–29999 and total events")
check(abs(_eph_est["event_share"] - 0.042) < 1e-9
      and _eph_est["bytes_estimate"] == int(10 * 1024 * 1024 * 0.042),
      "estimate_ephemeral_storage proportional bytes use count/total × db size")

# --- event_expiration (NIP-40) pure-function edges --------------------------
check(server86.event_expiration([["expiration", "1700000000"]]) == 1700000000,
      "event_expiration: reads the unix timestamp from a well-formed expiration tag")
check(server86.event_expiration([["p", "abc"], ["expiration", "42"]]) == 42,
      "event_expiration: finds the expiration tag among others")
check(server86.event_expiration([["expiration", "not-a-number"]]) is None,
      "event_expiration: a non-integer value is treated as absent, not an error")
check(server86.event_expiration([["e", "abc"]]) is None,
      "event_expiration: no expiration tag returns None")

# --- the gap ladder: this is the assertion that would have caught the
# permanent false alarm before it shipped (CLAUDE.md Part 0 #1 / Part 3).
# Reference-relay-shaped numbers: gap 2.008% (one tick over
# GAP_NOTICE_SHARE), spread across 444 kinds, largest kind 43 at 0.079% of
# non-gift-wrap (comfortably under KIND_ALARM_SHARE) -> 'notice', never
# 'stale'. The same gap with one kind (2003) at 27.9% -> 'stale', naming
# it. No walk record at all -> never 'stale', regardless of gap size.
_gap_non_giftwrap = 933085
_gap_share_notice = 18728 / _gap_non_giftwrap  # 2.008%
_walk_long_tail = {"scanned_at": 500, "events_read": 2642995, "distinct_authors_giftwrap": 1709910,
                    "unlisted_kinds": {"43": 735, "10011": 598}}
level = server86._compute_gap_level(_gap_share_notice, 500, _walk_long_tail)
check(level["gap_level"] == "notice" and level["needs_walk"] is False and level["gap_alarm_kind"] is None,
      "gap ladder: 2.008% gap spread across a long tail (largest kind 0.079% of non-gift-wrap) is 'notice', never 'stale'")

_walk_single_miss = {"scanned_at": 500, "events_read": 2642995, "distinct_authors_giftwrap": 1709910,
                      "unlisted_kinds": {"2003": 258290}}
level = server86._compute_gap_level(_gap_share_notice, 500, _walk_single_miss)
check(level["gap_level"] == "stale" and level["gap_alarm_kind"] == 2003 and level["gap_alarm_events"] == 258290,
      "gap ladder: a single unlisted kind at 27.9% of non-gift-wrap (kind 2003, the real miss this check exists for) is 'stale' and names it")

level = server86._compute_gap_level(_gap_share_notice, 500, None)
check(level["gap_level"] == "notice" and level["needs_walk"] is True,
      "gap ladder: with no walk record at all, a totals-only refresh never reaches 'stale' — it asks for a walk instead")

level_old_walk = server86._compute_gap_level(_gap_share_notice, 500, {**_walk_single_miss, "scanned_at": 100})
check(level_old_walk["gap_level"] == "notice" and level_old_walk["needs_walk"] is True,
      "gap ladder: a walk OLDER than the totals record being evaluated cannot promote it to 'stale', even if that walk contains an alarm-worthy kind")

level_ok = server86._compute_gap_level(0.001, 500, _walk_single_miss)
check(level_ok["gap_level"] == "ok" and level_ok["needs_walk"] is False,
      "gap ladder: a gap below GAP_NOTICE_SHARE is 'ok' regardless of what any walk would show")

# deadline: preserves the PREVIOUS walk record byte-identical, releases the
# lock, and never even touches the totals record — a walk timeout must not
# make totals look stale AND wrong at once.
server86._report_cache["walk"] = {**server86._empty_report_walk(), "scanned_at": 12345, "events_read": 999}
server86._report_cache["totals"] = {**server86._empty_report_totals(), "scanned_at": 67890, "total_events": 111}
_prev_totals_snapshot = dict(server86._report_cache["totals"])


def _streaming_timeout(filter_obj, on_event, timeout, on_progress=None):
    raise RuntimeError(f"strfry scan exceeded {timeout}s budget after reading 3 events")


server86.run_strfry_count = lambda filter_obj, timeout=None: 100
server86.run_strfry_scan_streaming = _streaming_timeout

walk_status = server86.start_report_walk_scan()
check(walk_status["status"] == "running" and walk_status["job"] == "walk",
      "POST /api/report/walk starts the job and returns 202 running immediately")
_time.sleep(0.3)

check(server86._report_cache["walk"]["events_read"] == 999 and server86._report_cache["walk"]["scanned_at"] == 12345,
      "a walk that hits its deadline preserves the PREVIOUS walk record byte-identical, never a partial")
check(server86._report_cache["walk"].get("error") is not None,
      "a walk that hits its deadline attaches an error (not a warning) to the walk record")
check(server86._report_cache["totals"] == _prev_totals_snapshot,
      "a failed walk leaves the totals record completely untouched — not even an error added")
check(server86._active_scan["name"] is None,
      "a deadline that fires RELEASES the global lock — a lock that never releases disables every scan in the deployment")

server86.run_strfry_count = _orig_run_strfry_count
server86.run_strfry_scan_streaming = _orig_streaming

# 'failed' is not 'result', for the other two async scans too (authors,
# recipients) — same rule as the walk above: a scan that hit its deadline
# preserves the PREVIOUS cache byte-identical (aside from `error`) and
# never stamps a new scanned_at.
server86._authors_cache.clear()
server86._authors_cache.update({**server86._empty_authors_result(), "scanned_at": 555, "events_read": 42})
_prev_authors_snapshot = dict(server86._authors_cache)
server86.run_strfry_scan_streaming = _streaming_timeout
authors_fail_status = server86.start_authors_scan("recent")
check(authors_fail_status["status"] == "running", "POST /api/authors/scan starts the job and returns 202 running immediately")
_time.sleep(0.3)
check(server86._authors_cache["scanned_at"] == 555 and server86._authors_cache["events_read"] == 42,
      "a failed author scan preserves the PREVIOUS cache byte-identical, never a partial")
check(server86._authors_cache.get("error") is not None,
      "a failed author scan attaches an error (not a warning) to the authors cache, and no rendered result claims to be this attempt's")
server86.run_strfry_scan_streaming = _orig_streaming

server86._recipients_cache.clear()
server86._recipients_cache.update({**server86._empty_recipients_result(), "scanned_at": 777, "events_read": 9})
server86.run_strfry_scan_streaming = _streaming_timeout
recipients_fail_status = server86.start_recipients_scan()
check(recipients_fail_status["status"] == "running", "POST /api/recipients starts the job and returns 202 running immediately")
_time.sleep(0.3)
check(server86._recipients_cache["scanned_at"] == 777 and server86._recipients_cache["events_read"] == 9,
      "a failed recipients scan preserves the PREVIOUS cache byte-identical, never a partial")
check(server86._recipients_cache.get("error") is not None,
      "a failed recipients scan attaches an error (not a warning) to the recipients cache")
server86.run_strfry_scan_streaming = _orig_streaming
server86._active_scan["name"] = None


# --- global scan lock (server86.py: shared by every async scan) -----------
# Every asynchronous scan in the project takes the SAME lock, so a POST to
# any OTHER scan endpoint while one is running must not start a second
# subprocess and must not 409 — it returns the RUNNING job's own status,
# and every scan GET names it via blocked_by.

server86._active_scan["name"] = None  # clean slate — earlier sections may have left a job mid-run
server86.AUTHORS_CACHE_PATH = os.path.join(_cache_tmpdir, "authors-cache.json")


def _slow_compute_authors(mode, progress_cb=None):
    _time.sleep(0.4)
    return server86._empty_authors_result()


_orig_compute_authors = server86.compute_authors
server86.compute_authors = _slow_compute_authors

authors_start = server86.start_authors_scan("recent")
check(authors_start["status"] == "running", "global lock setup: author scan is running")

recipients_blocked = server86.start_recipients_scan()
check(recipients_blocked["blocked_by"] == "authors" and recipients_blocked["status"] == "running",
      "global lock: POST /api/recipients while the author scan holds the lock returns the RUNNING job's own status, naming it, never a second scan")

subscribers_get_blocked = server86.get_subscribers_status()
check(subscribers_get_blocked["blocked_by"] == "authors",
      "global lock: GET /api/subscribers reports blocked_by while a DIFFERENT job holds the lock")

report_totals_blocked = server86.start_report_totals_scan()
check(report_totals_blocked["blocked_by"] == "authors",
      "global lock: POST /api/report/totals while the author scan holds the lock is blocked, never a concurrent scan")

_time.sleep(0.6)
check(server86.get_authors_status()["blocked_by"] is None,
      "global lock: once the author scan finishes, blocked_by clears for the next caller")
check(server86._active_scan["name"] is None,
      "global lock: a completed scan releases the lock")

server86.compute_authors = _orig_compute_authors


# --- gift-wrap purge: `started` is the only thing the audit trail trusts ---
# The purge is the one DESTRUCTIVE async job, so two silent failures matter
# more here than anywhere else: a press that is refused (another job holds
# the lock) must not be audited as a deletion, and a press that IS accepted
# must never run unaudited. A caller cannot answer that by reading the lock
# before starting — the lock can change in between — so _start_scan_job
# reports `started` from inside the lock that launched the thread.

_audit_fires = {"n": 0}


def _count_audit():
    _audit_fires["n"] += 1


server86._active_scan["name"] = None
server86.compute_authors = _slow_compute_authors
_purge_blocker = server86.start_authors_scan("recent")
purge_blocked = server86.start_giftwrap_purge(days=30, on_started=_count_audit)
check(purge_blocked.get("blocked_by") == "authors" and _audit_fires["n"] == 0,
      "gift-wrap purge refused while another job holds the lock never fires on_started — a refused press writes no audit record for a deletion that did not happen")
check(server86._giftwrap_purge_job.get("phase") != "deleting",
      "gift-wrap purge refused while blocked never entered its delete phase")
check("started" not in purge_blocked,
      "gift-wrap purge status carries no `started` field — the two POST bodies stay byte-identical, per the single-flight shape rule above")
_time.sleep(0.6)
server86.compute_authors = _orig_compute_authors
server86._active_scan["name"] = None

# A purge that really runs: the count and the delete each compute their own
# age cutoff, seconds apart, so they describe DIFFERENT sets of events. Both
# cutoffs are reported and the count is never renamed into a deletion total.
_orig_count = server86.run_strfry_count
_orig_retention_purge = server86.run_giftwrap_retention_purge
_delete_until = 1234567


def _fake_count(filter_obj, timeout=None):
    return 4242


def _fake_retention_purge(days=None):
    _time.sleep(0.05)
    return True, None, {"kinds": [1059], "until": _delete_until}


server86.run_strfry_count = _fake_count
server86.run_giftwrap_retention_purge = _fake_retention_purge

purge_started = server86.start_giftwrap_purge(days=30, on_started=_count_audit)
check(purge_started.get("status") == "running" and _audit_fires["n"] == 1,
      "gift-wrap purge that actually launches fires on_started exactly once — the audit record is written for this press and no other")
_purge_again = server86.start_giftwrap_purge(days=30, on_started=_count_audit)
# Same KEYS as the first response (the single-flight shape rule) but not the
# same values — unlike the other scans, this job publishes phase and totals
# as it runs, so a body frozen at launch would be the bug, not the fix.
check(_audit_fires["n"] == 1 and set(_purge_again) == set(purge_started)
      and _purge_again.get("blocked_by") is None,
      "a second press while the purge runs is recognised as the same job and does NOT audit again — one deletion, one record")
_time.sleep(0.5)
purge_done = server86.get_giftwrap_purge_status()
check("deleted" not in purge_done,
      "gift-wrap purge reports no `deleted` field — strfry's delete returns no count, so none is invented")
check(purge_done.get("counted") == 4242,
      "gift-wrap purge reports `counted`, the pre-delete count, under a name that says it is one")
check(purge_done.get("until") == _delete_until,
      "gift-wrap purge `until` comes from the filter the DELETE ran with, not the one the count used")
check(purge_done.get("counted_until") not in (None, _delete_until),
      "gift-wrap purge reports the count's own cutoff separately — the two are never reconciled into one figure")

check("error" not in server86.get_giftwrap_purge_status(),
      "public purge status withholds the failure text — it is strfry's stderr, which names this machine's config and database paths")
check("error" in server86.get_giftwrap_purge_status(include_detail=True),
      "authenticated purge status still carries the failure text, so an admin can see why a purge failed")
check(server86.get_giftwrap_purge_status().get("failed") is False,
      "public purge status still says WHETHER it failed — redaction removes the paths, not the outcome")

server86.run_strfry_count = _orig_count
server86.run_giftwrap_retention_purge = _orig_retention_purge
server86._active_scan["name"] = None


# --- DB compact: dump only, never swaps live data.mdb ----------------------
# Settings Compact only (console refuses the verb). Fixed path under db/,
# global lock, audit only when launched, public status withholds paths.

import tempfile as _tf_dbcompact, shutil as _sh_dbcompact

_compact_audit = {"n": 0}


def _count_compact_audit():
    _compact_audit["n"] += 1


_compact_db2 = _tf_dbcompact.mkdtemp()
_live_mdb = os.path.join(_compact_db2, "data.mdb")
with open(_live_mdb, "wb") as _fh:
    _fh.write(b"x" * 10_000)
_orig_db_bytes2 = server86._strfry_db_bytes
_orig_run_compact = server86.run_db_compact
server86._strfry_db_bytes = lambda: (10_000, _compact_db2)


def _fake_run_compact(output_path=None, progress_cb=None):
    if output_path is None:
        output_path = os.path.join(_compact_db2, server86.COMPACT_OUTPUT_NAME)
    # Mimic the real mid-run signal: growing output under a live-size ceiling.
    if progress_cb is not None:
        progress_cb(0, total=10_000)
        progress_cb(2_000, total=10_000)
        progress_cb(4_000, total=4_000)
    with open(output_path, "wb") as _fh:
        _fh.write(b"y" * 4_000)
    return True, None, {
        "output_path": output_path,
        "live_bytes": 10_000,
        "compacted_bytes": 4_000,
        "reclaimable_bytes": 6_000,
    }


server86.run_db_compact = _fake_run_compact
server86._active_scan["name"] = None
server86.compute_authors = _slow_compute_authors
_compact_blocker = server86.start_authors_scan("recent")
compact_blocked = server86.start_db_compact(on_started=_count_compact_audit)
check(compact_blocked.get("blocked_by") == "authors" and _compact_audit["n"] == 0,
      "db compact refused while another job holds the lock never fires on_started")
_time.sleep(0.6)
server86.compute_authors = _orig_compute_authors
server86._active_scan["name"] = None

compact_started = server86.start_db_compact(on_started=_count_compact_audit)
check(compact_started.get("status") == "running" and _compact_audit["n"] == 1,
      "db compact that launches fires on_started exactly once")
_compact_again = server86.start_db_compact(on_started=_count_compact_audit)
check(_compact_audit["n"] == 1 and _compact_again.get("blocked_by") is None,
      "a second press while compact runs is the same job and does not audit again")
# Give the progress_cb a tick to land on the job dict before the process ends.
_saw_progress = False
for _ in range(20):
    _mid = server86.get_db_compact_status()
    if _mid.get("status") == "running" and _mid.get("total") == 10_000:
        _saw_progress = True
        break
    if _mid.get("phase") == "done":
        break
    _time.sleep(0.05)
check(_saw_progress or server86.get_db_compact_status().get("phase") == "done",
      "db compact publishes total=live_bytes (or finishes) so the UI has a denominator mid-run")
_time.sleep(0.3)
compact_done = server86.get_db_compact_status(include_detail=True)
check(compact_done.get("phase") == "done" and compact_done.get("reclaimable_bytes") == 6_000,
      "db compact reports reclaimable_bytes as live − compacted, never claims the live file shrank")
check(compact_done.get("progress") == 4_000 and compact_done.get("total") == 4_000,
      "db compact snaps progress/total to the measured copy size when done — the live ceiling is not left as a half-full bar")
check(compact_done.get("output_path") == os.path.join(_compact_db2, "data.mdb.compacted"),
      "Settings compact always writes <db>/data.mdb.compacted")
check("error" not in server86.get_db_compact_status(),
      "public compact status withholds the failure text (and output_path)")
check("output_path" not in server86.get_db_compact_status(),
      "public compact status withholds output_path — it names this machine's filesystem")
check("error" in server86.get_db_compact_status(include_detail=True),
      "authenticated compact status still carries the failure field key")

# Console and Settings share the fixed basename rule.
_out, _err = server86.settings_compact_output_path()
check(_out == os.path.join(_compact_db2, "data.mdb.compacted") and _err is None,
      "settings_compact_output_path is always <db>/data.mdb.compacted")

server86.run_db_compact = _orig_run_compact
server86._strfry_db_bytes = _orig_db_bytes2
server86._active_scan["name"] = None
_sh_dbcompact.rmtree(_compact_db2, ignore_errors=True)


# --- blocked tally: the ban list's only proof a ban is doing anything -------
# Counted from plugin86's decision log, which is a rotating window read from
# its tail — so the figure is a FLOOR over a window, and every rule here is
# about never letting it be read as a lifetime total, or as a zero when
# nothing has been measured at all.

import json as _json, shutil as _shutil, tempfile as _tempfile

_tally_dir = _tempfile.mkdtemp()
_orig_dec_path = server86.DECISION_LOG_PATH
_orig_dec_prev = server86.DECISION_LOG_PREV_PATH
server86.DECISION_LOG_PATH = os.path.join(_tally_dir, "decisions.jsonl")
server86.DECISION_LOG_PREV_PATH = server86.DECISION_LOG_PATH + ".1"

_pk_spam = "aa" * 32
_pk_ok = "bb" * 32

# No log at all: not zero blocks — nothing judged anything.
server86._blocked_tally_cache["key"] = None
_empty_tally = server86.get_blocked_tally()
check(_empty_tally["present"] is False and _empty_tally["counts"] == {},
      "blocked tally with no decision log reports present=False — the column renders — , never 0 (rendering rule 9)")

with open(server86.DECISION_LOG_PATH, "w", encoding="utf-8") as _fh:
    for _i in range(3):
        _fh.write(_json.dumps({"at": 1000 + _i, "id": f"{_i:064x}", "pubkey": _pk_spam,
                               "kind": 1, "action": "reject", "msg": "blocked: banned pubkey"}) + "\n")
    _fh.write(_json.dumps({"at": 1010, "id": "f" * 64, "pubkey": _pk_ok,
                           "kind": 1, "action": "accept", "msg": ""}) + "\n")

server86._blocked_tally_cache["key"] = None
_tally = server86.get_blocked_tally()
check(_tally["counts"].get(_pk_spam) == 3,
      "blocked tally counts one rejection per decision record for the pubkey that was rejected")
check(_pk_ok not in _tally["counts"],
      "blocked tally counts ONLY rejections — an accepted event never adds to a pubkey's blocked count")
check(_tally["last_at"].get(_pk_spam) == 1002 and _tally["window_start"] == 1000,
      "blocked tally reports the newest rejection per pubkey and the window's own start, so the count can state its denominator")
check(_tally["present"] is True and _tally["records"] == 4,
      "blocked tally counts every decision read as the window, not just the rejections — the count names what it is out of")

# The cache must not outlive a write, or a freshly blocked pubkey reads stale.
with open(server86.DECISION_LOG_PATH, "a", encoding="utf-8") as _fh:
    _fh.write(_json.dumps({"at": 1100, "id": "e" * 64, "pubkey": _pk_spam,
                           "kind": 1, "action": "reject", "msg": "blocked: banned pubkey"}) + "\n")
check(server86.get_blocked_tally()["counts"].get(_pk_spam) == 4,
      "blocked tally is recomputed after plugin86 appends — the cache keys on the log's size and mtime, never on time alone")

_meta = server86.blocked_window_meta(server86.get_blocked_tally())
check(set(_meta) == {"present", "window_start", "records", "truncated"},
      "every page that renders a blocked count is handed the window it covers, so the figure is never shown bare")

server86.DECISION_LOG_PATH = _orig_dec_path
server86.DECISION_LOG_PREV_PATH = _orig_dec_prev
server86._blocked_tally_cache["key"] = None
_shutil.rmtree(_tally_dir, ignore_errors=True)


# --- static route table: query strings never affect routing --------------
# CLAUDE.md requires this exact check: /profile and /profile?npub=<anything>
# (including a value containing '../') must serve identical bytes, since
# the path is matched before the query string is ever inspected. Spins up
# a real (ephemeral, in-process) HTTP server rather than re-deriving the
# claim with urlparse directly — this exercises server86's actual do_GET,
# not a reimplementation of it.

import threading as _threading
import urllib.error as _urllib_error
import urllib.request as _urllib_request
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer

_httpd = _ThreadingHTTPServer(("127.0.0.1", 0), server86.Handler)
_httpd.strfry86_config = {"admin_pubkey_hex": "a" * 64, "port": 0, "bind": "127.0.0.1"}
_httpd_thread = _threading.Thread(target=_httpd.serve_forever, daemon=True)
_httpd_thread.start()
_base_url = f"http://127.0.0.1:{_httpd.server_address[1]}"


def _get_bytes(path):
    with _urllib_request.urlopen(_base_url + path, timeout=5) as resp:
        return resp.status, resp.read()


try:
    for route, label in (("/profile", "profile.html"), ("/domain", "domain.html"), ("/settings", "settings.html")):
        status_plain, body_plain = _get_bytes(route)
        status_query, body_query = _get_bytes(route + "?d=../../../../etc/passwd&npub=../../../../etc/passwd")
        check(status_plain == 200 and status_query == 200 and body_plain == body_query,
              f"static route {route} serves IDENTICAL bytes with and without a query string containing '../' ({label})")

    class _NoRedirect(_urllib_request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response
        https_response = http_response
    _opener = _urllib_request.build_opener(_NoRedirect)
    _resp = _opener.open(_urllib_request.Request(_base_url + "/", method="GET"), timeout=5)
    check(_resp.status == 200 and b"<title>strfry-86</title>" in _resp.read(),
          "GET / serves the live feed (home.html) with title strfry-86")
    _resp = _opener.open(_urllib_request.Request(_base_url + "/home", method="GET"), timeout=5)
    check(_resp.status == 302 and _resp.headers.get("Location") == "/",
          "GET /home redirects (302) to /")

    try:
        _get_bytes("/api/audit?q=")
        check(False, "GET /api/audit is no longer a public read")
    except _urllib_error.HTTPError as _e:
        check(_e.code == 401,
              "GET /api/audit requires auth (401) — use POST with NIP-98")

    # /api/relay-url: GET is a public read (same stance as /api/authors,
    # /api/recipients, /api/subscribers — only the mutating POST needs
    # auth), reflecting whatever's currently in config.json. get_relay_url
    # was monkeypatched to a lambda for the compute_subscribers tests above;
    # restore the real one so this actually exercises config.json, not a stub.
    server86.get_relay_url = _orig_get_relay_url
    server86.set_relay_url("wss://relay.example.com")
    status, body = _get_bytes("/api/relay-url")
    check(status == 200 and json.loads(body) == {"relay_url": "wss://relay.example.com"},
          "GET /api/relay-url is a public read reflecting the configured value")

    _post_req = _urllib_request.Request(
        _base_url + "/api/relay-url", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        _urllib_request.urlopen(_post_req, timeout=5)
        check(False, "POST /api/relay-url without auth is rejected")
    except _urllib_error.HTTPError as e:
        check(e.code == 401, "POST /api/relay-url without a valid NIP-98 auth is rejected with 401, same as every other mutating endpoint")

    _profile_new_req = _urllib_request.Request(
        _base_url + "/api/profile/new",
        data=json.dumps({"pubkey": "a" * 64, "since": 0}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        _urllib_request.urlopen(_profile_new_req, timeout=5)
        check(False, "POST /api/profile/new without auth is rejected")
    except _urllib_error.HTTPError as e:
        check(e.code == 401, "POST /api/profile/new without a valid NIP-98 auth is rejected with 401 — it's dispatchable (not 404) but still admin-only")
finally:
    _httpd.shutdown()
    _httpd_thread.join(timeout=5)


# --- AUTHOR_SCAN_KINDS gap check (requires a live strfry database) --------

strfry_bin = server86.get_strfry_bin()
if strfry_bin is None:
    print(f"SKIP: AUTHOR_SCAN_KINDS gap check (no strfry binary found; tried PATH and "
          f"{', '.join(server86.STRFRY_BIN_CANDIDATES)})")
else:
    try:
        import subprocess

        conf_path = server86.get_strfry_conf_path()
        if conf_path is None:
            print(f"SKIP: AUTHOR_SCAN_KINDS gap check (no strfry.conf found; "
                  f"set strfry_conf_path or start the relay with --config)")
        else:
            def count(filter_obj):
                argv = [strfry_bin, "--config", conf_path, "scan", "--count",
                        json.dumps(filter_obj, separators=(",", ":"))]
                result = subprocess.run(argv, cwd=server86.get_relay_cwd(), capture_output=True,
                                         timeout=server86.SCAN_TIMEOUT)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode("utf-8", "replace")[-300:])
                lines = [ln.strip() for ln in result.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
                return int(lines[-1])

            total = count({})
            allowlisted = count({"kinds": list(server86.AUTHOR_SCAN_KINDS)})
            giftwraps = count({"kinds": [1059]})
            non_giftwrap = total - giftwraps
            gap = non_giftwrap - allowlisted
            gap_pct = (gap / non_giftwrap * 100) if non_giftwrap else 0.0
            ok = gap_pct <= 2.0
            check(ok, f"AUTHOR_SCAN_KINDS gap check ({gap} missing of {non_giftwrap} non-giftwrap events, {gap_pct:.2f}%)",
                  None if ok else "allowlist is stale — extend AUTHOR_SCAN_KINDS (see CLAUDE.md 'Auditing the allowlist')")
    except Exception as e:
        check(False, "AUTHOR_SCAN_KINDS gap check", f"error running against live db: {type(e).__name__}: {e}")


for ok, name, detail in results:
    if ok:
        print(f"PASS: {name}")
    else:
        line = f"FAIL: {name}"
        if detail:
            line += f" ({detail})"
        print(line)
PYEOF

BOUNDS_OUTPUT="$(python3 "$BOUNDS_TEST_SCRIPT" "$REPO_ROOT")"
echo "$BOUNDS_OUTPUT"
BOUNDS_FAIL_COUNT="$(echo "$BOUNDS_OUTPUT" | grep -c '^FAIL: ')"
FAILURES=$((FAILURES + BOUNDS_FAIL_COUNT))
rm -f "$BOUNDS_TEST_SCRIPT"

# --- client-side rendering rules (common86.js), executed for real --------
# CLAUDE.md's "Rendering results" caps and the record-line safety rule are
# JS-side, not server-side — these run the ACTUAL shared functions (not a
# reimplementation of their logic) against a minimal DOM shim, so a future
# edit that reintroduces the per-record-line reason control, or that drops
# a rendering cap, fails here rather than only in a live browser.

if command -v node >/dev/null 2>&1; then
    JS_TEST_SCRIPT="$TESTDIR/js_render_test.js"
    cat > "$JS_TEST_SCRIPT" <<'JSEOF'
const fs = require('fs');
const vm = require('vm');
const path = process.argv[2];
const results = [];
function check(cond, name) { results.push([!!cond, name]); }

// A minimal DOM shim covering exactly what s86BuildRecordLine,
// s86BuildKeyValueTable, and s86BuildRankedFigureTable use:
// createElement/appendChild/textContent/setAttribute/addEventListener.
// No querySelector — these functions never call it.
function makeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    childNodes: [],
    attrs: {},
    _text: '',
    get textContent() { return this._text; },
    set textContent(v) {
      this._text = String(v);
      this.childNodes = [];
    },
    appendChild(child) { this.childNodes.push(child); return child; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    get title() { return this.attrs.title || ''; },
    set title(v) { this.attrs.title = v; },
  };
}
function flatten(node, out) {
  out.push(node.tagName);
  (node.childNodes || []).forEach((c) => flatten(c, out));
  return out;
}

function makeTextNode(text) {
  return { tagName: '#text', childNodes: [], _text: String(text), get textContent() { return this._text; } };
}

const sandbox = {
  document: { createElement: makeEl, createTextNode: makeTextNode },
  console,
  Math,
  Object,
  Date,
  JSON,
  parseInt,
  Array,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path });

// --- caps hold: RENDER_MAX-style truncation (s86TruncateForRender) -------
const rows = Array.from({ length: 12481 }, (_, i) => i);
const trunc = sandbox.s86TruncateForRender(rows, 500);
check(trunc.shown.length === 500 && trunc.truncatedCount === 11981 && trunc.totalCount === 12481,
  's86TruncateForRender: a 12,481-row list renders exactly 500 rows and reports 11,981 truncated against the full 12,481');

// --- caps hold: FIGURE_HEAD_MAX ranked table + tail summary + full <details>
const unlistedRows = [];
for (let i = 0; i < 444; i++) { unlistedRows.push(['kind ' + (20000 + i), 444 - i, '']); }
const grandTotal = unlistedRows.reduce((s, r) => s + r[1], 0);
const wrap = sandbox.s86BuildRankedFigureTable(unlistedRows, { unit: 'unlisted kinds', grandTotal });
const headTable = wrap.childNodes[0];
const headRows = headTable.childNodes.filter((tr) => tr.tagName === 'TR');
check(headRows.length === sandbox.S86_FIGURE_HEAD_MAX + 1,
  's86BuildRankedFigureTable: renders exactly FIGURE_HEAD_MAX rows plus one tail-summary row for a 444-row input');
const tailCell = headRows[headRows.length - 1].childNodes[0];
const expectedTailTotal = grandTotal - unlistedRows.slice(0, sandbox.S86_FIGURE_HEAD_MAX).reduce((s, r) => s + r[1], 0);
check(tailCell.textContent.indexOf(expectedTailTotal.toLocaleString()) !== -1,
  "s86BuildRankedFigureTable: the tail-summary row's stated event total equals the sum of the omitted rows");
const details = wrap.childNodes[1];
check(details.tagName === 'DETAILS', 's86BuildRankedFigureTable: the full list sits behind a <details>, not inline');
const fullTable = details.childNodes[1];
const fullRows = fullTable.childNodes.filter((tr) => tr.tagName === 'TR');
check(fullRows.length === 444, 's86BuildRankedFigureTable: the <details> contains all 444 rows, one per line, never a comma-separated run');

// --- no record line contains an <input>, in any state --------------------
const banLine = sandbox.s86BuildRecordLine(
  { verb: 'banned', name: null, nip05: null, suffix: null, npub: 'npub1xxxx', entries: null },
  () => {}, () => {});
check(flatten(banLine, []).indexOf('INPUT') === -1,
  's86BuildRecordLine (ban record): no <input> anywhere in the rendered line');

const reportLine = sandbox.s86BuildRecordLine(
  { verb: 'reported', name: null, nip05: null, suffix: ' — spam', npub: 'npub1yyyy', entries: null },
  () => {}, () => {});
check(flatten(reportLine, []).indexOf('INPUT') === -1,
  's86BuildRecordLine (report record): no <input> anywhere in the rendered line');

check(sandbox.s86BuildRecordLine.length === 3,
  's86BuildRecordLine takes exactly 3 parameters — no reasonEdit 4th argument for a record line to act on');

// s86PollStatus must keep polling while a job is BLOCKED, not only while it
// is running. A blocked job's own status reads `idle`, so stopping on status
// alone freezes the panel on "waiting on X…" forever — nothing ever fetches
// again to notice X finished, and the button never comes back.
{
  let fetches = 0;
  const seen = [];
  sandbox.setTimeout = (fn) => { if (fetches < 4) { fn(); } };
  sandbox.s86PollStatus(
    () => {
      fetches++;
      // idle-but-blocked twice, then idle and clear.
      return Promise.resolve(fetches <= 2
        ? { status: 'idle', blocked_by: 'authors' }
        : { status: 'idle', blocked_by: null });
    },
    (data) => { seen.push(data.blocked_by); }
  );
  // The shim resolves promises asynchronously; drain the microtask queue.
  const drain = () => new Promise((r) => setTimeout(r, 0));
  drain().then(drain).then(drain).then(drain).then(drain).then(() => {
    check(fetches >= 3,
      's86PollStatus keeps polling while blocked_by is set — a blocked panel un-freezes when the blocker finishes');
    check(seen[seen.length - 1] === null,
      's86PollStatus stops only once the job is both idle AND unblocked');
    for (const [ok, name] of results.slice(-2)) {
      console.log((ok ? 'PASS: ' : 'FAIL: ') + name);
    }
  });
}

for (const [ok, name] of results) {
  console.log((ok ? 'PASS: ' : 'FAIL: ') + name);
}
JSEOF
    JS_OUTPUT="$(node "$JS_TEST_SCRIPT" "$REPO_ROOT/common86.js" 2>&1)"
    echo "$JS_OUTPUT"
    JS_FAIL_COUNT="$(echo "$JS_OUTPUT" | grep -c '^FAIL: ')"
    FAILURES=$((FAILURES + JS_FAIL_COUNT))
else
    echo "SKIP: client-side rendering rules (node not found)"
fi

# --- strfry-86-updater.py: ensure_config() tops up a missing installer- ----
# prompted field on UPDATE, not just on a fresh install ----------------------
# contact_appeal was added to this project after its initial release, and
# ensure_config() grew a special-cased "if contact_appeal missing, prompt
# for it" block to top up existing installs — but that block only knew
# about contact_appeal by name. INSTALLER_PROMPTED_FIELDS generalizes it: a
# fresh install prompts for every field in that list, and an update tops up
# whichever of those are missing from an EXISTING config.json — so the next
# field added the way contact_appeal was doesn't need its own bespoke
# top-up block remembered alongside it. Executes the real ensure_config(),
# not a reimplementation, against a temp INSTALL_DIR.
UPDATER_TEST_SCRIPT="$TESTDIR/updater_config_test.py"
cat > "$UPDATER_TEST_SCRIPT" <<'PYEOF'
import builtins
import importlib.util
import json
import os
import sys
import tempfile

repo_root = sys.argv[1]
spec = importlib.util.spec_from_file_location(
    "strfry86_updater", os.path.join(repo_root, "strfry-86-updater.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

ADMIN_HEX = "a" * 64


def check(cond, name):
    print(("PASS: " if cond else "FAIL: ") + name)


def with_tmp_install_dir(fn):
    tmpdir = tempfile.mkdtemp()
    orig_install_dir = updater.INSTALL_DIR
    orig_config_path = updater.CONFIG_JSON_PATH
    orig_conf_path = updater.STRFRY_CONF_PATH
    updater.INSTALL_DIR = tmpdir
    updater.CONFIG_JSON_PATH = os.path.join(tmpdir, "config.json")
    # Doesn't exist -> find_relay_info_pubkey() returns None -> manual-entry
    # prompt path, so every scenario here drives input() deterministically.
    updater.STRFRY_CONF_PATH = os.path.join(tmpdir, "strfry.conf")
    try:
        fn(tmpdir)
    finally:
        updater.INSTALL_DIR = orig_install_dir
        updater.CONFIG_JSON_PATH = orig_config_path
        updater.STRFRY_CONF_PATH = orig_conf_path


def scenario_fresh(tmpdir):
    inputs = iter([ADMIN_HEX, "npub1contactxxxx"])
    orig_isatty, orig_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty = lambda: True
    builtins.input = lambda prompt="": next(inputs)
    try:
        status = updater.ensure_config()
    finally:
        builtins.input, sys.stdin.isatty = orig_input, orig_isatty
    cfg = json.load(open(updater.CONFIG_JSON_PATH))
    check(status == "created", "fresh install: ensure_config() reports 'created'")
    check(cfg["admin_pubkey_hex"] == ADMIN_HEX, "fresh install: admin_pubkey_hex prompted and written")
    check(cfg["contact_appeal"] == "npub1contactxxxx", "fresh install: contact_appeal prompted and written")
    check(cfg["port"] == updater.DEFAULT_PORT and cfg["bind"] == updater.DEFAULT_BIND,
          "fresh install: port/bind defaulted, never prompted")


def scenario_missing_contact_only(tmpdir):
    cfg_path = os.path.join(tmpdir, "config.json")
    json.dump({"admin_pubkey_hex": ADMIN_HEX, "port": 8686, "bind": "0.0.0.0"}, open(cfg_path, "w"))
    orig_isatty, orig_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty = lambda: True
    builtins.input = lambda prompt="": "someone@example.com"
    try:
        status = updater.ensure_config()
    finally:
        builtins.input, sys.stdin.isatty = orig_input, orig_isatty
    cfg = json.load(open(cfg_path))
    check(status == "contact_appeal added", "update: today's one known gap (missing contact_appeal alone) is topped up")
    check(cfg["contact_appeal"] == "someone@example.com", "update: topped-up contact_appeal value is written")
    check(cfg["admin_pubkey_hex"] == ADMIN_HEX, "update: pre-existing admin_pubkey_hex left untouched")


def scenario_missing_both(tmpdir):
    cfg_path = os.path.join(tmpdir, "config.json")
    json.dump({"port": 8686, "bind": "0.0.0.0"}, open(cfg_path, "w"))
    inputs = iter([ADMIN_HEX, "https://example.com/appeal"])
    orig_isatty, orig_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty = lambda: True
    builtins.input = lambda prompt="": next(inputs)
    try:
        status = updater.ensure_config()
    finally:
        builtins.input, sys.stdin.isatty = orig_input, orig_isatty
    cfg = json.load(open(cfg_path))
    check("admin_pubkey_hex" in status and "contact_appeal" in status,
          "update: a config missing BOTH installer-prompted fields tops up both, not just contact_appeal")
    check(cfg["admin_pubkey_hex"] == ADMIN_HEX, "update: admin_pubkey_hex itself is topped up when missing")
    check(cfg["contact_appeal"] == "https://example.com/appeal", "update: contact_appeal topped up in the same run")


def scenario_nothing_missing(tmpdir):
    cfg_path = os.path.join(tmpdir, "config.json")
    json.dump({"admin_pubkey_hex": ADMIN_HEX, "port": 8686, "bind": "0.0.0.0", "contact_appeal": "x"}, open(cfg_path, "w"))
    def boom(prompt=""):
        raise AssertionError("input() must not be called when nothing is missing")
    orig_isatty, orig_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty = lambda: True
    builtins.input = boom
    try:
        status = updater.ensure_config()
    finally:
        builtins.input, sys.stdin.isatty = orig_input, orig_isatty
    check(status == "unchanged", "update: a complete config.json is reported unchanged and never prompted")


def scenario_noninteractive(tmpdir):
    cfg_path = os.path.join(tmpdir, "config.json")
    json.dump({"admin_pubkey_hex": ADMIN_HEX, "port": 8686, "bind": "0.0.0.0"}, open(cfg_path, "w"))
    def boom(prompt=""):
        raise AssertionError("input() must not be called when stdin is not a tty")
    orig_isatty, orig_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty = lambda: False
    builtins.input = boom
    try:
        status = updater.ensure_config()
    finally:
        builtins.input, sys.stdin.isatty = orig_input, orig_isatty
    cfg = json.load(open(cfg_path))
    check(status == "unchanged", "update: a missing field found non-interactively is left for the next run")
    check("contact_appeal" not in cfg, "update: a non-interactive run never writes a guessed/blank value")


def scenario_conf_discovery_and_backup(tmpdir):
    # Conf lives under a non-/config path — the old hardcoded path would miss it.
    conf_dir = os.path.join(tmpdir, "etc")
    os.makedirs(conf_dir)
    conf_path = os.path.join(conf_dir, "strfry.conf")
    with open(conf_path, "w") as f:
        f.write('db = "./strfry-db/"\n')
    # Clear the pin so discovery walks candidates; inject our temp conf as the
    # only candidate that exists.
    orig_candidates = updater.STRFRY_CONF_CANDIDATES
    orig_pin = updater.STRFRY_CONF_PATH
    updater.STRFRY_CONF_PATH = None
    updater.STRFRY_CONF_CANDIDATES = (conf_path, "/no/such/strfry.conf")
    try:
        resolved = updater.resolve_strfry_conf()
        check(resolved.get("exists") and resolved.get("path") == conf_path
              and resolved.get("source") == "default location",
              "updater resolves strfry.conf from candidates, not only /config/strfry.conf")

        status, used = updater.edit_strfry_conf()
        check(status == "set" and used == conf_path,
              "updater edits writePolicy.plugin on the resolved conf path")
        with open(conf_path) as f:
            body = f.read()
        check(updater.PLUGIN_PATH in body,
              "updater wrote plugin86.py path into the resolved strfry.conf")
        baks = [n for n in os.listdir(conf_dir) if n.startswith("strfry.conf.bak-")]
        check(len(baks) == 1,
              "updater writes exactly one .bak when it actually edits the conf")

        # Second run: already configured — no new backup, no rewrite.
        before = open(conf_path).read()
        status2, used2 = updater.edit_strfry_conf()
        after = open(conf_path).read()
        baks2 = [n for n in os.listdir(conf_dir) if n.startswith("strfry.conf.bak-")]
        check(status2 == "already configured" and used2 == conf_path,
              "updater reports already configured when plugin is set")
        check(before == after and len(baks2) == 1,
              "updater does not write a backup or rewrite conf when nothing changes")
    finally:
        updater.STRFRY_CONF_CANDIDATES = orig_candidates
        updater.STRFRY_CONF_PATH = orig_pin


def scenario_conf_override_from_config_json(tmpdir):
    conf_path = os.path.join(tmpdir, "custom-strfry.conf")
    with open(conf_path, "w") as f:
        f.write('writePolicy {\n    plugin = ""\n}\n')
    cfg_path = os.path.join(tmpdir, "config.json")
    json.dump({
        "admin_pubkey_hex": ADMIN_HEX,
        "strfry_conf_path": conf_path,
        "port": 8686, "bind": "0.0.0.0", "contact_appeal": "",
    }, open(cfg_path, "w"))
    orig_pin = updater.STRFRY_CONF_PATH
    orig_candidates = updater.STRFRY_CONF_CANDIDATES
    updater.STRFRY_CONF_PATH = None
    # Candidates deliberately empty of real files so only config.json wins.
    updater.STRFRY_CONF_CANDIDATES = ("/nope/strfry.conf",)
    try:
        resolved = updater.resolve_strfry_conf()
        check(resolved.get("path") == conf_path and resolved.get("source") == "config.json",
              "updater honours config.json strfry_conf_path over candidate scan")
        status, used = updater.edit_strfry_conf()
        check(status == "set" and used == conf_path,
              "updater edits the config.json-override conf path")
    finally:
        updater.STRFRY_CONF_PATH = orig_pin
        updater.STRFRY_CONF_CANDIDATES = orig_candidates


with_tmp_install_dir(scenario_fresh)
with_tmp_install_dir(scenario_missing_contact_only)
with_tmp_install_dir(scenario_missing_both)
with_tmp_install_dir(scenario_nothing_missing)
with_tmp_install_dir(scenario_noninteractive)
with_tmp_install_dir(scenario_conf_discovery_and_backup)
with_tmp_install_dir(scenario_conf_override_from_config_json)
PYEOF
UPDATER_OUTPUT="$(python3 "$UPDATER_TEST_SCRIPT" "$REPO_ROOT" 2>&1)"
echo "$UPDATER_OUTPUT"
UPDATER_FAIL_COUNT="$(echo "$UPDATER_OUTPUT" | grep -c '^FAIL: ')"
FAILURES=$((FAILURES + UPDATER_FAIL_COUNT))

# --- the scan sections have no control bound to a page-assembled set -----
# The invariant moved with the panels: Report's rule was that the page which
# renders scan RESULTS never also acts on a set it assembled. Those panels now
# live in stats.html, so stats.html inherits the rule. Its only literal
# checkboxes would have to be new, page-specific controls — every checkbox in
# this project is built with document.createElement in common86.js or in the
# pages that legitimately own a selectable list (users, bans, domain).
if grep -q 'type="checkbox"' "$REPO_ROOT/stats.html"; then
    FAILURES=$((FAILURES + 1))
    echo 'FAIL: the scan sections contain no control bound to a page-assembled set (found a literal checkbox in stats.html source)'
else
    echo 'PASS: the scan sections contain no control bound to a page-assembled set (no literal checkbox in stats.html source)'
fi

# --- the removed pages are actually removed ------------------------------
# A leftover report.html/authors.html/userlist.html on disk is a second copy
# of a page that is no longer reachable — it drifts silently and gets edited
# by mistake.
for _stale in report.html authors.html userlist.html; do
    if [ -e "$REPO_ROOT/$_stale" ]; then
        FAILURES=$((FAILURES + 1))
        echo "FAIL: $_stale was merged away but still exists on disk"
    else
        echo "PASS: $_stale is gone, not left behind as a second copy"
    fi
done

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
