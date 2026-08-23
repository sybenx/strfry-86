# CLAUDE.md — Eighty-Six (repo: `strfry-86`)

Admin/moderation sidecar for a strfry Nostr relay. This file is a map of the project as
it stands today — what it is, what runs, where things live, and the invariants that are
actually load-bearing. It is not a history and does not record why past decisions were
made; local rationale belongs in code comments, not here.

**What this project exists to do**: an admin-signed NIP-56 report (kind `1984`) passing
through `plugin86.py`'s write policy adds every `"p"`-tagged pubkey straight to
`blacklist.json` ([plugin86.py:192-217](plugin86.py#L192-L217)) — before the report event
itself is even accepted. No HTTP request, no NIP-98 signature, and no trip through
`server86.py` are involved. Report a user from any ordinary Nostr client and they're
banned at the relay's write policy. `POST /api/ban` (below) is the separate, manual path
for banning without a report.

For the full from-the-code survey this file summarizes — every constant, every endpoint,
every page, every background loop, with `file:line` citations — see
[PROJECT-MAP.md](PROJECT-MAP.md).

## Runtime topology

Three processes live in this repo; a fourth (strfry itself) invokes one of them.

- **strfry** (not in this repo) runs `plugin86.py` as its `writePolicy.plugin`
  subprocess, piping one JSON event per line to stdin and reading one accept/reject
  line back.
- **plugin86.py** — the hot-path write-policy plugin. Checks the ban list, runs the
  auto-ban path above, appends one line per decision to `decisions.jsonl` (rotated to
  `decisions.jsonl.1` via `os.replace`, two segments retained), and independently
  spawns/hourly-respawns `server86.py` as a detached subprocess. It must never crash the
  stdin loop — every internal exception is caught and substituted with a default accept.
- **server86.py** — the admin HTTP server (`ThreadingHTTPServer`, default port 8686,
  bind `0.0.0.0`). Reads `config.json`, `strfry.conf` (located lazily per call, never a
  fixed path), every `*-cache.json`, `audit-log.json`, `domain-bans.json`. Reads
  `decisions.jsonl`(+`.1`) on every live-feed poll — never writes it, that channel is
  plugin86's alone. Runs `strfry scan`/`count`/`delete`/`compact`/`export` as
  subprocesses on demand.
- **strfry-86-updater.py** — separate, manually invoked
  (`docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py`), not
  scheduled anywhere in this repo. Offline-first source selection, syncs deployable
  files by SHA-256 diff against `manifest.json`, patches `strfry.conf`'s
  `writePolicy.plugin` line, writes/tops-up `config.json`, self-updates last, and kills
  + synchronously restarts `server86.py` every run. Never restarts or signals strfry
  itself.

## File layout

- `server86.py` — admin HTTP server: routing, auth, scans, job machinery, page/API
  handlers.
- `plugin86.py` — strfry's write-policy plugin: the auto-ban path, decision logging,
  server86 spawn/respawn.
- `strfry-86-updater.py` — installer/updater, run manually by the operator.
- `lib86/` — shared library code (`blacklist.py`, `bip340.py`, `namecache.py`, …), used
  by more than one of the three processes above.
- `common86.js` — shared client-side code: page chrome, nav, search, theme toggle,
  record-line rendering. Independently hardcodes its own copies of several
  server-side constants (see PROJECT-MAP.md §6) — a change to a server-side constant
  does not propagate here without a matching manual edit.
- `home.html`, `stats.html`, `users.html`, `bans.html`, `audit.html`, `settings.html`,
  `profile.html`, `domain.html` — the eight pages (see below).
- `tools/style86.css` — the one canonical style block (see Style block, below).
- `tools/stamp_style.py` — copies `style86.css` verbatim into every page's `<style>`;
  `--check` exits non-zero on drift.
- `tests/` — fixtures for `test.sh` (BIP-340 vectors, pre-signed NIP-98/kind-0 events);
  see `tests/README.md` for fixture provenance. Not deployable — excluded from
  `manifest.json`.
- `test.sh` — the test suite.
- `manifest.json` — deploy manifest consumed by the updater.

## Pages

Six pages, reached from the nav (`S86_NAV_LINKS` in `common86.js`): Live Feed (`/`),
Stats & Console (`/stats`), Users (`/users`), Banlist (`/bans`), Audit Log (`/audit`),
Settings (`/settings`). `/profile?hex=`/`?npub=` and `/domain?d=` are detail pages,
reached only from a row or the header search box, never from the nav.

`LEGACY_REDIRECTS` (`server86.py:114-119`) 302s `/home`, `/report`, `/authors`, and
`/userlist` to their current pages rather than serving a second copy — confirmed by
`test.sh`.

- **`/`** — public live event feed (Time/Kind/Author/Content/Result), fed by SSE
  (`GET /api/live`), newest first. The `Result` column comes solely from the decision
  log; a row with no matching decision renders `—`, never "accepted". Author and content
  are shown to logged-out visitors — a relay's accepted events are already public.
- **`/stats`** — six headline tiles (cache reads/`stat()` only, never a scan), the
  database-totals and full-walk scan panels, gift-wrap recipient/subscriber scan panels,
  and the two-panel console (write-policy decision stream + `strfry` command console).
- **`/users`** — the member table (User/NIP-05/Events/Reports/Gift-wraps/Last active/Ban)
  plus the author scan that fills it. While a scan is running, rows stream from
  `partial_authors` in `GET /api/authors`. No "First seen" column exists — nothing
  measures it without a whole-database walk per pubkey.
- **`/bans`** — Banned Pubkeys (what plugin86 actually enforces) and Banned NIP-05
  Domains (a receipt: it records that a domain's roster was read and banned at that
  point, not a live rule — plugin86 never fetches `.well-known/nostr.json` from its
  blocking write path). A domain row shows `n of m still banned`.
- **`/audit`** — server-side log of every admin action, newest first. The audit log is
  the sole undo authority; `localStorage` undo state is dead — do not resurrect it.
- **`/settings`** — two config files (`strfry.conf`, `config.json`) through one field
  builder, each with a raw-editor fallback and its own Save.

## Endpoints

Full table with line numbers: PROJECT-MAP.md §2. Highlights that matter for correctness:

- **`GET /api/audit` always returns 401.** The real, authenticated, paged audit read is
  **`POST /api/audit`** (`server86.py:5109-5121`) — every admin-only read in this project
  carries its NIP-98 auth in the POST body, including reads, because admin action history
  is not a public cache.
- `POST /api/subscribers`: saturation at `SUBSCRIBER_SCAN_LIMIT` **refuses** the
  exempt-purge action rather than labeling it — a saturated subscriber list is a floor,
  and an exempt-purge built on a floor deletes real subscribers' DMs. Returns
  `counted:{"10050":n,"10002":n}` alongside `saturated` so saturation is fact, not
  inference.
- `POST /api/recipients`: saturation at `RECIPIENT_SCAN_LIMIT` is safe to leave — a
  saturated recipient list only shortens the exempt `#p` filter, which under-deletes,
  never over-deletes. Same word, opposite implication from `/api/subscribers` above; do
  not treat the two as symmetric.
- `GET /api/authors`: empty until a scan has run; carries `modes` with measured
  durations, and, while a scan is `running`, `partial_authors` (top rows by count so
  far — no names/reporters yet).
- `GET /api/domain-bans` / `POST /api/domain-unban`: unban removes the domain-ban record
  **and** unbans every pubkey in that record's snapshot.
- `POST /api/settings/save`: `target:"relay"` rewrites only the touched field's value
  token in `strfry.conf` — comments, ordering, and blank lines stay byte-identical.
  `strfry.conf` is checked for balanced braces before any write; a copy is kept as
  `strfry.conf.bak` (a second, independently-named, timestamped backup,
  `strfry.conf.bak-<unixtime>`, is written by the updater's own conf patch — two
  mechanisms, same file). `target:"app"` raw saves are refused unless the result still
  carries a valid `admin_pubkey_hex`; that field itself is not editable through this UI,
  since it authorizes the very request that would change it.
- Live layer: one server-side strfry poll fanned out over SSE to every open `/` and
  `/stats` tab — N open tabs cost one poll, never N. Two independently-capped rings
  (durable kinds vs. NIP-16 ephemeral kinds 20000–29999), so ephemeral spam (auth,
  presence) cannot evict durable notes from the feed buffer.

## Load-bearing invariants

**NIP-98 auth.** The signed event travels inside the JSON POST body as `auth`, not an
`Authorization` header. `verify_nip98()` (`server86.py:4657`) checks event-id recompute,
BIP-340 signature, admin-pubkey match, `kind==27235`, `method=="POST"`, `u`-tag
origin+path match, `payload` tag = SHA-256 of the body minus `auth`, freshness, and
replay (keyed by event id). This is the one gate before path dispatch for every POST
endpoint — there is no per-endpoint auth check to drift.

**Console verb allowlist.** `/api/console` runs `strfry <verb> …` only when the first
token is in `CONSOLE_VERBS = ("scan", "info", "export")`; anything else is refused,
nothing executes. argv is passed as a list and `shell=True` is never used, so shell
metacharacters in the input are inert literals. The allowlist *is* the whole check —
`scan`/`info`/`export` cannot mutate the database however they're flagged, so there is no
per-verb flag table to keep in sync. The console never runs `delete` or `compact`;
purges and database compaction go through the confirm-gated Settings-page forms only.

**Atomic-write shape in `lib86`.** Files `lib86` owns (`blacklist.json`, `names.json`,
…) are written by writing to a temp file in the same directory and `os.replace`-ing it
over the target, never by truncating and rewriting in place — a crash mid-write leaves
either the old file or the new one, never a half-written one. `decisions.jsonl` rotation
in `plugin86.py` follows the same shape (write, then `os.replace`).

**Stamped style block.** `tools/style86.css` is the only place CSS is ever edited;
`tools/stamp_style.py` copies it verbatim into every page's `<style>` block, and
`tools/stamp_style.py --check` is the drift gate. Editing CSS inside an HTML file is the
bug, not the drift that follows from it. Within the block: every color is a `--var` on
`:root`, resolved with `light-dark()` against the `color-scheme` the theme toggle and the
per-page anti-flash `<head>` script both set — no page or element style in JS ever names
a color directly (the Eighty-Six rename added `.logo-ink`/`.logo-accent` classes rather
than inline colors for exactly this reason). An `@supports not (color: light-dark(…))`
block gives every `light-dark()` token a system-color fallback (`Canvas`/`CanvasText`/
`LinkText`/etc.) for browsers without the function; any token added to `:root` that uses
`light-dark()` must be added there too, or an old browser gets an unstyled page.

**Panel states.** Every scan/job panel is in exactly one of four states — never run
(cost line + button), running (server figures only), result (age abs+rel, then figures),
failed (why + fix + the previous result at its true age). A run that produces no usable
result never stamps `scanned_at` and never replaces the cache. Cache records carry
`error` (replaces a result) distinct from `warning` (accompanies one). While any job
runs, every other mutating button disables and states what it's waiting on (one global
job lock). Neither the recipient nor subscriber scan panel has a list, a checkbox, or
any control that acts on a set the page assembled — that is what keeps destructive
actions out of the scan UI.

**Rendering caps.** Every enumeration is capped at `RENDER_MAX` (500) rows; the tail is
summarized with its total, full data behind a `<details>`. Numeric results render as a
`<table>` or `<dl>`, never in a sentence or a space-padded `<pre>` — `<pre>` is reserved
for the one legitimate use, shell commands in the console and the command generator.
Not-yet-known values render `—`; `0` only when actually measured. `FIGURE_HEAD_MAX`
(`server86.py:168`) is defined but has zero read sites in `server86.py` — the figure
head/tail cap that's actually enforced is the client-side twin, `S86_FIGURE_HEAD_MAX`
in `common86.js`.

**Search box navigates, never bans.** The header search box (`#q`, Ctrl-K/Cmd-K)
accepts npub, 64-hex, or a domain, and routes to `/profile?hex=` or `/domain?d=` (or a
plain status line on invalid input) via `s86NavigateSearchInput` in `common86.js`. It
sets `window.location` only — no other side effect.

**Identity, never an avatar.** A pubkey is identified by a hue folded from the key plus
the best available label (name → nip-05 → truncated npub); no `<img>` anywhere. On
`/profile`, `about`/`website`/`picture`/`lud16` are plain text only, never a link or a
loaded image — an automatic fetch of an attacker-chosen URL would leak the admin's IP to
the account under investigation.

**Record lines.** At most three controls per record line, project-wide — `↩`, `Undo`,
expand arrow — and never an `<input>`, with one exception: the bulk-reason row on
`/bans`, bound to a checked set, which exists once and only there.

**Allowlist audit ladder** *(under review — may be simplified or removed)*. Exact index
counts: `gap = count{} − count{AUTHOR_SCAN_KINDS} − count{1059}`. Below
`GAP_NOTICE_SHARE` (0.02): one coverage line. At or above notice with no single kind at
or above `KIND_ALARM_SHARE` (0.005): a notice naming the shape, explicitly declining to
recommend an allowlist edit. Any single kind at or above `KIND_ALARM_SHARE`: a stale
alarm naming that kind. Totals alone can never report `stale` — that needs a walk no
older than itself.

## Build ritual

- After editing `tools/style86.css`, run `tools/stamp_style.py` to re-stamp every page,
  then `tools/stamp_style.py --check` to confirm no page has drifted.
- Run `./test.sh` before considering a change done.
- Deployable files are whatever `tools/make_bundle.py`'s `DEPLOYABLE_FILES` lists;
  `tests/` is deliberately excluded from both `manifest.json` and
  `strfry86-bundle.tar.gz`.
- The updater never removes a file that has left `manifest.json` — a page deleted from
  this repo stays present, inert, in every existing install indefinitely (harmless only
  because `STATIC_ROUTES` is an allowlist, not because the file is gone).
