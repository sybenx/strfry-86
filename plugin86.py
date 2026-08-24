#!/usr/bin/env python3
"""strfry write-policy plugin for strfry-86.

Reads one JSON message per line on stdin, writes one accept/reject decision
per line on stdout. stdout carries protocol JSON ONLY — all logging goes to
stderr. Must never crash: a dead plugin wedges the relay's write path.
"""

import json
import os
import select
import shlex
import socket
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from lib86 import blacklist  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SERVER_SCRIPT = os.path.join(SCRIPT_DIR, "server86.py")

# server86.py's own bind/port defaults (server86.py's load_config()) —
# duplicated rather than imported because importing server86.py here would
# pull its whole HTTP-server module (and its own startup side effects) into
# strfry's blocking write-policy process just to read two config keys.
DEFAULT_SERVER_PORT = 8686
DEFAULT_SERVER_BIND = "0.0.0.0"

# How often the background liveness thread below probes the admin port and
# spawns server86.py if it isn't bound. This used to be a blind, unconditional
# Popen once per hour, checked only inline in the stdin loop below — which
# meant: (1) a server86 that died a minute after a check stayed dead for up to
# 59 minutes, since nothing else ever looked; (2) on a quiet relay with no
# events, nothing ever looked at all; (3) every hourly check paid for a fresh
# Python interpreter fork inside strfry's blocking write path even when
# server86 was already up (the common case). Running this as a cheap
# non-blocking TCP connect on its own thread, independent of the stdin loop,
# fixes (1) and (3) outright and narrows (2) to "server86 still doesn't come
# back until plugin86 itself is first spawned" — see the optional
# ensure-server86.sh healthcheck hook (README, "Expose the admin page") for
# closing that remaining gap without depending on relay traffic at all.
SERVER_LIVENESS_PROBE_INTERVAL = 5.0

# Per-event timeout, in seconds, on a chained downstream write-policy plugin
# (config.json's next_plugin). Must match server86.py's
# NEXT_PLUGIN_TIMEOUT_DEFAULT — that is the value the Settings page shows and
# lets an operator change; this is only the fallback when config.json doesn't
# set one. Keep any override below strfry.conf's own
# relay.writePolicy.timeoutSeconds (10s in this repo's template): a downstream
# that's still within ITS budget but past strfry's own is moot, since strfry
# will already have timed out plugin86 and failed open.
NEXT_PLUGIN_TIMEOUT_DEFAULT = 2.0

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

# How stale a kind-1984 admin report's created_at may be, relative to now,
# and still apply its auto-ban side effect. A report event is signed,
# stored, and publicly re-servable — without this bound, replaying an old
# one (via writePolicy.lookbackSeconds after a restart, or a republish by
# anyone who fetched it) could resurrect a ban the admin explicitly undid,
# overwriting any curated reason/name with the stale report's content.
# Gates ONLY the auto-ban side effect; the report event itself is still
# accepted (or rejected on its own separate merits) either way.
REPORT_FRESHNESS_WINDOW = 300


def log(msg):
    print(msg, file=sys.stderr, flush=True)


_decision_fh = None
_decision_lines = 0
_decision_disabled = False


def _decision_open():
    global _decision_fh, _decision_lines, _decision_disabled
    try:
        # Count existing lines so a restart does not append another full
        # DECISION_LOG_MAX_LINES before the first rotation (file growth).
        existing = 0
        try:
            with open(DECISION_LOG_PATH, "rb") as fh:
                existing = sum(1 for _ in fh)
        except FileNotFoundError:
            existing = 0
        _decision_fh = open(DECISION_LOG_PATH, "a", encoding="utf-8")
        _decision_lines = existing
        if _decision_lines >= DECISION_LOG_MAX_LINES:
            _decision_fh.close()
            os.replace(DECISION_LOG_PATH, DECISION_LOG_PREV_PATH)
            _decision_fh = open(DECISION_LOG_PATH, "a", encoding="utf-8")
            _decision_lines = 0
    except OSError as e:
        log(f"plugin86: decision log disabled ({e})")
        _decision_fh = None
        _decision_disabled = True


def record_decision(event, result, stage="plugin86"):
    """Append one accept/reject record. Never raises, never blocks on
    anything but the append itself. `stage` names whichever process actually
    made the call — "plugin86" for its own verdict (including a fallback
    after a chained next_plugin failed) or "next_plugin" when a downstream
    plugin's verdict was relayed verbatim."""
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
        content = content[:DECISION_CONTENT_PREVIEW]
        # An attacker-choosable byte (e.g. a lone UTF-16 surrogate) must
        # not be able to raise a UnicodeEncodeError on write and trip the
        # permanent-disable branch below — this is the only channel that
        # ever records a REJECT. Sanitize before it can reach the write.
        content = content.encode("utf-8", errors="replace").decode("utf-8")
        rec = {
            "at": int(time.time()),
            "id": result.get("id"),
            "pubkey": event.get("pubkey"),
            "kind": event.get("kind"),
            "created_at": event.get("created_at"),
            "action": result.get("action"),
            "msg": result.get("msg") or "",
            "content": content,
            "stage": stage,
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


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except Exception as e:
        log(f"plugin86: failed to load config.json: {e}")
    return {}


def admin_pubkey_from_config(cfg):
    pk = cfg.get("admin_pubkey_hex")
    # Accept mixed-case config; always return lowercase so ban checks
    # and is_banned keys agree with event pubkeys.
    if is_valid_hex_pubkey(pk):
        return pk.lower()
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


def admin_port_bound(cfg):
    """True if something is already accepting on the configured admin
    port — mirrors strfry-86-updater.py's _server86_port_bound() exactly
    (that copy can't import this one; it must stay self-contained, see its
    own module docstring). A bound port is the only trustworthy signal:
    server86.py exits 0 silently on EADDRINUSE and on any startup crash, so
    a PID existing for a moment after spawn is not evidence the admin UI
    actually came up."""
    port = cfg.get("port", DEFAULT_SERVER_PORT)
    bind = cfg.get("bind", DEFAULT_SERVER_BIND)
    host = "127.0.0.1" if bind in ("0.0.0.0", "", None) else bind
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_server_running(cfg):
    """Spawn server86.py only if the admin port isn't already bound. Safe to
    call as often as the caller likes — a redundant spawn while one is
    already coming up just exits 0 on EADDRINUSE, same as before this
    change; this only makes the common case (already up) skip the fork
    entirely instead of paying for one every time."""
    if not admin_port_bound(cfg):
        spawn_server()


def _server_liveness_loop(cfg):
    """Runs for the life of the process, independent of whether strfry is
    sending any events at all. Replaces the old hourly, stdin-loop-only
    check — see SERVER_LIVENESS_PROBE_INTERVAL's comment for why."""
    while True:
        try:
            ensure_server_running(cfg)
        except Exception as e:
            log(f"plugin86: liveness probe failed: {e}")
        time.sleep(SERVER_LIVENESS_PROBE_INTERVAL)


# --- chained downstream write-policy plugin (config.json's next_plugin) ----
#
# strfry only has one writePolicy.plugin slot, and this project has always
# taken it, leaving an operator who also wants a spam filter (or anything
# else that speaks strfry's plugin protocol) stuck choosing one or the
# other. plugin86.py already speaks that protocol on both stdin and stdout,
# so it can just as well speak it to a second process as strfry speaks it to
# this one: spawn the downstream once, keep it alive for the process
# lifetime, and forward every event this plugin would itself ACCEPT — a
# REJECT short-circuits here and never reaches the downstream at all, since
# there is nothing left for a second opinion to change. The whole thing
# fails toward plugin86's own verdict, permanently, the instant the
# downstream can't be trusted: a hang, a dead process, or a malformed/
# mismatched reply disables forwarding for the rest of this process's life
# and logs once — the same discipline record_decision() above already uses
# for its own failure mode, and for the same reason: strfry is blocked on
# this reply, so this must never retry and never wedge the write path.

_next_plugin_proc = None
_next_plugin_disabled = False
_next_plugin_timeout = NEXT_PLUGIN_TIMEOUT_DEFAULT


def spawn_next_plugin(command):
    """Start the configured downstream plugin as a long-lived child, line-
    buffered text mode so a written line is flushed to it immediately and a
    completed reply line is available to read without waiting on a buffer
    to fill. shlex.split (not shell=True) so config.json's next_plugin value
    is parsed as an argv the way strfry.conf's own plugin value is treated —
    a path plus arguments, never a string a shell could reinterpret."""
    try:
        args = shlex.split(command)
        if not args:
            log("plugin86: next_plugin is blank after parsing — chaining disabled")
            return None
        return subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=SCRIPT_DIR,
        )
    except Exception as e:
        log(f"plugin86: failed to spawn next_plugin ({command!r}): {e}")
        return None


def _read_line_with_timeout(stream, timeout):
    """One line from `stream`, or None on timeout / EOF (downstream hung,
    was killed, or exited). select() rather than a blocking readline() is
    what makes the per-event timeout actually bound the wait — a plain
    readline() has no timeout of its own and would block exactly as long as
    a hung downstream chose to hang."""
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        return None
    line = stream.readline()
    return line if line != "" else None


def forward_to_next_plugin(raw_line, event_id):
    """Forward the exact line strfry sent this process to the downstream,
    unmodified — the downstream sees the same protocol message it would
    have gotten directly from strfry. Returns the downstream's verdict as a
    result dict on success, or None on any failure (after permanently
    disabling further forwarding), so the caller's own plugin86 verdict is
    always the safe fallback."""
    global _next_plugin_disabled
    try:
        _next_plugin_proc.stdin.write(raw_line + "\n")
        _next_plugin_proc.stdin.flush()
        reply_line = _read_line_with_timeout(_next_plugin_proc.stdout, _next_plugin_timeout)
        if reply_line is None:
            raise TimeoutError("no reply within next_plugin_timeout (hung, killed, or exited)")
        reply = json.loads(reply_line)
        if not isinstance(reply, dict) or reply.get("id") != event_id:
            raise ValueError(f"malformed or mismatched reply: {reply_line.strip()!r}")
        action = reply.get("action")
        if action not in ("accept", "reject", "shadowReject"):
            raise ValueError(f"unrecognized action in downstream reply: {action!r}")
        return {"id": event_id, "action": action, "msg": reply.get("msg") or ""}
    except Exception as e:
        log(f"plugin86: next_plugin chaining disabled ({type(e).__name__}: {e}) — "
            f"falling back to plugin86's own verdict for the rest of this process")
        _next_plugin_disabled = True
        try:
            _next_plugin_proc.kill()
        except Exception:
            pass
        return None


# NIP-16 ephemeral kinds (20000–29999) are intentionally NOT rejected here.
# strfry only broadcasts events that pass the write policy and are written;
# a plugin reject would also kill live fan-out to open WebSocket subscriptions.
# strfry itself marks those kinds with expiration=1, serves them to matching
# REQs, and RelayCron deletes them after events.ephemeralEventsLifetimeSeconds
# (default 300). That is the supported "do not keep" path in this stack —
# not a write-policy reject.


def process_event(event, admin_pubkey, msg_type=None, now=None):
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
        # Only a genuinely NEW report — not a lookback replay of a
        # historical one after a restart, and not a stale event whose
        # created_at is outside the freshness window (someone republishing
        # an old signed report) — may apply the auto-ban side effect. See
        # REPORT_FRESHNESS_WINDOW's comment for why.
        is_fresh = (
            msg_type == "new"
            and isinstance(created_at, (int, float))
            and now is not None
            and abs(now - created_at) <= REPORT_FRESHNESS_WINDOW
        )
        if isinstance(tags, list) and is_fresh:
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

            # name/nip05 start null: strfry blocks its whole write
            # pipeline on this plugin's reply, so a name lookup in this
            # hot path would stall the relay. Resolution happens later,
            # from the admin's browser. blacklist.add_many lowercases the
            # target; pass lower here so the key is unambiguous if it is
            # ever bypassed.
            entries = []
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
                    entries.append((
                        tag[1].lower(),
                        created_at,
                        event_id,
                        content if isinstance(content, str) else "",
                        report_type,
                    ))
            # One flock + one read + one write for the whole report,
            # regardless of p-tag count — batched here rather than one
            # blacklist.add() call per tag, which used to do a full
            # read/parse/rewrite/fsync of blacklist.json (plus a separate
            # lock-and-rewrite of names.json) PER PUBKEY, serially, inside
            # strfry's blocking write path.
            if entries:
                blacklist.add_many(entries, admin_pubkey_hex=admin_pubkey)
        return {"id": event_id, "action": "accept"}

    return {"id": event_id, "action": "accept"}


def next_plugin_timeout_from_config(cfg):
    raw = cfg.get("next_plugin_timeout")
    try:
        v = float(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return NEXT_PLUGIN_TIMEOUT_DEFAULT


def main():
    global _next_plugin_proc, _next_plugin_disabled, _next_plugin_timeout

    cfg = load_config()
    admin_pubkey = admin_pubkey_from_config(cfg)

    # Probe-then-spawn even on this very first call: a plugin86 restart
    # (strfry re-execs it on mtime change, e.g. after an update) may find
    # server86 already running from before, and there's no reason to fork
    # a redundant one just because this is startup.
    ensure_server_running(cfg)
    threading.Thread(target=_server_liveness_loop, args=(cfg,), daemon=True).start()

    next_plugin_cmd = cfg.get("next_plugin")
    if isinstance(next_plugin_cmd, str) and next_plugin_cmd.strip():
        _next_plugin_timeout = next_plugin_timeout_from_config(cfg)
        _next_plugin_proc = spawn_next_plugin(next_plugin_cmd)
        if _next_plugin_proc is None:
            _next_plugin_disabled = True

    for line in sys.stdin:
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
            msg_type = msg.get("type") if isinstance(msg, dict) else None
            event_id = event.get("id") if isinstance(event, dict) else None
            result = process_event(event, admin_pubkey, msg_type=msg_type, now=time.time())
        except Exception as e:
            log(f"plugin86: error processing event {event_id}: {e}")
            result = {"id": event_id, "action": "accept"}

        # Only an ACCEPT is ever forwarded — a reject short-circuits here,
        # so a downstream never sees an event plugin86 itself already
        # decided to block.
        stage = "plugin86"
        if (
            result.get("action") == "accept"
            and _next_plugin_proc is not None
            and not _next_plugin_disabled
        ):
            forwarded = forward_to_next_plugin(line, event_id)
            if forwarded is not None:
                result = forwarded
                stage = "next_plugin"

        record_decision(event if isinstance(event, dict) else {}, result, stage=stage)

        try:
            print(json.dumps(result), flush=True)
        except Exception as e:
            log(f"plugin86: failed to write output: {e}")


if __name__ == "__main__":
    if "--ensure-server86" in sys.argv[1:]:
        # One-shot liveness probe/spawn meant to be invoked from OUTSIDE the
        # stdin loop entirely — e.g. the ensure-server86.sh Docker
        # HEALTHCHECK wrapper (see README, "Expose the admin page"), which
        # keeps server86.py alive independent of relay traffic instead of
        # waiting for plugin86 to be spawned by strfry's first event. Never
        # touches stdin/stdout's plugin protocol.
        try:
            ensure_server_running(load_config())
        except Exception as e:
            log(f"plugin86: --ensure-server86 failed: {e}")
        sys.exit(0)
    main()
