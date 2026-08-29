# CLAUDE.md — Eighty-Six (repo: `strfry-86`)

Admin/moderation sidecar for a strfry Nostr relay. Its reason for existing: an
admin-signed NIP-56 report (kind `1984`) passing through `plugin86.py`'s write policy
adds every `"p"`-tagged pubkey straight to `blacklist.json`
([plugin86.py:192-217](plugin86.py#L192-L217)), before the report event itself is even
accepted — no HTTP, no NIP-98, no server86 involved. `POST /api/ban` is the separate
manual path for banning without a report. The admin web UI (`server86.py`) reads that
same ban list and the relay's own data to give an operator visibility and manual
control.

## Build

- Edit CSS only in `tools/style86.css`; run `tools/stamp_style.py` to re-stamp every
  page's `<style>` block, then `tools/stamp_style.py --check` to confirm none drifted.
- `./test.sh` is the test suite; run it before calling a change done.
- `tools/make_bundle.py`'s `DEPLOYABLE_FILES` list is what actually ships; `tests/` is
  excluded from it and from `manifest.json`.

## Test relay only — never production

- Anything that runs `strfry delete`, `strfry compact`, or a purge runs against
  `test-relay/` (`bin/up.sh && bin/seed.sh`). Never against a relay you did not spin up
  yourself.
- Production is read-only, and only for observing real data shape — author
  cardinality, saturation, kind distribution. Never a write, and never a measurement
  that requires deploying uncommitted code.

## Architecture

Three processes live in this repo; strfry itself does not.

- **strfry** runs `plugin86.py` as its `writePolicy.plugin` subprocess: one JSON event
  in per line on stdin, one accept/reject JSON line out per line. Confirmed against a
  real container: strfry spawns it lazily, on the first inbound event — never eagerly
  at relay startup, so a quiet relay's admin page stays down after a restart until
  something is actually published (`ensure-server86.sh` closes that gap; see README).
- **`plugin86.py`** is that plugin — the auto-ban path above, plus a decision log
  (`decisions.jsonl`, rotated to `.1`) for every accept/reject, each record naming
  which stage made the call. A background thread probes and, only if the admin port
  isn't already bound, spawns `server86.py` on a short interval — not a blind
  unconditional respawn on a long one. `plugin86.py` can also chain a second
  write-policy plugin (`config.json`'s `next_plugin`): every event it would itself
  ACCEPT is forwarded and the downstream's verdict relayed verbatim; a REJECT never
  reaches it, and a hung/dead/malformed downstream falls back to plugin86's own
  verdict permanently, never blocking strfry's write path.
- **`server86.py`** is the admin HTTP server (default port 8686). It reads/writes the
  JSON state files, reads `decisions.jsonl` (never writes it), and shells out to
  `strfry scan`/`count`/`delete`/`compact`/`export` on demand. It also answers **NIP-86**
  relay-management RPC (`supportedmethods`/`banpubkey`/`unbanpubkey`/`listbannedpubkeys`)
  for phone clients like Amethyst — dispatched on the `application/nostr+json+rpc` media
  type at any path, authorized by a kind-27235 event in an `Authorization: Nostr` header,
  and writing through the same `lib86/blacklist.py` as every other ban path.
- **`strfry-86-updater.py`** is a separate, manually-run installer/updater — not
  scheduled by anything in this repo. It syncs files from a bundle or the network,
  patches `strfry.conf`'s plugin line, and restarts `server86.py` every run. It never
  touches strfry itself.

## Key design decisions

- All state is JSON files beside the scripts (`blacklist.json`, `config.json`, the
  `*-cache.json` scan results, `decisions.jsonl`) — no database, no ORM.
- NIP-98 auth travels inside the POST JSON body as `auth`, not an `Authorization`
  header, and is the one gate before path dispatch for every mutating/authenticated
  endpoint — enforced once, not per-route. **NIP-86 is the one exception and has its own
  verifier** (`verify_nip86_auth`): the event arrives base64'd in an `Authorization`
  header and its `payload` tag hashes the RAW request bytes, not this project's
  re-serialized non-auth body, so neither contract can serve the other. It checks
  cheap-to-expensive (structure → `u` → payload → freshness → signature → admin) because
  it faces the internet and `lib86/bip340.py` is pure-Python point math at ~112ms a
  verification; a token bucket gates only that last expensive step. Auth failures return
  real 401/403 with a JSON body — never bodyless, never a 200 — and the body carries a
  fixed reason string only. Diagnostics go to `nip86-auth.jsonl`, never to the caller.
- `via` on an audit record is the fixed enum `AUDIT_VIA`
  (`dashboard`/`nip86`/`plugin`/`undo`), never free text: the actor pubkey is always the
  same admin, so `via` is the only field that answers "what changed my relay". `plugin`
  is reserved — `plugin86.py` cannot append to `audit-log.json` while that file is a
  whole-file rewrite under a lock no second process shares; auto-bans stay in
  `decisions.jsonl`, and the cheap fix later is synthesizing those records on read from
  `blacklist.json`'s `report_event_id`, not adding a second writer.
- `config.json`'s `canonical_urls` is what a NIP-86 `u` tag is matched against — never
  the request's `Host` header, or an auth event signed for another relay would replay
  here. Empty means the endpoint is off; there is no separate flag. The updater PROMPTS
  for it (`INSTALLER_PROMPTED_FIELDS`) rather than deriving it from `relay.info.url`,
  because a plausible wrong value fails every request with what looks like a client-side
  signing bug.
- The console (`/api/console`) only ever runs verbs from a fixed allowlist
  (`scan`/`info`/`export`); `delete` and `compact` exist only as separate,
  confirm-gated forms elsewhere.
- Recipient-scan saturation is safe to leave (it can only under-delete); subscriber-scan
  saturation instead refuses the exempt-purge action it would otherwise mislabel as
  complete. Neither scan carries a count cap any more — both run until they finish or
  `AUTHOR_SCAN_DEADLINE` binds, so saturation is now the rare "ran out of time" case
  instead of the routine one — but the asymmetry in what saturation MEANS for each is
  unchanged and is the reason the two get handled differently on `/stats`.
- Every scan/job panel is in exactly one of four states — never run, running, result,
  failed — and a failed run never overwrites the cached result or its timestamp.
- The author-scan kind allowlist (`AUTHOR_SCAN_KINDS`) is a floor an operator can
  extend from Settings (`extra_author_scan_kinds`), never a ceiling they can shrink —
  every scan/count/comparison that means "the allowlist" reads
  `get_effective_author_scan_kinds()` (shipped default ∪ operator additions), never the
  raw constant. Adding a kind does not retroactively cover an existing author-scan
  cache; the Users page says so (`authors_kinds_stale`) rather than presenting stale
  coverage as current.
- Settings saves rewrite only the one config field/token touched, leaving the rest of
  `strfry.conf` byte-identical; the file is brace-checked and backed up before write.
- The live feed and Stats page share a single server-side strfry poll fanned out over
  SSE to every open tab, with independently-capped buffers for durable vs. ephemeral
  event kinds.
- Legacy page paths 302-redirect to their current page rather than existing as a second
  copy.
- `lib86`'s JSON writes are atomic (temp file + `os.replace`), never truncate-in-place.
- A pubkey's identity in the UI is a color + label, never an avatar or a fetched image.

## Layout

- `server86.py` — admin HTTP server: routing, auth, scans, job machinery, pages/API.
- `plugin86.py` — write-policy plugin: auto-ban, decision log, server86 spawn.
- `strfry-86-updater.py` — manual installer/updater.
- `ensure-server86.sh` — optional Docker `HEALTHCHECK` wrapper; keeps server86.py alive
  independent of relay traffic (see README, "Keep the admin page up across restarts").
- `lib86/` — shared library (ban list, BIP-340 verify, name cache), used by more than
  one of the processes above. `bip340.py` only ever VERIFIES; signing exists solely in
  `test.sh`, self-validated against the reference vectors before anything trusts it.
- `common86.js` — shared client code: page chrome, nav, search, theme toggle, record
  lines. Hardcodes its own copies of a few server-side constants (see PROJECT-MAP.md
  §6).
- `home.html`, `stats.html`, `users.html`, `bans.html`, `audit.html`, `settings.html`,
  `profile.html`, `domain.html` — the eight pages.
- `tools/style86.css` — the one canonical style block.
- `tools/stamp_style.py`, `tools/make_bundle.py` — style stamping and deploy bundling.
- `tests/` — fixtures for `test.sh`; see `tests/README.md` for provenance.
- `manifest.json` — deploy manifest the updater diffs against.
- `test-relay/` — disposable local strfry + Eighty-Six for measurement/delete testing;
  see `test-relay/README.md`.

## Conventions

- Colors are `--var` tokens on `:root`, resolved with `light-dark()` — no page or
  script names a color literally.
- `tools/style86.css` is stamped verbatim into every page; nothing edits CSS inside an
  HTML file directly.
- No webfonts, no runtime asset fetches from this server — only the browser itself
  makes the few explicit, user-triggered external lookups (relay/profile queries).
- Record lines cap at three controls and never contain an `<input>`, except the one
  bulk-reason row on `/bans`. The header search box only navigates (`/profile`,
  `/domain`, or a status line) — it never bans.

## Where to look

`PROJECT-MAP.md` is the line-cited survey of every constant, endpoint, page, and
background loop. `test.sh` is what actually enforces the invariants above — read it for
the complete, current list.
