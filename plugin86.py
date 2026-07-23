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


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_admin_pubkey():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        pk = cfg.get("admin_pubkey_hex")
        if is_valid_hex_pubkey(pk):
            return pk
    except Exception as e:
        log(f"plugin86: failed to load config.json: {e}")
    return None


def is_valid_hex_pubkey(s):
    if not isinstance(s, str) or len(s) != 64 or s != s.lower():
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


def process_event(event, admin_pubkey):
    event_id = event.get("id")
    pubkey = event.get("pubkey")
    kind = event.get("kind")

    if blacklist.is_banned(pubkey):
        return {"id": event_id, "action": "reject", "msg": "blocked: banned pubkey"}

    if kind == 1984 and admin_pubkey is not None and pubkey == admin_pubkey:
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
                    blacklist.add(
                        tag[1],
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

        try:
            print(json.dumps(result), flush=True)
        except Exception as e:
            log(f"plugin86: failed to write output: {e}")


if __name__ == "__main__":
    main()
