#!/usr/bin/env python3
"""Draft spam rules, and a replay that scores them against labeled posts.

    tools/spam_rules.py replay corpus.json

corpus.json: {"spam": [events], "other": [events], "profiles": {pubkey: kind0}}.
"spam" is what was flagged; "other" is everything else, unlabeled. The replay
prints what each rule catches in each set, and samples of "other" posts the
rules flag, for a person to judge.
"""

import json
import re
import sys
import unicodedata
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
WORD_RE = re.compile(r"\w+", re.U)
REF_PARAMS = {"ref", "referral", "invite", "code", "r", "aff", "affiliate"}
BURST_WINDOW = 3600
BURST_KEYS = 10
MANY_HASHTAGS = 5


LOOKALIKE_SCRIPTS = {"CYRILLIC", "GREEK", "CHEROKEE", "FULLWIDTH", "MATHEMATICAL", "ARMENIAN", "COPTIC"}


def script_of(ch):
    if not ch.isalpha():
        return None
    return unicodedata.name(ch, "?").split(" ")[0]


def ref_value(url):
    q = parse_qs(urlsplit(url).query)
    for k, v in q.items():
        if k.lower() in REF_PARAMS and v:
            return v[0]
    return None


def is_homoglyph_word(word):
    scripts = [script_of(c) for c in word if c.isalpha()]
    latin = scripts.count("LATIN")
    lookalike = sum(1 for s in scripts if s in LOOKALIKE_SCRIPTS)
    return latin >= 2 and 0 < lookalike < latin


def features(ev, profiles):
    text = ev.get("content", "")
    urls = URL_RE.findall(text)
    prose = URL_RE.sub(" ", text)
    return {
        "hashtags": sum(1 for t in ev.get("tags", []) if t and t[0] == "t"),
        "no_profile": ev.get("pubkey") not in profiles,
        "hosts": {h for h in (urlsplit(u).hostname for u in urls) if h},
        "ref_hosts": {urlsplit(u).hostname for u in urls if urlsplit(u).hostname and ref_value(u)},
        "homoglyphs": any(is_homoglyph_word(w) for w in WORD_RE.findall(prose)),
    }


def link_bursts(events, profiles):
    """Hosts that BURST_KEYS distinct keys link inside one window, each link carrying
    a referral code: one campaign handing every throwaway key its own code."""
    by_host = defaultdict(list)
    for ev in events:
        for host in features(ev, profiles)["ref_hosts"]:
            by_host[host].append((ev["created_at"], ev["pubkey"]))
    bursting = set()
    for host, rows in by_host.items():
        rows.sort()
        lo = 0
        for hi in range(len(rows)):
            while rows[hi][0] - rows[lo][0] > BURST_WINDOW:
                lo += 1
            if len({pk for _, pk in rows[lo:hi + 1]}) >= BURST_KEYS:
                bursting.add(host)
                break
    return bursting


# "reject" rules are meant to block on their own; "review" rules only queue a post.
RULES = {
    "ref_link_burst": ("reject", lambda f, b: bool(f["ref_hosts"] & b)),
    "stuffed_ref_link": ("review", lambda f, b: bool(f["ref_hosts"]) and f["hashtags"] >= MANY_HASHTAGS),
    "homoglyph_link": ("reject", lambda f, b: bool(f["hosts"]) and f["homoglyphs"]),
    "new_key_ref_link": ("review", lambda f, b: f["no_profile"] and bool(f["ref_hosts"])),
    "stuffed_new_key": ("review", lambda f, b: f["no_profile"] and f["hashtags"] >= MANY_HASHTAGS),
}


def classify(ev, profiles, bursting):
    f = features(ev, profiles)
    return [name for name, (_, rule) in RULES.items() if rule(f, bursting)]


def tier_of(fired):
    if any(RULES[r][0] == "reject" for r in fired):
        return "reject"
    return "review" if fired else None


def replay(corpus):
    profiles = corpus.get("profiles", {})
    sets = {"spam": corpus["spam"], "other": corpus["other"]}
    bursting = link_bursts(sets["spam"] + sets["other"], profiles)
    out = {"bursting_hosts": sorted(bursting), "rules": {r: {"spam": 0, "other": 0} for r in RULES},
           "tiers": {}, "missed_spam": [], "flagged_other": []}
    for label, events in sets.items():
        counts = {"reject": 0, "review": 0, None: 0}
        for ev in events:
            fired = classify(ev, profiles, bursting)
            for r in fired:
                out["rules"][r][label] += 1
            tier = tier_of(fired)
            counts[tier] += 1
            row = {"id": ev["id"], "rules": fired, "tier": tier, "content": ev.get("content", "")[:160]}
            if label == "spam" and tier is None:
                out["missed_spam"].append(row)
            elif label == "other" and tier:
                out["flagged_other"].append(row)
        out["tiers"][label] = {"total": len(events), "reject": counts["reject"],
                               "review": counts["review"], "passed": counts[None]}
    return out


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "replay":
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[2], encoding="utf-8") as f:
        print(json.dumps(replay(json.load(f)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
