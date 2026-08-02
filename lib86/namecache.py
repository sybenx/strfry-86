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

Same mtime-cache / flock / unique-temp write shape as lib86/blacklist.py,
checked at most once per second so repeated requests in a burst don't
restat the filesystem. Pubkey keys are stored lowercase.
"""

import fcntl
import json
import os
import tempfile
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


def _as_hex64(s):
    if not isinstance(s, str) or len(s) != 64:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return s.lower()


def _normalize_keys(data):
    out = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        nk = _as_hex64(k)
        if nk is None:
            continue
        out[nk] = v
    return out


def _read_file():
    try:
        with open(NAMES_PATH, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, True
    except (json.JSONDecodeError, ValueError, OSError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    return _normalize_keys(data), True


def _refresh(force=False):
    """force=True must mean 'read from disk NOW, unconditionally' — not
    merely 'skip the once-per-second throttle'. See lib86/blacklist.py's
    _refresh() for why: two close-enough writes can share an mtime on
    some filesystems, and every force=True caller here is a
    reload-fresh-before-write path that must not let an mtime-equality
    skip serve stale data."""
    global _cache, _cache_mtime, _last_checked
    now = time.monotonic()
    if not force and _last_checked is not None and (now - _last_checked) < _MIN_CHECK_INTERVAL:
        return
    _last_checked = now
    try:
        mtime = os.stat(NAMES_PATH).st_mtime
    except OSError:
        mtime = None
    if force or mtime != _cache_mtime:
        data, ok = _read_file()
        if not ok:
            return
        _cache = data
        _cache_mtime = mtime


def _write_atomic(data):
    directory = os.path.dirname(NAMES_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".names-", suffix=".tmp", dir=directory,
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, NAMES_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class _FileLock:
    def __init__(self):
        self._fh = None

    def __enter__(self):
        lock_path = NAMES_PATH + ".lock"
        self._fh = open(lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


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
    out = {}
    for pk in pubkeys:
        key = _as_hex64(pk)
        if key is not None and key in data:
            out[key] = data[key]
    return out


def set_local(results, now, max_entries=DEFAULT_MAX_ENTRIES):
    """Record local-scan resolution results (hits AND misses — a miss is
    still worth caching so it isn't rescanned every request within the
    24h window). `results` is {pubkey: {"name": str_or_None, "nip05": str_or_None}}."""
    global _cache, _cache_mtime
    with _FileLock():
        _refresh(force=True)
        data = dict(_cache)
        for pk, hit in results.items():
            key = _as_hex64(pk)
            if key is None:
                continue
            data[key] = {
                "name": hit.get("name"), "nip05": hit.get("nip05"),
                "checked_at": now, "source": "local",
            }
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
    Returns the list of pubkeys stamped (always == normalized `queried`;
    returned for symmetry with blacklist.set_names, whose "still present"
    gate has no equivalent here)."""
    global _cache, _cache_mtime
    queried_norm = []
    for pk in queried:
        key = _as_hex64(pk)
        if key is not None:
            queried_norm.append(key)
    if not queried_norm:
        return []
    hits_norm = {}
    for pk, hit in (hits or {}).items():
        key = _as_hex64(pk)
        if key is not None:
            hits_norm[key] = hit
    with _FileLock():
        _refresh(force=True)
        data = dict(_cache)
        for pk in queried_norm:
            hit = hits_norm.get(pk)
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
    return queried_norm


def drop(pubkey_hex):
    """Remove one pubkey's cached entry, if present. Called on ban — a
    pubkey's name lives in blacklist.json once banned, never both places."""
    global _cache, _cache_mtime
    key = _as_hex64(pubkey_hex)
    if key is None:
        return
    with _FileLock():
        _refresh(force=True)
        if key not in _cache:
            return
        data = dict(_cache)
        del data[key]
        _write_atomic(data)
        _cache = data
        try:
            _cache_mtime = os.stat(NAMES_PATH).st_mtime
        except OSError:
            _cache_mtime = None
