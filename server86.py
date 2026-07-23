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
"""

import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from lib86 import bech32, bip340, blacklist  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
ADMIN_HTML_PATH = os.path.join(SCRIPT_DIR, "admin.html")
STRFRY_BIN = "strfry"
STRFRY_CONF_PATH = "/config/strfry.conf"

NIP98_KIND = 27235
NIP98_MAX_SKEW = 60
NAME_CACHE_TTL = 24 * 3600
STRFRY_SCAN_TIMEOUT = 5

_name_cache = {}  # pubkey_hex -> (name_or_None, checked_at)

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
            filter_json = json.dumps({"kinds": [0], "authors": to_query})
            result = subprocess.run(
                [STRFRY_BIN, "--config", STRFRY_CONF_PATH, "scan", filter_json],
                capture_output=True,
                timeout=STRFRY_SCAN_TIMEOUT,
            )
            found = set()
            if result.returncode == 0:
                for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        content = json.loads(ev.get("content", "{}"))
                        name = content.get("display_name") or content.get("name")
                        pk = ev.get("pubkey")
                    except Exception:
                        continue
                    if pk in to_query:
                        _name_cache[pk] = (name if isinstance(name, str) and name else None, now)
                        found.add(pk)
            else:
                log(f"server86: strfry scan exited {result.returncode}: "
                    f"{result.stderr.decode('utf-8', errors='replace')[:200]}")
            for pk in to_query:
                if pk not in found:
                    _name_cache[pk] = (None, now)
        except Exception as e:
            log(f"server86: strfry scan failed: {e}")
            for pk in to_query:
                if pk not in _name_cache:
                    _name_cache[pk] = (None, now)

    return {pk: _name_cache[pk][0] for pk in pubkeys}


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
                "contact_appeal": get_contact_appeal(),
                "banned": banned,
            })
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
