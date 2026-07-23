"""Deterministic audit detectors for strfry-86.

PURE functions only: take lists of already-parsed nostr event dicts (plus a
normalized relay URL / banned-pubkey set / admin pubkey) and return the
notices/activity structures served verbatim by GET /api/audit. No I/O, no
subprocess — server86 owns the strfry scans and feeds parsed events in here.
Malformed events (missing fields, non-list tags) are skipped silently; these
functions must never raise on untrusted relay data.
"""

import datetime

GHOST_ALLOWED_KINDS = frozenset({0, 3, 10002, 10050})
BURST_MIN_EVENTS = 10
BURST_MIN_AUTHORS = 10
BURST_MAX_NOTICES = 5
FINGERPRINT_MIN_AUTHORS = 5
FINGERPRINT_MAX_NOTICES = 5
MAX_ACTIVITY_KINDS = 12
ACTIVITY_DAYS = 28


def normalize_relay_url(s):
    """Strip whitespace, lowercase, strip trailing '/' repeatedly, so
    'wss://x.y//', 'wss://X.Y', and 'wss://x.y/' all collide."""
    if not isinstance(s, str):
        return ""
    s = s.strip().lower()
    while s.endswith("/"):
        s = s[:-1]
    return s


def _valid_event(ev):
    return (
        isinstance(ev, dict)
        and isinstance(ev.get("pubkey"), str)
        and isinstance(ev.get("kind"), int)
        and isinstance(ev.get("created_at"), int)
        and isinstance(ev.get("tags"), list)
    )


def _valid_events(events):
    return [ev for ev in (events or []) if _valid_event(ev)]


def _r_tag_urls(tags):
    urls = []
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "r" and isinstance(tag[1], str):
            norm = normalize_relay_url(tag[1])
            if norm:
                urls.append(norm)
    return urls


def build_ghosts_notice(relay_list_events, footprint_events, banned_pubkeys, admin_pubkey_hex, relay_url_norm):
    relay_list_events = _valid_events(relay_list_events)
    footprint_events = _valid_events(footprint_events)
    banned_pubkeys = banned_pubkeys or set()

    relay_list_authors = set()
    for ev in relay_list_events:
        pk = ev["pubkey"]
        if pk == admin_pubkey_hex or pk in banned_pubkeys:
            continue
        relay_list_authors.add(pk)

    footprint_kinds = {}
    for ev in footprint_events:
        footprint_kinds.setdefault(ev["pubkey"], set()).add(ev["kind"])

    ghosts = [
        pk for pk in relay_list_authors
        if footprint_kinds.get(pk, set()).issubset(GHOST_ALLOWED_KINDS)
    ]

    if not ghosts:
        return None

    ghost_set = set(ghosts)
    targets_relay = 0
    if relay_url_norm:
        targeted = {
            ev["pubkey"] for ev in relay_list_events
            if ev["pubkey"] in ghost_set and relay_url_norm in _r_tag_urls(ev.get("tags", []))
        }
        targets_relay = len(targeted)
        text = (
            f"{len(ghosts)} pubkeys have published only a relay list "
            f"({targets_relay} point at this relay)."
        )
    else:
        text = f"{len(ghosts)} pubkeys have published only a relay list."

    return {
        "type": "ghosts",
        "text": text,
        "pubkeys": sorted(ghosts),
        "suggested_reason": "audit: relay-list ghost",
    }


def build_burst_notices(relay_list_events):
    relay_list_events = _valid_events(relay_list_events)

    buckets = {}
    for ev in relay_list_events:
        dt = datetime.datetime.utcfromtimestamp(ev["created_at"])
        hour_key = dt.strftime("%Y-%m-%d@%H")
        bucket = buckets.setdefault(hour_key, {"count": 0, "authors": set()})
        bucket["count"] += 1
        bucket["authors"].add(ev["pubkey"])

    candidates = [
        (hour_key, bucket["count"], bucket["authors"])
        for hour_key, bucket in buckets.items()
        if bucket["count"] >= BURST_MIN_EVENTS and len(bucket["authors"]) >= BURST_MIN_AUTHORS
    ]
    candidates.sort(key=lambda c: c[1], reverse=True)

    notices = []
    for hour_key, count, authors in candidates[:BURST_MAX_NOTICES]:
        text = (
            f"{count} relay lists from {len(authors)} pubkeys arrived "
            f"within one hour on {hour_key} UTC."
        )
        notices.append({
            "type": "burst",
            "text": text,
            "pubkeys": sorted(authors),
            "suggested_reason": "audit: relay-list burst",
        })
    return notices


def build_fingerprint_notices(relay_list_events):
    relay_list_events = _valid_events(relay_list_events)

    groups = {}
    for ev in relay_list_events:
        urls = frozenset(_r_tag_urls(ev.get("tags", [])))
        if not urls:
            continue
        groups.setdefault(urls, set()).add(ev["pubkey"])

    candidates = [
        (urls, authors) for urls, authors in groups.items()
        if len(authors) >= FINGERPRINT_MIN_AUTHORS
    ]
    candidates.sort(key=lambda c: len(c[1]), reverse=True)

    notices = []
    for urls, authors in candidates[:FINGERPRINT_MAX_NOTICES]:
        text = f"{len(authors)} pubkeys share an identical relay list of {len(urls)} relays."
        notices.append({
            "type": "fingerprint",
            "text": text,
            "pubkeys": sorted(authors),
            "suggested_reason": "audit: shared relay-list fingerprint",
        })
    return notices


def build_purge_pending_notice(banned_events):
    banned_events = _valid_events(banned_events)

    counts = {}
    for ev in banned_events:
        counts[ev["pubkey"]] = counts.get(ev["pubkey"], 0) + 1

    pubkeys = sorted(counts.keys())
    if not pubkeys:
        return None

    text = (
        f"{len(pubkeys)} banned pubkeys still have stored events — "
        "run strfry delete to purge them."
    )
    return {
        "type": "purge_pending",
        "text": text,
        "pubkeys": pubkeys,
        "suggested_reason": None,
    }


def build_activity(activity_events, now_ts=None):
    activity_events = _valid_events(activity_events)
    if now_ts is None:
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    today = datetime.datetime.utcfromtimestamp(now_ts).date()

    days_by_kind = {}
    totals = {}
    for ev in activity_events:
        kind = ev["kind"]
        day = datetime.datetime.utcfromtimestamp(ev["created_at"]).date()
        delta = (today - day).days
        if delta < 0 or delta >= ACTIVITY_DAYS:
            continue
        idx = ACTIVITY_DAYS - 1 - delta
        arr = days_by_kind.setdefault(kind, [0] * ACTIVITY_DAYS)
        arr[idx] += 1
        totals[kind] = totals.get(kind, 0) + 1

    kinds_sorted = sorted(totals, key=lambda k: totals[k], reverse=True)[:MAX_ACTIVITY_KINDS]
    return [{"kind": k, "days": days_by_kind[k], "total": totals[k]} for k in kinds_sorted]


def build_report(relay_list_events, footprint_events, activity_events, banned_events,
                  banned_pubkeys, admin_pubkey_hex, relay_url, now_ts=None):
    """Assemble the full notices+activity structure from already-scanned
    event lists. Returns {"notices": [...], "activity": [...]}."""
    relay_url_norm = normalize_relay_url(relay_url)

    notices = []
    ghosts = build_ghosts_notice(
        relay_list_events, footprint_events, banned_pubkeys, admin_pubkey_hex, relay_url_norm
    )
    if ghosts:
        notices.append(ghosts)
    notices.extend(build_burst_notices(relay_list_events))
    notices.extend(build_fingerprint_notices(relay_list_events))
    purge_pending = build_purge_pending_notice(banned_events)
    if purge_pending:
        notices.append(purge_pending)

    activity = build_activity(activity_events, now_ts=now_ts)

    return {"notices": notices, "activity": activity}
