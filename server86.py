#!/usr/bin/env python3
"""stdlib admin HTTP server for strfry-86.

Spawned (detached) by plugin86.py. Enforces singleton via port-bind: if the
configured port is already taken, this process exits 0 silently, so repeated
spawns from the plugin are harmless.

Routes:
  GET  /            -> admin.html
  GET  /api/banned  -> public read of the ban list
  POST /api/unban   -> NIP-98 authenticated unban
  POST /api/ban     -> NIP-98 authenticated manual ban
  GET  /api/audit   -> public read of the audit report (notices + activity)
"""

import errno
import hashlib
import json
import os
import selectors
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

from lib86 import audit, bech32, bip340, blacklist  # noqa: E402

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
# Hard safety net enforced by run_strfry_scan on every call (name lookup and
# audit alike): kills the subprocess if stdout exceeds this regardless of
# what filter produced it, so a future filter change that drops a `limit`
# or authors bound can't reintroduce unbounded streaming.
STRFRY_SCAN_MAX_STDOUT_BYTES = 16 * 1024 * 1024
STRFRY_SCAN_READ_CHUNK = 65536

_name_cache = {}  # pubkey_hex -> (name_or_None, checked_at)

_strfry_bin_path = None
_strfry_bin_checked = False

_relay_cwd_pid = None
_relay_cwd_path = None

CONFIG_CHECK_INTERVAL = 1.0
_dynamic_config_cache = {"contact_appeal": "", "relay_url": ""}
_dynamic_config_mtime = None
_dynamic_config_last_checked = 0.0

AUDIT_CACHE_TTL = 10 * 60
AUDIT_SYNC_TIMEOUT = 5
AUDIT_SYNC_SCAN_TIMEOUT = 5
AUDIT_BG_SCAN_TIMEOUT = 60
ACTIVITY_WINDOW_DAYS = 28

_audit_lock = threading.Lock()
_audit_cache = None  # None until first successful/attempted compute
_audit_computing = False

SERVER_LOG_PATH = os.path.join(SCRIPT_DIR, "server86.log")
SERVER_LOG_MAX_BYTES = 64 * 1024
SERVER_LOG_TRIM_BYTES = 32 * 1024


def log(msg):
    print(msg, file=sys.stderr, flush=True)
    try:
        with open(SERVER_LOG_PATH, "a") as f:
            f.write(f"{int(time.time())} {msg}\n")
        _trim_server_log()
    except Exception:
        pass


def _trim_server_log():
    try:
        if os.path.getsize(SERVER_LOG_PATH) <= SERVER_LOG_MAX_BYTES:
            return
        with open(SERVER_LOG_PATH, "rb") as f:
            data = f.read()
        trimmed = data[-SERVER_LOG_TRIM_BYTES:]
        nl = trimmed.find(b"\n")
        if nl != -1:
            trimmed = trimmed[nl + 1:]
        tmp_path = SERVER_LOG_PATH + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(trimmed)
        os.replace(tmp_path, SERVER_LOG_PATH)
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    return {
        "admin_pubkey_hex": cfg["admin_pubkey_hex"],
        "port": int(cfg.get("port", 8686)),
        "bind": cfg.get("bind", "0.0.0.0"),
    }


def get_dynamic_config():
    """Return {"contact_appeal", "relay_url"}, re-reading config.json when
    its mtime changes (checked at most once per second). Never raises —
    a hand-edited or briefly-invalid config.json just keeps the last good
    values."""
    global _dynamic_config_cache, _dynamic_config_mtime, _dynamic_config_last_checked
    now = time.monotonic()
    if (now - _dynamic_config_last_checked) < CONFIG_CHECK_INTERVAL:
        return _dynamic_config_cache
    _dynamic_config_last_checked = now
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime
    except OSError:
        return _dynamic_config_cache
    if mtime == _dynamic_config_mtime:
        return _dynamic_config_cache
    _dynamic_config_mtime = mtime
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        contact_appeal = cfg.get("contact_appeal")
        relay_url = cfg.get("relay_url")
        _dynamic_config_cache = {
            "contact_appeal": contact_appeal if isinstance(contact_appeal, str) else "",
            "relay_url": relay_url if isinstance(relay_url, str) else "",
        }
    except (OSError, ValueError):
        pass
    return _dynamic_config_cache


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
    it for every subsequent scan (name lookup and audit alike): prefer
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


def resolve_names(pubkeys):
    """Return {pubkey: name_or_None} for the given pubkeys, querying the local
    strfry database for uncached (or stale-miss) pubkeys in one batched scan."""
    now = time.time()
    to_query = [
        pk for pk in pubkeys
        if pk not in _name_cache or (
            _name_cache[pk][0] is None and now - _name_cache[pk][1] >= NAME_CACHE_TTL
        )
    ]

    if to_query:
        try:
            events = run_strfry_scan({"kinds": [0], "authors": to_query}, timeout=STRFRY_SCAN_TIMEOUT)
            found = set()
            for ev in events:
                try:
                    content = json.loads(ev.get("content", "{}"))
                    name = content.get("display_name") or content.get("name")
                    pk = ev.get("pubkey")
                except Exception:
                    continue
                if pk in to_query:
                    _name_cache[pk] = (name if isinstance(name, str) and name else None, now)
                    found.add(pk)
            for pk in to_query:
                if pk not in found:
                    _name_cache[pk] = (None, now)
        except Exception as e:
            log(f"server86: strfry scan failed: {e}")
            for pk in to_query:
                if pk not in _name_cache:
                    _name_cache[pk] = (None, now)

    return {pk: _name_cache[pk][0] for pk in pubkeys}


class StdoutCapExceeded(RuntimeError):
    def __init__(self, max_bytes):
        super().__init__(f"stdout exceeded {max_bytes} byte cap")


def _run_capped(argv, cwd, timeout, max_stdout_bytes):
    """Run argv to completion, killing it early if stdout exceeds
    max_stdout_bytes or timeout elapses. Reads via os.read() on the raw pipe
    fds (not the buffered file objects' .read(), which loops trying to fill
    the requested size and can block on a pipe) so the byte cap is checked
    incrementally instead of after an unbounded buffer is already collected.
    Returns (stdout_bytes, stderr_bytes, returncode). Raises
    subprocess.TimeoutExpired or StdoutCapExceeded if either limit trips."""
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    out_chunks, err_chunks = [], []
    out_len = 0
    open_fds = 2
    deadline = time.monotonic() + timeout
    try:
        while open_fds > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in sel.select(timeout=remaining):
                data = os.read(key.fileobj.fileno(), STRFRY_SCAN_READ_CHUNK)
                if not data:
                    sel.unregister(key.fileobj)
                    open_fds -= 1
                    continue
                if key.data == "out":
                    out_len += len(data)
                    if out_len > max_stdout_bytes:
                        proc.kill()
                        proc.wait()
                        raise StdoutCapExceeded(max_stdout_bytes)
                    out_chunks.append(data)
                else:
                    err_chunks.append(data)
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    finally:
        sel.close()
        proc.stdout.close()
        proc.stderr.close()
    return b"".join(out_chunks), b"".join(err_chunks), proc.returncode


def run_strfry_scan(filter_obj, timeout=STRFRY_SCAN_TIMEOUT, max_stdout_bytes=STRFRY_SCAN_MAX_STDOUT_BYTES):
    """Run `strfry scan <filter>` and return a list of parsed event dicts.
    `filter_obj` may be a single filter dict or a list of filter dicts (each
    with its own `limit` — strfry applies limit per FILTER, not per author,
    so bounding output per-author requires one filter per author). Raises on
    any failure (bad binary, non-zero exit, timeout, stdout cap) so callers
    can treat a broken scan environment as a single reportable failure
    rather than silently returning an empty result set."""
    strfry_bin = require_strfry_bin()
    filter_json = json.dumps(filter_obj, separators=(",", ":"))
    argv = [strfry_bin, "--config", STRFRY_CONF_PATH, "scan", filter_json]
    stdout_bytes, stderr_bytes, returncode = _run_capped(argv, get_relay_cwd(), timeout, max_stdout_bytes)
    if returncode != 0:
        raise RuntimeError(f"strfry scan exited {returncode}: {stderr_tail(stderr_bytes)}")
    events = []
    for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


class AuditScanError(Exception):
    """Wraps a scan failure with which numbered/named scan raised it, so the
    /api/audit warning can name it instead of reporting a bare generic
    string."""

    def __init__(self, step, name, original, batch_info=""):
        message = (
            f"audit scan {step} ({name}){batch_info} failed: "
            f"{type(original).__name__}: {original}"
        )
        super().__init__(message[:600])


AUTHOR_CHUNK_SIZE = 1000
AUTHOR_HARD_CAP = 10000
# Ghost-relevant kinds (0, 3, 10002, 10050) are all strfry-enforced
# replaceable — at most one stored event per kind per author, ever,
# regardless of how many times it was republished (verified against the
# real binary: importing 3 kind-0 events for one pubkey leaves exactly 1
# stored). So an author's ENTIRE ghost-kind footprint is capped at
# len(GHOST_ALLOWED_KINDS) events. Requesting one more than that per author
# means: if fewer come back, that IS their whole stored history (exact, not
# sampled); if the full limit comes back, at least one of those events must
# be a non-ghost kind by pigeonhole (only that many replaceable slots
# exist) — so ghost/non-ghost classification is exact either way, with
# output capped per author instead of streaming their whole history.
FOOTPRINT_PER_AUTHOR_LIMIT = len(audit.GHOST_ALLOWED_KINDS) + 1
# purge_pending only needs existence (>=1 stored event), not a real count.
PURGE_PENDING_PER_AUTHOR_LIMIT = 1


def apply_author_cap(authors, name):
    """Truncate authors to AUTHOR_HARD_CAP, returning (kept, dropped,
    sampled_note). dropped is the set of authors excluded by truncation
    (never scanned at all) — callers that would otherwise treat "no
    footprint events" as a positive signal (ghosts) must fold this into
    their unknown/excluded set exactly like a failed scan batch, or a huge
    relay would silently ghost-flag every truncated, never-scanned author."""
    total = len(authors)
    if total > AUTHOR_HARD_CAP:
        kept = authors[:AUTHOR_HARD_CAP]
        dropped = set(authors[AUTHOR_HARD_CAP:])
        return kept, dropped, f"audit sampled first {AUTHOR_HARD_CAP} of {total} authors ({name})"
    return authors, set(), None


def scan_by_authors_bounded(step, name, authors, per_author_limit, timeout):
    """Run a per-author-bounded `strfry scan` in chunks of <=AUTHOR_CHUNK_SIZE.

    Each chunk is submitted as a JSON ARRAY of one filter per author
    (`[{"authors":[pk],"limit":N}, ...]`) rather than a single
    `{"authors":[...]}` filter: strfry applies `limit` per FILTER, so a
    single combined filter's limit would cap the whole batch's output, not
    each author's — an author with a huge posting history could still
    starve the rest of the batch's output budget (and, before this
    per-author bound existed, streamed their entire history un-limited,
    which is what caused production timeouts on real accounts with
    thousands of stored events). One filter per author bounds EVERY
    author's contribution independently, regardless of relay contents.
    A single argv element with 1000 such filters is ~90KB, safely under
    Linux's 128KiB MAX_ARG_STRLEN per argv element (the very limit that
    made chunking necessary in the first place, at ~1900 authors in the old
    authors-list format).

    Returns (events, unknown_authors): events is the deduped concatenation
    of every successful batch; unknown_authors is the set of authors whose
    batch failed (timeout, non-zero exit, or the stdout byte cap) — logged
    to server86.log per batch, but a batch failure degrades only that
    batch's authors to "unknown" rather than aborting the whole scan.
    """
    if not authors:
        return [], set()

    batches = [
        authors[i:i + AUTHOR_CHUNK_SIZE]
        for i in range(0, len(authors), AUTHOR_CHUNK_SIZE)
    ]

    seen_ids = set()
    events = []
    unknown_authors = set()
    for idx, batch in enumerate(batches, start=1):
        batch_info = f" batch {idx}/{len(batches)}" if len(batches) > 1 else ""
        filters = [{"authors": [pk], "limit": per_author_limit} for pk in batch]
        try:
            batch_events = run_strfry_scan(filters, timeout=timeout)
        except Exception as e:
            log(f"server86: audit scan {step} ({name}){batch_info} failed: "
                f"{type(e).__name__}: {e}")
            unknown_authors.update(batch)
            continue
        for ev in batch_events:
            eid = ev.get("id") if isinstance(ev, dict) else None
            if eid is None or eid in seen_ids:
                continue
            seen_ids.add(eid)
            events.append(ev)

    return events, unknown_authors


def empty_audit_skeleton(relay_url, warning):
    return {
        "generated_at": int(time.time()),
        "relay_url": relay_url,
        "warning": warning,
        "notices": [],
        "activity": [],
    }


def compute_audit_report(admin_pubkey_hex, scan_timeout):
    dyn = get_dynamic_config()
    relay_url = dyn["relay_url"]
    banned = set(blacklist.load().keys())

    def scan(step, name, filter_obj):
        try:
            return run_strfry_scan(filter_obj, timeout=scan_timeout)
        except Exception as e:
            raise AuditScanError(step, name, e) from e

    relay_list_events = scan(1, "relay_lists", {"kinds": [10002, 10050]})

    authors = sorted({
        ev.get("pubkey") for ev in relay_list_events
        if isinstance(ev, dict)
        and isinstance(ev.get("pubkey"), str)
        and ev.get("pubkey") != admin_pubkey_hex
        and ev.get("pubkey") not in banned
    })
    authors, dropped_footprint_authors, footprint_sampled = apply_author_cap(authors, "footprint")
    footprint_events, unknown_footprint_authors = scan_by_authors_bounded(
        2, "footprint", authors, FOOTPRINT_PER_AUTHOR_LIMIT, scan_timeout
    )
    unknown_footprint_authors |= dropped_footprint_authors

    since = int(time.time()) - ACTIVITY_WINDOW_DAYS * 86400
    activity_events = scan(3, "activity", {"since": since})

    banned_list, _dropped_purge_authors, purge_sampled = apply_author_cap(sorted(banned), "purge_pending")
    banned_events, unknown_purge_authors = scan_by_authors_bounded(
        4, "purge_pending", banned_list, PURGE_PENDING_PER_AUTHOR_LIMIT, scan_timeout
    )

    report = audit.build_report(
        relay_list_events, footprint_events, activity_events, banned_events,
        banned, admin_pubkey_hex, relay_url,
        unknown_pubkeys=unknown_footprint_authors,
    )

    warning_parts = [n for n in (footprint_sampled, purge_sampled) if n]
    if unknown_footprint_authors:
        warning_parts.append(
            f"audit scan 2 (footprint): {len(unknown_footprint_authors)} authors' footprint "
            "could not be scanned and were excluded from ghost detection"
        )
    if unknown_purge_authors:
        warning_parts.append(
            f"audit scan 4 (purge_pending): {len(unknown_purge_authors)} banned authors' "
            "event counts could not be scanned"
        )
    return {
        "generated_at": int(time.time()),
        "relay_url": relay_url,
        "warning": "; ".join(warning_parts) if warning_parts else None,
        "notices": report["notices"],
        "activity": report["activity"],
    }


def _recompute_audit(admin_pubkey_hex, scan_timeout):
    global _audit_cache, _audit_computing
    try:
        report = compute_audit_report(admin_pubkey_hex, scan_timeout)
    except Exception as e:
        if isinstance(e, AuditScanError):
            warning = str(e)
        else:
            warning = f"audit recompute failed: {type(e).__name__}: {e}"[:600]
        log(f"server86: {warning}")
        with _audit_lock:
            if _audit_cache is None:
                _audit_cache = empty_audit_skeleton(
                    get_dynamic_config()["relay_url"], warning
                )
            _audit_computing = False
        return
    with _audit_lock:
        _audit_cache = report
        _audit_computing = False


def get_audit_report(admin_pubkey_hex):
    """Serve the cached audit report, kicking off a background recompute
    (single-flight) when the cache is stale or missing. Never blocks beyond
    AUDIT_SYNC_TIMEOUT, and never raises."""
    global _audit_computing
    with _audit_lock:
        cache = _audit_cache
        fresh = cache is not None and (time.time() - cache["generated_at"]) < AUDIT_CACHE_TTL
        should_kickoff = not fresh and not _audit_computing
        if should_kickoff:
            _audit_computing = True

    if fresh:
        return cache

    if should_kickoff:
        if cache is None:
            t = threading.Thread(
                target=_recompute_audit,
                args=(admin_pubkey_hex, AUDIT_SYNC_SCAN_TIMEOUT),
                daemon=True,
            )
            t.start()
            t.join(AUDIT_SYNC_TIMEOUT)
            with _audit_lock:
                if _audit_cache is not None:
                    return _audit_cache
            return empty_audit_skeleton(
                get_dynamic_config()["relay_url"], "audit still computing — try again shortly"
            )
        else:
            threading.Thread(
                target=_recompute_audit,
                args=(admin_pubkey_hex, AUDIT_BG_SCAN_TIMEOUT),
                daemon=True,
            ).start()

    if cache is not None:
        return cache
    return empty_audit_skeleton(
        get_dynamic_config()["relay_url"], "audit still computing — try again shortly"
    )


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
                names = resolve_names(pubkeys)
            except Exception as e:
                log(f"server86: name resolution failed: {e}")
                names = {}
            banned = []
            for pubkey, info in data.items():
                try:
                    npub = bech32.npub_encode(pubkey)
                except (ValueError, TypeError):
                    continue
                banned.append(
                    {
                        "pubkey": pubkey,
                        "npub": npub,
                        "banned_at": info.get("banned_at"),
                        "reason": info.get("reason", ""),
                        "report_type": info.get("report_type"),
                        "name": names.get(pubkey),
                    }
                )
            banned.sort(key=lambda b: (b["banned_at"] is None, b["banned_at"]), reverse=True)
            self._send_json(200, {
                "admin": cfg["admin_pubkey_hex"],
                "contact_appeal": get_dynamic_config()["contact_appeal"],
                "banned": banned,
            })
            return

        if path == "/api/audit":
            cfg = self.server.strfry86_config
            report = get_audit_report(cfg["admin_pubkey_hex"])
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
