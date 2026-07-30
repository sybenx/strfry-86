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

server86.get_relay_url = lambda: None
try:
    server86.compute_subscribers()
    check(False, "compute_subscribers raises — never a fake-empty result — when relay.info.url is unconfigured")
except RuntimeError as e:
    check("relay.info.url" in str(e), "compute_subscribers's precondition-failure message names relay.info.url")
    check("strfry.conf" in str(e), "compute_subscribers's precondition-failure message states the fix location")

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
_orig_get_relay_cwd = server86.get_relay_cwd
server86.require_strfry_bin = lambda: "/fake/strfry"
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
                                        "reports": [], "reports_saturated": False, "warning": None}
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

resp2 = server86.build_profile_response(banned_target)
check(resp2["banned"] is True and resp2["ban"]["reason"] == "spam" and resp2["ban"]["report_type"] == "manual",
      "build_profile_response includes the full blacklist entry when banned")
check(resp2["scan_rank"] is None and resp2["scan_count"] is None,
      "build_profile_response reports scan_rank/scan_count null for a pubkey absent from the author-scan cache")

server86.compute_profile = _orig_compute_profile
server86.resolve_profiles = _orig_resolve_profiles


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
    on_event({"kind": 1, "pubkey": pk_a})
    on_event({"kind": 1, "pubkey": pk_b})
    on_event({"kind": 1059, "pubkey": "c" * 64})
    on_event({"kind": 1059, "pubkey": "d" * 64})
    on_event({"kind": 99999, "pubkey": pk_a})
    return 5


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
check(walk_result["walk"]["unlisted_total"] == 1 and walk_result["walk"]["unlisted_kind_count"] == 1,
      "compute_report_walk: unlisted_total/unlisted_kind_count summarise unlisted_kinds so the page needs no client-side arithmetic")

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


# --- static route table: query strings never affect routing --------------
# CLAUDE.md requires this exact check: /profile and /profile?npub=<anything>
# (including a value containing '../') must serve identical bytes, since
# the path is matched before the query string is ever inspected. Spins up
# a real (ephemeral, in-process) HTTP server rather than re-deriving the
# claim with urlparse directly — this exercises server86's actual do_GET,
# not a reimplementation of it.

import threading as _threading
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
    for route, label in (("/profile", "profile.html"), ("/domain", "domain.html"), ("/report", "report.html")):
        status_plain, body_plain = _get_bytes(route)
        status_query, body_query = _get_bytes(route + "?d=../../../../etc/passwd&npub=../../../../etc/passwd")
        check(status_plain == 200 and status_query == 200 and body_plain == body_query,
              f"static route {route} serves IDENTICAL bytes with and without a query string containing '../' ({label})")
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

# --- report.html has no control bound to a page-assembled set ------------
# Static-source check: every checkbox in this project (author-checkbox,
# ban-checkbox, select-all, the command generator's exempt-subscribers
# field) is created via document.createElement in common86.js, never as a
# literal <input> in an .html file — so a literal type="checkbox" in
# report.html's own source would mean a NEW, page-specific control, which
# is exactly what this rule forbids.
if grep -q 'type="checkbox"' "$REPO_ROOT/report.html"; then
    FAILURES=$((FAILURES + 1))
    echo 'FAIL: report.html contains no control bound to a page-assembled set (found a literal checkbox in report.html source)'
else
    echo 'PASS: report.html contains no control bound to a page-assembled set (no literal checkbox in report.html source)'
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
else
    echo "$FAILURES TEST(S) FAILED"
    exit 1
fi
