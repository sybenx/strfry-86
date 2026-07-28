#!/usr/bin/env python3
"""stdlib admin HTTP server for strfry-86.

Spawned (detached) by plugin86.py. Enforces singleton via port-bind: if the
configured port is already taken, this process exits 0 silently, so repeated
spawns from the plugin are harmless.

Routes:
  GET  /                  -> bans.html (public ban list)
  GET  /authors           -> authors.html (admin-only active-author page)
  GET  /common86.js       -> shared client JS for both pages
  GET  /api/banned        -> public read of the ban list
  GET  /api/authors       -> public read of the last author-scan result (never scans)
  POST /api/authors/scan  -> NIP-98 authenticated: run exactly one bounded scan
  POST /api/unban         -> NIP-98 authenticated unban
  POST /api/ban           -> NIP-98 authenticated manual ban
  POST /api/names         -> NIP-98 authenticated: intake for externally-verified profile names
"""

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from lib86 import bech32, bip340, blacklist  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STRFRY_CONF_PATH = "/config/strfry.conf"
# dockurr/strfry ships the binary at /app/strfry, which is NOT on PATH for a
# detached process; the official image installs to /usr/local/bin.
STRFRY_BIN_CANDIDATES = ("/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry")

# Static routes are an explicit allowlist, never a filesystem path join —
# there is no directory serving and no path derived from the request, so
# path traversal is not mitigated here, it is impossible. config.json and
# blacklist.json sit next to these files and must never be reachable.
STATIC_ROUTES = {
    "/": ("bans.html", "text/html; charset=utf-8"),
    "/authors": ("authors.html", "text/html; charset=utf-8"),
    "/common86.js": ("common86.js", "application/javascript"),
}

NIP98_KIND = 27235
NIP98_MAX_SKEW = 60
NAME_CACHE_TTL = 24 * 3600
STRFRY_SCAN_TIMEOUT = 5

# The most recent N events define the author-scan window. A constant limit
# is one of exactly two bounding mechanisms this project allows for a scan
# (the other being an explicit `authors` list) — never since/until alone.
AUTHOR_SCAN_LIMIT = 20000
# Overall wall-clock budget for reading the scan's output, checked between
# lines, so a degraded strfry can't hang a request indefinitely.
AUTHOR_SCAN_TIMEOUT = 60

_name_cache = {}  # pubkey_hex -> ({"name": str_or_None, "nip05": str_or_None}, checked_at)

_strfry_bin_path = None
_strfry_bin_checked = False

_relay_cwd_pid = None
_relay_cwd_path = None

# The author list is never recomputed on a timer or because it went stale —
# only POST /api/authors/scan (an explicit admin button press) changes it.
_authors_lock = threading.Lock()
_authors_cache = {
    "scanned_at": None,
    "events_read": 0,
    "span_start": None,
    "span_end": None,
    "warning": None,
    "authors": [],
}
_authors_computing = False

CONTACT_APPEAL_CHECK_INTERVAL = 1.0
_contact_appeal_cache = ""
_contact_appeal_mtime = None
_contact_appeal_last_checked = None


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_config():
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    return {
        "admin_pubkey_hex": cfg["admin_pubkey_hex"],
        "port": int(cfg.get("port", 8686)),
        "bind": cfg.get("bind", "0.0.0.0"),
    }


def get_contact_appeal():
    """Return the current contact_appeal string, re-reading config.json when
    its mtime changes (checked at most once per second). Never raises —
    a hand-edited or briefly-invalid config.json just keeps the last good
    value."""
    global _contact_appeal_cache, _contact_appeal_mtime, _contact_appeal_last_checked
    now = time.monotonic()
    if _contact_appeal_last_checked is not None and (now - _contact_appeal_last_checked) < CONTACT_APPEAL_CHECK_INTERVAL:
        return _contact_appeal_cache
    _contact_appeal_last_checked = now
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime
    except OSError:
        return _contact_appeal_cache
    if mtime == _contact_appeal_mtime:
        return _contact_appeal_cache
    _contact_appeal_mtime = mtime
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        value = cfg.get("contact_appeal")
        _contact_appeal_cache = value if isinstance(value, str) else ""
    except (OSError, ValueError):
        pass
    return _contact_appeal_cache


def compute_event_id(pubkey, created_at, kind, tags, content):
    data = [0, pubkey, created_at, kind, tags, content]
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def is_hex64(s):
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def get_tag(tags, name):
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            return tag[1]
    return None


def get_strfry_bin():
    """Discover the strfry binary path once per process lifetime and cache
    it for every subsequent scan: prefer PATH via shutil.which, else the
    first existing+executable fallback candidate. Returns None if nothing
    is found."""
    global _strfry_bin_path, _strfry_bin_checked
    if _strfry_bin_checked:
        return _strfry_bin_path
    _strfry_bin_checked = True
    found = shutil.which("strfry")
    if not found:
        for candidate in STRFRY_BIN_CANDIDATES:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    _strfry_bin_path = found
    return found


def require_strfry_bin():
    """Return the discovered strfry binary path or raise, so callers report
    a clear cause instead of a bare 'file not found' from subprocess."""
    bin_path = get_strfry_bin()
    if bin_path is None:
        raise RuntimeError(
            "strfry binary not found (tried PATH and " + ", ".join(STRFRY_BIN_CANDIDATES) + ")"
        )
    return bin_path


def get_relay_cwd():
    """Return the working directory `strfry scan` must run from.

    strfry.conf commonly points `db` at a path relative to wherever the
    relay process itself was launched (e.g. dockurr/strfry runs `./strfry`
    from `/app` with `db = "./strfry-db/"`). server86 is spawned by
    plugin86 with cwd=SCRIPT_DIR (/config/strfry86), which is NOT that
    directory, so a scan subprocess with no cwd override resolves the
    relative db path against the wrong directory and strfry exits 1 with
    `mdb_env_open: No such file or directory`.

    Find the running strfry relay process via /proc and reuse its cwd, so
    every scan resolves relative paths exactly as the relay does. Falls
    back to the discovered binary's parent directory if no relay process
    is found (e.g. /proc unavailable, or the relay hasn't started yet).
    Cached until the located pid disappears."""
    global _relay_cwd_pid, _relay_cwd_path
    if _relay_cwd_pid is not None and os.path.isdir(f"/proc/{_relay_cwd_pid}"):
        return _relay_cwd_path

    strfry_bin = require_strfry_bin()
    bin_name = os.path.basename(strfry_bin)
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    args = [a.decode("utf-8", "replace") for a in f.read().split(b"\x00") if a]
            except OSError:
                continue
            if not args or os.path.basename(args[0]) != bin_name or "relay" not in args[1:]:
                continue
            try:
                cwd = os.readlink(f"/proc/{entry}/cwd")
            except OSError:
                continue
            _relay_cwd_pid = entry
            _relay_cwd_path = cwd
            return cwd
    except OSError:
        pass

    _relay_cwd_pid = None
    _relay_cwd_path = os.path.dirname(strfry_bin)
    return _relay_cwd_path


STDERR_TAIL_CHARS = 300


def stderr_tail(stderr_bytes):
    """Return the last ~300 chars of a failed scan's stderr. strfry's loguru
    output puts a startup banner first and the actual error (tao::json parse
    or LMDB env open) as the LAST line, so the head of stderr is useless for
    diagnosis — always take the tail."""
    text = stderr_bytes.decode("utf-8", errors="replace").strip()
    return text[-STDERR_TAIL_CHARS:]


def _run_strfry(filter_obj, timeout):
    """Run `strfry scan <filter>` and return raw stdout bytes.

    The filter is passed as exactly ONE argv element of compact JSON with
    shell=False — never a single command string, never .split(), never
    quoted or backslash-escaped (escaped quotes reach strfry verbatim and it
    exits 1). Raises on any failure (missing binary, timeout, non-zero exit)
    so callers can report the cause instead of silently returning nothing.

    `strfry scan --count` also exists (undocumented in strfry's public
    README, which shows only body-printing `strfry scan '<filter>'`; confirm
    against the deployed strfry's source before relying on its exact
    behavior) but nothing here calls it: the command-block UI only shows
    `--count` as copyable text for the operator to run themselves, per the
    "no scan without an explicit admin request" rule — server86 never
    invokes it as a subprocess."""
    strfry_bin = require_strfry_bin()
    filter_json = json.dumps(filter_obj, separators=(",", ":"))
    argv = [strfry_bin, "--config", STRFRY_CONF_PATH, "scan", filter_json]
    result = subprocess.run(argv, cwd=get_relay_cwd(), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"strfry scan exited {result.returncode}: {stderr_tail(result.stderr)}")
    return result.stdout


def run_strfry_scan(filter_obj, timeout=STRFRY_SCAN_TIMEOUT):
    """Run `strfry scan <filter>` and return a list of parsed event dicts.
    Used only for small, already-bounded scans (an explicit `authors` list)
    where holding the full result in memory is fine."""
    events = []
    for line in _run_strfry(filter_obj, timeout).decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def run_strfry_scan_streaming(filter_obj, on_event, timeout):
    """Run `strfry scan <filter>` and call on_event(parsed_dict) for each
    line as it arrives, never holding more than one parsed event at a time
    (used for the `limit`-bounded author scan, which can return up to
    AUTHOR_SCAN_LIMIT full event bodies — too much to buffer as a list).
    Returns the number of events read. Raises on failure (missing binary,
    non-zero exit, or exceeding the wall-clock budget)."""
    strfry_bin = require_strfry_bin()
    filter_json = json.dumps(filter_obj, separators=(",", ":"))
    argv = [strfry_bin, "--config", STRFRY_CONF_PATH, "scan", filter_json]
    deadline = time.monotonic() + timeout

    proc = subprocess.Popen(
        argv, cwd=get_relay_cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    stderr_chunks = []

    def read_stderr():
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    count = 0
    timed_out = False
    try:
        for raw_line in proc.stdout:
            if time.monotonic() > deadline:
                timed_out = True
                proc.kill()
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            on_event(event)
            count += 1
    finally:
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stderr_thread.join(timeout=2)

    if timed_out:
        raise RuntimeError(f"strfry scan exceeded {timeout}s budget after reading {count} events")
    if proc.returncode != 0:
        tail = stderr_tail(b"".join(stderr_chunks))
        raise RuntimeError(f"strfry scan exited {proc.returncode}: {tail}")
    return count


def _clean_profile_field(value):
    return value if isinstance(value, str) and value else None


def resolve_profiles(pubkeys):
    """Return {pubkey: {"name": ..., "nip05": ...}} for the given pubkeys.

    Two layers: a pubkey that already has a name/nip05 persisted in
    blacklist.json (written by POST /api/names — only possible for a
    currently-banned pubkey) is served from there directly, with no scan.
    Everything else falls through to _resolve_profiles_locally, unchanged
    from before this existed."""
    blacklist_data = blacklist.load()
    stored = {}
    to_resolve_locally = []
    for pk in pubkeys:
        entry = blacklist_data.get(pk)
        if entry and (entry.get("name") or entry.get("nip05")):
            stored[pk] = {"name": entry.get("name"), "nip05": entry.get("nip05")}
        else:
            to_resolve_locally.append(pk)

    result = _resolve_profiles_locally(to_resolve_locally)
    result.update(stored)
    return result


def _resolve_profiles_locally(pubkeys):
    """Resolve names/nip05 from the LOCAL strfry database only, querying
    uncached (or stale-miss) pubkeys in one batched scan bounded by an
    explicit `authors` list. Both fields come from the same kind-0 event;
    the nip05 string is displayed as-is, never verified (verification would
    need outbound HTTP to arbitrary domains). Local results live only in
    the in-memory cache below — never written to blacklist.json, since the
    local database is already the source of truth for them."""
    now = time.time()
    miss = {"name": None, "nip05": None}
    to_query = [
        pk for pk in pubkeys
        if pk not in _name_cache or (
            _name_cache[pk][0] == miss and now - _name_cache[pk][1] >= NAME_CACHE_TTL
        )
    ]

    if to_query:
        try:
            events = run_strfry_scan({"kinds": [0], "authors": to_query})
            found = set()
            for ev in events:
                try:
                    content = json.loads(ev.get("content", "{}"))
                    name = content.get("display_name") or content.get("name")
                    nip05 = content.get("nip05")
                    pk = ev.get("pubkey")
                except Exception:
                    continue
                if pk in to_query:
                    _name_cache[pk] = (
                        {"name": _clean_profile_field(name), "nip05": _clean_profile_field(nip05)},
                        now,
                    )
                    found.add(pk)
            for pk in to_query:
                if pk not in found:
                    _name_cache[pk] = (dict(miss), now)
        except Exception as e:
            log(f"server86: strfry scan failed: {e}")
            for pk in to_query:
                if pk not in _name_cache:
                    _name_cache[pk] = (dict(miss), now)

    return {pk: _name_cache[pk][0] for pk in pubkeys}


def compute_authors():
    """One bounded scan of the most recent AUTHOR_SCAN_LIMIT events, tallied
    per author while streaming — no list of event bodies is ever held in
    full. Depends on NIP-01 `limit` returning the newest matching events
    first, as documented; verify that against the deployed strfry version
    before relying on it."""
    tally = {}  # pubkey -> {"count": int, "last_seen": int}
    span = {"start": None, "end": None}

    def on_event(ev):
        if not isinstance(ev, dict):
            return
        pk = ev.get("pubkey")
        created_at = ev.get("created_at")
        if not isinstance(pk, str) or not isinstance(created_at, int):
            return
        entry = tally.get(pk)
        if entry is None:
            tally[pk] = {"count": 1, "last_seen": created_at}
        else:
            entry["count"] += 1
            if created_at > entry["last_seen"]:
                entry["last_seen"] = created_at
        if span["start"] is None or created_at < span["start"]:
            span["start"] = created_at
        if span["end"] is None or created_at > span["end"]:
            span["end"] = created_at

    events_read = run_strfry_scan_streaming(
        {"limit": AUTHOR_SCAN_LIMIT}, on_event, timeout=AUTHOR_SCAN_TIMEOUT
    )

    pubkeys = list(tally.keys())
    try:
        profiles = resolve_profiles(pubkeys)
    except Exception as e:
        log(f"server86: author name resolution failed: {e}")
        profiles = {}

    authors = []
    for pk, entry in tally.items():
        try:
            npub = bech32.npub_encode(pk)
        except (ValueError, TypeError):
            continue
        profile = profiles.get(pk) or {}
        authors.append({
            "pubkey": pk,
            "npub": npub,
            "name": profile.get("name"),
            "nip05": profile.get("nip05"),
            "count": entry["count"],
            "last_seen": entry["last_seen"],
        })
    authors.sort(key=lambda a: a["count"], reverse=True)

    return {
        "scanned_at": int(time.time()),
        "events_read": events_read,
        "span_start": span["start"],
        "span_end": span["end"],
        "warning": None,
        "authors": authors,
    }


def get_authors_cache():
    with _authors_lock:
        return _authors_cache


def run_authors_scan():
    """Run exactly one author scan, single-flight: a scan already in
    progress returns the existing cache with a warning rather than starting
    a second one. On failure the previous cache survives untouched and the
    returned copy carries a warning naming the exception."""
    global _authors_cache, _authors_computing
    with _authors_lock:
        if _authors_computing:
            report = dict(_authors_cache)
            report["warning"] = "a scan is already in progress — showing the previous result"
            return report
        _authors_computing = True

    try:
        report = compute_authors()
    except Exception as e:
        warning = f"author scan failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {warning}")
        with _authors_lock:
            _authors_computing = False
            stale = dict(_authors_cache)
        stale["warning"] = warning
        return stale

    with _authors_lock:
        _authors_cache = report
        _authors_computing = False
    return report


def verify_nip98(auth, admin_pubkey_hex, expected_path, now=None):
    """Return (ok, error_message). `now` defaults to the real current time;
    a test harness may inject a fixed value to isolate the freshness check
    against a pre-signed fixture."""
    if not isinstance(auth, dict):
        return False, "malformed auth event"

    pubkey = auth.get("pubkey")
    sig = auth.get("sig")
    event_id = auth.get("id")
    kind = auth.get("kind")
    created_at = auth.get("created_at")
    tags = auth.get("tags")
    content = auth.get("content", "")

    if not is_hex64(pubkey) or not is_hex64(event_id) or not isinstance(sig, str) or len(sig) != 128:
        return False, "malformed auth fields"
    if not isinstance(tags, list) or not isinstance(created_at, int) or not isinstance(kind, int):
        return False, "malformed auth fields"

    try:
        int(sig, 16)
    except ValueError:
        return False, "malformed signature"

    expected_id = compute_event_id(pubkey, created_at, kind, tags, content)
    if expected_id != event_id:
        return False, "event id mismatch"

    try:
        sig_ok = bip340.schnorr_verify(
            bytes.fromhex(event_id), bytes.fromhex(pubkey), bytes.fromhex(sig)
        )
    except ValueError:
        return False, "malformed signature"
    if not sig_ok:
        return False, "invalid signature"

    if pubkey != admin_pubkey_hex:
        return False, "not the admin"

    if kind != NIP98_KIND:
        return False, "wrong kind"

    method = get_tag(tags, "method")
    if method != "POST":
        return False, "wrong method tag"

    u = get_tag(tags, "u")
    if not isinstance(u, str) or urlparse(u).path != expected_path:
        return False, "wrong u tag"

    if now is None:
        now = int(time.time())
    if abs(created_at - now) > NIP98_MAX_SKEW:
        return False, "stale auth event"

    return True, None


def verify_kind0_event(event, queried, banned_pubkeys):
    """Verify one raw kind-0 event POSTed to /api/names. Returns
    (pubkey, created_at, name, nip05) if the event is validly signed,
    kind 0, and its pubkey is in both `queried` and `banned_pubkeys` — or
    None on ANY failure. Never raises: a hostile relay's malformed or
    forged event must be silently dropped, not a 500."""
    try:
        if not isinstance(event, dict):
            return None
        pubkey = event.get("pubkey")
        sig = event.get("sig")
        event_id = event.get("id")
        kind = event.get("kind")
        created_at = event.get("created_at")
        tags = event.get("tags")
        content = event.get("content", "")

        if not is_hex64(pubkey) or not is_hex64(event_id):
            return None
        if not isinstance(sig, str) or len(sig) != 128:
            return None
        if not isinstance(tags, list) or not isinstance(created_at, int):
            return None
        if kind != 0:
            return None
        if pubkey not in queried:
            return None
        if pubkey not in banned_pubkeys:
            return None

        expected_id = compute_event_id(pubkey, created_at, kind, tags, content)
        if expected_id != event_id:
            return None

        try:
            sig_ok = bip340.schnorr_verify(
                bytes.fromhex(event_id), bytes.fromhex(pubkey), bytes.fromhex(sig)
            )
        except ValueError:
            return None
        if not sig_ok:
            return None

        try:
            content_obj = json.loads(content)
        except ValueError:
            content_obj = {}
        name = _clean_profile_field(content_obj.get("display_name") or content_obj.get("name"))
        nip05 = _clean_profile_field(content_obj.get("nip05"))
        return (pubkey, created_at, name, nip05)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "strfry86/1.0"

    def log_message(self, fmt, *args):
        log("server86: " + (fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
            try:
                with open(os.path.join(SCRIPT_DIR, filename), "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/banned":
            cfg = self.server.strfry86_config
            data = blacklist.load()
            pubkeys = list(data.keys())
            try:
                profiles = resolve_profiles(pubkeys)
            except Exception as e:
                log(f"server86: name resolution failed: {e}")
                profiles = {}
            banned = []
            for pubkey, info in data.items():
                try:
                    npub = bech32.npub_encode(pubkey)
                except (ValueError, TypeError):
                    continue
                profile = profiles.get(pubkey) or {}
                banned.append(
                    {
                        "pubkey": pubkey,
                        "npub": npub,
                        "banned_at": info.get("banned_at"),
                        "reason": info.get("reason", ""),
                        "report_type": info.get("report_type"),
                        "report_event_id": info.get("report_event_id"),
                        "name": profile.get("name"),
                        "nip05": profile.get("nip05"),
                        "name_checked_at": info.get("name_checked_at"),
                    }
                )
            banned.sort(key=lambda b: (b["banned_at"] is None, b["banned_at"]), reverse=True)
            self._send_json(200, {
                "admin": cfg["admin_pubkey_hex"],
                "contact_appeal": get_contact_appeal(),
                "banned": banned,
            })
            return

        if path == "/api/authors":
            self._send_json(200, get_authors_cache())
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/unban", "/api/ban", "/api/authors/scan", "/api/names"):
            self._send_json(404, {"error": "not found"})
            return

        cfg = self.server.strfry86_config
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"error": "malformed request body"})
            return

        if not isinstance(body, dict):
            self._send_json(400, {"error": "malformed request body"})
            return

        auth = body.get("auth")

        ok, err = verify_nip98(auth, cfg["admin_pubkey_hex"], path)
        if not ok:
            self._send_json(401, {"error": err})
            return

        if path == "/api/authors/scan":
            report = run_authors_scan()
            self._send_json(200, report)
            return

        if path == "/api/names":
            queried_raw = body.get("queried")
            events_raw = body.get("events")
            if not isinstance(queried_raw, list) or not isinstance(events_raw, list):
                self._send_json(400, {"error": "malformed request body"})
                return

            queried = [pk for pk in queried_raw if is_hex64(pk)]
            queried_set = set(queried)
            banned_pubkeys = set(blacklist.load().keys())

            hits = {}
            newest_created_at = {}
            for ev in events_raw:
                verified = verify_kind0_event(ev, queried_set, banned_pubkeys)
                if verified is None:
                    continue
                pubkey, created_at, name, nip05 = verified
                if pubkey in newest_created_at and newest_created_at[pubkey] >= created_at:
                    continue
                newest_created_at[pubkey] = created_at
                hits[pubkey] = {"name": name, "nip05": nip05}

            stamped = blacklist.set_names(hits, queried, now=int(time.time()))
            stamped_set = set(stamped)
            named = sorted(pk for pk in hits if pk in stamped_set)
            self._send_json(200, {"ok": True, "named": named, "stamped": len(stamped)})
            return

        if path == "/api/unban":
            pubkeys = body.get("pubkeys")
            if not isinstance(pubkeys, list) or not all(is_hex64(pk) for pk in pubkeys):
                self._send_json(400, {"error": "malformed pubkeys list"})
                return

            removed = blacklist.remove(pubkeys)
            self._send_json(200, {"ok": True, "removed": removed})
            return

        # /api/ban
        entries = body.get("entries")
        if not isinstance(entries, list):
            self._send_json(400, {"error": "malformed entries list"})
            return

        added = []
        skipped = []
        now = int(time.time())
        for entry in entries:
            if not isinstance(entry, dict):
                skipped.append(entry)
                continue
            raw_pk = entry.get("pubkey")
            reason = entry.get("reason") or ""
            pubkey = None
            if is_hex64(raw_pk):
                pubkey = raw_pk
            elif isinstance(raw_pk, str):
                try:
                    pubkey = bech32.npub_decode(raw_pk)
                except (ValueError, TypeError):
                    pubkey = None
            if not is_hex64(pubkey):
                skipped.append(raw_pk)
                continue
            ok_added = blacklist.add(
                pubkey,
                banned_at=now,
                report_event_id=None,
                reason=reason,
                report_type="manual",
                admin_pubkey_hex=cfg["admin_pubkey_hex"],
            )
            if ok_added:
                added.append(pubkey)
            else:
                skipped.append(raw_pk)

        self._send_json(200, {"ok": True, "added": added, "skipped": skipped})


def main():
    try:
        cfg = load_config()
    except Exception as e:
        log(f"server86: cannot start, config.json missing/invalid: {e}")
        sys.exit(1)

    try:
        httpd = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(0)
        log(f"server86: failed to bind {cfg['bind']}:{cfg['port']}: {e}")
        sys.exit(0)

    httpd.strfry86_config = cfg
    log(f"server86: listening on {cfg['bind']}:{cfg['port']}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
