# Implementation phases: async scanning, persisted caches, profile/domain pages

**All 5 phases below are landed — this document is now a historical record
of how the CLAUDE.md rewrite in `f078edb` was rolled out, not a to-do
list.** Kept (not deleted) because the "Design decisions worth
remembering" and "Live testing" sections below still explain WHY several
non-obvious choices were made, including one real bug found and fixed via
testing against a real strfry binary. Not a deployable file — not in
`manifest.json`, not in the bundle.

**To check what's landed**: `git log --oneline` and look for which of
`lib86/namecache.py`, `recipients-cache.json`-related code,
`/api/reason`, `profile.html`, `domain.html` exist. Update the checkboxes
below as phases land.

## Why phased

The CLAUDE.md diff that prompted this is ~2000 lines of new/changed spec
across six major features. Landing it as one changeset risked either a
quality drop or an unreviewable single diff. Phases are ordered so each
depends only on earlier ones, and each phase is a complete, tested,
working state on its own.

## Phases

- [x] **Phase 1 — async scan infra, persisted caches, widened author scan.**
  Background-thread + polling for `/api/authors/scan`, `authors-cache.json`
  persistence (survives the updater's restart-on-every-update), a new
  `lib86/namecache.py` (`names.json`) shared name cache for non-banned
  pubkeys, `AUTHOR_SCAN_MODES` (`recent`/`full`) with an audited kind
  allowlist, reports tally, singleton/composition stats, `/api/names`'s
  accept-bound widened to "banned OR in the author-scan cache." Client:
  mode radios, poll loop, composition line, hide-singletons checkbox, sort
  row, bounded "look up names for the N shown" button. `common86.js`:
  generic sort/truncate helpers, "select all" → "select visible" rename
  (label + live-count format) applied everywhere including `bans.html`.
  *Foundational — every later phase's scans reuse this async/cache
  pattern.*

- [x] **Phase 2 — recipients/subscribers + retention purge exemption.**
  `POST /api/recipients` (gift-wrap `p`-tag tally), `POST /api/subscribers`
  (DM-relay-list search, hostname-only matching against `relay.info`),
  both reusing Phase 1's async/polling/cache-file machinery. Feeds the
  Phase 5 command generator's subscriber-exempt gift-wrap purge.
  *Landed*: `_run_scan_job`/`_start_scan_job`/`_scan_status` factor the
  authors/recipients/subscribers async machinery into one shared code
  path (authors' own globals — `_authors_lock`, `_authors_job`,
  `_authors_cache`, `authors_scan_pubkeys()` — are unchanged, so
  `test.sh`'s direct access to them still works). `get_relay_url()` reads
  a `url` field from strfry.conf's `relay.info` block — NOT a standard
  NIP-11 field, so it's operator-added by hand (documented in README);
  `_hostname_of()` does the host-only match (never substring) against
  each `relay` tag on kind 10050 (DM) and kind 10002 (general) events,
  scanned separately per CLAUDE.md. `giftwrap_count` on each subscriber
  row is cross-referenced from whatever `_recipients_cache` holds at scan
  time (`null` if no recipients scan has ever completed) — never a fresh
  join, always best-effort. `GET /api/recipients` and `GET
  /api/subscribers` are left unauthenticated public reads, same stance as
  `GET /api/authors` (derived-cache reads are public throughout this
  project; only the scan-triggering POST costs a NIP-98 signature) —
  CLAUDE.md's "admin-only and never reachable logged-out" language reads
  as describing the feature (no recipient leaderboard, no page at all)
  rather than mandating server-side auth on a bounded-cache GET, and
  NIP-98 as implemented hard-requires `method: POST` with no carve-out
  for GET. Worth a second look if that reading turns out wrong.

- [x] **Phase 3 — bulk reason editing.** `POST /api/reason`
  (`replace`/`append`, `REASON_MAX_LEN`, reload-fresh-before-write,
  `old_reason` returned for undo). `bans.html`: bulk-reason row below the
  live count (own line, never adjacent to "Unban selected"), reason
  record lines with `REASON_UNDO_MAX`-capped undo. Independent of
  Phases 1/2 beyond existing `blacklist.py` conventions.
  *Landed*: `lib86/blacklist.set_reasons()` reloads from disk before
  writing (so `append` joins the CURRENT reason, not a stale one) and
  touches `reason` only — `report_type`/`report_event_id`/`banned_at`/
  `name` are untouched. `server86.validate_reason_request()` mirrors
  `validate_authors_scan_mode()`'s pattern for testability. Client-side:
  a reason-record undo can't just replay one `/api/reason` call, since
  different pubkeys in the same bulk edit can have had different prior
  reasons — `s86UndoReasonRecord()` groups the `S86_REASON_UNDO_MAX`-capped
  snapshot by `old_reason` and issues one sequential `replace` POST per
  distinct value. Above the cap, the record stores `entries: null` and
  renders "set reason on N bans — undo unavailable" with no Undo button
  at all — a partial undo would be worse than none. `s86BuildRecordLine`'s
  `onUndo` became optional to support that, and picked up `title`/
  `aria-label` on both buttons while it was already open (CLAUDE.md
  requires both; cheap to add, no layout change). Did NOT touch the
  pre-existing ban/unban/report record-line ordering, which doesn't yet
  match CLAUDE.md's newer "dismiss, label, Undo, trailing npub" spec —
  reason records don't need a trailing npub (no single subject; CLAUDE.md
  gives them no npub at all), so that gap stayed out of scope here. Worth
  a follow-up pass since it's real spec drift, just not one this phase's
  work depended on. Verified end-to-end in a browser against a real
  server86.py instance (fetch shim standing in for signEvent, since no
  NIP-07 extension is available in this environment) — bulk row hidden
  when logged out, both buttons gated on checkbox-AND-reason-text, status
  line, record creation, Undo, dismiss, and the oversized-snapshot
  "undo unavailable" path all confirmed visually.

- [x] **Phase 4 — `profile.html`.** `POST /api/profile` (lifetime
  `--count`, one bounded scan for kind tally + previews, one bounded
  `kind:1984,#p` scan for reports against; ban status/name/scan-rank from
  in-memory lookups, no scan). New admin-only single-subject page — no
  list, no filter, no sort. The one page where automatic external name
  lookup in bulk is NOT the concern (single pubkey, deliberately opened).
  *Landed*: `run_strfry_count()` is a new `--count` subprocess helper
  (raises on non-zero exit or non-numeric output, never parses loosely).
  `_report_type_for_target()` duplicates plugin86.py's dual p-tag/e-tag
  lookup rather than sharing it via lib86 — plugin86.py's hot path is the
  single most sensitive file in this project, and three lines of
  duplication isn't worth touching it for a read-only admin page.
  `compute_profile()` runs the three bounded scans and never fails the
  whole response over one sub-scan failing (`warning` accumulates
  instead); `build_profile_response()` layers ban status, name/nip05
  (via the existing `resolve_profiles()`), and this pubkey's rank/count
  in the last author-scan cache on top, all in-memory, no extra scan.
  Added a new shared `s86BuildProfileEntryField()` to common86.js (per
  CLAUDE.md, every page needs one directly above its command generator)
  and retrofitted it onto `bans.html`/`authors.html`, not just the new
  page — without it `profile.html` would have been reachable only by
  hand-typing a URL. It navigates via `?hex=`, not `?npub=`, since that
  needs no client-side bech32 ENCODER (only a decoder exists in
  common86.js) and the server/profile.html both already accept hex.
  `s86BuildCommandBlock()` gained an optional prefill argument for the
  same reason profile.html needs it pre-filled with its one subject.
  **Judgment call, flagged for a second look**: profile.html's automatic
  external name lookup (spec: "ONE automatic purplepag.es query... posted
  back through /api/names") reuses the EXISTING accept-bound from Phase 1
  ("banned OR in the author-scan cache") unchanged — meaning the lookup
  can succeed over the wire but the server will silently decline to
  persist the result for a pubkey that is neither banned nor in the
  author-scan cache (an arbitrary profile subject very often is neither).
  Widening the bound to "or is the current /api/profile subject" was
  considered and rejected: there's no session state to scope such a
  widening to "the one pubkey this admin is currently looking at" without
  adding new server-side state, and CLAUDE.md's existing bound is written
  as a hard invariant ("must never accept an arbitrary pubkey posted by
  the client"). Net effect: the automatic lookup on profile.html mostly
  helps for pubkeys already known to the system (reported → banned, or in
  the author-scan cache); for a cold, unrelated pubkey the name just
  won't get cached, even if resolved for that one page view. Verified
  end-to-end in a browser (fetch-shimmed server response, since no NIP-07
  extension is available here): logged-out view, malformed-parameter
  fallback (error line + entry field, nothing else), full render
  including ban status/profile fields (picture/website confirmed as
  plain text — zero `<img>` elements, no extra `<a href>`), kind
  breakdown with saturation note, reports-against list, previews, and the
  pre-filled command generator resolving the npub back to hex correctly.

## Live testing against a real strfry (dockurr/strfry via Docker)

Everything above had only ever been unit-tested with mocked scan
subprocesses (no strfry binary was reachable in earlier sessions). Given
Docker on this machine, ran a real `dockurr/strfry` container: seeded it
with ~900 real signed events (a throwaway pure-Python BIP-340 signer,
never merged into `lib86/bip340.py`, which stays verification-only) across
a realistic kind distribution including gift wraps and DM/general relay
lists, then exercised the actual code — `get_strfry_bin()`,
`get_relay_cwd()`'s `/proc`-based relay-process discovery, `run_strfry_count`,
`compute_authors`/`compute_recipients`/`compute_subscribers`/
`compute_profile`, the full async-job HTTP path with real NIP-98 signing,
and the `AUTHOR_SCAN_KINDS` gap check (previously never run — confirmed 0%
gap on the seeded kinds, then confirmed the check actually FAILS when 20
events of kind 20001 — CLAUDE.md's own "known open item" — were added, at
4.05% gap). All of it worked as designed.

**One real bug found and fixed**: `lib86/blacklist.py` and
`lib86/namecache.py`'s `_refresh(force=True)` was supposed to guarantee an
unconditional fresh read (used by every reload-fresh-before-write path:
`set_reasons`, `set_names`, `add`, `remove`), but it still gated the
actual re-read on `mtime != _cache_mtime` — `force` only skipped the
once-per-second throttle, not that check. Two writes close enough
together can land on the identical mtime on some filesystems (reproduced
on the container's virtiofs mount with back-to-back writes and no gap;
did NOT reproduce the same way on macOS/APFS, which is exactly why this
sat latent through every prior unit test run). Fixed by making `force`
bypass the mtime-equality check too, not just the throttle — one line in
each file. This is the kind of bug that specifically needed a real
filesystem under a real container to surface at all.

- [x] **Phase 5 — `domain.html` + command generator. This was the last
  planned phase — all five are now landed.**
  `POST /api/pubkeys/lookup` (`DOMAIN_LOOKUP_MAX`, `claims_domain`
  cross-check). New page: client-side `.well-known/nostr.json` fetch +
  paste fallback, roster list, sort, bulk ban. Command generator
  (`<select>` of intents + one conditional input + one `<pre>`) replaces
  the static command-block list in `common86.js` — last, because it
  depends on Phase 2's endpoints for the subscriber-exempt purge variant.

  *Landed*: `compute_pubkeys_lookup()` reuses `resolve_profiles()`
  unchanged (a banned pubkey's stored name still wins over a fresh local
  scan, same as everywhere else), sliced to `NAME_RESOLVE_MAX` for the
  batched kind-0 resolution while still returning a full row per posted
  pubkey. `validate_pubkeys_lookup_request()` rejects a body over
  `DOMAIN_LOOKUP_MAX` or containing any malformed pubkey outright — never
  truncates, never skips a row.

  The command generator (`s86BuildCommandGenerator` in `common86.js`,
  replacing `s86BuildCommandBlock`) implements all seven intents from
  CLAUDE.md's table. Two design calls worth recording:
  - The whole-database report, per-author kind tally, and DM-inbox audit
    intents pipe `strfry scan` through **python3**, not `jq` or an awk
    one-liner — python3 is a hard requirement of this entire project
    (server86.py/plugin86.py both run on it inside the operator's own
    container), so it's guaranteed present in a way `jq` is not, and a
    proper JSON parse doesn't break the moment `content` contains an
    embedded quote the way naive `awk -F'"'` splitting would.
  - The gift-wrap purge intent is the one place the generator does
    something beyond rendering text: it fetches `GET /api/recipients` and
    `GET /api/subscribers` (public reads) on construction, and offers
    inline "scan now" buttons (real NIP-98 POSTs, real polling) when
    either cache is missing or the subscriber cache is over 7 days old.
    This still isn't a violation of "nothing here is executed" — that
    rule is about the RENDERED commands, never about Phase 2's
    already-existing, already-legitimate admin-triggered scans that
    happen to feed this one intent's parameters.

  Found one real bug via manual testing before it shipped: the
  recipients/subscribers "scan now" buttons' failure path called the
  poll-until-idle callback regardless of whether the triggering POST
  actually succeeded — a failed auth (or any POST error) silently
  produced no feedback at all, indistinguishable from the click not
  registering. Fixed by threading the error through to the button instead
  of swallowing it.

  Live-verified against a real dockurr/strfry container (same throwaway
  signer as Phase 1-4's testing): `compute_pubkeys_lookup()` against
  three real pubkeys — one with a real kind-0 claiming the domain
  (`claims_domain: true`), one with no kind-0 at all, one claiming a
  DIFFERENT domain (correctly `false`, the "stale roster entry" case)
  — and the generated `kinds_by_author` python3 pipeline actually piped
  through a real `strfry scan` and produced the right per-kind tally.

  Also closed a test.sh gap that had been open since Phase 4 shipped
  `profile.html`: CLAUDE.md's test.sh requirements explicitly call for
  asserting that the static route table serves IDENTICAL bytes for a
  route with and without a query string containing `../` (path traversal
  is impossible by construction here, since the query string is stripped
  before matching — but that claim had never actually been exercised).
  Added by spinning up a real, ephemeral, in-process `ThreadingHTTPServer`
  running the genuine `Handler` class — the one place in this suite that
  does that, everywhere else tests call functions directly — because the
  claim is specifically about `do_GET`'s own behavior, not something a
  reimplementation with `urlparse` could prove.

## Design decisions worth remembering (Phase 1)

- `lib86/namecache.py` is a separate module from `lib86/blacklist.py` —
  different lifecycle (freely deletable/evicted vs. operator-owned,
  never auto-evicted). `blacklist.add()` imports it to drop a pubkey's
  `names.json` entry on ban — the one coupling point, enforced centrally
  regardless of whether the ban came from `plugin86.py`'s hot path or
  `server86.py`'s `/api/ban`.
- A second `POST /api/authors/scan` while one is already running returns
  the SAME `202 {"status":"running",...}` shape the original POST
  returned, not a 409 — so POST-then-GET from the same client never sees
  a state its own POST couldn't have produced.
- `STRFRY_SCAN_TIMEOUT` (5s) was renamed to `SCAN_TIMEOUT` (10s) per
  CLAUDE.md's exact constants block.
- The `AUTHOR_SCAN_KINDS` gap-check test requires a live strfry database;
  it detects the binary the same way `server86.get_strfry_bin()` does and
  prints `SKIP: ...` rather than failing when none is found (true in the
  sandbox with no Docker access — no strfry binary reachable there). It
  HAS since run for real, against a dockurr/strfry container with ~900
  seeded events — see "Live testing against a real strfry" below, which
  also covers everything else that was only mock-tested before that.
