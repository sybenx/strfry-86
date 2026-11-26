#!/usr/bin/env python3
"""Eighty Six sidecar: watches a relay for kind 1984 reports and bans what the
admin or a moderator reports, keeping a copy of the reported content first.

Runs as an ordinary relay client. Mode "log-only" (the only mode built so far)
records every ban it would make in sidecar-decisions.jsonl and calls nothing.
"""

import hashlib
import json
import os
import re
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib86 import bip340, ws  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "sidecar.json")

DEFAULTS = {
    "relay_url": "",
    "admin_pubkey": "",
    "moderator_list_d": "eighty-six-moderators",
    "mode": "log-only",
    "good_sample_every": 50,
    "context_limit": 20,
    "data_dir": "sidecar-data",
    "lookup_relays": [],
}
MODES = ("log-only",)
MODERATOR_LIST_KIND = 30000
KEEP_FILE_MAX_BYTES = 64 * 1024 * 1024
SEEN_REPORTS_MAX = 5000
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def is_hex64(s):
    return isinstance(s, str) and bool(HEX64.match(s))


def compute_id(ev):
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode("utf-8")).hexdigest()


def verify_event(ev):
    try:
        if not (is_hex64(ev["id"]) and is_hex64(ev["pubkey"])):
            return False
        if not isinstance(ev["sig"], str) or len(ev["sig"]) != 128:
            return False
        if not isinstance(ev["created_at"], int) or not isinstance(ev["kind"], int):
            return False
        if not isinstance(ev["content"], str) or not isinstance(ev["tags"], list):
            return False
        if compute_id(ev) != ev["id"]:
            return False
        return bip340.schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]),
                                     bytes.fromhex(ev["sig"]))
    except (KeyError, TypeError, ValueError):
        return False


def tag_values(ev, name):
    return [t[1] for t in ev.get("tags", []) if isinstance(t, list) and len(t) >= 2 and t[0] == name]


def moderators_from(list_event, admin):
    """The moderator set a list event names, or None if it isn't the admin's list."""
    if list_event.get("pubkey") != admin or list_event.get("kind") != MODERATOR_LIST_KIND:
        return None
    return {p for p in tag_values(list_event, "p") if is_hex64(p) and p != admin}


def role_of(pubkey, admin, moderators):
    if pubkey == admin:
        return "admin"
    if pubkey in moderators:
        return "moderator"
    return None


def report_actions(report, admin, moderators, signer_role):
    """Ban calls a report asks for. Reported pubkeys are banned; a reported event is
    banned only when the report names no pubkey. A moderator cannot ban the admin or
    another moderator; nobody bans the admin."""
    reason = ""
    actions = []
    for t in report.get("tags", []):
        if isinstance(t, list) and len(t) >= 3 and t[0] in ("p", "e") and isinstance(t[2], str):
            reason = reason or t[2]
    pubkeys = []
    for p in tag_values(report, "p"):
        if not is_hex64(p) or p in pubkeys or p == admin:
            continue
        if signer_role == "moderator" and p in moderators:
            continue
        pubkeys.append(p)
    for p in pubkeys:
        actions.append({"method": "banpubkey", "target": p, "reason": reason})
    if not tag_values(report, "p"):
        for e in tag_values(report, "e"):
            if is_hex64(e) and not any(a["target"] == e for a in actions):
                actions.append({"method": "banevent", "target": e, "reason": reason})
    return actions


class JsonlFile:
    def __init__(self, path, max_bytes=KEEP_FILE_MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes

    def append(self, record):
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        try:
            if os.path.getsize(self.path) + len(line) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
        except FileNotFoundError:
            pass
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


class Sidecar:
    def __init__(self, cfg, fetch, now=time.time, lookup=None):
        self.cfg = cfg
        self.admin = cfg["admin_pubkey"]
        self.fetch = fetch
        self.lookup = lookup or fetch
        self.now = now
        data = cfg["data_dir"]
        if not os.path.isabs(data):
            data = os.path.join(BASE_DIR, data)
        os.makedirs(data, exist_ok=True)
        self.decisions = JsonlFile(os.path.join(data, "sidecar-decisions.jsonl"))
        self.kept_spam = JsonlFile(os.path.join(data, "kept-spam.jsonl"))
        self.kept_good = JsonlFile(os.path.join(data, "kept-good.jsonl"))
        self.state_path = os.path.join(data, "sidecar-state.json")
        self.moderators = set()
        self.moderator_list_at = -1
        self.good_counter = 0
        self.since = 0
        self.seen = []
        self._load_state()

    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.since = int(st.get("since", 0))
            self.seen = [s for s in st.get("seen", []) if is_hex64(s)][-SEEN_REPORTS_MAX:]
        except (FileNotFoundError, ValueError, TypeError):
            pass

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"since": self.since, "seen": self.seen[-SEEN_REPORTS_MAX:]}, f)
        os.replace(tmp, self.state_path)

    def handle_moderator_list(self, ev):
        mods = moderators_from(ev, self.admin)
        if mods is None or ev.get("created_at", -1) <= self.moderator_list_at:
            return False
        if self.cfg["moderator_list_d"] not in tag_values(ev, "d") or not verify_event(ev):
            return False
        self.moderators = mods
        self.moderator_list_at = ev["created_at"]
        self.decisions.append({"at": int(self.now()), "type": "moderators", "list_event": ev["id"],
                               "moderators": sorted(mods)})
        return True

    def handle_report(self, ev):
        rid = ev.get("id")
        if ev.get("kind") != 1984 or not is_hex64(rid) or rid in self.seen:
            return None
        role = role_of(ev.get("pubkey"), self.admin, self.moderators)
        if role is not None and not verify_event(ev):
            role = None
        record = {"at": int(self.now()), "type": "report", "report": rid,
                  "signer": ev.get("pubkey"), "role": role}
        if role is None:
            record["result"] = "request"
        else:
            actions = report_actions(ev, self.admin, self.moderators, role)
            if actions:
                self.keep(ev, role, actions)
            record["result"] = "would-ban" if actions else "nothing-to-ban"
            record["actions"] = actions
        self.decisions.append(record)
        self.seen.append(rid)
        self.since = max(self.since, int(ev.get("created_at", 0)))
        self._save_state()
        return record

    def catch_up(self, page=500):
        """Handle every report since the checkpoint, oldest first. Relays cap how many
        events one query returns, so this pages backwards with `until`."""
        found = {}
        until = None
        while True:
            f = {"kinds": [1984], "since": self.since, "limit": page}
            if until is not None:
                f["until"] = until
            new = [e for e in self.fetch([f]) if is_hex64(e.get("id")) and e["id"] not in found]
            if not new:
                break
            for e in new:
                found[e["id"]] = e
            until = min(int(e.get("created_at", 0)) for e in new)
        for e in sorted(found.values(), key=lambda e: int(e.get("created_at", 0))):
            self.handle_report(e)
        return len(found)

    def keep(self, report, role, actions):
        """Copy the reported content and its authors' context before any ban removes it."""
        limit = int(self.cfg["context_limit"])
        event_ids = [a["target"] for a in actions if a["method"] == "banevent"]
        event_ids += [e for e in tag_values(report, "e") if is_hex64(e) and e not in event_ids]
        reported = self.lookup([{"ids": event_ids}]) if event_ids else []
        authors = [a["target"] for a in actions if a["method"] == "banpubkey"]
        authors += [e["pubkey"] for e in reported if e.get("pubkey") not in authors]
        profiles = {}
        recent = {}
        if authors:
            for p in self.lookup([{"kinds": [0], "authors": authors}]):
                if p.get("pubkey") in authors and p.get("created_at", 0) >= profiles.get(p["pubkey"], {}).get("created_at", -1):
                    profiles[p["pubkey"]] = p
            for a in authors:
                recent[a] = sorted(self.lookup([{"authors": [a], "limit": limit}]),
                                   key=lambda e: -int(e.get("created_at", 0)))[:limit]
        self.kept_spam.append({"kept_at": int(self.now()), "report": report, "role": role,
                               "actions": actions, "reported_events": reported,
                               "profiles": profiles, "recent": recent})

    def handle_note(self, ev):
        """Keep every Nth note as a sample of what normally gets through."""
        if ev.get("kind") != 1:
            return False
        self.good_counter += 1
        if self.good_counter % max(1, int(self.cfg["good_sample_every"])):
            return False
        self.kept_good.append({"kept_at": int(self.now()), "event": ev})
        return True


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    with open(path, encoding="utf-8") as f:
        cfg.update(json.load(f))
    if not cfg["relay_url"].startswith(("ws://", "wss://")):
        raise ValueError("sidecar.json: relay_url must be a ws:// or wss:// url")
    if not is_hex64(cfg["admin_pubkey"]):
        raise ValueError("sidecar.json: admin_pubkey must be 64-character lowercase hex")
    if not isinstance(cfg["lookup_relays"], list) or not all(
            isinstance(u, str) and u.startswith(("ws://", "wss://")) for u in cfg["lookup_relays"]):
        raise ValueError("sidecar.json: lookup_relays must be a list of ws:// or wss:// urls")
    if cfg["mode"] not in MODES:
        raise ValueError(f"sidecar.json: mode must be one of {MODES}; enforcing is not built yet")
    return cfg


def lookup_across(urls, filters):
    """The same query on every relay, merged by id. A relay that fails is skipped."""
    merged = {}
    for u in urls:
        try:
            for e in ws.query(u, filters):
                if is_hex64(e.get("id")):
                    merged.setdefault(e["id"], e)
        except (OSError, ws.WSClosed, ConnectionError, ValueError):
            continue
    return list(merged.values())


def run(cfg, stop):
    url = cfg["relay_url"]
    urls = [url] + [u for u in cfg["lookup_relays"] if u != url]
    side = Sidecar(cfg, fetch=lambda filters: ws.query(url, filters),
                   lookup=lambda filters: lookup_across(urls, filters))
    backoff = 5
    while not stop["now"]:
        conn = None
        try:
            conn = ws.WebSocket(url)
            conn.send(["REQ", "mods", {"kinds": [MODERATOR_LIST_KIND], "authors": [side.admin],
                                       "#d": [cfg["moderator_list_d"]]}])
            ready = False
            while not stop["now"]:
                try:
                    msg = conn.recv(timeout=5)
                except socket.timeout:
                    continue
                if not isinstance(msg, list) or len(msg) < 2:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                    ev = msg[2]
                    if msg[1] == "mods":
                        side.handle_moderator_list(ev)
                    elif msg[1] == "reports":
                        side.handle_report(ev)
                    elif msg[1] == "good":
                        side.handle_note(ev)
                elif msg[0] == "EOSE" and msg[1] == "mods" and not ready:
                    ready = True
                    side.catch_up()
                    conn.send(["REQ", "reports", {"kinds": [1984], "since": side.since}])
                    conn.send(["REQ", "good", {"kinds": [1], "since": int(time.time())}])
                    backoff = 5
                elif msg[0] == "CLOSED":
                    raise ws.WSClosed(f"relay closed subscription {msg[1]}")
        except (OSError, ws.WSClosed, ConnectionError, ValueError) as exc:
            print(f"sidecar86: {exc}; reconnecting in {backoff}s", file=sys.stderr, flush=True)
            for _ in range(backoff):
                if stop["now"]:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 300)
        finally:
            if conn:
                conn.close()


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH)
    stop = {"now": False}

    def _stop(*_):
        # Exiting mid-report is safe: state is rewritten atomically after each report,
        # so an unfinished one is simply handled again on the next start.
        stop["now"] = True
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run(cfg, stop)


if __name__ == "__main__":
    main()
