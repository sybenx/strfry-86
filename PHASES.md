# Implementation phases: async scanning, persisted caches, profile/domain pages

Tracks rollout of the CLAUDE.md update committed at `f078edb` ("async
scanning, persisted caches, profile/domain pages"). Not a deployable file —
not in `manifest.json`, not in the bundle, purely a repo-local roadmap so
implementation can resume across sessions without re-deriving the plan.

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

- [ ] **Phase 2 — recipients/subscribers + retention purge exemption.**
  `POST /api/recipients` (gift-wrap `p`-tag tally), `POST /api/subscribers`
  (DM-relay-list search, hostname-only matching against `relay.info`),
  both reusing Phase 1's async/polling/cache-file machinery. Feeds the
  Phase 5 command generator's subscriber-exempt gift-wrap purge.

- [ ] **Phase 3 — bulk reason editing.** `POST /api/reason`
  (`replace`/`append`, `REASON_MAX_LEN`, reload-fresh-before-write,
  `old_reason` returned for undo). `bans.html`: bulk-reason row below the
  live count (own line, never adjacent to "Unban selected"), reason
  record lines with `REASON_UNDO_MAX`-capped undo. Independent of
  Phases 1/2 beyond existing `blacklist.py` conventions.

- [ ] **Phase 4 — `profile.html`.** `POST /api/profile` (lifetime
  `--count`, one bounded scan for kind tally + previews, one bounded
  `kind:1984,#p` scan for reports against; ban status/name/scan-rank from
  in-memory lookups, no scan). New admin-only single-subject page — no
  list, no filter, no sort. The one page where automatic external name
  lookup in bulk is NOT the concern (single pubkey, deliberately opened).

- [ ] **Phase 5 — `domain.html` + command generator.**
  `POST /api/pubkeys/lookup` (`DOMAIN_LOOKUP_MAX`, `claims_domain`
  cross-check). New page: client-side `.well-known/nostr.json` fetch +
  paste fallback, roster list, sort, bulk ban. Command generator
  (`<select>` of intents + one conditional input + one `<pre>`) replaces
  the static command-block list in `common86.js` — last, because it
  depends on Phase 2's endpoints for the subscriber-exempt purge variant.

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
  prints `SKIP: ...` rather than failing when none is found (true in this
  sandbox — there's no strfry binary here, so this check has never
  actually run against real data. Whoever has a real relay should run
  `bash test.sh` there at least once).
