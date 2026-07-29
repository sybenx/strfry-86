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


# --- authors_scan_pubkeys / /api/names widened accept-bound --------------

check(server86.authors_scan_pubkeys() == set(), "authors_scan_pubkeys is empty before any scan has run")

with open(os.path.join(REPO_ROOT, "tests", "kind0-fixture.json")) as f:
    kind0_fixture = json.load(f)
real_pubkey = kind0_fixture["valid"]["pubkey"]

with server86._authors_lock:
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

# never-scanned skeleton shape
check(server86.get_recipients_status() == {**server86._empty_recipients_result(), "status": "idle", "started_at": None, "progress": None},
      "get_recipients_status is the never-scanned skeleton before any scan has run")
check(server86.get_subscribers_status() == {**server86._empty_subscribers_result(), "status": "idle", "started_at": None, "progress": None},
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

server86.get_relay_url = lambda: None
empty_subs = server86.compute_subscribers()
check(empty_subs["relay_url"] is None and empty_subs["subscribers"] == [] and empty_subs["general_subscribers"] == [],
      "compute_subscribers returns empty (never a guess) when relay.info.url is unconfigured")
check(empty_subs["warning"] is not None, "compute_subscribers explains why the list is empty")

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


server86.get_relay_url = lambda: "wss://relay.example.com"
server86.run_strfry_scan_streaming = _streaming_subscribers
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

server86.run_strfry_scan_streaming = _orig_streaming
server86.resolve_profiles = _orig_resolve_profiles


# --- AUTHOR_SCAN_KINDS gap check (requires a live strfry database) --------

strfry_bin = server86.get_strfry_bin()
if strfry_bin is None:
    print(f"SKIP: AUTHOR_SCAN_KINDS gap check (no strfry binary found; tried PATH and "
          f"{', '.join(server86.STRFRY_BIN_CANDIDATES)})")
else:
    try:
        import subprocess

        def count(filter_obj):
            argv = [strfry_bin, "--config", server86.STRFRY_CONF_PATH, "scan", "--count",
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

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
