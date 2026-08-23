#!/usr/bin/env python3
"""Fills the throwaway test relay with data of the SHAPE that drives
/api/userlist and /api/authors size — see test-relay/README.md for the
reasoning behind every number below. Deterministic: same RNG seed, same
admin keypair (admin_key.py), same output every run.

Two delivery paths:
  - The bulk corpus (everything except the admin's own reports) goes in via
    `strfry import --no-verify` — confirmed to accept syntactically-valid
    but unsigned/unverified events, so none of this needs a real signature.
  - The admin-authored kind-1984 reports have to reach plugin86's write-policy
    hook to prove the auto-ban path fires, and that hook only ever sees
    events that came in over the relay's own websocket — `import` bypasses
    it entirely. Those few events are genuinely signed (bip340_sign.py) and
    published over ws_client.
"""

import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

SEED_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SEED_DIR))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, SEED_DIR)

import admin_key  # noqa: E402
import bip340_sign  # noqa: E402
import ws_client  # noqa: E402
import server86  # noqa: E402  (constants only — see tests/../test.sh for the same pattern)

CONTAINER = "strfry86-test"
RELAY_HOST, RELAY_PORT = "127.0.0.1", 17777
ADMIN_HOST, ADMIN_PORT = "127.0.0.1", 18686
ADMIN_BASE = f"http://{ADMIN_HOST}:{ADMIN_PORT}"

RNG_SEED = 86
NOW = int(time.time())

# --- corpus shape ------------------------------------------------------
# Distinct author cardinality is what /api/userlist's size actually tracks
# (CACHE_LIST_MAX truncates the row list, not the event count) — so this
# target is set safely above it, not scaled to production's 2.6M events.
N_SINGLE_AUTHORS = 55_000          # one event each — the purge-candidate population
N_MULTI_TIER_A = 50                # "super" posters
N_MULTI_TIER_B = 950                # "active" posters
N_MULTI_TIER_C = 6_000              # occasional posters
N_KIND0_PROFILES = 3_000            # subset of multi-authors that also get a kind-0
N_GIFTWRAP_RECIPIENTS = 2_500
GIFTWRAP_SHARE = 0.65               # ~65% of total events are kind 1059, per production
N_GAP_TAIL_KINDS = 300              # unlisted kinds outside AUTHOR_SCAN_KINDS + 1059
N_ADMIN_REPORTS = 3                 # real signed kind-1984s, published live

AUTHOR_SCAN_KINDS = server86.AUTHOR_SCAN_KINDS
CACHE_LIST_MAX = server86.CACHE_LIST_MAX

# Heavier weight on the social kinds production actually sees a lot of;
# every other AUTHOR_SCAN_KINDS entry still gets a small residual weight so
# nothing in the allowlist is left at zero. kind 0 is handled separately
# (explicit profile injection, below) so name resolution has real hits.
HEAVY_KIND_WEIGHTS = {
    1: 400, 7: 250, 6: 80, 3: 60, 5: 30, 1984: 20, 9735: 40,
    10002: 15, 10050: 15, 30023: 25,
}
_weighted_kinds = []
_weights = []
for k in AUTHOR_SCAN_KINDS:
    if k == 0:
        continue
    _weighted_kinds.append(k)
    _weights.append(HEAVY_KIND_WEIGHTS.get(k, 5))


def rand_pubkey(rng):
    return rng.getrandbits(256).to_bytes(32, "big").hex()


_seq = [0]


def make_event(rng, pubkey, kind, created_at, content="", tags=None):
    _seq[0] += 1
    ident = hashlib.sha256(f"{pubkey}|{kind}|{created_at}|{_seq[0]}".encode()).hexdigest()
    return {
        "id": ident,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags or [],
        "content": content,
        "sig": "0" * 128,
    }


def rand_created_at(rng, days=90):
    return NOW - rng.randint(0, days * 86400)


def generate_corpus(rng):
    """Yields (event, is_giftwrap) — giftwraps are interleaved at the end
    once the non-1059 total is known, so GIFTWRAP_SHARE is exact."""
    events = []
    author_kind_counts = {}  # pubkey -> total events, for the printed summary

    def emit(pk, kind, content="", tags=None):
        ca = rand_created_at(rng)
        events.append(make_event(rng, pk, kind, ca, content, tags))
        author_kind_counts[pk] = author_kind_counts.get(pk, 0) + 1

    # 1. single-event authors — the large purge-candidate population.
    for _ in range(N_SINGLE_AUTHORS):
        pk = rand_pubkey(rng)
        kind = rng.choices(_weighted_kinds, weights=_weights, k=1)[0]
        emit(pk, kind, f"seed note {_seq[0]}")

    # 2. multi-event "power" authors, three tiers.
    multi_authors = []
    for _count_range, n in (
        ((150, 500), N_MULTI_TIER_A),
        ((8, 40), N_MULTI_TIER_B),
        ((2, 6), N_MULTI_TIER_C),
    ):
        for _ in range(n):
            pk = rand_pubkey(rng)
            multi_authors.append(pk)
            count = rng.randint(*_count_range)
            for _ in range(count):
                kind = rng.choices(_weighted_kinds, weights=_weights, k=1)[0]
                emit(pk, kind, f"seed note {_seq[0]}")

    # 3. kind-0 profiles for a subset of the multi-authors — gives name
    # resolution real hits; the rest of the top-ranked authors stay
    # nameless (misses), on purpose.
    rng.shuffle(multi_authors)
    profiled = multi_authors[:N_KIND0_PROFILES]
    for i, pk in enumerate(profiled):
        content = json.dumps({
            "name": f"seeduser{i}",
            "display_name": f"Seed User {i}",
            "nip05": f"seeduser{i}@example.test" if i % 2 == 0 else None,
        })
        emit(pk, 0, content)

    # 4. gap-audit tail: kinds NOT in AUTHOR_SCAN_KINDS and not 1059, one
    # or two events each. Deliberately excludes the 20000-29999 ephemeral
    # range (strfry deletes those on its own timer) and anything already
    # in AUTHOR_SCAN_KINDS or 1059.
    reserved = set(AUTHOR_SCAN_KINDS) | {1059}
    tail_kind = 40000
    tail_kinds = []
    while len(tail_kinds) < N_GAP_TAIL_KINDS:
        if tail_kind not in reserved and not (20000 <= tail_kind <= 29999):
            tail_kinds.append(tail_kind)
        tail_kind += 7  # arbitrary deterministic stride, avoids a second RNG draw
    for kind in tail_kinds:
        for _ in range(rng.choice((1, 1, 2))):
            emit(rand_pubkey(rng), kind, "unlisted-kind seed event")

    non_giftwrap_total = len(events)

    # 5. gift wraps — sized so they land at GIFTWRAP_SHARE of the grand
    # total, recipients drawn from a fixed pool with a skewed (Zipf-like)
    # distribution so /api/recipients has real winners, not a flat spread.
    giftwrap_total = int(non_giftwrap_total * GIFTWRAP_SHARE / (1 - GIFTWRAP_SHARE))
    recipients = [rand_pubkey(rng) for _ in range(N_GIFTWRAP_RECIPIENTS)]
    weights = [1.0 / (i + 1) for i in range(N_GIFTWRAP_RECIPIENTS)]
    chosen_recipients = rng.choices(recipients, weights=weights, k=giftwrap_total)
    for recipient in chosen_recipients:
        sender = rand_pubkey(rng)  # fresh single-use key, per NIP-17/59
        ca = rand_created_at(rng, days=30)
        events.append(make_event(rng, sender, 1059, ca, "gift-wrapped ciphertext, not real", [["p", recipient]]))

    return events, author_kind_counts, multi_authors, non_giftwrap_total, giftwrap_total, tail_kinds


def write_ndjson(events, path):
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")


def bulk_import(path):
    print(f"importing {path} via `strfry import --no-verify` inside {CONTAINER}...")
    with open(path, "rb") as f:
        subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "./strfry", "import", "--no-verify"],
            stdin=f, check=True,
        )


def payload_sha256(body):
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_event_id(pubkey, created_at, kind, tags, content):
    data = [0, pubkey, created_at, kind, tags, content]
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def make_nip98_auth(path, body_without_auth, origin=ADMIN_BASE):
    created_at = int(time.time())
    tags = [
        ["u", origin + path],
        ["method", "POST"],
        ["payload", payload_sha256(body_without_auth)],
    ]
    ident = compute_event_id(admin_key.PUBKEY_HEX, created_at, 27235, tags, "")
    sig = bip340_sign.sign(bytes.fromhex(ident), admin_key.PRIVKEY_INT).hex()
    return {
        "id": ident, "pubkey": admin_key.PUBKEY_HEX, "created_at": created_at,
        "kind": 27235, "tags": tags, "content": "", "sig": sig,
    }


def nip98_post(path, body_without_auth):
    auth = make_nip98_auth(path, body_without_auth)
    body = dict(body_without_auth)
    body["auth"] = auth
    req = urllib.request.Request(
        ADMIN_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(ADMIN_BASE + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_scan_and_wait(post_path, get_path, body, label):
    print(f"starting {label} scan...")
    nip98_post(post_path, body)
    while True:
        status = get_json(get_path)
        s = status.get("status")
        if s != "running":
            print(f"{label} scan: {s}")
            return status
        time.sleep(2)


def publish_admin_reports(target_pubkeys):
    """Real signature, real websocket publish — the one path that actually
    exercises plugin86's auto-ban, per CLAUDE.md's write-policy description."""
    reports = []
    groups = [target_pubkeys[i::N_ADMIN_REPORTS] for i in range(N_ADMIN_REPORTS)]
    for group in groups:
        if not group:
            continue
        created_at = int(time.time())
        tags = [["p", pk, "spam"] for pk in group]
        content = "seeded test report"
        ident = compute_event_id(admin_key.PUBKEY_HEX, created_at, 1984, tags, content)
        sig = bip340_sign.sign(bytes.fromhex(ident), admin_key.PRIVKEY_INT).hex()
        reports.append({
            "id": ident, "pubkey": admin_key.PUBKEY_HEX, "created_at": created_at,
            "kind": 1984, "tags": tags, "content": content, "sig": sig,
        })
    print(f"publishing {len(reports)} admin-signed kind-1984 report(s) over ws://{RELAY_HOST}:{RELAY_PORT} ...")
    results = ws_client.publish_events(RELAY_HOST, RELAY_PORT, reports)
    for ident, ok, msg in results:
        print(f"  {ident[:12]}... ok={ok} {msg}")
    return reports


def main():
    rng = random.Random(RNG_SEED)
    print(f"admin pubkey: {admin_key.PUBKEY_HEX}")
    print("generating deterministic corpus...")
    events, author_counts, multi_authors, non_gw, gw, tail_kinds = generate_corpus(rng)
    distinct_authors = len(author_counts)
    print(
        f"  {len(events):,} events total "
        f"({non_gw:,} non-giftwrap across {distinct_authors:,} distinct authors, "
        f"{gw:,} kind-1059, {len(tail_kinds)} unlisted tail kinds)"
    )
    if distinct_authors <= CACHE_LIST_MAX:
        print(f"  WARNING: distinct author count is not above CACHE_LIST_MAX ({CACHE_LIST_MAX:,}) — bump N_SINGLE_AUTHORS")

    ndjson_path = os.path.join(SEED_DIR, "_events.ndjson")
    write_ndjson(events, ndjson_path)
    bulk_import(ndjson_path)
    os.remove(ndjson_path)

    # Ban a handful of the real "power" authors so the ban is visible
    # against a non-trivial pubkey in the admin UI.
    ban_targets = multi_authors[:6]
    publish_admin_reports(ban_targets)

    time.sleep(1)  # let plugin86 finish writing blacklist.json
    banned = get_json("/api/banned")
    print(f"banned pubkeys now on record: {len(banned.get('banned') or [])}")

    run_scan_and_wait("/api/authors/scan", "/api/authors", {"mode": "full"}, "authors (full)")
    run_scan_and_wait("/api/recipients", "/api/recipients", {}, "recipients")
    run_scan_and_wait("/api/subscribers", "/api/subscribers", {}, "subscribers")
    run_scan_and_wait("/api/reports", "/api/reports", {}, "reports")

    print("\nseed complete.")
    print(f"admin UI: {ADMIN_BASE}   relay ws: ws://{RELAY_HOST}:{RELAY_PORT}")
    print("next: bin/measure.py")


if __name__ == "__main__":
    main()
