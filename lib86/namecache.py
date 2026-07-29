"""Shared, disposable name cache for strfry-86 (names.json).

Serves every name lookup EXCEPT banned pubkeys, which live permanently in
blacklist.json (operator-owned) instead — duplicating them here would
create two sources of truth that drift. This file is the opposite:
derived, never operator-owned, safe to delete at any time (costs one
rescan, nothing more), and rewritten wholesale rather than merged.

Data on disk (names.json) is a JSON object:
    { "<pubkey_hex>": {"name": <str|null>, "nip05": <str|null>,
                       "checked_at": <unix>, "source": "local"|"external"}, ... }

`source: "local"` entries came from a local strfry scan and a full miss is
eligible for re-query after 24h (the local database keeps changing).
`source: "external"` entries came from a verified POST /api/names lookup
and are NEVER auto re-queried, hit or miss — the same once-ever discipline
blacklist.json's name_checked_at enforces on banned pubkeys, for the same
reason (an external query is the thing this project bounds hardest).

Same mtime-cache / atomic-write shape as lib86/blacklist.py, checked at
most once per second so repeated requests in a burst don't restat the
filesystem.
"""

import json
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES_PATH = os.path.join(BASE_DIR, "names.json")

# Retained entries before eviction kicks in. server86.py's NAME_CACHE_MAX
# constant is the source of truth and is passed explicitly by every
# caller there; this is only a sane default if the module is ever used
# standalone.
DEFAULT_MAX_ENTRIES = 20000

_MIN_CHECK_INTERVAL = 1.0

_cache = {}
_cache_mtime = None
_last_checked = None


def _read_file():
    try:
        with open(NAMES_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}


def _refresh(force=False):
    global _cache, _cache_mtime, _last_checked
    now = time.monotonic()
    if not force and _last_checked is not None and (now - _last_checked) < _MIN_CHECK_INTERVAL:
        return
    _last_checked = now
    try:
        mtime = os.stat(NAMES_PATH).st_mtime
    except OSError:
        mtime = None
    if mtime != _cache_mtime:
        _cache = _read_file()
        _cache_mtime = mtime


def _write_atomic(data):
    tmp_path = NAMES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, NAMES_PATH)


def _evict(data, max_entries):
    """Not a correctness event — it costs one rescan of the local database.
    Drops the oldest-checked entries down to 90% of max_entries."""
    if len(data) <= max_entries:
        return data
    target = int(max_entries * 0.9)
    newest_first = sorted(data.items(), key=lambda kv: kv[1].get("checked_at") or 0, reverse=True)
    return dict(newest_first[:target])


def load():
    """Return the current names dict, reloading from disk if it changed."""
    _refresh()
    return _cache


def get_many(pubkeys):
    """Return {pubkey: entry} for whichever of `pubkeys` are cached. Callers
    decide staleness themselves (source + checked_at age) — this is a plain
    lookup, not a freshness filter."""
    data = load()
    return {pk: data[pk] for pk in pubkeys if pk in data}


def set_local(results, now, max_entries=DEFAULT_MAX_ENTRIES):
    """Record local-scan resolution results (hits AND misses — a miss is
    still worth caching so it isn't rescanned every request within the
    24h window). `results` is {pubkey: {"name": str_or_None, "nip05": str_or_None}}."""
    global _cache, _cache_mtime
    _refresh(force=True)
    data = dict(_cache)
    for pk, hit in results.items():
        data[pk] = {"name": hit.get("name"), "nip05": hit.get("nip05"), "checked_at": now, "source": "local"}
    data = _evict(data, max_entries)
    _write_atomic(data)
    _cache = data
    try:
        _cache_mtime = os.stat(NAMES_PATH).st_mtime
    except OSError:
        _cache_mtime = None


def set_external(hits, queried, now, max_entries=DEFAULT_MAX_ENTRIES):
    """Record externally-verified profile results for non-banned pubkeys
    (POST /api/names' widened accept-bound). Every pubkey in `queried`
    gets a `source: "external"` entry stamped `now`, hit or miss — misses
    are never auto re-queried, same discipline as blacklist.json's
    name_checked_at. `hits` is {pubkey: {"name": ..., "nip05": ...}}.
    Returns the list of pubkeys stamped (always == `queried`; returned for
    symmetry with blacklist.set_names, whose "still present" gate has no
    equivalent here)."""
    global _cache, _cache_mtime
    queried = list(queried)
    if not queried:
        return []
    _refresh(force=True)
    data = dict(_cache)
    for pk in queried:
        hit = hits.get(pk)
        data[pk] = {
            "name": hit.get("name") if hit else None,
            "nip05": hit.get("nip05") if hit else None,
            "checked_at": now,
            "source": "external",
        }
    data = _evict(data, max_entries)
    _write_atomic(data)
    _cache = data
    try:
        _cache_mtime = os.stat(NAMES_PATH).st_mtime
    except OSError:
        _cache_mtime = None
    return queried


def drop(pubkey_hex):
    """Remove one pubkey's cached entry, if present. Called on ban — a
    pubkey's name lives in blacklist.json once banned, never both places."""
    global _cache, _cache_mtime
    _refresh(force=True)
    if pubkey_hex not in _cache:
        return
    data = dict(_cache)
    del data[pubkey_hex]
    _write_atomic(data)
    _cache = data
    try:
        _cache_mtime = os.stat(NAMES_PATH).st_mtime
    except OSError:
        _cache_mtime = None
