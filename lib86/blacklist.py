"""Shared blacklist storage for strfry-86.

Used by both plugin86.py (hot path: is_banned() per event) and server86.py
(listing + unban). Each process keeps its own in-memory cache and reloads
from disk when the file's mtime changes, checked at most once per second so
the hot path never stats the filesystem on every single event.

Data on disk (blacklist.json) is a JSON object:
    { "<pubkey_hex>": {"banned_at": <int>, "report_event_id": "<hex or null>",
                       "reason": "<str>", "report_type": "<str or null>"}, ... }
"""

import json
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLACKLIST_PATH = os.path.join(BASE_DIR, "blacklist.json")

_MIN_CHECK_INTERVAL = 1.0

_cache = {}
_cache_mtime = None
_last_checked = 0.0


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
    if not force and (now - _last_checked) < _MIN_CHECK_INTERVAL:
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


def add(pubkey_hex, banned_at, report_event_id, reason, report_type=None, admin_pubkey_hex=None):
    """Add/refresh a ban entry. No-op returning False if pubkey_hex is the admin."""
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
    }
    _write_atomic(data)
    _cache = data
    try:
        _cache_mtime = os.stat(BLACKLIST_PATH).st_mtime
    except OSError:
        _cache_mtime = None
    return True


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
