#!/usr/bin/env python3
"""stdlib admin HTTP server for strfry-86.

Spawned (detached) by plugin86.py. Enforces singleton via port-bind: if the
configured port is already taken, this process exits 0 silently, so repeated
spawns from the plugin are harmless.

Routes:
  GET  /                  -> activity.html (public live activity feed)
  GET  /stats             -> stats.html (totals, live delta, terminal)
  GET  /report            -> report.html (admin-only cached-scan results page)
  GET  /authors           -> authors.html (admin-only active-author page)
  GET  /userlist          -> userlist.html (admin member table)
  GET  /audit             -> audit.html (server-side admin action log)
  GET  /bans              -> bans.html (public ban list + admin ban UI)
  GET  /profile           -> profile.html (admin-only single-pubkey detail page)
  GET  /domain            -> domain.html (admin-only nip-05 domain roster page)
  GET  /common86.js       -> shared client JS for all pages
  GET  /api/activity      -> public recent events (kind, timestamp, id only)
  GET  /api/banned        -> public read of the ban list
  GET  /api/authors       -> public read of the last author-scan result (never scans)
  POST /api/authors/scan  -> NIP-98 authenticated: run exactly one bounded scan
  GET  /api/recipients    -> public read of the last gift-wrap recipient tally (never scans)
  POST /api/recipients    -> NIP-98 authenticated: run exactly one bounded scan
  GET  /api/subscribers   -> public read of the last DM/general relay-list search (never scans)
  POST /api/subscribers   -> NIP-98 authenticated: run exactly one bounded scan
  GET  /api/report        -> public read of the totals + whole-database-walk records (never scans)
  POST /api/report/totals -> NIP-98 authenticated: four index counts, seconds even on a large relay
  POST /api/report/walk   -> NIP-98 authenticated: the one unlimited scan in the project
  POST /api/unban         -> NIP-98 authenticated unban
  POST /api/ban           -> NIP-98 authenticated manual ban
  POST /api/reason        -> NIP-98 authenticated: bulk-edit reason on existing bans
  POST /api/profile       -> NIP-98 authenticated: everything known about one pubkey
  POST /api/profile/day   -> NIP-98 authenticated: one pubkey's events on one UTC calendar day
  POST /api/profile/new   -> NIP-98 authenticated: events newer than a given time, bounded
  POST /api/pubkeys/lookup -> NIP-98 authenticated: what's known about a domain's roster
  POST /api/names         -> NIP-98 authenticated: intake for externally-verified profile names
"""

import calendar
import errno
import hashlib
import json
import os
import re
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

from lib86 import bech32, bip340, blacklist, namecache  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STRFRY_CONF_PATH = "/config/strfry.conf"
AUTHORS_CACHE_PATH = os.path.join(SCRIPT_DIR, "authors-cache.json")
RECIPIENTS_CACHE_PATH = os.path.join(SCRIPT_DIR, "recipients-cache.json")
SUBSCRIBERS_CACHE_PATH = os.path.join(SCRIPT_DIR, "subscribers-cache.json")
REPORT_CACHE_PATH = os.path.join(SCRIPT_DIR, "report-cache.json")
# dockurr/strfry ships the binary at /app/strfry, which is NOT on PATH for a
# detached process; the official image installs to /usr/local/bin.
STRFRY_BIN_CANDIDATES = ("/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry")

# Static routes are an explicit allowlist, never a filesystem path join —
# there is no directory serving and no path derived from the request, so
# path traversal is not mitigated here, it is impossible. config.json and
# blacklist.json sit next to these files and must never be reachable.
STATIC_ROUTES = {
    "/": ("activity.html", "text/html; charset=utf-8"),
    "/stats": ("stats.html", "text/html; charset=utf-8"),
    "/report": ("report.html", "text/html; charset=utf-8"),
    "/authors": ("authors.html", "text/html; charset=utf-8"),
    "/userlist": ("userlist.html", "text/html; charset=utf-8"),
    "/audit": ("audit.html", "text/html; charset=utf-8"),
    "/bans": ("bans.html", "text/html; charset=utf-8"),
    "/profile": ("profile.html", "text/html; charset=utf-8"),
    "/domain": ("domain.html", "text/html; charset=utf-8"),
    "/common86.js": ("common86.js", "application/javascript"),
}

NIP98_KIND = 27235
NIP98_MAX_SKEW = 60
NAME_CACHE_TTL = 24 * 3600

# --- bounds --------------------------------------------------------------
# Every bound in the project, in one block, so the bounded-scan rule can be
# audited by reading this block alone. Every scan is bounded by either a
# constant `limit` (never a value taken from a request) or an explicit
# `authors` list assembled from data server86 already holds — nothing else
# counts as a bound, per the hard constraint in CLAUDE.md.

AUTHOR_SCAN_KINDS = (
    # Every kind EXCEPT 1059 (gift wraps: single-use keys, structurally
    # unbannable, and 88% of events on the reference relay). See CLAUDE.md
    # "Auditing the allowlist" for how this tuple is checked for drift
    # against a live relay — test.sh runs that check when one is reachable.
    0, 1, 3, 4, 5, 6, 7, 21, 42, 321, 445, 1000, 1018, 1040, 1111, 1311, 1337,
    1618, 1619, 1984, 1985, 2003, 4000, 4244, 5300, 6300, 7000, 9735, 10002,
    10050, 30000, 30023, 30078, 30166, 30383, 30800, 34236, 36787, 420001,
)
AUTHOR_SCAN_FULL_LIMIT = 1500000  # named separately: saturation at this value ends completeness
AUTHOR_SCAN_MODES = {
    # Selectable by NAME only; a request never supplies a filter, a limit,
    # or a kinds array — see the mode validation in do_POST.
    "recent": {"limit": 20000},                                              # all kinds, newest 20k, ~3s
    "full": {"kinds": AUTHOR_SCAN_KINDS, "limit": AUTHOR_SCAN_FULL_LIMIT},    # every moderation-kind event, ~137s measured
}
AUTHOR_SCAN_DEADLINE = 240      # seconds; a FAILURE timeout for the deep scans, not a bound
REPORT_WALK_DEADLINE = 1800     # seconds; FAILURE timeout for the one unlimited scan (see compute_report_walk)
REPORT_AUTHOR_KEY_BYTES = 8     # bytes of pubkey retained per distinct non-giftwrap author during the walk
RATE_SMOOTHING_WINDOW = 10      # seconds of history the live throughput estimate (rate/eta) averages over
REASON_MAX_LEN = 500            # characters accepted for a ban reason
REASON_UNDO_MAX = 50            # entries whose prior reasons are snapshotted for undo
RECIPIENT_SCAN_LIMIT = 250000   # newest kind-1059 events tallied for storage accounting
SUBSCRIBER_SCAN_LIMIT = 50000   # kind-10050/10002 events searched for this relay's own URL
SCAN_TIMEOUT = 10               # seconds; every other scan subprocess
REPORT_SCAN_LIMIT = 5000        # kind-1984 events read to build the reports-against tally
NAME_RESOLVE_MAX = 500          # pubkeys per batched kind-0 lookup
NAME_CACHE_MAX = 20000          # entries retained in names.json
PROFILE_EVENT_LIMIT = 500       # events read for one pubkey's kind tally
PROFILE_PREVIEW_MAX = 20        # event previews retained from that read
PROFILE_REPORT_LIMIT = 100      # kind-1984 events read for reports against one pubkey
PROFILE_DAY_EVENTS_MAX = 50     # events read for one pubkey's single-day view
DOMAIN_LOOKUP_MAX = 1000        # pubkeys accepted in one /api/pubkeys/lookup body
ACTIVITY_FEED_LIMIT = 50        # newest events on the public Activity landing (kind/ts/id only)
# --- rendering caps: no result reaches a page uncapped --------------------
RENDER_MAX = 500                # list rows rendered client-side before truncation
FIGURE_HEAD_MAX = 10             # ranked rows shown before the tail is summarised
# --- allowlist audit: size and actionability are separate questions -------
GAP_NOTICE_SHARE = 0.02         # gap worth a sentence, never an alarm on its own
KIND_ALARM_SHARE = 0.005        # ONE unlisted kind this big is the actionable alarm

_strfry_bin_path = None
_strfry_bin_checked = False

_relay_cwd_pid = None
_relay_cwd_path = None

# The author list is never recomputed on a timer or because it went stale
# — only POST /api/authors/scan (an explicit admin button press) starts a
# scan, which then runs in a background thread so the request never blocks.
#
# ONE global lock is shared by every asynchronous scan in the project
# (author list, gift-wrap recipients, DM/general-relay-list subscribers,
# report totals, report walk) — see _start_scan_job / _run_scan_job below.
# Running two of these concurrently would contend the same LMDB and the
# same disk, which is slower than running them in series and destroys the
# throughput measurement every progress estimate depends on. _active_scan
# names whichever job currently holds it, or None; _JOB_REGISTRY maps a
# job name to the job-status dict a blocked POST should echo back.
_scan_lock = threading.Lock()
_active_scan = {"name": None}
_JOB_REGISTRY = {}  # populated once every job dict below has been created

_authors_job = {"status": "idle", "mode": None, "started_at": None, "progress": None, "total": None, "rate": None, "eta": None}
# _authors_cache is initialized after _load_cache_or()/_empty_authors_result() are defined below.

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


def event_expiration(tags):
    """The NIP-40 `expiration` timestamp of an event, or None when it
    carries no well-formed one. The tag holds a unix timestamp (as a
    string) past which the event is expired; a relay may stop serving it
    and delete it, but strfry keeps it on disk until a delete actually
    runs, so an expired event is live storage a full walk still sees. A
    non-integer value is treated as absent rather than as an error: a
    malformed tag is not a promise about lifetime."""
    raw = get_tag(tags, "expiration")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
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


def run_strfry_scan(filter_obj, timeout=SCAN_TIMEOUT):
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


def get_activity_feed():
    """Public Activity landing payload: newest ACTIVITY_FEED_LIMIT events,
    stripped to kind + created_at + id only. Never includes pubkey, content,
    or tags — the page is logged-out-safe by construction, not by client
    filtering. On scan failure returns error without a stamped scanned_at
    (four-state rule: failed ≠ empty result)."""
    try:
        raw = run_strfry_scan({"limit": ACTIVITY_FEED_LIMIT}, timeout=SCAN_TIMEOUT)
    except Exception as e:
        return {
            "events": [],
            "error": f"activity feed failed: {type(e).__name__}: {e}"[:600],
            "scanned_at": None,
            "limit": ACTIVITY_FEED_LIMIT,
        }
    events = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id")
        kind = ev.get("kind")
        created_at = ev.get("created_at")
        if not isinstance(eid, str) or not isinstance(kind, int) or not isinstance(created_at, int):
            continue
        events.append({"id": eid, "kind": kind, "created_at": created_at})
    return {
        "events": events,
        "error": None,
        "scanned_at": int(time.time()),
        "limit": ACTIVITY_FEED_LIMIT,
    }


def get_stats_snapshot():
    """Stats page payload: cached walk/totals as the baseline, plus a live
    all-events --count so the page can state both (baseline age + delta).
    Never starts a walk. Live count failure is reported without inventing a
    baseline."""
    report = get_report_status()
    totals = report.get("totals")
    walk = report.get("walk")
    live_total = None
    live_error = None
    try:
        live_total = run_strfry_count({})
    except Exception as e:
        live_error = f"live count failed: {type(e).__name__}: {e}"[:600]
    baseline_total = None
    baseline_at = None
    if totals and totals.get("scanned_at") is not None and totals.get("total_events") is not None:
        baseline_total = totals["total_events"]
        baseline_at = totals["scanned_at"]
    elif walk and walk.get("scanned_at") is not None and walk.get("events_read") is not None:
        baseline_total = walk["events_read"]
        baseline_at = walk["scanned_at"]
    delta = None
    if live_total is not None and baseline_total is not None:
        delta = live_total - baseline_total
    return {
        "totals": totals,
        "walk": walk,
        "baseline_total": baseline_total,
        "baseline_at": baseline_at,
        "live_total": live_total,
        "live_error": live_error,
        "delta": delta,
        "live_at": int(time.time()) if live_total is not None else None,
    }


def get_userlist_snapshot():
    """Join authors cache + recipients cache for the Userlist page. Never
    starts a scan. Missing fields stay null so the client can render —
    rather than inventing zeros."""
    authors_status = get_authors_status()
    recipients_status = get_recipients_status()
    authors = authors_status.get("authors") or []
    recipients = recipients_status.get("recipients") or []
    recipient_counts = {r["pubkey"]: r.get("count") for r in recipients if isinstance(r, dict) and r.get("pubkey")}
    recipients_saturated = bool(recipients_status.get("saturated"))
    recipients_scanned_at = recipients_status.get("scanned_at")
    rows = []
    for a in authors:
        if not isinstance(a, dict) or not a.get("pubkey"):
            continue
        pk = a["pubkey"]
        gw = recipient_counts.get(pk)
        rows.append({
            "pubkey": pk,
            "npub": a.get("npub"),
            "name": a.get("name"),
            "nip05": a.get("nip05"),
            "event_count": a.get("count"),
            "giftwrap_count": gw,
            "reporters": a.get("reporters"),
        })
    return {
        "rows": rows,
        "authors_scanned_at": authors_status.get("scanned_at"),
        "authors_error": authors_status.get("error"),
        "authors_modes": authors_status.get("modes"),
        "recipients_scanned_at": recipients_scanned_at,
        "recipients_saturated": recipients_saturated,
        "recipients_error": recipients_status.get("error"),
    }


# --- terminal allowlist (Stats page) ---------------------------------------
# Non-destructive verbs only. Never shell=True; argv is reconstructed from
# a validated command string. Anything not on this list is refused with a
# reason so the saturation/absence guards stay meaningful.

TERMINAL_TIMEOUT = 30
_TERMINAL_ALLOWED = (
    # (verb, required_flag_substrings_any_of or None means verb alone is ok)
    ("info", None),
    ("scan", ("--count",)),
    ("sync", ("--dry-run",)),
)


def validate_terminal_command(raw):
    """Return (argv, error). argv is a list suitable for subprocess without
    a shell; error is a human reason when refused."""
    if not isinstance(raw, str):
        return None, "command must be a string"
    text = raw.strip()
    if not text:
        return None, "empty command"
    if len(text) > 2000:
        return None, "command too long"
    # naive split on whitespace — filters are JSON and must not contain
    # unescaped spaces the operator didn't type; this is deliberate
    # vs shlex so we never expand quotes into a shell.
    parts = text.split()
    if not parts:
        return None, "empty command"
    verb = parts[0]
    if verb == "strfry":
        parts = parts[1:]
        if not parts:
            return None, "missing strfry subcommand"
        verb = parts[0]
    for allowed_verb, required_flags in _TERMINAL_ALLOWED:
        if verb != allowed_verb:
            continue
        if required_flags is None:
            return ["strfry", "--config", STRFRY_CONF_PATH] + parts, None
        joined = " ".join(parts[1:])
        if any(flag in parts or flag in joined for flag in required_flags):
            return ["strfry", "--config", STRFRY_CONF_PATH] + parts, None
        return None, f"refused: {verb} requires one of {', '.join(required_flags)}"
    return None, (
        f"refused: '{verb}' is not on the non-destructive allowlist "
        f"(allowed: info, scan --count, sync --dry-run)"
    )


def run_terminal_command(raw):
    """Run one allowlisted strfry command; return a JSON-serialisable result."""
    argv, err = validate_terminal_command(raw)
    if err:
        return {"ok": False, "error": err, "argv": None, "stdout": "", "stderr": "", "exit_code": None}
    try:
        strfry_bin = require_strfry_bin()
        argv = [strfry_bin] + argv[1:]  # replace literal 'strfry' with discovered path
        result = subprocess.run(
            argv, cwd=get_relay_cwd(), capture_output=True, timeout=TERMINAL_TIMEOUT
        )
        return {
            "ok": result.returncode == 0,
            "error": None if result.returncode == 0 else f"exit {result.returncode}",
            "argv": argv,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-8000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-4000:],
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {TERMINAL_TIMEOUT}s", "argv": argv,
                "stdout": "", "stderr": "", "exit_code": None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:600], "argv": argv,
                "stdout": "", "stderr": "", "exit_code": None}


def run_strfry_count(filter_obj, timeout=SCAN_TIMEOUT):
    """Run `strfry scan --count <filter>` and return the integer count.
    `--count` walks the index without streaming bodies, so it is cheap
    even for a filter matching many events — used for /api/profile's
    lifetime total, the one number in this project that covers the whole
    database for a single pubkey. Raises on failure (missing binary,
    timeout, non-zero exit, or non-numeric output) exactly like the
    body-streaming scans; treat non-numeric output as a scan failure
    rather than parsing it loosely, per CLAUDE.md's strfry scan execution
    rules."""
    strfry_bin = require_strfry_bin()
    filter_json = json.dumps(filter_obj, separators=(",", ":"))
    argv = [strfry_bin, "--config", STRFRY_CONF_PATH, "scan", "--count", filter_json]
    result = subprocess.run(argv, cwd=get_relay_cwd(), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"strfry scan --count exited {result.returncode}: {stderr_tail(result.stderr)}")
    lines = [ln.strip() for ln in result.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("strfry scan --count produced no output")
    try:
        return int(lines[-1])
    except ValueError:
        raise RuntimeError(f"strfry scan --count returned non-numeric output: {lines[-1]!r}")


def run_strfry_scan_streaming(filter_obj, on_event, timeout, on_progress=None):
    """Run `strfry scan <filter>` and call on_event(parsed_dict) for each
    line as it arrives, never holding more than one parsed event at a time
    (used for the `limit`-bounded author scan, which can return up to
    AUTHOR_SCAN_FULL_LIMIT full event bodies — too much to buffer as a
    list). If given, on_progress(count) is called every 500 events, not
    every one, so a long scan's background thread doesn't contend the
    status lock a polling GET also reads on every request. Returns the
    number of events read. Raises on failure (missing binary, non-zero
    exit, or exceeding the wall-clock budget)."""
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
            if on_progress is not None and count % 500 == 0:
                on_progress(count)
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
    uncached (or stale local-miss) pubkeys in one batched scan bounded by
    an explicit `authors` list. Both fields come from the same kind-0
    event; the nip05 string is displayed as-is, never verified
    (verification would need outbound HTTP to arbitrary domains). Results
    persist in names.json (lib86/namecache.py) rather than in memory only,
    so they survive server86 restarts — never written to blacklist.json,
    since these pubkeys aren't (necessarily) banned.

    A cached `source: "external"` entry (hit or miss) is treated as final
    and never re-attempted locally: if a purplepag.es-backed lookup found
    nothing, the profile is presumed absent everywhere, not just here.
    Only `source: "local"` full misses older than NAME_CACHE_TTL are
    eligible for another local scan attempt — the local database keeps
    changing in a way an external miss never un-misses."""
    now = time.time()
    cached = namecache.get_many(pubkeys)
    to_query = {
        pk for pk in pubkeys
        if pk not in cached or (
            not cached[pk].get("name") and not cached[pk].get("nip05")
            and cached[pk].get("source") == "local"
            and now - (cached[pk].get("checked_at") or 0) >= NAME_CACHE_TTL
        )
    }

    if to_query:
        results = {}
        try:
            events = run_strfry_scan({"kinds": [0], "authors": list(to_query)})
            found = set()
            for ev in events:
                try:
                    content = json.loads(ev.get("content", "{}"))
                    name = content.get("display_name") or content.get("name")
                    nip05 = content.get("nip05")
                    pk = ev.get("pubkey")
                except Exception:
                    continue
                if pk in to_query and pk not in found:
                    results[pk] = {"name": _clean_profile_field(name), "nip05": _clean_profile_field(nip05)}
                    found.add(pk)
            for pk in to_query:
                if pk not in found:
                    results[pk] = {"name": None, "nip05": None}
        except Exception as e:
            log(f"server86: strfry scan failed: {e}")
            for pk in to_query:
                results[pk] = {"name": None, "nip05": None}
        namecache.set_local(results, now, max_entries=NAME_CACHE_MAX)
        for pk, hit in results.items():
            cached[pk] = {"name": hit["name"], "nip05": hit["nip05"], "checked_at": now, "source": "local"}

    miss = {"name": None, "nip05": None}
    return {pk: ({"name": cached[pk]["name"], "nip05": cached[pk]["nip05"]} if pk in cached else miss) for pk in pubkeys}


# --- generic async scan job machinery -------------------------------------
# Shared by every asynchronous scan (author list, gift-wrap recipients,
# DM/general-relay-list subscribers, report totals, report walk): validate
# auth, start ONE scan in a background thread under the single global
# _scan_lock, return 202 immediately, and let the page poll the matching
# GET until status returns to idle. One code path for all of them — not a
# fast one and a slow one that behave differently under failure.
#
# `job` and `cache` are mutated IN PLACE rather than rebound to a new dict,
# so the caller's module-level dict object — the thing readers actually
# hold a reference to — is always what gets updated.

def _save_cache_atomic(path, result):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_cache_or(path, empty_result):
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return dict(empty_result)


# --- server-side audit log -------------------------------------------------
AUDIT_LOG_PATH = os.path.join(SCRIPT_DIR, "audit-log.json")
AUDIT_LOG_MAX = 500


def _empty_audit_log():
    return {"records": []}


_audit_lock = threading.Lock()
_audit_log = _load_cache_or(AUDIT_LOG_PATH, _empty_audit_log())
if not isinstance(_audit_log.get("records"), list):
    _audit_log = _empty_audit_log()


def audit_append(record):
    """Append one admin-action record and persist. Caps at AUDIT_LOG_MAX."""
    if not isinstance(record, dict):
        return
    rec = dict(record)
    rec.setdefault("id", f"{int(time.time() * 1000)}-{os.urandom(3).hex()}")
    rec.setdefault("at", int(time.time()))
    rec.setdefault("undone", False)
    with _audit_lock:
        records = list(_audit_log.get("records") or [])
        records.append(rec)
        if len(records) > AUDIT_LOG_MAX:
            records = records[-AUDIT_LOG_MAX:]
        _audit_log["records"] = records
        try:
            _save_cache_atomic(AUDIT_LOG_PATH, _audit_log)
        except OSError as e:
            log(f"server86: failed to persist audit-log.json: {e}")


def get_audit_records(query=None):
    q = (query or "").strip().lower()
    with _audit_lock:
        records = list(_audit_log.get("records") or [])
    records.reverse()  # newest first
    if not q:
        return records
    out = []
    for r in records:
        blob = json.dumps(r, separators=(",", ":"), ensure_ascii=False).lower()
        if q in blob:
            out.append(r)
    return out


def audit_mark_undone(record_id):
    with _audit_lock:
        for r in _audit_log.get("records") or []:
            if r.get("id") == record_id:
                r["undone"] = True
                try:
                    _save_cache_atomic(AUDIT_LOG_PATH, _audit_log)
                except OSError as e:
                    log(f"server86: failed to persist audit-log.json: {e}")
                return dict(r)
    return None


def _make_progress_cb(job):
    """Returns a stateful progress_cb(count, total=None) closure for one
    scan run. `total` is sticky — once given (e.g. the report walk seeding
    it from its own `--count` before streaming starts), later calls that
    omit it keep the last known value. rate/eta are a trailing average over
    RATE_SMOOTHING_WINDOW seconds of (time, count) samples rather than
    since-start, so they settle quickly instead of drifting; before two
    samples exist in the window both are None and callers must render
    'estimating…' rather than a number."""
    samples = []
    state = {"total": None}

    def cb(count, total=None):
        if total is not None:
            state["total"] = total
        now = time.monotonic()
        samples.append((now, count))
        while len(samples) > 1 and now - samples[0][0] > RATE_SMOOTHING_WINDOW:
            samples.pop(0)
        rate = None
        eta = None
        if len(samples) >= 2:
            dt = samples[-1][0] - samples[0][0]
            dc = samples[-1][1] - samples[0][1]
            if dt > 0 and dc > 0:
                rate = dc / dt
                if state["total"] is not None and state["total"] > count:
                    eta = (state["total"] - count) / rate
        with _scan_lock:
            job["progress"] = count
            job["total"] = state["total"]
            job["rate"] = rate
            job["eta"] = eta

    return cb


def _run_scan_job(name, job, cache, cache_path, compute_fn):
    """Background-thread body for one async scan. `compute_fn(progress_cb)`
    returns the result dict on success. On failure the previous cache is
    preserved (mutated with an `error` attached, never `scanned_at`) and
    NEVER replaced with a partial result — a scan that hit its deadline, or
    a precondition that was never satisfiable, is a FAILED run, not a
    smaller answer. `error` is distinct from `warning`: `warning` rides
    inside a result that IS rendered; `error` replaces a result that is
    not, and a stale `scanned_at` must never be presented as this attempt's
    age. Always releases the global lock on the way out, success or
    failure — a deadline that fires without releasing the lock would
    disable every scan in the deployment until the container restarts."""
    progress_cb = _make_progress_cb(job)
    try:
        result = compute_fn(progress_cb)
    except Exception as e:
        error = f"{name} scan failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {error}")
        with _scan_lock:
            cache["error"] = error
            job["status"] = "idle"
            job["progress"] = None
            job["total"] = None
            job["rate"] = None
            job["eta"] = None
            _active_scan["name"] = None
        return

    try:
        _save_cache_atomic(cache_path, result)
    except OSError as e:
        log(f"server86: failed to persist {os.path.basename(cache_path)}: {e}")

    with _scan_lock:
        cache.clear()
        cache.update(result)
        job["status"] = "idle"
        job["progress"] = None
        job["total"] = None
        job["rate"] = None
        job["eta"] = None
        _active_scan["name"] = None


def _start_scan_job(name, job, run_target, extra_job_fields=None):
    """Single-flight per endpoint, AND global across every scan endpoint:

    - A POST naming the job that is already running returns the SAME 202
      shape the original POST returned, so a POST-then-GET from one client
      never observes a state its own POST couldn't have produced.
    - A POST naming a DIFFERENT job while one is running does not start a
      second subprocess and does not 409 — it returns the RUNNING job's own
      status, with `blocked_by` naming it, so the caller can tell the two
      apart from the response shape alone."""
    with _scan_lock:
        running = _active_scan["name"]
        if running == name:
            status = dict(job)
            status["blocked_by"] = None
            return status
        if running is not None:
            status = dict(_JOB_REGISTRY[running])
            status["blocked_by"] = running
            return status
        _active_scan["name"] = name
        job.clear()
        job.update({
            "status": "running", "started_at": int(time.time()),
            "progress": 0, "total": None, "rate": None, "eta": None,
        })
        if extra_job_fields:
            job.update(extra_job_fields)
        status = dict(job)
        status["blocked_by"] = None

    threading.Thread(target=run_target, daemon=True).start()
    return status


def _scan_status(name, job, cache):
    """GET handler body: never scans, never starts one. Merges the live
    job status over the last persisted result, so status/progress always
    reflect what's happening now and every other field reflects the last
    completed scan (or the never-scanned skeleton). `blocked_by` names
    whatever OTHER job currently holds the global lock — null while this
    job is the one running, or while nothing is running at all."""
    with _scan_lock:
        status = dict(job)
        cached = dict(cache)
        running = _active_scan["name"]
    cached.update(status)
    cached["blocked_by"] = running if running not in (None, name) else None
    return cached


def compute_authors(mode, progress_cb=None):
    """One bounded scan of AUTHOR_SCAN_MODES[mode], tallied per author and
    per kind while streaming — no list of event bodies is ever held in
    full. Depends on NIP-01 `limit` returning the newest matching events
    first, as documented; verify that against the deployed strfry version
    before relying on it.

    A second, independent scan tallies DISTINCT reporters per pubkey from
    recent kind-1984 reports — never itself grounds for a ban, a signal to
    look at. If it fails, `reporters` is null for every author (never a
    misleading 0) and the default sort falls back to event count alone;
    the main author list still succeeds. Neither scan touches purplepag.es
    or writes to blacklist.json — both are paid for by the same admin
    press."""
    start = time.monotonic()
    filter_obj = dict(AUTHOR_SCAN_MODES[mode])
    limit = filter_obj["limit"]
    if progress_cb:
        progress_cb(0, total=limit)
    tally = {}  # pubkey -> {"count": int, "last_seen": int, "kind": int_or_None}
    kind_counts = {}
    span = {"start": None, "end": None}

    def on_event(ev):
        if not isinstance(ev, dict):
            return
        pk = ev.get("pubkey")
        created_at = ev.get("created_at")
        kind = ev.get("kind")
        if not isinstance(pk, str) or not isinstance(created_at, int):
            return
        entry = tally.get(pk)
        if entry is None:
            tally[pk] = {"count": 1, "last_seen": created_at, "kind": kind if isinstance(kind, int) else None}
        else:
            entry["count"] += 1
            if created_at > entry["last_seen"]:
                entry["last_seen"] = created_at
                entry["kind"] = kind if isinstance(kind, int) else None
        if isinstance(kind, int):
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if span["start"] is None or created_at < span["start"]:
            span["start"] = created_at
        if span["end"] is None or created_at > span["end"]:
            span["end"] = created_at

    events_read = run_strfry_scan_streaming(filter_obj, on_event, timeout=AUTHOR_SCAN_DEADLINE, on_progress=progress_cb)
    saturated = events_read >= limit

    singleton_kinds = {}
    for entry in tally.values():
        if entry["count"] == 1 and entry["kind"] is not None:
            singleton_kinds[entry["kind"]] = singleton_kinds.get(entry["kind"], 0) + 1

    reporters_by_pubkey = {}
    reports_ok = True
    reports_scanned = 0
    reports_saturated = False
    try:
        report_events = run_strfry_scan({"kinds": [1984], "limit": REPORT_SCAN_LIMIT}, timeout=SCAN_TIMEOUT)
        reports_scanned = len(report_events)
        reports_saturated = reports_scanned >= REPORT_SCAN_LIMIT
        for ev in report_events:
            reporter = ev.get("pubkey")
            tags = ev.get("tags")
            if not isinstance(reporter, str) or not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p" and isinstance(tag[1], str):
                    reporters_by_pubkey.setdefault(tag[1], set()).add(reporter)
    except Exception as e:
        log(f"server86: reports tally failed: {e}")
        reports_ok = False

    all_pubkeys = set(tally.keys())
    if reports_ok:
        all_pubkeys |= set(reporters_by_pubkey.keys())

    rows = []
    for pk in all_pubkeys:
        try:
            npub = bech32.npub_encode(pk)
        except (ValueError, TypeError):
            continue
        entry = tally.get(pk, {"count": 0, "last_seen": None})
        reporters = len(reporters_by_pubkey.get(pk, ())) if reports_ok else None
        rows.append({
            "pubkey": pk, "npub": npub,
            "count": entry["count"], "last_seen": entry["last_seen"],
            "reporters": reporters,
        })
    if reports_ok:
        rows.sort(key=lambda a: (a["reporters"] or 0, a["count"]), reverse=True)
    else:
        rows.sort(key=lambda a: a["count"], reverse=True)

    # Names: batched, capped at NAME_RESOLVE_MAX, taken from the top of
    # the ranking above. Single-event authors are skipped entirely — on a
    # real relay they are overwhelmingly gift-wrap-adjacent throwaway
    # keys with no kind-0 to find, and resolving them would spend the
    # whole cap on the least useful rows.
    to_resolve = [r["pubkey"] for r in rows if r["count"] != 1][:NAME_RESOLVE_MAX]
    try:
        profiles = resolve_profiles(to_resolve)
    except Exception as e:
        log(f"server86: author name resolution failed: {e}")
        profiles = {}

    authors = []
    for r in rows:
        profile = profiles.get(r["pubkey"]) or {}
        authors.append({
            "pubkey": r["pubkey"], "npub": r["npub"],
            "name": profile.get("name"), "nip05": profile.get("nip05"),
            "count": r["count"], "last_seen": r["last_seen"],
            "reporters": r["reporters"],
        })

    return {
        "scanned_at": int(time.time()),
        "duration": time.monotonic() - start,
        "mode": mode,
        "limit": limit,
        "saturated": saturated,
        "events_read": events_read,
        "span_start": span["start"],
        "span_end": span["end"],
        "kinds": kind_counts,
        "singleton_kinds": singleton_kinds,
        "reports_saturated": reports_saturated,
        "reports_scanned": reports_scanned,
        "warning": None if reports_ok else "reports tally failed — sorting by event count only",
        "error": None,
        "authors": authors,
    }


def _empty_authors_result():
    return {
        "scanned_at": None, "duration": None, "mode": None, "limit": None, "saturated": False,
        "events_read": 0, "span_start": None, "span_end": None,
        "kinds": {}, "singleton_kinds": {},
        "reports_saturated": False, "reports_scanned": 0,
        "warning": None, "error": None, "authors": [],
    }


_authors_cache = _load_cache_or(AUTHORS_CACHE_PATH, _empty_authors_result())
_JOB_REGISTRY["authors"] = _authors_job

# Measured duration of the last successful run of EACH mode, ON THIS RELAY —
# kept separate from authors-cache.json (which holds only the latest scan's
# result and would lose every other mode's measurement the moment a
# different mode is run) so an empty-state press can be estimated honestly
# without inventing a number or reusing one from a different relay.
AUTHOR_MODE_DURATIONS_PATH = os.path.join(SCRIPT_DIR, "author-mode-durations.json")
_author_mode_durations = _load_cache_or(AUTHOR_MODE_DURATIONS_PATH, {})


def start_authors_scan(mode):
    """Start one bounded author scan in a background thread if none is
    already running (and no OTHER scan holds the global lock) — never
    blocks the request."""
    def run():
        _run_scan_job(
            "authors", _authors_job, _authors_cache, AUTHORS_CACHE_PATH,
            lambda progress_cb: compute_authors(mode, progress_cb=progress_cb),
        )
        with _scan_lock:
            ok = _authors_cache.get("error") is None and _authors_cache.get("mode") == mode
            duration = _authors_cache.get("duration") if ok else None
        if duration is not None:
            _author_mode_durations[mode] = duration
            try:
                _save_cache_atomic(AUTHOR_MODE_DURATIONS_PATH, _author_mode_durations)
            except OSError as e:
                log(f"server86: failed to persist author-mode-durations.json: {e}")
    return _start_scan_job("authors", _authors_job, run, extra_job_fields={"mode": mode})


def get_authors_status():
    """GET /api/authors: never scans, never starts one. `modes` names every
    selectable AUTHOR_SCAN_MODES entry with its bound and its measured
    typical duration ON THIS RELAY — null until a run of that mode has
    completed successfully at least once, never a number from elsewhere."""
    status = _scan_status("authors", _authors_job, _authors_cache)
    status["modes"] = {
        name: {"events": cfg.get("limit"), "typical_seconds": _author_mode_durations.get(name)}
        for name, cfg in AUTHOR_SCAN_MODES.items()
    }
    return status


def authors_scan_pubkeys():
    """The set of pubkeys currently in the author-scan cache — the second
    half of /api/names' accept-bound (banned OR in this set), and never
    itself a scan."""
    with _scan_lock:
        cached = _authors_cache
    return {a["pubkey"] for a in cached.get("authors", [])}


# --- gift-wrap recipient tally ---------------------------------------------
# Storage accounting, not moderation: kind 1059 (gift wraps) is the bulk of
# a DM-carrying relay's disk, and the unencrypted `p` tag says who each one
# is addressed to. GET /api/recipients is left as an unauthenticated public
# read, exactly like GET /api/authors / GET /api/banned — the "admin-only"
# framing in CLAUDE.md describes the feature (no recipient leaderboard, no
# UI on any logged-out page — there is no recipients.html at all), not a
# server-side auth requirement on a read of an already-bounded cache. Only
# the scan-triggering POST costs a NIP-98 signature.

def _empty_recipients_result():
    return {
        "scanned_at": None, "events_read": 0, "span_start": None, "span_end": None,
        "saturated": False, "warning": None, "error": None, "recipients": [],
    }


def compute_recipients(progress_cb=None):
    """One bounded scan of the newest RECIPIENT_SCAN_LIMIT gift-wrap
    events, tallying the `p` tag — the wrapped message's recipient, never
    the event's `pubkey`, which is a fresh single-use sender key — while
    streaming, so no event body is ever held in full. Gift-wrap CONTENT is
    encrypted and senders are unlinkable; nothing here reveals who is
    talking to whom, and recipient counts are for retention/capacity
    decisions only, never a moderation signal."""
    if progress_cb:
        progress_cb(0, total=RECIPIENT_SCAN_LIMIT)
    tally = {}
    span = {"start": None, "end": None}

    def on_event(ev):
        if not isinstance(ev, dict):
            return
        created_at = ev.get("created_at")
        tags = ev.get("tags")
        if not isinstance(created_at, int) or not isinstance(tags, list):
            return
        recipient = get_tag(tags, "p")
        if isinstance(recipient, str) and is_hex64(recipient):
            tally[recipient] = tally.get(recipient, 0) + 1
        if span["start"] is None or created_at < span["start"]:
            span["start"] = created_at
        if span["end"] is None or created_at > span["end"]:
            span["end"] = created_at

    events_read = run_strfry_scan_streaming(
        {"kinds": [1059], "limit": RECIPIENT_SCAN_LIMIT}, on_event,
        timeout=AUTHOR_SCAN_DEADLINE, on_progress=progress_cb,
    )
    saturated = events_read >= RECIPIENT_SCAN_LIMIT

    rows = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
    to_resolve = [pk for pk, _ in rows[:NAME_RESOLVE_MAX]]
    try:
        profiles = resolve_profiles(to_resolve)
    except Exception as e:
        log(f"server86: recipient name resolution failed: {e}")
        profiles = {}

    recipients = []
    for pk, count in rows:
        try:
            npub = bech32.npub_encode(pk)
        except (ValueError, TypeError):
            continue
        profile = profiles.get(pk) or {}
        recipients.append({
            "pubkey": pk, "npub": npub,
            "name": profile.get("name"), "nip05": profile.get("nip05"),
            "count": count,
        })

    return {
        "scanned_at": int(time.time()),
        "events_read": events_read,
        "span_start": span["start"],
        "span_end": span["end"],
        "saturated": saturated,
        "warning": None,
        "error": None,
        "recipients": recipients,
    }


_recipients_job = {"status": "idle", "started_at": None, "progress": None, "total": None, "rate": None, "eta": None}
_recipients_cache = _load_cache_or(RECIPIENTS_CACHE_PATH, _empty_recipients_result())
_JOB_REGISTRY["recipients"] = _recipients_job


def start_recipients_scan():
    def run():
        _run_scan_job(
            "recipients", _recipients_job, _recipients_cache,
            RECIPIENTS_CACHE_PATH, compute_recipients,
        )
    return _start_scan_job("recipients", _recipients_job, run)


def get_recipients_status():
    return _scan_status("recipients", _recipients_job, _recipients_cache)


# --- DM / general relay-list subscriber search -----------------------------
# Who lists THIS relay in a NIP-17 DM relay list (kind 10050) or a general
# relay list (kind 10002) — the retention-purge exemption (Phase 5) reads
# this to avoid deleting the gift wraps of people who explicitly asked this
# relay to hold them. Kept separate from recipients: a subscriber published
# a signed event announcing this relay is their inbox, which is itself
# public data and discloses nothing new.

def _empty_subscribers_result():
    return {
        "scanned_at": None, "relay_url": None, "saturated": False,
        "scan_limit": SUBSCRIBER_SCAN_LIMIT, "counted": {},
        "warning": None, "error": None,
        "subscribers": [], "general_subscribers": [],
    }


def _hostname_of(value):
    """Host-only parse, tolerant of a missing scheme (a bare 'relay.example'
    is valid for both the config value and a kind-10050/10002 'relay' tag).
    Returns None on anything unparseable or empty so a missing relay_url
    and a malformed tag never accidentally compare equal via two empty
    strings. Substring matching on the raw tag was rejected deliberately —
    it would match 'relay.example.evil.com' against 'relay.example'."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "//" + value
    host = urlparse(value).hostname
    return host.lower() if host else None


_relay_url_cache = None
_relay_url_mtime = None
_relay_url_last_checked = None


def get_relay_url():
    """Best-effort read of config.json's `relay_url` — this relay's own
    public address, needed only so /api/subscribers can tell whether a
    kind-10050/10002 relay tag names THIS relay. strfry has no notion of
    its own address and never reads this field; it's admin-page state,
    set via POST /api/relay-url (see set_relay_url) rather than by hand-
    editing strfry.conf. Re-read on config.json mtime change, checked at
    most once per second, same shape as get_contact_appeal()."""
    global _relay_url_cache, _relay_url_mtime, _relay_url_last_checked
    now = time.monotonic()
    if _relay_url_last_checked is not None and (now - _relay_url_last_checked) < CONTACT_APPEAL_CHECK_INTERVAL:
        return _relay_url_cache
    _relay_url_last_checked = now
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime
    except OSError:
        return _relay_url_cache
    if mtime == _relay_url_mtime:
        return _relay_url_cache
    _relay_url_mtime = mtime
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        value = cfg.get("relay_url")
        _relay_url_cache = value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError):
        pass
    return _relay_url_cache


def set_relay_url(value):
    """Writes (or clears, when value is falsy) config.json's `relay_url`.
    Admin-only — called from POST /api/relay-url, which sits behind the
    blanket NIP-98 check in do_POST alongside every other mutating
    endpoint. Forces the next get_relay_url() to re-read rather than wait
    out CONTACT_APPEAL_CHECK_INTERVAL, so a save takes effect immediately
    for the very next subscriber scan."""
    global _relay_url_last_checked
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    if value:
        cfg["relay_url"] = value
    else:
        cfg.pop("relay_url", None)
    _save_cache_atomic(CONFIG_PATH, cfg)
    _relay_url_last_checked = None


def compute_subscribers(progress_cb=None):
    """Two bounded scans, kept separate: kind 10050 (NIP-17 DM relay lists)
    and kind 10002 (general relay lists) — a pubkey listing this relay for
    general use is a different relationship from one listing it for DMs.
    Each is capped at SUBSCRIBER_SCAN_LIMIT and matched host-only against
    get_relay_url().

    An unconfigured relay.info.url is a FAILED run, not a result: raising
    here means the caller (_run_scan_job) preserves whatever cache existed
    before, stamps no new scanned_at, and attaches `error` rather than
    rendering an empty subscriber list as a fresh answer. This result feeds
    a destructive retention-purge exemption (Phase 5); a silently-empty
    list presented as current is indistinguishable from 'nobody subscribes'
    and must never be produced.

    SATURATION on this endpoint is unsafe in the direction that matters:
    a saturated subscriber list is a FLOOR, so missing subscribers means
    missing exemptions means MORE deletion, not less (the opposite of a
    saturated recipient scan, which only deletes less). `counted` runs an
    exact index count per kind alongside the bounded tally read, so
    saturation is a fact rather than an inference, and callers (the
    exempt-purge command builder) must refuse to build an exemption list
    from a saturated scan."""
    relay_url = get_relay_url()
    relay_host = _hostname_of(relay_url)
    if relay_host is None:
        raise RuntimeError(
            "relay_url is not set — set it from the admin page and re-run"
        )

    counted = {}

    def scan_kind(kind):
        counted[str(kind)] = run_strfry_count({"kinds": [kind]}, timeout=AUTHOR_SCAN_DEADLINE)

        tally = {}  # pubkey -> latest created_at among matching events
        if progress_cb:
            progress_cb(0, total=SUBSCRIBER_SCAN_LIMIT)

        def on_event(ev):
            if not isinstance(ev, dict):
                return
            pk = ev.get("pubkey")
            created_at = ev.get("created_at")
            tags = ev.get("tags")
            if not is_hex64(pk) or not isinstance(created_at, int) or not isinstance(tags, list):
                return
            matched = any(
                isinstance(tag, list) and len(tag) >= 2 and tag[0] == "relay"
                and _hostname_of(tag[1]) == relay_host
                for tag in tags
            )
            if not matched:
                return
            if pk not in tally or created_at > tally[pk]:
                tally[pk] = created_at

        events_read = run_strfry_scan_streaming(
            {"kinds": [kind], "limit": SUBSCRIBER_SCAN_LIMIT}, on_event,
            timeout=AUTHOR_SCAN_DEADLINE, on_progress=progress_cb,
        )
        return tally, events_read >= SUBSCRIBER_SCAN_LIMIT

    dm_tally, dm_saturated = scan_kind(10050)
    general_tally, general_saturated = scan_kind(10002)

    # giftwrap_count cross-references the last COMPLETED recipients scan,
    # which may be older than this scan or (giftwrap_count: null) may never
    # have run at all — never presented as a fresh number.
    recipients_ever_scanned = _recipients_cache.get("scanned_at") is not None
    recipient_counts = {r["pubkey"]: r["count"] for r in _recipients_cache.get("recipients", [])}

    def build_rows(tally):
        rows = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
        to_resolve = [pk for pk, _ in rows[:NAME_RESOLVE_MAX]]
        try:
            profiles = resolve_profiles(to_resolve)
        except Exception as e:
            log(f"server86: subscriber name resolution failed: {e}")
            profiles = {}
        out = []
        for pk, listed_at in rows:
            try:
                npub = bech32.npub_encode(pk)
            except (ValueError, TypeError):
                continue
            profile = profiles.get(pk) or {}
            out.append({
                "pubkey": pk, "npub": npub,
                "name": profile.get("name"), "nip05": profile.get("nip05"),
                "listed_at": listed_at,
                "giftwrap_count": recipient_counts.get(pk) if recipients_ever_scanned else None,
            })
        return out

    return {
        "scanned_at": int(time.time()),
        "relay_url": relay_url,
        "saturated": dm_saturated or general_saturated,
        "scan_limit": SUBSCRIBER_SCAN_LIMIT,
        "counted": counted,
        "warning": None,
        "error": None,
        "subscribers": build_rows(dm_tally),
        "general_subscribers": build_rows(general_tally),
    }


_subscribers_job = {"status": "idle", "started_at": None, "progress": None, "total": None, "rate": None, "eta": None}
_subscribers_cache = _load_cache_or(SUBSCRIBERS_CACHE_PATH, _empty_subscribers_result())
_JOB_REGISTRY["subscribers"] = _subscribers_job


def start_subscribers_scan():
    def run():
        _run_scan_job(
            "subscribers", _subscribers_job, _subscribers_cache,
            SUBSCRIBERS_CACHE_PATH, compute_subscribers,
        )
    return _start_scan_job("subscribers", _subscribers_job, run)


def get_subscribers_status():
    return _scan_status("subscribers", _subscribers_job, _subscribers_cache)


# --- relay report: exact totals + the one unlimited scan -------------------
# /api/report/totals answers "how big is the AUTHOR_SCAN_KINDS gap" with
# three index counts — seconds even on a large relay, no window, no
# `saturated`, because a `--count` is simply exact. /api/report/walk is the
# one scan in the project allowed to be genuinely unbounded (see CLAUDE.md's
# bounding-rule carve-out): it streams the whole database once for aggregate
# figures only — a distinct-author count and a per-kind histogram — never a
# list an operator could act on. Both records live in one report-cache.json,
# written wholesale per record, with independent `scanned_at` stamps.

def _empty_report_totals():
    return {
        "scanned_at": None, "duration": None, "total_events": None,
        "giftwrap_events": None, "giftwrap_share": None,
        "allowlist_events": None, "gap_events": None, "gap_share": None,
        "gap_level": None, "needs_walk": False,
        "gap_alarm_kind": None, "gap_alarm_events": None,
        "warning": None, "error": None,
    }


def _empty_report_walk():
    return {
        "scanned_at": None, "duration": None, "rate": None, "events_read": None,
        "distinct_authors": None, "distinct_authors_nongiftwrap": None,
        "distinct_authors_giftwrap": None, "kinds": {}, "unlisted_kinds": {},
        "unlisted_total": None, "unlisted_kind_count": None,
        "expired_events": None,
        "warning": None, "error": None,
    }


def _compute_gap_level(gap_share, totals_scanned_at, walk_record):
    """See CLAUDE.md 'Auditing the allowlist — two questions, two
    thresholds'. Below GAP_NOTICE_SHARE the gap isn't worth a sentence.
    At or above it, only a walk's per-kind composition can tell size apart
    from actionability — a `--count` alone cannot, since Nostr filters have
    neither negation nor group-by — and only when that walk is at least as
    recent as the totals record being evaluated; a composition from a month
    ago paired with a size from a minute ago is not one measurement, so a
    totals-only refresh always reads back down to 'notice' until the walk
    is re-run alongside it."""
    if gap_share < GAP_NOTICE_SHARE:
        return {"gap_level": "ok", "needs_walk": False, "gap_alarm_kind": None, "gap_alarm_events": None}

    walk_scanned_at = walk_record.get("scanned_at") if walk_record else None
    if walk_scanned_at is None or walk_scanned_at < totals_scanned_at:
        return {"gap_level": "notice", "needs_walk": True, "gap_alarm_kind": None, "gap_alarm_events": None}

    events_read = walk_record.get("events_read") or 0
    giftwrap = walk_record.get("distinct_authors_giftwrap") or 0
    non_giftwrap = events_read - giftwrap
    unlisted = walk_record.get("unlisted_kinds") or {}
    if non_giftwrap > 0 and unlisted:
        worst_kind, worst_count = max(unlisted.items(), key=lambda kv: kv[1])
        if (worst_count / non_giftwrap) >= KIND_ALARM_SHARE:
            return {
                "gap_level": "stale", "needs_walk": False,
                "gap_alarm_kind": int(worst_kind), "gap_alarm_events": worst_count,
            }
    return {"gap_level": "notice", "needs_walk": False, "gap_alarm_kind": None, "gap_alarm_events": None}


def _load_report_cache():
    data = _load_cache_or(REPORT_CACHE_PATH, {})
    totals = data.get("totals")
    walk = data.get("walk")
    return {
        "totals": totals if isinstance(totals, dict) else _empty_report_totals(),
        "walk": walk if isinstance(walk, dict) else _empty_report_walk(),
    }


def compute_report_totals(progress_cb=None):
    """Three `scan --count` calls, no event bodies — exact, index-only, and
    seconds even on a large relay. The fourth figure (`gap_events`) is pure
    arithmetic on the other three; there is no fourth call. This is the
    AUTHOR_SCAN_KINDS staleness check from CLAUDE.md's "Auditing the
    allowlist", promoted from a test.sh-only assertion to a number an
    operator can press a button to see."""
    start = time.monotonic()
    total_events = run_strfry_count({}, timeout=AUTHOR_SCAN_DEADLINE)
    giftwrap_events = run_strfry_count({"kinds": [1059]}, timeout=AUTHOR_SCAN_DEADLINE)
    allowlist_events = run_strfry_count({"kinds": list(AUTHOR_SCAN_KINDS)}, timeout=AUTHOR_SCAN_DEADLINE)
    duration = time.monotonic() - start

    non_giftwrap = total_events - giftwrap_events
    gap_events = total_events - allowlist_events - giftwrap_events
    scanned_at = int(time.time())
    gap_share = (gap_events / non_giftwrap) if non_giftwrap > 0 else 0.0
    result = {
        "scanned_at": scanned_at,
        "duration": duration,
        "total_events": total_events,
        "giftwrap_events": giftwrap_events,
        "giftwrap_share": (giftwrap_events / total_events) if total_events > 0 else 0.0,
        "allowlist_events": allowlist_events,
        "gap_events": gap_events,
        "gap_share": gap_share,
        "warning": None,
        "error": None,
    }
    result.update(_compute_gap_level(gap_share, scanned_at, _report_cache["walk"]))
    return result


def compute_report_walk(progress_cb=None):
    """The one genuinely unbounded scan in the project. Runs `--count '{}'`
    first to learn the denominator (and seed it into progress_cb as `total`
    before streaming starts, so a polling GET can show a percentage and an
    ETA from the first progress tick), then streams every event exactly
    once. All-or-nothing: on success it returns BOTH a totals record and a
    walk record (the `--count` this needed anyway, plus the kind tally the
    stream produces as a byproduct, refresh the totals record without a
    second full-database pass); on hitting REPORT_WALK_DEADLINE it raises,
    and the caller (_run_scan_job) discards everything read and preserves
    whatever was there before.

    Distinct authors are NOT stored as 64-char hex strings — on a
    DM-carrying relay the distinct-pubkey set approaches the event count,
    and a few million hex strings is several hundred MB inside the
    operator's live relay container. Two measures: kind 1059 (gift wraps)
    is skipped entirely, since NIP-17 gift wraps are signed by a fresh key
    per message by specification, so their distinct-author count IS their
    event count, already known exactly from the tally; every other author
    is folded into a set of REPORT_AUTHOR_KEY_BYTES-byte prefixes, whose
    collision probability stays far below the honesty threshold of every
    other number on this page even past a million distinct authors."""
    count_start = time.monotonic()
    total_events = run_strfry_count({}, timeout=REPORT_WALK_DEADLINE)
    if progress_cb:
        progress_cb(0, total=total_events)
    count_duration = time.monotonic() - count_start

    kind_counts = {}
    distinct_prefixes = set()
    giftwrap_count = 0
    expired_count = 0
    walk_now = int(time.time())

    def on_event(ev):
        nonlocal giftwrap_count, expired_count
        if not isinstance(ev, dict):
            return
        kind = ev.get("kind")
        if isinstance(kind, int):
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        tags = ev.get("tags")
        if isinstance(tags, list):
            exp = event_expiration(tags)
            if exp is not None and exp < walk_now:
                expired_count += 1
        if kind == 1059:
            giftwrap_count += 1
            return
        pk = ev.get("pubkey")
        if isinstance(pk, str) and is_hex64(pk):
            distinct_prefixes.add(bytes.fromhex(pk)[:REPORT_AUTHOR_KEY_BYTES])

    stream_start = time.monotonic()
    events_read = run_strfry_scan_streaming(
        {}, on_event, timeout=REPORT_WALK_DEADLINE,
        on_progress=(lambda c: progress_cb(c)) if progress_cb else None,
    )
    duration = count_duration + (time.monotonic() - stream_start)
    rate = events_read / duration if duration > 0 else None

    allowlist_kinds_set = set(AUTHOR_SCAN_KINDS)
    unlisted_kinds = {
        k: c for k, c in kind_counts.items()
        if k not in allowlist_kinds_set and k != 1059
    }

    giftwrap_events = kind_counts.get(1059, 0)
    allowlist_events = sum(c for k, c in kind_counts.items() if k in allowlist_kinds_set)
    non_giftwrap = total_events - giftwrap_events
    gap_events = total_events - allowlist_events - giftwrap_events

    gap_share = (gap_events / non_giftwrap) if non_giftwrap > 0 else 0.0
    totals_record = {
        "scanned_at": int(time.time()),
        "duration": count_duration,
        "total_events": total_events,
        "giftwrap_events": giftwrap_events,
        "giftwrap_share": (giftwrap_events / total_events) if total_events > 0 else 0.0,
        "allowlist_events": allowlist_events,
        "gap_events": gap_events,
        "gap_share": gap_share,
        "warning": None,
        "error": None,
    }
    walk_record = {
        "scanned_at": int(time.time()),
        "duration": duration,
        "rate": rate,
        "events_read": events_read,
        "distinct_authors": len(distinct_prefixes) + giftwrap_count,
        "distinct_authors_nongiftwrap": len(distinct_prefixes),
        "distinct_authors_giftwrap": giftwrap_count,
        "kinds": kind_counts,
        "unlisted_kinds": unlisted_kinds,
        "unlisted_total": sum(unlisted_kinds.values()),
        "unlisted_kind_count": len(unlisted_kinds),
        "expired_events": expired_count,
        "warning": None,
        "error": None,
    }
    totals_record.update(_compute_gap_level(gap_share, totals_record["scanned_at"], walk_record))
    return {"totals": totals_record, "walk": walk_record}


_report_job = {
    "status": "idle", "job": None, "started_at": None,
    "progress": None, "total": None, "rate": None, "eta": None,
}
_report_cache = _load_report_cache()
# Both report job names resolve to the same _report_job dict: totals and
# walk share the report resource and therefore share the report entry in
# _JOB_REGISTRY — only one of the two can ever run at a time anyway, since
# both take the same global _scan_lock like every other scan here.
_JOB_REGISTRY["report-totals"] = _report_job
_JOB_REGISTRY["report-walk"] = _report_job


def _run_report_job(job_name, compute_fn):
    """Same shape as _run_scan_job, specialized for the two-record report
    cache: a totals-only run replaces just the totals record; a walk run
    replaces both (see compute_report_walk). A failure attaches `error`
    (never `scanned_at`) to the record THIS run was attempting and leaves
    the other completely untouched — a walk timeout must never even brush
    the totals record."""
    progress_cb = _make_progress_cb(_report_job)
    try:
        result = compute_fn(progress_cb)
    except Exception as e:
        error = f"report {job_name} failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {error}")
        with _scan_lock:
            key = "totals" if job_name == "totals" else "walk"
            _report_cache[key]["error"] = error
            _report_job["status"] = "idle"
            _report_job["job"] = None
            _report_job["progress"] = None
            _report_job["total"] = None
            _report_job["rate"] = None
            _report_job["eta"] = None
            _active_scan["name"] = None
        return

    if job_name == "totals":
        new_totals, new_walk = result, dict(_report_cache["walk"])
    else:
        new_totals, new_walk = result["totals"], result["walk"]

    try:
        _save_cache_atomic(REPORT_CACHE_PATH, {"totals": new_totals, "walk": new_walk})
    except OSError as e:
        log(f"server86: failed to persist report-cache.json: {e}")

    with _scan_lock:
        _report_cache["totals"] = new_totals
        _report_cache["walk"] = new_walk
        _report_job["status"] = "idle"
        _report_job["job"] = None
        _report_job["progress"] = None
        _report_job["total"] = None
        _report_job["rate"] = None
        _report_job["eta"] = None
        _active_scan["name"] = None


def start_report_totals_scan():
    def run():
        _run_report_job("totals", compute_report_totals)
    return _start_scan_job("report-totals", _report_job, run, extra_job_fields={"job": "totals"})


def start_report_walk_scan():
    def run():
        _run_report_job("walk", compute_report_walk)
    return _start_scan_job("report-walk", _report_job, run, extra_job_fields={"job": "walk"})


def get_report_status():
    """GET /api/report: never scans, never starts one. `totals`/`walk` are
    null only in the genuine 'never run' state — no result and no error
    yet — rather than a skeleton dict with every field null, so the client
    can tell 'no result yet' apart from 'a result with nulls in it' with
    one falsy check. A record that failed before ever succeeding (`error`
    set, `scanned_at` still null) is NOT collapsed back to null — the
    client still needs to see why it failed."""
    with _scan_lock:
        job = dict(_report_job)
        totals = dict(_report_cache["totals"])
        walk = dict(_report_cache["walk"])
        running = _active_scan["name"]
    blocked_by = running if running not in (None, "report-totals", "report-walk") else None
    return {
        "status": job["status"],
        "job": job["job"],
        "started_at": job["started_at"],
        "progress": job["progress"],
        "total": job["total"],
        "rate": job["rate"],
        "eta": job["eta"],
        "blocked_by": blocked_by,
        "totals": totals if (totals.get("scanned_at") is not None or totals.get("error") is not None) else None,
        "walk": walk if (walk.get("scanned_at") is not None or walk.get("error") is not None) else None,
    }


def validate_relay_url_input(body):
    """Return (value, error) for a POST /api/relay-url body. `value` is
    None to CLEAR relay_url (an absent field or a blank/whitespace-only
    string are both treated as 'clear', not as errors) or the trimmed
    string to store. A non-string relay_url, or one with no parseable
    hostname, is rejected outright rather than silently stored — a typo
    that /api/subscribers can't match against anything is worse than the
    'not set' state, since it reads as configured while quietly matching
    nothing."""
    raw = body.get("relay_url")
    if raw is not None and not isinstance(raw, str):
        return None, "relay_url must be a string"
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None, None
    if not _hostname_of(value):
        return None, "could not parse a hostname out of that URL"
    return value, None


def validate_authors_scan_mode(body):
    """Return (mode, error) for a POST /api/authors/scan body. `mode`
    selects a named constant filter; the request may never supply a
    filter, a limit, or a kinds array directly — a name indexing a table
    readable in the source is auditable, a caller-supplied number or
    filter is not. On success `error` is None and `mode` is a key of
    AUTHOR_SCAN_MODES; on failure `mode` is None."""
    if "limit" in body or "kinds" in body:
        return None, "limit/kinds may not be supplied by the request"
    mode = body.get("mode")
    if mode not in AUTHOR_SCAN_MODES:
        return None, "unrecognized mode"
    return mode, None


def validate_reason_request(body):
    """Return (pubkeys, reason, mode, error) for a POST /api/reason body.
    On success `error` is None; on failure the first three are None. A
    reason over REASON_MAX_LEN is rejected outright, never truncated."""
    pubkeys = body.get("pubkeys")
    reason = body.get("reason")
    mode = body.get("mode")
    if not isinstance(pubkeys, list) or not all(is_hex64(pk) for pk in pubkeys):
        return None, None, None, "malformed pubkeys list"
    if not isinstance(reason, str) or len(reason) > REASON_MAX_LEN:
        return None, None, None, "reason missing or exceeds REASON_MAX_LEN"
    if mode not in ("replace", "append"):
        return None, None, None, "mode must be 'replace' or 'append'"
    return pubkeys, reason, mode, None


# --- single-pubkey profile -------------------------------------------------

def _report_type_for_target(tags, target_pubkey):
    """Same dual lookup plugin86.py's hot path uses when a report names
    multiple pubkeys: `target_pubkey`'s own `p` tag third element if
    present, else the first `e`/`a` tag's third element, else None.
    Duplicated here rather than shared via lib86 — plugin86.py's hot path
    is the most sensitive file in this project, and this is three lines,
    not worth the risk of touching it for a read-only admin page."""
    if not isinstance(tags, list):
        return None
    fallback = None
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 3 and tag[0] in ("e", "a") and isinstance(tag[2], str):
            fallback = tag[2]
            break
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p" and tag[1] == target_pubkey:
            if len(tag) >= 3 and isinstance(tag[2], str):
                return tag[2]
            return fallback
    return fallback


def _build_event_preview(ev):
    """One event summarized for a profile's events list — shared by
    compute_profile's recent-events read and compute_profile_day's
    windowed one, so the two previews lists are the exact same shape.
    `reply` is only ever true/false for kind 1 (a NIP-10 reply/quote has
    at least one `e` tag; a top-level note has none) and None for every
    other kind, since 'reply vs note' is a kind-1-only distinction — the
    client falls back to its own kind-name table otherwise. `note` is the
    event's id as a note1... bech32 string so the client can link straight
    to njump without decoding anything itself; None if the id is missing
    or malformed."""
    kind = ev.get("kind")
    reply = None
    if kind == 1:
        tags = ev.get("tags")
        reply = isinstance(tags, list) and any(
            isinstance(tag, list) and len(tag) >= 1 and tag[0] == "e" for tag in tags
        )
    event_id = ev.get("id")
    note = None
    if isinstance(event_id, str) and is_hex64(event_id):
        try:
            note = bech32.note_encode(event_id)
        except (ValueError, TypeError):
            note = None
    content = ev.get("content")
    return {
        "kind": kind,
        "reply": reply,
        "created_at": ev.get("created_at"),
        "content": (content if isinstance(content, str) else "")[:280],
        "note": note,
    }


def compute_profile(pubkey_hex):
    """Everything server86 can say about ONE pubkey from the local
    database: three subprocesses, each bounded by the single author or by
    a constant. Admin-only because it scans; needs no button, because
    opening /profile?npub=... IS the deliberate act. A failure in any one
    subscan is reported in `warning` rather than failing the whole
    response — the other two are still worth showing."""
    warnings = []

    try:
        total_events = run_strfry_count({"authors": [pubkey_hex]}, timeout=SCAN_TIMEOUT)
    except Exception as e:
        log(f"server86: profile lifetime count failed for {pubkey_hex}: {e}")
        total_events = None
        warnings.append("lifetime event count failed")

    kinds = {}
    kinds_saturated = False
    previews = []
    profile_fields = None
    try:
        events = run_strfry_scan({"authors": [pubkey_hex], "limit": PROFILE_EVENT_LIMIT}, timeout=SCAN_TIMEOUT)
        # `limit` returns the newest matching events first (verified —
        # see CLAUDE.md's "strfry scan execution rules"), but sort
        # explicitly anyway so this doesn't depend on strfry's own
        # ordering staying newest-first within one response.
        events.sort(key=lambda ev: ev.get("created_at") or 0, reverse=True)
        kinds_saturated = len(events) >= PROFILE_EVENT_LIMIT
        for ev in events:
            kind = ev.get("kind")
            if not isinstance(kind, int):
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == 0 and profile_fields is None:
                try:
                    content = json.loads(ev.get("content", "{}"))
                except ValueError:
                    content = {}
                if not isinstance(content, dict):
                    content = {}
                profile_fields = {
                    "about": _clean_profile_field(content.get("about")),
                    "picture": _clean_profile_field(content.get("picture")),
                    "website": _clean_profile_field(content.get("website")),
                    "lud16": _clean_profile_field(content.get("lud16")),
                }
        for ev in events[:PROFILE_PREVIEW_MAX]:
            previews.append(_build_event_preview(ev))
    except Exception as e:
        log(f"server86: profile event scan failed for {pubkey_hex}: {e}")
        warnings.append("recent event scan failed")

    reports = []
    reports_saturated = False
    try:
        report_events = run_strfry_scan(
            {"kinds": [1984], "#p": [pubkey_hex], "limit": PROFILE_REPORT_LIMIT}, timeout=SCAN_TIMEOUT
        )
        report_events.sort(key=lambda ev: ev.get("created_at") or 0, reverse=True)
        reports_saturated = len(report_events) >= PROFILE_REPORT_LIMIT
        for ev in report_events:
            reporter = ev.get("pubkey")
            if not isinstance(reporter, str):
                continue
            try:
                reporter_npub = bech32.npub_encode(reporter)
            except (ValueError, TypeError):
                continue
            content = ev.get("content")
            reports.append({
                "reporter": reporter,
                "reporter_npub": reporter_npub,
                "report_type": _report_type_for_target(ev.get("tags"), pubkey_hex),
                "content": content if isinstance(content, str) else "",
                "created_at": ev.get("created_at"),
                "name": None,
                "nip05": None,
            })
    except Exception as e:
        log(f"server86: profile reports-against scan failed for {pubkey_hex}: {e}")
        warnings.append("reports-against scan failed")

    # Local-only name/nip05 per distinct reporter — the same batched kind-0
    # lookup (resolve_profiles) every other page uses, bounded by the
    # reporters actually on this page (<= PROFILE_REPORT_LIMIT, well under
    # NAME_RESOLVE_MAX). No outbound network access here; a reporter this
    # leaves unresolved stays eligible for the client's button-triggered
    # s86ResolveNamesExternally pass, same as authors.html's unresolved rows.
    distinct_reporters = list(dict.fromkeys(r["reporter"] for r in reports))
    if distinct_reporters:
        try:
            resolved_reporters = resolve_profiles(distinct_reporters)
        except Exception as e:
            log(f"server86: reporter name resolution failed for {pubkey_hex}: {e}")
            resolved_reporters = {}
        for r in reports:
            info = resolved_reporters.get(r["reporter"]) or {}
            r["name"] = info.get("name")
            r["nip05"] = info.get("nip05")

    return {
        "total_events": total_events,
        "kinds": kinds,
        "kinds_window": PROFILE_EVENT_LIMIT,
        "kinds_saturated": kinds_saturated,
        "profile": profile_fields,
        "previews": previews,
        "reports": reports,
        "reports_saturated": reports_saturated,
        "warning": "; ".join(warnings) if warnings else None,
    }


def build_profile_response(pubkey_hex):
    """Assemble the full POST /api/profile response: the three bounded
    scans from compute_profile() plus everything server86 already holds
    in memory with no scan at all — ban status, name/nip05, and this
    pubkey's rank in the last author-scan cache, if it appears there."""
    blacklist_data = blacklist.load()
    ban_entry = blacklist_data.get(pubkey_hex)
    banned = ban_entry is not None

    try:
        npub = bech32.npub_encode(pubkey_hex)
    except (ValueError, TypeError):
        npub = None

    try:
        resolved = resolve_profiles([pubkey_hex]).get(pubkey_hex) or {}
    except Exception as e:
        log(f"server86: profile name resolution failed for {pubkey_hex}: {e}")
        resolved = {}

    scan_rank = None
    scan_count = None
    with _scan_lock:
        cached_authors = list(_authors_cache.get("authors", []))
    for i, a in enumerate(cached_authors):
        if a.get("pubkey") == pubkey_hex:
            scan_rank = i + 1
            scan_count = a.get("count")
            break

    result = compute_profile(pubkey_hex)
    result.update({
        "pubkey": pubkey_hex,
        "npub": npub,
        "name": resolved.get("name"),
        "nip05": resolved.get("nip05"),
        "banned": banned,
        "ban": dict(ban_entry) if ban_entry else None,
        "scan_rank": scan_rank,
        "scan_count": scan_count,
    })
    return result


def validate_profile_day_request(body):
    """Return (pubkey_hex, since, until, error) for a POST /api/profile/day
    body. The client sends a calendar date, never a raw time window — the
    [since, until) bounds are computed HERE, server-side, as one UTC day, so
    the client can never hand this endpoint an arbitrarily large span. Every
    other timestamp on this page (s86FormatDate) is already rendered in UTC,
    so the picked date has to mean the same calendar day the server scans,
    not whatever the browser's local timezone would offset it to."""
    pubkey_hex = body.get("pubkey")
    date_str = body.get("date")
    if not is_hex64(pubkey_hex):
        return None, None, None, "malformed pubkey"
    if not isinstance(date_str, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return None, None, None, "malformed date (expected YYYY-MM-DD)"
    try:
        parsed = time.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None, None, None, "invalid date"
    since = calendar.timegm(parsed)
    until = since + 86400
    return pubkey_hex, since, until, None


def compute_profile_day(pubkey_hex, since, until):
    """One pubkey's events within [since, until) — a single UTC calendar day,
    already validated and computed by validate_profile_day_request(). Same
    shape and same PROFILE_DAY_EVENTS_MAX bound as compute_profile()'s own
    recent-events previews, just windowed by date instead of by recency;
    `truncated` says so explicitly rather than letting a full day of events
    look like a complete one. Admin-only because it scans; needs no button
    beyond picking the date, same reasoning as opening /profile itself."""
    events = run_strfry_scan(
        {"authors": [pubkey_hex], "since": since, "until": until, "limit": PROFILE_DAY_EVENTS_MAX},
        timeout=SCAN_TIMEOUT,
    )
    events.sort(key=lambda ev: ev.get("created_at") or 0, reverse=True)
    truncated = len(events) >= PROFILE_DAY_EVENTS_MAX
    previews = [_build_event_preview(ev) for ev in events]
    return {"previews": previews, "truncated": truncated}


def validate_profile_new_request(body):
    """Return (pubkey_hex, since, error) for a POST /api/profile/new body.
    `since` is the caller's own newest-known created_at PLUS ONE — an
    exclusive lower bound the client computes so the boundary event it
    already has is never double-counted — never a raw relative offset the
    server would have to interpret."""
    pubkey_hex = body.get("pubkey")
    since = body.get("since")
    if not is_hex64(pubkey_hex):
        return None, None, "malformed pubkey"
    if not isinstance(since, int) or isinstance(since, bool) or since < 0:
        return None, None, "malformed since (expected a non-negative integer)"
    return pubkey_hex, since, None


def compute_profile_new_events(pubkey_hex, since):
    """Events with created_at >= `since`, bounded by PROFILE_EVENT_LIMIT —
    the cheap alternative to a full compute_profile() recompute when the
    profile page's '>' button is pressed at the latest day. Reuses
    PROFILE_EVENT_LIMIT (the same bound compute_profile's own kind-tally
    scan uses) as the definition of "one page": `truncated` is True when
    that many new events exist, meaning the true count is unknown — there
    could be more past the bound — and the caller must not treat
    `kinds_delta`/`len(previews)` as exact in that case. When truncated is
    False, every new event was actually read, so total_events and each
    kinds[] entry can be incremented by an EXACT delta instead of
    re-scanning the whole pubkey."""
    events = run_strfry_scan(
        {"authors": [pubkey_hex], "since": since, "limit": PROFILE_EVENT_LIMIT}, timeout=SCAN_TIMEOUT,
    )
    events.sort(key=lambda ev: ev.get("created_at") or 0, reverse=True)
    truncated = len(events) >= PROFILE_EVENT_LIMIT
    kinds_delta = {}
    for ev in events:
        kind = ev.get("kind")
        if isinstance(kind, int):
            kinds_delta[kind] = kinds_delta.get(kind, 0) + 1
    previews = [_build_event_preview(ev) for ev in events]
    return {"previews": previews, "truncated": truncated, "kinds_delta": kinds_delta}


# --- domain roster lookup ---------------------------------------------------

def validate_pubkeys_lookup_request(body):
    """Return (pubkeys, domain, error) for a POST /api/pubkeys/lookup
    body. A body over DOMAIN_LOOKUP_MAX is rejected outright — never
    silently truncated, since the admin is about to bulk-act on this
    list. Any malformed pubkey is rejected the same way rather than
    skipped: a roster row that vanished between fetch and render is worse
    than an error."""
    pubkeys = body.get("pubkeys")
    domain = body.get("domain")
    if not isinstance(pubkeys, list) or len(pubkeys) == 0:
        return None, None, "malformed pubkeys list"
    if len(pubkeys) > DOMAIN_LOOKUP_MAX:
        return None, None, f"too many pubkeys (max {DOMAIN_LOOKUP_MAX})"
    if not all(is_hex64(pk) for pk in pubkeys):
        return None, None, "malformed pubkey in list"
    if not isinstance(domain, str) or not domain.strip():
        return None, None, "malformed domain"
    return pubkeys, domain.strip(), None


def compute_pubkeys_lookup(pubkeys, domain):
    """Answers 'what do you know about these pubkeys' for a domain roster
    the browser already fetched. The posted list IS the authors bound:
    one batched local kind-0 scan (via the shared resolve_profiles(), so
    a banned pubkey's stored name still wins over a fresh scan) capped at
    NAME_RESOLVE_MAX, everything else a dict lookup against the
    blacklist and the author-scan cache. No scan beyond that one batch."""
    blacklist_data = blacklist.load()
    with _scan_lock:
        scan_counts = {a["pubkey"]: a.get("count") for a in _authors_cache.get("authors", [])}

    try:
        profiles = resolve_profiles(pubkeys[:NAME_RESOLVE_MAX])
    except Exception as e:
        log(f"server86: pubkeys/lookup name resolution failed: {e}")
        profiles = {}

    domain_suffix = "@" + domain.lower()
    results = []
    for pk in pubkeys:
        try:
            npub = bech32.npub_encode(pk)
        except (ValueError, TypeError):
            continue
        entry = blacklist_data.get(pk)
        profile = profiles.get(pk) or {}
        nip05 = profile.get("nip05")
        results.append({
            "pubkey": pk, "npub": npub,
            "name": profile.get("name"), "nip05": nip05,
            "banned": entry is not None,
            "ban_reason": entry.get("reason") if entry else None,
            "scan_count": scan_counts.get(pk),
            # A comparison of two unverified claims, never proof of
            # anything: true when this pubkey's OWN kind-0 nip05 ends in
            # @<domain>, the cross-check that flags a stale roster entry
            # (listed but doesn't claim it back) versus an impersonator
            # (claims it but the roster doesn't list them — invisible
            # here by construction; see the author list's nip05 filter).
            "claims_domain": bool(nip05) and nip05.lower().endswith(domain_suffix),
        })
    return {"domain": domain, "results": results}


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

        if path == "/api/activity":
            self._send_json(200, get_activity_feed())
            return

        if path == "/api/stats":
            self._send_json(200, get_stats_snapshot())
            return

        if path == "/api/userlist":
            self._send_json(200, get_userlist_snapshot())
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
            self._send_json(200, get_authors_status())
            return

        if path == "/api/recipients":
            self._send_json(200, get_recipients_status())
            return

        if path == "/api/subscribers":
            self._send_json(200, get_subscribers_status())
            return

        if path == "/api/relay-url":
            self._send_json(200, {"relay_url": get_relay_url()})
            return

        if path == "/api/report":
            self._send_json(200, get_report_status())
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in (
            "/api/unban", "/api/ban", "/api/authors/scan", "/api/names",
            "/api/recipients", "/api/subscribers", "/api/reason", "/api/profile",
            "/api/profile/day", "/api/profile/new", "/api/pubkeys/lookup", "/api/report/totals",
            "/api/report/walk", "/api/relay-url",
            "/api/terminal", "/api/audit", "/api/audit/undo",
        ):
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
            mode, err = validate_authors_scan_mode(body)
            if err:
                self._send_json(400, {"error": err})
                return
            status = start_authors_scan(mode)
            self._send_json(202, status)
            return

        if path == "/api/recipients":
            status = start_recipients_scan()
            self._send_json(202, status)
            return

        if path == "/api/subscribers":
            status = start_subscribers_scan()
            self._send_json(202, status)
            return

        if path == "/api/relay-url":
            value, err = validate_relay_url_input(body)
            if err:
                self._send_json(400, {"error": err})
                return
            set_relay_url(value)
            self._send_json(200, {"ok": True, "relay_url": get_relay_url()})
            return

        if path == "/api/report/totals":
            status = start_report_totals_scan()
            self._send_json(202, status)
            return

        if path == "/api/report/walk":
            status = start_report_walk_scan()
            self._send_json(202, status)
            return

        if path == "/api/terminal":
            raw_cmd = body.get("command")
            result = run_terminal_command(raw_cmd if isinstance(raw_cmd, str) else "")
            self._send_json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/audit":
            query = body.get("q") if isinstance(body.get("q"), str) else ""
            self._send_json(200, {"records": get_audit_records(query)})
            return

        if path == "/api/audit/undo":
            record_id = body.get("id")
            if not isinstance(record_id, str) or not record_id:
                self._send_json(400, {"error": "missing audit record id"})
                return
            with _audit_lock:
                found = None
                for r in _audit_log.get("records") or []:
                    if r.get("id") == record_id:
                        found = dict(r)
                        break
            if found is None:
                self._send_json(404, {"error": "audit record not found"})
                return
            if found.get("undone"):
                self._send_json(400, {"error": "already undone"})
                return
            rtype = found.get("type")
            pubkeys = found.get("pubkeys") or []
            if rtype == "ban":
                removed = blacklist.remove(pubkeys)
                audit_mark_undone(record_id)
                audit_append({
                    "type": "unban", "actor": auth.get("pubkey"),
                    "pubkeys": removed, "via": "audit-undo", "undo_of": record_id,
                })
                self._send_json(200, {"ok": True, "removed": removed})
                return
            if rtype == "unban":
                now = int(time.time())
                added = []
                for pk in pubkeys:
                    if not is_hex64(pk):
                        continue
                    reason = ""
                    for e in (found.get("entries") or []):
                        if e.get("pubkey") == pk:
                            reason = e.get("reason") or ""
                            break
                    if blacklist.add(
                        pk, banned_at=now, report_event_id=None, reason=reason,
                        report_type="manual", admin_pubkey_hex=cfg["admin_pubkey_hex"],
                    ):
                        added.append(pk)
                audit_mark_undone(record_id)
                audit_append({
                    "type": "ban", "actor": auth.get("pubkey"),
                    "pubkeys": added, "via": "audit-undo", "undo_of": record_id,
                })
                self._send_json(200, {"ok": True, "added": added})
                return
            if rtype == "reason":
                # Restore prior reasons from the snapshot when present.
                entries = found.get("entries") or []
                if not entries:
                    self._send_json(400, {"error": "undo unavailable for this reason record"})
                    return
                restored = 0
                for e in entries:
                    pk = e.get("pubkey")
                    old = e.get("old_reason")
                    if not is_hex64(pk):
                        continue
                    blacklist.set_reasons([pk], old or "", "replace", now=int(time.time()))
                    restored += 1
                audit_mark_undone(record_id)
                self._send_json(200, {"ok": True, "restored": restored})
                return
            self._send_json(400, {"error": f"cannot undo type {rtype!r}"})
            return

        if path == "/api/reason":
            pubkeys, reason, mode, err = validate_reason_request(body)
            if err:
                self._send_json(400, {"error": err})
                return
            updated, skipped = blacklist.set_reasons(pubkeys, reason, mode, now=int(time.time()))
            if updated:
                audit_append({
                    "type": "reason",
                    "actor": auth.get("pubkey"),
                    "pubkeys": [u["pubkey"] for u in updated],
                    "reason": reason,
                    "mode": mode,
                    "entries": [
                        {"pubkey": u["pubkey"], "old_reason": u.get("old_reason")}
                        for u in updated[:REASON_UNDO_MAX]
                    ] if len(updated) <= REASON_UNDO_MAX else None,
                    "count": len(updated),
                })
            self._send_json(200, {"ok": True, "updated": updated, "skipped": skipped})
            return

        if path == "/api/profile":
            raw_pk = body.get("pubkey")
            pubkey_hex = None
            if is_hex64(raw_pk):
                pubkey_hex = raw_pk
            elif isinstance(raw_pk, str):
                try:
                    pubkey_hex = bech32.npub_decode(raw_pk)
                except (ValueError, TypeError):
                    pubkey_hex = None
            if not is_hex64(pubkey_hex):
                self._send_json(400, {"error": "malformed pubkey"})
                return
            self._send_json(200, build_profile_response(pubkey_hex))
            return

        if path == "/api/profile/day":
            pubkey_hex, since, until, err = validate_profile_day_request(body)
            if err:
                self._send_json(400, {"error": err})
                return
            try:
                self._send_json(200, compute_profile_day(pubkey_hex, since, until))
            except Exception as e:
                log(f"server86: profile day-events scan failed for {pubkey_hex}: {e}")
                self._send_json(502, {"error": "day-events scan failed"})
            return

        if path == "/api/profile/new":
            pubkey_hex, since, err = validate_profile_new_request(body)
            if err:
                self._send_json(400, {"error": err})
                return
            try:
                self._send_json(200, compute_profile_new_events(pubkey_hex, since))
            except Exception as e:
                log(f"server86: profile new-events scan failed for {pubkey_hex}: {e}")
                self._send_json(502, {"error": "new-events scan failed"})
            return

        if path == "/api/pubkeys/lookup":
            pubkeys, domain, err = validate_pubkeys_lookup_request(body)
            if err:
                self._send_json(400, {"error": err})
                return
            self._send_json(200, compute_pubkeys_lookup(pubkeys, domain))
            return

        if path == "/api/names":
            queried_raw = body.get("queried")
            events_raw = body.get("events")
            if not isinstance(queried_raw, list) or not isinstance(events_raw, list):
                self._send_json(400, {"error": "malformed request body"})
                return

            queried = [pk for pk in queried_raw if is_hex64(pk)]
            queried_set = set(queried)
            # The accept-bound is "banned OR present in the current
            # author-scan cache" — still a bound assembled from server-held
            # state, never an arbitrary client-supplied pubkey.
            acceptable_pubkeys = set(blacklist.load().keys()) | authors_scan_pubkeys()

            hits = {}
            newest_created_at = {}
            for ev in events_raw:
                verified = verify_kind0_event(ev, queried_set, acceptable_pubkeys)
                if verified is None:
                    continue
                pubkey, created_at, name, nip05 = verified
                if pubkey in newest_created_at and newest_created_at[pubkey] >= created_at:
                    continue
                newest_created_at[pubkey] = created_at
                hits[pubkey] = {"name": name, "nip05": nip05}

            # Storage routes by ban status, never by request: re-check
            # fresh, right before writing — the lookup took seconds and
            # the admin may have banned or unbanned someone meanwhile.
            now = int(time.time())
            fresh_banned = set(blacklist.load().keys())
            banned_queried = [pk for pk in queried if pk in fresh_banned]
            cache_queried = [pk for pk in queried if pk not in fresh_banned]
            banned_hits = {pk: h for pk, h in hits.items() if pk in fresh_banned}
            cache_hits = {pk: h for pk, h in hits.items() if pk not in fresh_banned}

            stamped = blacklist.set_names(banned_hits, banned_queried, now=now)
            stamped += namecache.set_external(cache_hits, cache_queried, now=now, max_entries=NAME_CACHE_MAX)

            stamped_set = set(stamped)
            named = sorted(pk for pk in hits if pk in stamped_set)
            self._send_json(200, {"ok": True, "named": named, "stamped": len(stamped)})
            return

        if path == "/api/unban":
            pubkeys = body.get("pubkeys")
            if not isinstance(pubkeys, list) or not all(is_hex64(pk) for pk in pubkeys):
                self._send_json(400, {"error": "malformed pubkeys list"})
                return

            # Snapshot ban rows before removal so audit undo can re-ban.
            data = blacklist.load()
            entries_snap = []
            for pk in pubkeys:
                info = data.get(pk) or {}
                entries_snap.append({
                    "pubkey": pk,
                    "reason": info.get("reason") or "",
                })
            removed = blacklist.remove(pubkeys)
            if removed:
                audit_append({
                    "type": "unban",
                    "actor": auth.get("pubkey"),
                    "pubkeys": removed,
                    "entries": [e for e in entries_snap if e["pubkey"] in set(removed)],
                })
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
        reasons_by_pk = {}
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
                reasons_by_pk[pubkey] = reason
            else:
                skipped.append(raw_pk)

        if added:
            audit_append({
                "type": "ban",
                "actor": auth.get("pubkey"),
                "pubkeys": added,
                "entries": [{"pubkey": pk, "reason": reasons_by_pk.get(pk, "")} for pk in added],
            })
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
