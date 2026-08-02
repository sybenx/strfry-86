#!/usr/bin/env python3
"""strfry write-policy plugin for strfry-86.

Reads one JSON message per line on stdin, writes one accept/reject decision
per line on stdout. stdout carries protocol JSON ONLY — all logging goes to
stderr. Must never crash: a dead plugin wedges the relay's write path.
"""

import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from lib86 import blacklist  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SERVER_SCRIPT = os.path.join(SCRIPT_DIR, "server86.py")
SERVER_RESPAWN_INTERVAL = 3600.0

# This process is the ONLY place a reject is ever observed: a blocked event
# never reaches the database, so `strfry scan` — which is all server86 can
# see — cannot report one. The decision log is that missing channel. It is
# written from strfry's blocking write path, so every rule below is a
# latency rule, not a style one:
#   - one append of one short line per event, no read, no stat, no seek;
#   - the content preview is truncated HERE, so line length is bounded by
#     the plugin rather than by whatever an author decided to publish;
#   - rotation is os.replace + reopen, O(1) regardless of file size, never
#     a rewrite (server86 reads the .1 segment and the live one together);
#   - any failure disables logging for the life of the process. A relay
#     that cannot write its decision log must still accept events at full
#     speed; it must never retry, and must never raise into the reply path.
DECISION_LOG_PATH = os.path.join(SCRIPT_DIR, "decisions.jsonl")
DECISION_LOG_PREV_PATH = DECISION_LOG_PATH + ".1"
DECISION_LOG_MAX_LINES = 2000    # lines per segment before rotation; two segments are retained
DECISION_CONTENT_PREVIEW = 160   # characters of content kept per record


def log(msg):
    print(msg, file=sys.stderr, flush=True)


_decision_fh = None
_decision_lines = 0
_decision_disabled = False


def _decision_open():
    global _decision_fh, _decision_lines, _decision_disabled
    try:
        _decision_fh = open(DECISION_LOG_PATH, "a", encoding="utf-8")
        _decision_lines = 0
    except OSError as e:
        log(f"plugin86: decision log disabled ({e})")
        _decision_fh = None
        _decision_disabled = True


def record_decision(event, result):
    """Append one accept/reject record. Never raises, never blocks on
    anything but the append itself."""
    global _decision_fh, _decision_lines, _decision_disabled
    if _decision_disabled:
        return
    if _decision_fh is None:
        _decision_open()
        if _decision_fh is None:
            return
    try:
        content = event.get("content")
        if not isinstance(content, str):
            content = ""
        rec = {
            "at": int(time.time()),
            "id": result.get("id"),
            "pubkey": event.get("pubkey"),
            "kind": event.get("kind"),
            "created_at": event.get("created_at"),
            "action": result.get("action"),
            "msg": result.get("msg") or "",
            "content": content[:DECISION_CONTENT_PREVIEW],
        }
        _decision_fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        _decision_fh.flush()
        _decision_lines += 1
        if _decision_lines >= DECISION_LOG_MAX_LINES:
            _decision_fh.close()
            os.replace(DECISION_LOG_PATH, DECISION_LOG_PREV_PATH)
            _decision_open()
    except Exception as e:
        log(f"plugin86: decision log disabled ({type(e).__name__}: {e})")
        try:
            if _decision_fh is not None:
                _decision_fh.close()
        except Exception:
            pass
        _decision_fh = None
        _decision_disabled = True


def load_admin_pubkey():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        pk = cfg.get("admin_pubkey_hex")
        # Accept mixed-case config; always return lowercase so ban checks
        # and is_banned keys agree with event pubkeys.
        if is_valid_hex_pubkey(pk):
            return pk.lower()
    except Exception as e:
        log(f"plugin86: failed to load config.json: {e}")
    return None


def is_valid_hex_pubkey(s):
    """64 hex characters, any case. Callers that store keys must .lower()."""
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def spawn_server():
    try:
        subprocess.Popen(
            ["python3", SERVER_SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=SCRIPT_DIR,
        )
    except Exception as e:
        log(f"plugin86: failed to spawn server86: {e}")


# NIP-16 ephemeral kinds (20000–29999) are intentionally NOT rejected here.
# strfry only broadcasts events that pass the write policy and are written;
# a plugin reject would also kill live fan-out to open WebSocket subscriptions.
# strfry itself marks those kinds with expiration=1, serves them to matching
# REQs, and RelayCron deletes them after events.ephemeralEventsLifetimeSeconds
# (default 300). That is the supported "do not keep" path in this stack —
# not a write-policy reject.


def process_event(event, admin_pubkey):
    event_id = event.get("id")
    pubkey = event.get("pubkey")
    kind = event.get("kind")
    pubkey_l = pubkey.lower() if is_valid_hex_pubkey(pubkey) else None

    if pubkey_l is not None and blacklist.is_banned(pubkey_l):
        return {"id": event_id, "action": "reject", "msg": "blocked: banned pubkey"}

    if kind == 1984 and admin_pubkey is not None and pubkey_l == admin_pubkey:
        created_at = event.get("created_at")
        content = event.get("content", "")
        tags = event.get("tags", [])
        if isinstance(tags, list):
            fallback_type = None
            for tag in tags:
                if (
                    isinstance(tag, list)
                    and len(tag) >= 3
                    and tag[0] in ("e", "a")
                    and isinstance(tag[2], str)
                ):
                    fallback_type = tag[2]
                    break

            for tag in tags:
                if (
                    isinstance(tag, list)
                    and len(tag) >= 2
                    and tag[0] == "p"
                    and is_valid_hex_pubkey(tag[1])
                ):
                    if len(tag) >= 3 and isinstance(tag[2], str):
                        report_type = tag[2]
                    else:
                        report_type = fallback_type
                    # name/nip05 start null: strfry blocks its whole write
                    # pipeline on this plugin's reply, so a name lookup in
                    # this hot path would stall the relay. Resolution
                    # happens later, from the admin's browser.
                    # blacklist.add lowercases the target; pass lower here
                    # so the key is unambiguous if add is ever bypassed.
                    blacklist.add(
                        tag[1].lower(),
                        banned_at=created_at,
                        report_event_id=event_id,
                        reason=content if isinstance(content, str) else "",
                        report_type=report_type,
                        admin_pubkey_hex=admin_pubkey,
                    )
        return {"id": event_id, "action": "accept"}

    return {"id": event_id, "action": "accept"}


def main():
    admin_pubkey = load_admin_pubkey()
    spawn_server()
    last_spawn_check = time.monotonic()

    for line in sys.stdin:
        now = time.monotonic()
        if now - last_spawn_check >= SERVER_RESPAWN_INTERVAL:
            spawn_server()
            last_spawn_check = now

        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except Exception as e:
            log(f"plugin86: failed to parse input line: {e}")
            continue

        event_id = None
        try:
            event = msg.get("event", {}) if isinstance(msg, dict) else {}
            event_id = event.get("id") if isinstance(event, dict) else None
            result = process_event(event, admin_pubkey)
        except Exception as e:
            log(f"plugin86: error processing event {event_id}: {e}")
            result = {"id": event_id, "action": "accept"}

        record_decision(event if isinstance(event, dict) else {}, result)

        try:
            print(json.dumps(result), flush=True)
        except Exception as e:
            log(f"plugin86: failed to write output: {e}")


if __name__ == "__main__":
    main()
