#!/usr/bin/env python3
"""stdlib admin HTTP server for strfry-86.

Spawned (detached) by plugin86.py. Enforces singleton via port-bind: if the
configured port is already taken, this process exits 0 silently, so repeated
spawns from the plugin are harmless.

Routes:
  GET  /              -> admin.html
  GET  /api/banned    -> public read of the ban list
  GET  /api/activity  -> public read of per-kind activity sparkline data
  POST /api/unban     -> NIP-98 authenticated unban
  POST /api/ban       -> NIP-98 authenticated manual ban
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
ADMIN_HTML_PATH = os.path.join(SCRIPT_DIR, "admin.html")
STRFRY_CONF_PATH = "/config/strfry.conf"
# dockurr/strfry ships the binary at /app/strfry, which is NOT on PATH for a
# detached process; the official image installs to /usr/local/bin.
STRFRY_BIN_CANDIDATES = ("/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry")

NIP98_KIND = 27235
NIP98_MAX_SKEW = 60
NAME_CACHE_TTL = 24 * 3600
STRFRY_SCAN_TIMEOUT = 5

ACTIVITY_WINDOW_DAYS = 28
ACTIVITY_CACHE_TTL = 10 * 60
ACTIVITY_MAX_KINDS = 12
# Kind discovery samples this many events; the sample is the ONLY scan that
# streams event bodies, and the limit bounds it. Every count below it is
# `strfry scan --count`, which streams nothing.
ACTIVITY_DISCOVERY_LIMIT = 500
ACTIVITY_COUNT_TIMEOUT = 10
ACTIVITY_SYNC_TIMEOUT = 5
# Overall wall-clock budget for one recompute, checked between scans, so a
# degraded strfry can't wedge the single-flight recompute for hours.
ACTIVITY_COMPUTE_DEADLINE = 120

_name_cache = {}  # pubkey_hex -> ({"name": str_or_None, "nip05": str_or_None}, checked_at)

_strfry_bin_path = None
_strfry_bin_checked = False

_relay_cwd_pid = None
_relay_cwd_path = None

_activity_lock = threading.Lock()
_activity_cache = None
_activity_computing = False

CONTACT_APPEAL_CHECK_INTERVAL = 1.0
_contact_appeal_cache = ""
_contact_appeal_mtime = None
_contact_appeal_last_checked = 0.0


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
    if (now - _contact_appeal_last_checked) < CONTACT_APPEAL_CHECK_INTERVAL:
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
    it for every subsequent scan (name lookup and activity alike): prefer
    PATH via shutil.which, else the first existing+executable fallback
    candidate. Returns None if nothing is found."""
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


def _run_strfry(filter_obj, timeout, count=False):
    """Run `strfry scan [--count] <filter>` and return raw stdout bytes.

    The filter is passed as exactly ONE argv element of compact JSON with
    shell=False — never a single command string, never .split(), never
    quoted or backslash-escaped (escaped quotes reach strfry verbatim and it
    exits 1). Raises on any failure (missing binary, timeout, non-zero exit)
    so callers can report the cause instead of silently returning nothing."""
    strfry_bin = require_strfry_bin()
    filter_json = json.dumps(filter_obj, separators=(",", ":"))
    argv = [strfry_bin, "--config", STRFRY_CONF_PATH, "scan"]
    if count:
        argv.append("--count")
    argv.append(filter_json)
    result = subprocess.run(argv, cwd=get_relay_cwd(), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"strfry scan exited {result.returncode}: {stderr_tail(result.stderr)}")
    return result.stdout


def run_strfry_scan(filter_obj, timeout=STRFRY_SCAN_TIMEOUT):
    """Run `strfry scan <filter>` and return a list of parsed event dicts."""
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


def run_strfry_count(filter_obj, timeout=ACTIVITY_COUNT_TIMEOUT):
    """Run `strfry scan --count <filter>` and return the match count. Streams
    no event data — the output is a single number read from the index."""
    out = _run_strfry(filter_obj, timeout, count=True).decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("strfry scan --count produced no output")
    return int(lines[-1])


def _clean_profile_field(value):
    return value if isinstance(value, str) and value else None


def resolve_profiles(pubkeys):
    """Return {pubkey: {"name": ..., "nip05": ...}} for the given pubkeys,
    querying the local strfry database for uncached (or stale-miss) pubkeys
    in one batched scan. Both fields come from the same kind-0 event; the
    nip05 string is displayed as-is, never verified (verification would need
    outbound HTTP to arbitrary domains)."""
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


def empty_activity_skeleton(warning):
    return {"generated_at": int(time.time()), "warning": warning, "activity": []}


def compute_activity():
    """Count-only activity computation: per-kind, per-UTC-day event counts
    for the last ACTIVITY_WINDOW_DAYS days.

    Kinds are discovered from one bounded sample scan (`limit` caps its
    output; a kind absent from the sample gets no sparkline — acceptable,
    since a kind carrying meaningful traffic appears in a 500-event sample).
    Everything else is `--count` scans against the index, so no event body
    is ever streamed regardless of relay size."""
    deadline = time.monotonic() + ACTIVITY_COMPUTE_DEADLINE

    def check_deadline():
        if time.monotonic() > deadline:
            raise RuntimeError(f"activity recompute exceeded {ACTIVITY_COMPUTE_DEADLINE}s budget")

    now = int(time.time())
    today_start = (now // 86400) * 86400
    window_start = today_start - (ACTIVITY_WINDOW_DAYS - 1) * 86400
    window_end = today_start + 86400 - 1  # `until` is inclusive per NIP-01

    sample = run_strfry_scan(
        {"since": window_start, "until": window_end, "limit": ACTIVITY_DISCOVERY_LIMIT},
        timeout=ACTIVITY_COUNT_TIMEOUT,
    )
    kinds = sorted({
        ev.get("kind") for ev in sample
        if isinstance(ev, dict) and isinstance(ev.get("kind"), int)
    })

    totals = []
    for kind in kinds:
        check_deadline()
        total = run_strfry_count({"kinds": [kind], "since": window_start, "until": window_end})
        if total > 0:
            totals.append((total, kind))
    totals.sort(reverse=True)

    activity = []
    for _, kind in totals[:ACTIVITY_MAX_KINDS]:
        days = []
        for i in range(ACTIVITY_WINDOW_DAYS):
            check_deadline()
            day_start = window_start + i * 86400
            days.append(run_strfry_count(
                {"kinds": [kind], "since": day_start, "until": day_start + 86400 - 1}
            ))
        activity.append({"kind": kind, "days": days, "total": sum(days)})
    activity.sort(key=lambda a: a["total"], reverse=True)

    return {"generated_at": int(time.time()), "warning": None, "activity": activity}


def _recompute_activity():
    global _activity_cache, _activity_computing
    try:
        report = compute_activity()
    except Exception as e:
        warning = f"activity recompute failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {warning}")
        with _activity_lock:
            if _activity_cache is None:
                _activity_cache = empty_activity_skeleton(warning)
            _activity_computing = False
        return
    with _activity_lock:
        _activity_cache = report
        _activity_computing = False


def get_activity_report():
    """Serve the cached activity report, kicking off a background recompute
    (single-flight) when the cache is stale or missing. The request path
    never blocks beyond ACTIVITY_SYNC_TIMEOUT and never raises — a failed or
    slow recompute degrades to the previous result or an empty skeleton."""
    global _activity_computing
    with _activity_lock:
        cache = _activity_cache
        fresh = cache is not None and (time.time() - cache["generated_at"]) < ACTIVITY_CACHE_TTL
        should_kickoff = not fresh and not _activity_computing
        if should_kickoff:
            _activity_computing = True

    if fresh:
        return cache

    if should_kickoff:
        t = threading.Thread(target=_recompute_activity, daemon=True)
        t.start()
        if cache is None:
            t.join(ACTIVITY_SYNC_TIMEOUT)
            with _activity_lock:
                if _activity_cache is not None:
                    return _activity_cache
            return empty_activity_skeleton("activity still computing — try again shortly")

    if cache is not None:
        return cache
    return empty_activity_skeleton("activity still computing — try again shortly")


def verify_nip98(auth, admin_pubkey_hex, expected_path):
    """Return (ok, error_message)."""
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

    now = int(time.time())
    if abs(created_at - now) > NIP98_MAX_SKEW:
        return False, "stale auth event"

    return True, None


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
        if path == "/":
            try:
                with open(ADMIN_HTML_PATH, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
                        "name": profile.get("name"),
                        "nip05": profile.get("nip05"),
                    }
                )
            banned.sort(key=lambda b: (b["banned_at"] is None, b["banned_at"]), reverse=True)
            self._send_json(200, {
                "admin": cfg["admin_pubkey_hex"],
                "contact_appeal": get_contact_appeal(),
                "banned": banned,
            })
            return

        if path == "/api/activity":
            try:
                report = get_activity_report()
            except Exception as e:
                log(f"server86: activity report failed: {e}")
                report = empty_activity_skeleton(f"activity report failed: {type(e).__name__}")
            self._send_json(200, report)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/unban", "/api/ban"):
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
