#!/usr/bin/env python3
"""stdlib admin HTTP server for strfry-86.

Spawned (detached) by plugin86.py. Enforces singleton via port-bind: if the
configured port is already taken, this process exits 0 silently, so repeated
spawns from the plugin are harmless.

Routes:
  GET  /                  -> bans.html (public ban list)
  GET  /authors           -> authors.html (admin-only active-author page)
  GET  /profile           -> profile.html (admin-only single-pubkey detail page)
  GET  /domain            -> domain.html (admin-only nip-05 domain roster page)
  GET  /common86.js       -> shared client JS for all pages
  GET  /api/banned        -> public read of the ban list
  GET  /api/authors       -> public read of the last author-scan result (never scans)
  POST /api/authors/scan  -> NIP-98 authenticated: run exactly one bounded scan
  GET  /api/recipients    -> public read of the last gift-wrap recipient tally (never scans)
  POST /api/recipients    -> NIP-98 authenticated: run exactly one bounded scan
  GET  /api/subscribers   -> public read of the last DM/general relay-list search (never scans)
  POST /api/subscribers   -> NIP-98 authenticated: run exactly one bounded scan
  POST /api/unban         -> NIP-98 authenticated unban
  POST /api/ban           -> NIP-98 authenticated manual ban
  POST /api/reason        -> NIP-98 authenticated: bulk-edit reason on existing bans
  POST /api/profile       -> NIP-98 authenticated: everything known about one pubkey
  POST /api/pubkeys/lookup -> NIP-98 authenticated: what's known about a domain's roster
  POST /api/names         -> NIP-98 authenticated: intake for externally-verified profile names
"""

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
DOMAIN_LOOKUP_MAX = 1000        # pubkeys accepted in one /api/pubkeys/lookup body
RENDER_MAX = 500                # list rows rendered client-side before truncation

_strfry_bin_path = None
_strfry_bin_checked = False

_relay_cwd_pid = None
_relay_cwd_path = None

# The author list is never recomputed on a timer or because it went stale
# — only POST /api/authors/scan (an explicit admin button press) starts a
# scan, which then runs in a background thread so the request never blocks.
_authors_lock = threading.Lock()
_authors_job = {"status": "idle", "mode": None, "started_at": None, "progress": None}
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
# DM/general-relay-list subscribers): validate auth, start ONE bounded scan
# in a background thread, return 202 immediately, and let the page poll the
# matching GET until status returns to idle. One code path for all three —
# not a fast one and a slow one that behave differently under failure.
#
# Each resource still owns its own lock/job/cache module globals (so a
# stalled recipients scan can never block an author-scan poll, and so
# direct access like `server86._authors_lock` / `server86._authors_cache`
# keeps working), but every one of them runs through the three functions
# below. `job` and `cache` are mutated IN PLACE rather than rebound to a
# new dict, so the caller's module-level dict object — the thing readers
# actually hold a reference to — is always what gets updated.

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


def _run_scan_job(name, lock, job, cache, cache_path, compute_fn):
    """Background-thread body for one async scan. `compute_fn(progress_cb)`
    returns the result dict on success. On failure the previous cache is
    preserved (mutated with a `warning` attached) and NEVER replaced with
    a partial result — a scan that hit its deadline is an error, not a
    smaller answer."""
    def progress_cb(count):
        with lock:
            job["progress"] = count

    try:
        result = compute_fn(progress_cb)
    except Exception as e:
        warning = f"{name} scan failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {warning}")
        with lock:
            cache["warning"] = warning
            job["status"] = "idle"
            job["progress"] = None
        return

    try:
        _save_cache_atomic(cache_path, result)
    except OSError as e:
        log(f"server86: failed to persist {os.path.basename(cache_path)}: {e}")

    with lock:
        cache.clear()
        cache.update(result)
        job["status"] = "idle"
        job["progress"] = None


def _start_scan_job(lock, job, run_target, extra_job_fields=None):
    """Single-flight: a POST while one is already running does NOT start a
    second scan and does NOT 409 — it returns the SAME 202 shape the
    original POST returned, so a POST-then-GET from one client never
    observes a state its own POST couldn't have produced."""
    with lock:
        if job["status"] == "running":
            return dict(job)
        job.clear()
        job.update({"status": "running", "started_at": int(time.time()), "progress": 0})
        if extra_job_fields:
            job.update(extra_job_fields)
        status = dict(job)

    threading.Thread(target=run_target, daemon=True).start()
    return status


def _scan_status(lock, job, cache):
    """GET handler body: never scans, never starts one. Merges the live
    job status over the last persisted result, so status/progress always
    reflect what's happening now and every other field reflects the last
    completed scan (or the never-scanned skeleton)."""
    with lock:
        status = dict(job)
        cached = dict(cache)
    cached.update(status)
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
    filter_obj = dict(AUTHOR_SCAN_MODES[mode])
    limit = filter_obj["limit"]
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
        "authors": authors,
    }


def _empty_authors_result():
    return {
        "scanned_at": None, "mode": None, "limit": None, "saturated": False,
        "events_read": 0, "span_start": None, "span_end": None,
        "kinds": {}, "singleton_kinds": {},
        "reports_saturated": False, "reports_scanned": 0,
        "warning": None, "authors": [],
    }


_authors_cache = _load_cache_or(AUTHORS_CACHE_PATH, _empty_authors_result())


def start_authors_scan(mode):
    """Start one bounded author scan in a background thread if none is
    already running — never blocks the request."""
    def run():
        _run_scan_job(
            "author", _authors_lock, _authors_job, _authors_cache, AUTHORS_CACHE_PATH,
            lambda progress_cb: compute_authors(mode, progress_cb=progress_cb),
        )
    return _start_scan_job(_authors_lock, _authors_job, run, extra_job_fields={"mode": mode})


def get_authors_status():
    """GET /api/authors: never scans, never starts one."""
    return _scan_status(_authors_lock, _authors_job, _authors_cache)


def authors_scan_pubkeys():
    """The set of pubkeys currently in the author-scan cache — the second
    half of /api/names' accept-bound (banned OR in this set), and never
    itself a scan."""
    with _authors_lock:
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
        "saturated": False, "warning": None, "recipients": [],
    }


def compute_recipients(progress_cb=None):
    """One bounded scan of the newest RECIPIENT_SCAN_LIMIT gift-wrap
    events, tallying the `p` tag — the wrapped message's recipient, never
    the event's `pubkey`, which is a fresh single-use sender key — while
    streaming, so no event body is ever held in full. Gift-wrap CONTENT is
    encrypted and senders are unlinkable; nothing here reveals who is
    talking to whom, and recipient counts are for retention/capacity
    decisions only, never a moderation signal."""
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
        "recipients": recipients,
    }


_recipients_lock = threading.Lock()
_recipients_job = {"status": "idle", "started_at": None, "progress": None}
_recipients_cache = _load_cache_or(RECIPIENTS_CACHE_PATH, _empty_recipients_result())


def start_recipients_scan():
    def run():
        _run_scan_job(
            "recipients", _recipients_lock, _recipients_job, _recipients_cache,
            RECIPIENTS_CACHE_PATH, compute_recipients,
        )
    return _start_scan_job(_recipients_lock, _recipients_job, run)


def get_recipients_status():
    return _scan_status(_recipients_lock, _recipients_job, _recipients_cache)


# --- DM / general relay-list subscriber search -----------------------------
# Who lists THIS relay in a NIP-17 DM relay list (kind 10050) or a general
# relay list (kind 10002) — the retention-purge exemption (Phase 5) reads
# this to avoid deleting the gift wraps of people who explicitly asked this
# relay to hold them. Kept separate from recipients: a subscriber published
# a signed event announcing this relay is their inbox, which is itself
# public data and discloses nothing new.

def _empty_subscribers_result():
    return {
        "scanned_at": None, "relay_url": None, "saturated": False, "warning": None,
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
    """Best-effort read of a `url` field inside strfry.conf's `relay.info`
    block. This is NOT a standard NIP-11 field — strfry ships only
    name/description/pubkey/contact/icon/nips there — so it is absent
    unless the operator adds it by hand (documented in README); callers
    treat None as 'unconfigured' and return empty rather than guessing.
    Re-read on strfry.conf mtime change, checked at most once per second,
    same shape as get_contact_appeal()."""
    global _relay_url_cache, _relay_url_mtime, _relay_url_last_checked
    now = time.monotonic()
    if _relay_url_last_checked is not None and (now - _relay_url_last_checked) < CONTACT_APPEAL_CHECK_INTERVAL:
        return _relay_url_cache
    _relay_url_last_checked = now
    try:
        mtime = os.stat(STRFRY_CONF_PATH).st_mtime
    except OSError:
        return _relay_url_cache
    if mtime == _relay_url_mtime:
        return _relay_url_cache
    _relay_url_mtime = mtime
    try:
        with open(STRFRY_CONF_PATH, "r") as f:
            lines = f.readlines()
    except OSError:
        return _relay_url_cache

    in_info = False
    depth = 0
    url = None
    for line in lines:
        if line.strip().startswith("#"):
            continue
        if not in_info:
            if re.match(r'^\s*info\s*\{', line):
                in_info = True
                depth = line.count("{") - line.count("}")
            continue
        depth += line.count("{") - line.count("}")
        m = re.search(r'\burl\s*=\s*"([^"]*)"', line)
        if m and m.group(1).strip():
            url = m.group(1).strip()
            break
        if depth <= 0:
            in_info = False
    _relay_url_cache = url
    return url


def compute_subscribers(progress_cb=None):
    """Two bounded scans, kept separate: kind 10050 (NIP-17 DM relay lists)
    and kind 10002 (general relay lists) — a pubkey listing this relay for
    general use is a different relationship from one listing it for DMs.
    Each is capped at SUBSCRIBER_SCAN_LIMIT and matched host-only against
    get_relay_url(). If that is unconfigured, returns empty rather than
    guessing: this result feeds a destructive retention-purge exemption
    (Phase 5), so a silently-empty subscriber list must never be mistaken
    for 'nobody subscribes'."""
    relay_url = get_relay_url()
    relay_host = _hostname_of(relay_url)
    if relay_host is None:
        return {
            "scanned_at": int(time.time()),
            "relay_url": None,
            "saturated": False,
            "warning": "relay.info.url is not set in strfry.conf — cannot match subscriber relay lists",
            "subscribers": [],
            "general_subscribers": [],
        }

    def scan_kind(kind):
        tally = {}  # pubkey -> latest created_at among matching events

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
        "warning": None,
        "subscribers": build_rows(dm_tally),
        "general_subscribers": build_rows(general_tally),
    }


_subscribers_lock = threading.Lock()
_subscribers_job = {"status": "idle", "started_at": None, "progress": None}
_subscribers_cache = _load_cache_or(SUBSCRIBERS_CACHE_PATH, _empty_subscribers_result())


def start_subscribers_scan():
    def run():
        _run_scan_job(
            "subscribers", _subscribers_lock, _subscribers_job, _subscribers_cache,
            SUBSCRIBERS_CACHE_PATH, compute_subscribers,
        )
    return _start_scan_job(_subscribers_lock, _subscribers_job, run)


def get_subscribers_status():
    return _scan_status(_subscribers_lock, _subscribers_job, _subscribers_cache)


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
            content = ev.get("content")
            previews.append({
                "kind": ev.get("kind"),
                "created_at": ev.get("created_at"),
                "content": (content if isinstance(content, str) else "")[:280],
            })
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
            })
    except Exception as e:
        log(f"server86: profile reports-against scan failed for {pubkey_hex}: {e}")
        warnings.append("reports-against scan failed")

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
    with _authors_lock:
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
    with _authors_lock:
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

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in (
            "/api/unban", "/api/ban", "/api/authors/scan", "/api/names",
            "/api/recipients", "/api/subscribers", "/api/reason", "/api/profile",
            "/api/pubkeys/lookup",
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

        if path == "/api/reason":
            pubkeys, reason, mode, err = validate_reason_request(body)
            if err:
                self._send_json(400, {"error": err})
                return
            updated, skipped = blacklist.set_reasons(pubkeys, reason, mode, now=int(time.time()))
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
