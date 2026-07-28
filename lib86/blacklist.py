"""Shared blacklist storage for strfry-86.

Used by both plugin86.py (hot path: is_banned() per event) and server86.py
(listing + unban). Each process keeps its own in-memory cache and reloads
from disk when the file's mtime changes, checked at most once per second so
the hot path never stats the filesystem on every single event.

Data on disk (blacklist.json) is a JSON object:
    { "<pubkey_hex>": {"banned_at": <int>, "report_event_id": "<hex or null>",
                       "reason": "<str>", "report_type": "<str or null>",
                       "name": "<str or null>", "nip05": "<str or null>",
                       "name_checked_at": "<int or null>"}, ... }

`name`/`nip05`/`name_checked_at` are set only by set_names() (the intake
for POST /api/names' externally-verified profile lookups) — never by a
local strfry scan, which resolves names in memory only. `name_checked_at`
being non-null means an external lookup was attempted for that pubkey,
hit or miss; entries written before these fields existed simply lack the
keys, which read as null via plain dict.get() — no migration needed.
"""

import json
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLACKLIST_PATH = os.path.join(BASE_DIR, "blacklist.json")

_MIN_CHECK_INTERVAL = 1.0

_cache = {}
_cache_mtime = None
_last_checked = None


def _read_file():
    try:
        with open(BLACKLIST_PATH, "r") as f:
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
        mtime = os.stat(BLACKLIST_PATH).st_mtime
    except OSError:
        mtime = None
    if mtime != _cache_mtime:
        _cache = _read_file()
        _cache_mtime = mtime


def _write_atomic(data):
    tmp_path = BLACKLIST_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, BLACKLIST_PATH)


def load():
    """Return the current blacklist dict, reloading from disk if it changed."""
    _refresh()
    return _cache


def is_banned(pubkey_hex):
    return pubkey_hex in load()


def add(pubkey_hex, banned_at, report_event_id, reason, report_type=None,
        name=None, nip05=None, name_checked_at=None, admin_pubkey_hex=None):
    """Add/refresh a ban entry. No-op returning False if pubkey_hex is the admin.
    A fresh ban always starts with name/nip05/name_checked_at null — a
    pubkey earns a new external lookup only after it's banned again."""
    global _cache, _cache_mtime
    if admin_pubkey_hex is not None and pubkey_hex == admin_pubkey_hex:
        return False
    _refresh(force=True)
    data = dict(_cache)
    data[pubkey_hex] = {
        "banned_at": banned_at,
        "report_event_id": report_event_id,
        "reason": reason,
        "report_type": report_type,
        "name": name,
        "nip05": nip05,
        "name_checked_at": name_checked_at,
    }
    _write_atomic(data)
    _cache = data
    try:
        _cache_mtime = os.stat(BLACKLIST_PATH).st_mtime
    except OSError:
        _cache_mtime = None
    return True


def set_names(hits, queried, now):
    """Record externally-resolved profile names/nip05 and stamp
    name_checked_at on every pubkey in `queried` that is still in the
    blacklist (hit or miss). Reloads from disk first — the external lookup
    took seconds, and the admin may have banned or unbanned someone in the
    meantime, so this must never write back a pre-lookup copy. `hits` is
    {pubkey: {"name": str_or_None, "nip05": str_or_None}} for pubkeys that
    got a verified profile back. Returns the pubkeys actually stamped
    (i.e. still present after the reload)."""
    global _cache, _cache_mtime
    _refresh(force=True)
    data = dict(_cache)
    stamped = []
    for pk in queried:
        if pk not in data:
            continue
        entry = dict(data[pk])
        hit = hits.get(pk)
        if hit is not None:
            entry["name"] = hit.get("name")
            entry["nip05"] = hit.get("nip05")
        entry["name_checked_at"] = now
        data[pk] = entry
        stamped.append(pk)
    if stamped:
        _write_atomic(data)
        _cache = data
        try:
            _cache_mtime = os.stat(BLACKLIST_PATH).st_mtime
        except OSError:
            _cache_mtime = None
    return stamped


def remove(pubkeys):
    """Remove the given pubkeys from the blacklist. Returns the list actually removed."""
    global _cache, _cache_mtime
    _refresh(force=True)
    data = dict(_cache)
    removed = [pk for pk in pubkeys if pk in data]
    for pk in removed:
        del data[pk]
    if removed:
        _write_atomic(data)
        _cache = data
        try:
            _cache_mtime = os.stat(BLACKLIST_PATH).st_mtime
        except OSError:
            _cache_mtime = None
    return removed
