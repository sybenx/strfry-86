# CLAUDE.md — strfry-86

## What this project is

**strfry-86** is a pure-Python moderation sidecar for a [strfry](https://github.com/hoytech/strfry) Nostr relay (repo: https://github.com/sybenx/strfry-86), deployed *inside* the operator's existing strfry Docker container with zero image changes, zero pip installs, and zero new compose services. ("86" is restaurant slang for banning someone from the establishment — fitting for a relay literally named stir fry.)

How it works, end to end:

1. The admin reports a user from any normal Nostr client (primarily jumble.social). The client publishes a NIP-56 report (`kind 1984`).
2. The strfry write-policy plugin (`plugin86.py`) sees the report. If — and only if — it is authored by the admin pubkey, every pubkey in its `p` tags is added to the blacklist. From then on, all events authored by blacklisted pubkeys are rejected.
3. A stdlib web server (`server86.py`) serves four bare HTML pages: the public ban list, the author list, a single-pubkey profile, and a domain roster. The admin logs in with a NIP-07 extension; every privileged action is authorized per-request with a NIP-98 signed event — no sessions.
4. A single self-contained installer/updater (`strfry-86-updater.py`) handles first install, config, strfry.conf rewrite, and all future updates, run via one `docker exec` command.

**No charts, no graphs, no history reporting, and no sweep that the admin did not press a button for.** The scans that exist return tallies rendered as plain text lines. Anything larger than what those bounded scans cover — a whole-database walk, or anything destructive — is emitted as a copyable `strfry` command for the operator to run in a terminal, where a ten-minute operation is visible, interruptible, and harmless. server86 has no delete endpoint of any kind.

## Hard environment constraints (do not violate)

- **Python 3 stdlib ONLY.** No pip, no venv, no third-party imports anywhere. The target is the Python that ships in the operator's strfry container. Schnorr verification and bech32 are vendored (see below).
- **Everything lives in `/config/strfry86/`** inside the container — this is on the operator's permanent `strfry_config` named volume, so it survives container recreation. Nothing is written anywhere else except the strfry.conf edit and its backup.
- **No custom Docker image, no new compose service, no entrypoint changes.** The only compose change the operator makes by hand is adding a `ports:` line for the admin page (document in README).
- **`plugin86.py` writes nothing but protocol JSON to stdout** (stderr for all logging) and never crashes on bad input — a dead plugin can wedge the relay.
- Only the admin pubkey can ban (via kind 1984, or manually via NIP-98 `/api/ban`) or unban (via NIP-98). No other trust roots.
- The admin pubkey can never end up in the blacklist (silent no-op on any attempt).
- **Every scan that can exceed ~10 seconds runs ASYNCHRONOUSLY and is polled; it is never held open inside an HTTP request.** `POST /api/authors/scan`, `POST /api/recipients`, and `POST /api/subscribers` validate auth, start ONE bounded scan in a background thread, and return `202 {"status": "running", "started_at": <unix>}` immediately. The page polls the matching `GET` every few seconds until `status` returns to `idle`, then renders the persisted result. One code path for all of them — not a fast one and a slow one that behave differently under failure.

  **This does not weaken the no-background-work rule; read that rule precisely.** What it forbids is UNREQUESTED and UNBOUNDED work — a scan on a timer, on page load, or because a cache went stale. A scan the admin pressed a labelled button to start, bounded before it was issued by mechanism 1 or 2, does not become unrequested merely because the HTTP response returned before it finished. The button is still the only thing that starts it, the single-flight lock still permits exactly one at a time, and `AUTHOR_SCAN_DEADLINE` still terminates it. Nothing may start a scan except an authenticated press; a `GET` that finds no cache returns empty and starts nothing, forever.

  Why it is worth the thread: the full author scan runs past two minutes, and no HTTP request should be held open that long by anything. Polling means no request lasts more than a second and closing the tab mid-scan costs nothing, because the result persists. The status must be visible while running — `scanning… 412,000 events read` — since a progress line is the only thing distinguishing a running scan from a broken button.

- **Every expensive scan result is persisted; every cheap one is recomputed.** The threshold is roughly ten seconds: if a scan can exceed it, its result is written to a `<name>-cache.json` in `/config/strfry86/` and reloaded at startup. This is not an optimisation, it is a correctness rule about a specific failure — **the updater kills and respawns server86 after every successful update**, so an in-memory-only result is destroyed on a schedule the operator does not control, and the page then reports "no scan yet" for work the operator deliberately paid two minutes for. Today that covers `authors-cache.json`, `recipients-cache.json`, and `subscribers-cache.json`; `names.json` already worked this way. Sub-second lookups — `/api/banned` name resolution, `/api/profile`, `/api/pubkeys/lookup` — are recomputed and must not grow cache files.

  Three rules for every one of them: they are DERIVED, never operator-owned — absent from the manifest, absent from the bundle, safe to delete at any moment, and rewritten wholesale rather than merged. They store `scanned_at` verbatim and every page renders the AGE alongside the data, because a persisted result silently presented as current is worse than no result. And they are written atomically (`.tmp` + `os.replace`) like everything else here.

- **No unbounded or background database work, ever.** No scan on server start, on a timer, or because a cache went stale. Every scan is either bounded by an authors list and issued in service of a request being answered right now, or it is triggered by a labeled button that says what it is about to do before it is pressed. `/api/authors/scan` is button-triggered and admin-only because it reads a constant-sized slice of the whole relay; the authors-bounded kind-0 name lookup behind `/api/banned` is not, because its size is fixed by the ban list itself.

  The same distinction places `/api/profile` and `/api/pubkeys/lookup`: they scan on page load without a button, because each is answering a request the admin deliberately made about a named subject — one pubkey, or one posted list — and its size is fixed by that subject, not by the relay. Opening `/profile?npub=…` IS the button. What stays button-triggered is anything whose size is set by the relay rather than by the request: `/api/authors/scan`, `/api/recipients`, and `/api/subscribers`.
- **The server never opens an outbound network connection. Ever.** All third-party contact happens from the admin's logged-in browser. There are exactly two such contacts, and both are listed here or they do not exist:
  1. **kind-0 profile lookup against `wss://purplepag.es`** (fallback `wss://relay.damus.io`), for pubkeys that could not be resolved locally. Results come back through ONE authenticated endpoint (`POST /api/names`) which cryptographically verifies every displayable claim before storing it.
  2. **`https://<domain>/.well-known/nostr.json` fetch** on the domain page, for one operator-typed domain at a time. What comes back is a name→pubkey map, not signed data: it is treated as a ROSTER CLAIM BY THAT DOMAIN, never as a verified fact about any pubkey, and it never becomes a stored `name` or `nip05` on any entry.

  Each banned pubkey is queried externally at most once, ever — results AND misses are persisted in `blacklist.json`. Names are display sugar and rosters are navigation: every page must work fully, forever, with both sockets blocked and nothing ever resolved externally.
- **Every scan must be bounded before it is issued, by one of exactly two mechanisms**:
  1. An explicit `limit` whose value is a constant in `server86.py`. **A request may SELECT among a fixed tuple of such constants by name; it may never supply a number.** `AUTHOR_SCAN_MODES` is a constant table of named scan shapes, and a request naming one of its keys selects a filter written entirely in `server86.py` — an unrecognised name is a 400, never a fallback to some default. A request carrying `{"limit": 250000}`, or its own `kinds` array, is rejected on sight; `{"mode": "full"}` is fine. A name indexing a table you can read in the source is auditable; a caller-supplied integer or filter is not.

     **A constant `kinds` allowlist may narrow a scan, and never widens it.** `{"kinds": AUTHOR_SCAN_KINDS, "limit": AUTHOR_SCAN_FULL_LIMIT}` is still bounded by mechanism 1 — the limit is doing the bounding exactly as before — while the kind filter makes the limit reach vastly further through history, because strfry serves it from the kind index rather than by post-filtering a full walk. The kinds tuple is a constant and never accepted from a request. The scan is still a window, and the moment it returns exactly `AUTHOR_SCAN_FULL_LIMIT` results it has stopped being complete and must say so.
  2. An explicit `authors` list assembled from data server86 already holds — the ban list, a scan-result cache, a posted-and-validated pubkey list.

  Nothing else counts as a bound — not `since`/`until`, which bound the time range but not the result size, and not "it is probably small." A scan whose result size is not knowable in advance from a constant or a finite list must not be issued at all.

  **A wall-clock deadline is NOT a bound, and the attempt to make it one was tried and rejected.** A whole-database option (`limit: 0`, `scan '{}'`) was specified and then killed by measurement: an unlimited body-streaming scan runs past four minutes, so it would have returned a deadline-truncated partial on every press. And `limit` is verified to return the NEWEST events while the traversal order of an UNLIMITED scan is specified nowhere — so a truncated `{}` scan yields an arbitrary slice in unknown order, possibly the oldest events in the database, presented as a survey of the relay. Deadlines remain FAILURE timeouts (`SCAN_TIMEOUT`, `AUTHOR_SCAN_DEADLINE`); a scan that hits one is an ERROR that preserves the previous cache, never a result.

## Repo layout

```
strfry-86-updater.py   # installer + updater, the only file the operator ever runs
plugin86.py            # strfry write-policy plugin (stdin/stdout JSONL)
server86.py            # stdlib http.server admin server, spawned by plugin86
lib86/__init__.py      # empty
lib86/bip340.py        # vendored BIP-340 schnorr verification (pure python reference impl)
lib86/bech32.py        # vendored bech32/npub encode+decode (pure python reference impl)
lib86/blacklist.py     # shared blacklist load/save/add/remove, atomic writes, mtime reload; missing entry keys (incl. name fields) read as null
lib86/namecache.py     # shared name cache for non-banned pubkeys (names.json)
bans.html              # public ban list page, served at /
authors.html           # admin-only active-author page, served at /authors
profile.html           # admin-only single-pubkey detail page, served at /profile
domain.html            # admin-only nip-05 domain roster page, served at /domain
common86.js            # shared client code for all four pages
manifest.json          # sha256 of every deployable file, consumed by the updater
strfry86-bundle.tar.gz # all deployable files + manifest, for offline (no-network-container) installs
tools/make_bundle.py   # regenerates manifest.json AND strfry86-bundle.tar.gz; run before every release commit
README.md
test.sh
```

Deployed layout inside the container (created by the updater):

```
/config/strfry86/
  strfry-86-updater.py
  plugin86.py
  server86.py
  lib86/...
  bans.html
  authors.html
  profile.html
  domain.html
  common86.js
  manifest.json
  config.json        # OPERATOR-OWNED: admin pubkey (hex), port, bind, contact_appeal. Never in manifest; the updater may only ADD a missing key, never change an existing value.
  blacklist.json     # OPERATOR-OWNED: the ban list, incl. resolved profile names. Never in manifest, never overwritten.
  authors-cache.json     # DERIVED CACHE: last author-scan result, verbatim.
  recipients-cache.json  # DERIVED CACHE: last gift-wrap recipient tally.
  subscribers-cache.json # DERIVED CACHE: last DM-relay-list search.
  names.json         # DERIVED CACHE: pubkey -> name/nip05/checked_at/source, for pubkeys that are NOT banned.
```

## strfry-86-updater.py

Single file, self-contained (may not import lib86 — it must run before lib86 exists). Idempotent: safe to run any number of times.

**Source selection (offline-first):** on startup, look for `strfry86-bundle.tar.gz` in the updater's own directory. If present → OFFLINE MODE: the bundle is the source; read `manifest.json` from inside it and extract files from it instead of downloading. If absent → NETWORK MODE: fetch from the repo raw URL as described below. All downstream logic (diffing, verification, config, strfry.conf, server restart, self-update) is identical in both modes.

Offline-mode specifics:
- Open with stdlib `tarfile`. Before extracting ANYTHING, validate every member name: reject absolute paths, `..` components, and links; abort loudly on violation.
- Extract each needed file to `<name>.tmp`, verify sha256 against the bundle's manifest, then `os.replace` into place — same atomicity as network mode. Only extract files that are missing or hash-differ locally.
- After a fully successful run, rename the bundle to `strfry86-bundle.tar.gz.applied-<unixtime>` so a re-run without a fresh bundle cleanly no-ops. Older applied bundles are removed by the end-of-run prune, never here.
- Self-update in offline mode: if the bundle contains a changed updater, extract it LAST, replace atomically, print "updater updated — effective next run."

Network-mode flow:

1. **Fetch `manifest.json`** from `https://raw.githubusercontent.com/sybenx/strfry-86/main/` (repo base URL is a constant at the top of the file). Manifest maps relative path → sha256 for every deployable file. `config.json` and `blacklist.json` are NEVER in the manifest.
2. **Diff against local**: sha256 each local file; download only missing/changed files. Download to `<name>.tmp` then `os.replace` (atomic). Verify sha256 of each download against the manifest before installing; abort loudly on mismatch.
3. **First-run config**: if `config.json` doesn't exist, determine the admin pubkey and write `config.json` with `{"admin_pubkey_hex": ..., "port": 8686, "bind": "0.0.0.0", "contact_appeal": ""}`. To determine the pubkey: first try to read `relay.info.pubkey` from `/config/strfry.conf`; if found and it parses as a valid pubkey, offer it as the default — "Found relay.info.pubkey <npub...> in strfry.conf — use as admin? [Y/n]". On decline, or if the field is absent/invalid, prompt for a paste (accept npub or 64-hex; decode npub via inline bech32 — small enough to duplicate in the updater). Never silently adopt the strfry.conf value without confirmation: this key is the sole root of trust for banning. (Bind inside the container must be 0.0.0.0 for the compose port mapping to work; the README tells the operator to scope exposure via the compose `ports:` line, e.g. `127.0.0.1:8686:8686`.)

   **`contact_appeal`**: an optional free-text string shown publicly on the ban list page so a banned user knows where to appeal. Prompted for once on first run, right after the admin pubkey — "Optional appeal contact, shown publicly on the admin page (email, npub, URL, or any free text) — blank for none:". An empty answer is a valid answer and stores `""`.

   On EVERY run, if `config.json` exists but has no `contact_appeal` key at all, ask the same question once and merge the answer in. Merging means: load the existing JSON, add the one key, write back via `<name>.tmp` + `os.replace`, preserving every other key and value verbatim — including keys this updater doesn't know about. A key that is present but empty is an answered question; never re-prompt on it. This top-up is the ONLY circumstance in which the updater writes to an existing `config.json`, and it never alters a value already there.

   If stdin is not a TTY, skip both prompts silently: write `""` on first run, and on top-up leave the existing file untouched rather than half-writing it.
4. **strfry.conf edit**: locate `/config/strfry.conf` (constant, documented). Decide what the edit will be FIRST; only if a byte will actually change, copy to `strfry.conf.bak-<unixtime>` and then write. A run that determines the plugin line is already correct (the steady state) must produce NO backup file at all. Then:
   - If `writePolicy.plugin` already points at `/config/strfry86/plugin86.py` → no-op.
   - If it is empty/unset → set it to `plugin = "/config/strfry86/plugin86.py"`.
   - If it points at some OTHER plugin → do NOT touch it; print a loud warning telling the operator to resolve manually.
   - Edit conservatively with line-oriented matching on the `writePolicy` block; do not reformat the rest of the file.
   - When detecting whether a plugin is already configured, ignore comment lines (first non-whitespace character `#`) — a commented-out `# plugin = ...` is NOT an active plugin. Never modify, remove, or uncomment any comment line anywhere in the file.
5. **chmod +x** `plugin86.py` (it has a `#!/usr/bin/env python3` shebang; strfry executes it directly).
6. **Restart the web server**: after any successful update, find and kill any running `server86.py` (match on cmdline via `/proc`, no pgrep dependency), then **spawn the replacement directly** — `python3 /config/strfry86/server86.py`, fully detached (`start_new_session=True`, stdin/stdout/stderr to devnull), exactly as plugin86 spawns it. Do NOT delegate the respawn to plugin86: the plugin is blocked on `stdin.readline()` and its once-per-hour check only fires when an event actually arrives, so on a quiet relay the admin page would stay down indefinitely after every update, with nothing indicating why.
   - **Wait for the old process to exit before spawning.** server86 enforces singleton by port bind and exits 0 silently on EADDRINUSE, so a replacement started while the old one still holds the port dies without a word. Poll `/proc/<pid>` until the killed pid disappears, up to ~5s, before spawning.
   - Kill by SIGTERM first; only escalate if the pid is still present at the end of the wait.
   - Verify the result: after spawning, poll for a `server86.py` cmdline in `/proc` for a couple of seconds and report `admin page restarted` or a loud `admin page did NOT come back up — check stderr`. A failed spawn is a warning, not an abort; plugin86's hourly check remains the backstop.
7. **Self-update**: if the manifest shows the updater itself changed, download it LAST, replace atomically, and print "updater updated — already effective next run."
8. **Prune** per the Retention rules below — the LAST action of the run, in both modes, after self-update, and only if everything above succeeded.
9. Exit with a clear summary: installed/updated/unchanged file counts, config status, strfry.conf status, pruned-file count, and the hint "if in doubt: docker restart <container>".

**Retention:**

- `strfry.conf.bak-<unixtime>` in the strfry.conf directory → **keep the 3 newest, delete older**.
- `strfry86-bundle.tar.gz.applied-<unixtime>` in `/config/strfry86/` → **keep the 1 newest, delete older**.

Rules that make this safe:

- Prune ONLY at the very end of a fully successful run. A run that aborted, warned about a foreign plugin, or hit a hash mismatch prunes nothing — never destroy the fallback while the current state is in question.
- Match with a strict regex anchored to the exact patterns (`^strfry\.conf\.bak-\d+$`, `^strfry86-bundle\.tar\.gz\.applied-\d+$`). Anything the operator renamed by hand, any non-matching neighbor, and any directory is invisible to the pruner. Never glob loosely, never recurse, never follow symlinks.
- Order by the integer parsed from the filename, not mtime — `docker cp` and volume restores rewrite mtimes.
- Deletion failures are non-fatal: log to stderr, continue, still exit 0.
- Print exactly what was removed (`pruned 4 old conf backups, 2 old applied bundles`).
- The counts are constants at the top of the file (`KEEP_CONF_BACKUPS = 3`, `KEEP_APPLIED_BUNDLES = 1`), not prompts or config keys.

## plugin86.py — strfry write policy

strfry spawns the plugin once and writes one JSON object per line to stdin; the plugin answers one JSON per line on stdout.

Input (relevant fields): `{ "type": "new", "event": { "id", "pubkey", "kind", "tags", "content", "sig", "created_at" }, ... }`
Output: `{"id": "<event id>", "action": "accept"}` or `{"id": "<event id>", "action": "reject", "msg": "blocked: banned pubkey"}`

Logic per event, in order:

1. Parse; on failure log to stderr, continue.
2. Author blacklisted → reject `"blocked: banned pubkey"`.
3. `kind == 1984` and `pubkey == admin` → for every valid `p` tag (64-char lowercase hex, validate, skip malformed), add to blacklist with `banned_at = created_at`, `report_event_id = id`, `reason = content`, `name`/`nip05`/`name_checked_at` all null (name resolution happens later, from the admin's browser — NEVER in this hot path: strfry blocks its entire write pipeline waiting on the plugin's reply), and `report_type` resolved as: the p tag's own third element if present, otherwise the third element of the first `e` (or `a`) tag that has one, otherwise null. This dual lookup is required by how jumble builds reports (verified against its source): profile reports carry the type on the p tag (`["p", <pubkey>, <type>]`), note reports carry it on the e tag (`["e", <id>, <type>]`) with a bare p tag. Jumble's type options are `nudity`, `malware`, `profanity`, `illegal`, `spam`, `other` (NIP-56 also defines `impersonation`; store whatever string arrives, don't whitelist). Jumble always sends `content: ""`, so `reason` will be empty for jumble reports — the type is the only signal. Then accept. Do NOT verify the signature here — strfry has already verified it, and this is the hot path.
4. Otherwise accept.

On startup (and once per hour thereafter), plugin86 ensures `server86.py` is running: spawn `python3 /config/strfry86/server86.py` fully detached (`start_new_session=True`, stdin/stdout/stderr to devnull — the plugin's stdout is sacred). server86 enforces singleton by port-bind: if the bind fails with EADDRINUSE it exits 0 silently, so repeated spawns are harmless.

Use unbuffered/line-flushed stdout. Reload the blacklist on mtime change, checked at most once per second (see `lib86/blacklist.py`).

## server86.py — page server + ban/unban/scan API

stdlib `http.server` (ThreadingHTTPServer).

**Constants** — every bound in the project, in one block at the top of the file, so the bounded-scan rule can be audited by reading them all in one place:

```python
AUTHOR_SCAN_KINDS      = (   # every kind EXCEPT 1059; see "Auditing the allowlist"
    0, 1, 3, 4, 5, 6, 7, 21, 42, 321, 445, 1000, 1018, 1040, 1111, 1311, 1337,
    1618, 1619, 1984, 1985, 2003, 4000, 4244, 5300, 6300, 7000, 9735, 10002,
    10050, 30000, 30023, 30078, 30166, 30383, 30800, 34236, 36787, 420001,
)
AUTHOR_SCAN_FULL_LIMIT = 1500000 # saturation at this value ends the completeness claim
AUTHOR_SCAN_MODES      = {       # selectable by NAME only; the request never supplies a filter
    "recent": {"limit": 20000},                                          # all kinds, newest 20k, ~3s
    "full":   {"kinds": AUTHOR_SCAN_KINDS, "limit": AUTHOR_SCAN_FULL_LIMIT},
}
AUTHOR_SCAN_DEADLINE   = 240     # seconds; a FAILURE timeout for the async scans, not a bound
REASON_MAX_LEN         = 500     # characters accepted for a ban reason
REASON_UNDO_MAX        = 50      # entries whose prior reasons are snapshotted for undo
RECIPIENT_SCAN_LIMIT   = 250000  # newest kind-1059 events tallied for storage accounting
SUBSCRIBER_SCAN_LIMIT  = 50000   # kind-10050/10002 events searched for this relay's own URL
SCAN_TIMEOUT           = 10      # seconds; every SYNCHRONOUS scan subprocess
REPORT_SCAN_LIMIT      = 5000    # kind-1984 events read to build the reports-against tally
NAME_RESOLVE_MAX       = 500     # pubkeys per batched kind-0 lookup
NAME_CACHE_MAX         = 20000   # entries retained in names.json
PROFILE_EVENT_LIMIT    = 500     # events read for one pubkey's kind tally
PROFILE_PREVIEW_MAX    = 20      # event previews retained from that read
PROFILE_REPORT_LIMIT   = 100     # kind-1984 events read for reports against one pubkey
DOMAIN_LOOKUP_MAX      = 1000    # pubkeys accepted in one /api/pubkeys/lookup body
RENDER_MAX             = 500     # list rows rendered client-side before truncation
```

`AUTHOR_SCAN_DEADLINE` bounds the three asynchronous scans (`/api/authors/scan`, `/api/recipients`, `/api/subscribers`). `SCAN_TIMEOUT` bounds every synchronous one: the `/api/banned` name lookup, the three `/api/profile` subprocesses, and the `/api/pubkeys/lookup` name scan.

### Routes

- **Static routes are an explicit allowlist**, never a filesystem path join: `/` → `bans.html`, `/authors` → `authors.html`, `/profile` → `profile.html`, `/domain` → `domain.html`, `/common86.js` → `common86.js` (`Content-Type: application/javascript`). Match on the PATH ONLY, with any query string stripped before matching and never otherwise inspected server-side — `/profile?npub=…` and `/domain?d=…` serve the identical static bytes to every requester, and the parameter is read by the page's own JavaScript from `location.search`. Anything else 404s. There is no directory serving and no path derived from the request, so path traversal is not mitigated here — it is impossible. `config.json`, `blacklist.json`, `names.json`, and the three `*-cache.json` files sit in the same directory and must never be reachable over HTTP.

- `GET /api/banned` → `{"admin": "<hex>", "contact_appeal": "<string>", "banned": [{"pubkey", "npub", "banned_at", "reason", "report_type", "report_event_id", "name", "nip05", "name_checked_at"}]}`. `report_event_id` is the id of the admin's kind-1984 report that caused the ban (null for manual bans) — already-public event data, and it lets the page derive the report record lines. Public read is fine. `contact_appeal` is echoed verbatim from `config.json`, or `""` if the key is absent, null, or not a string — this endpoint must never 500 over a hand-edited config. Re-read it from `config.json` on mtime change (the same once-per-second check `lib86/blacklist.py` uses) so a hand-edit takes effect on the next page load without restarting anything.

  **Name resolution, in two layers.** If the blacklist entry already carries a `name`/`nip05` (written by `POST /api/names`), serve those and do nothing else for that pubkey. For every other entry, resolve from the LOCAL strfry database: run `scan '{"kinds":[0],"authors":[<uncached hex>]}'` via subprocess (see **strfry scan execution rules**; scan is a read-only LMDB read, safe while the relay runs, and bounded by the authors list), parse each event's `content` JSON, take `display_name || name` for `name` and the `nip05` string for `nip05` (same event, same scan — `nip05` is served as-is and NEVER verified; verification would require outbound HTTP to arbitrary domains). Cache results through the shared name cache; re-query only pubkeys that are uncached or were full misses (both fields null) older than 24h, and batch all of them into ONE scan call per request, capped at `NAME_RESOLVE_MAX`. Local results are NOT written to `blacklist.json` — the local database is already the source of truth for them. If the subprocess fails for any reason, log to stderr and return `name: null, nip05: null`; the endpoint must never break because name lookup broke.

  `name_checked_at` is served verbatim and means one thing only: an EXTERNAL lookup was attempted for this pubkey. It is set by `POST /api/names` and by nothing else — never by a local scan, hit or miss. An entry whose `name` and `nip05` are both null AND whose `name_checked_at` is null is the unresolved set the admin's browser may take to purplepag.es; everything else it leaves alone. Entries written before these fields existed simply lack the keys, and a missing key reads as null — no migration step, ever.

### strfry scan execution rules

Every scan subprocess server86 issues follows these, without exception.

- **Verified against the live target** (record the strfry version and date beside these):
  - `--count` accepts a `#p` tag filter, returning 0 for an absent pubkey rather than erroring. `/api/profile`'s reports-against query depends on this.
  - **`limit` returns the NEWEST matching events**, confirmed by decoding `created_at` from `scan '{"limit":3}'` and finding them seconds old. The entire author-scan feature inverts if this ever stops holding, so re-verify it on any strfry upgrade — it is a one-line check.
  - `scan '{}'` parses and `scan --count '{}'` returns promptly, but a full body-streaming `scan '{}'` takes minutes (see the measurement block below). `--count` walks the index without emitting bodies; the author scan cannot, since it needs `pubkey`/`kind`/`created_at` per event. A fast `--count` is therefore no evidence about scan cost.
- `--count` is NOT documented in strfry's public README; it was verified against the source, where it prints the number of matching events read from the index without streaming bodies. Pin the strfry version this was verified against in a comment, and treat a `--count` invocation returning non-numeric output as a failure of that scan rather than parsing it loosely.
- The strfry BINARY path is DISCOVERED once at startup, not assumed on PATH: use `shutil.which("strfry")`, else the first path in `("/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry")` that exists and is executable — dockurr/strfry ships the binary at `/app/strfry`, which is NOT on PATH for a detached process, while the official image installs to `/usr/local/bin`. Cache the discovered path for the process lifetime. If no candidate is found, don't crash: the `/api/banned` name lookup returns `name: null` as usual, and the scan endpoints return their previous cache with a `warning` stating `strfry binary not found` and listing the candidates tried.
- Invoke as an argv LIST with `shell=False`: `[<discovered binary>, "--config", "/config/strfry.conf", "scan", <optional "--count">, json.dumps(<filter>, separators=(",", ":"))]`. The filter is exactly ONE argv element containing raw compact JSON. Never build the command as a single string, never `.split()` a command string, never wrap the filter in quotes, never backslash-escape its quotes (escaped quotes reach strfry verbatim and it exits 1), never `shell=True`.
- Every scan subprocess MUST pass `cwd=`: `strfry.conf`'s `db` is conventionally a path relative to wherever the relay process itself was launched (e.g. dockurr/strfry runs `./strfry` from `/app` with `db = "./strfry-db/"`), which is NOT server86's own cwd, so a scan with no cwd override fails with `mdb_env_open: No such file or directory`. Resolve the cwd by locating the running strfry relay process via `/proc` (match on the discovered binary's basename plus a `relay` argv element) and reading `/proc/<pid>/cwd`; fall back to the discovered binary's parent directory if no relay process is found. Cache it for the process lifetime, re-deriving only if the located pid disappears.
- Capture stderr and include its TAIL (last ~300 chars) in failure messages — strfry's loguru output puts a startup banner first and the actual error as the LAST line, so truncating from the head discards the error itself; always slice from the end.

### Name cache — `names.json` (`lib86/namecache.py`)

One cache serves every name lookup in the project: `/api/banned`, the author scan, `/api/profile`, and `/api/pubkeys/lookup`. It exists because the author list made repeat resolution expensive in a way the ban list never did — a ban list is dozens of pubkeys and stable; an author list is thousands and regenerates on every scan.

It is a SEPARATE module from `lib86/blacklist.py` because the lifecycles are opposite: freely deletable and evicted, versus operator-owned and never auto-evicted. `blacklist.add()` imports it to drop a pubkey's `names.json` entry on ban — the one coupling point, enforced centrally regardless of whether the ban came from `plugin86.py`'s hot path or `server86.py`'s `/api/ban`.

Shape: `{"<hex>": {"name": <str|null>, "nip05": <str|null>, "checked_at": <unix>, "source": "local"|"external"}}`. Loaded once at startup into memory, mtime-reloaded like `blacklist.json`, written back atomically and never more than once per request.

- **Banned pubkeys are not stored here.** Their names live in `blacklist.json`, which is operator-owned and permanent; duplicating them would create two sources of truth that drift. On an unban nothing is restored — the entry earns a fresh lookup like anything else.
- **Eviction**: at `NAME_CACHE_MAX` entries, drop the oldest `checked_at` down to 90% before writing. Eviction is not a correctness event — it costs one rescan of the local database.
- **Re-query policy**: full misses (both fields null) are eligible again after 24h for `source: "local"`, and NEVER automatically for `source: "external"` — the same once-ever discipline `name_checked_at` enforces on the ban list. A miss is usually a pubkey with no kind-0 anywhere, not a failure to retry.
- **Deleting the file is always safe** and is the documented remedy for anything that looks stale. No moderation decision may rest on it: names and nip05 here are unverified self-claims, useful for reading a list and never sufficient grounds for a ban.

### Write and scan endpoints

- `POST /api/unban` → body `{"auth": <signed nostr event>, "pubkeys": ["<hex>", ...]}` → removes each (the whole entry, including any resolved name and `name_checked_at` — a later re-ban starts fresh and earns one new lookup), returns `{"ok": true, "removed": [...]}`.

- `POST /api/ban` → body `{"auth": <signed nostr event>, "entries": [{"pubkey": "<npub or 64-hex>", "reason": "<optional>"}, ...]}` → for each entry: decode npub via `lib86/bech32.py` if needed, validate, skip malformed; add to blacklist with `banned_at = now`, `reason` (empty string if omitted), `report_type = "manual"`, no `report_event_id`. Admin pubkey is silently skipped. New entries get `name`/`nip05`/`name_checked_at` null. Returns `{"ok": true, "added": [...], "skipped": [...]}`.

- `POST /api/names` → body `{"auth": <signed nostr event>, "queried": ["<hex>", ...], "events": [<raw kind-0 events exactly as received>, ...]}`. NIP-98 admin auth. The ONLY way externally-fetched profile data enters the system, and the server trusts NONE of it on arrival. **The browser posts signed events, never extracted strings** — a name detached from its signature would be an unverifiable claim stored forever, whereas a kind-0 is self-certifying regardless of the route it traveled. For each posted event: recompute the NIP-01 event id and require it to match, verify the BIP-340 signature via `lib86/bip340.py`, require `kind == 0`, require `pubkey` to be in `queried`, AND require `pubkey` to be **either currently banned or present in the current author-scan cache**. That second clause is still a bound assembled from server-held state, so the endpoint only ever accepts claims about pubkeys it already knows of. It must never accept an arbitrary pubkey posted by the client — that would turn a verification endpoint into an open write path into `names.json`.

  **Storage routes by ban status, never by request**: verified results for banned pubkeys are written to `blacklist.json`; verified results for scan-cache pubkeys are written to `names.json` with `source: "external"`. A pubkey banned between the lookup and the write lands in `blacklist.json` — the post-reload ban check decides, not the pre-lookup state. Drop anything that fails (a dropped event is not an error — a hostile relay must not be able to 500 this endpoint), keep only the newest `created_at` per pubkey, and extract `display_name || name` and `nip05` SERVER-SIDE from the verified event's `content`. Then RELOAD `blacklist.json` from disk before writing — the lookup took seconds and the admin may have banned or unbanned someone meanwhile — set `name`/`nip05` on surviving hits, and stamp `name_checked_at = now` on EVERY pubkey in `queried` that is still banned, hits and misses alike. The miss stamp is what makes "once, max" true. (A miss is unverifiable by nature — you cannot sign an absence — and is accepted because it is only an admin-authenticated timestamp, not displayable data. The operator can hand-delete an entry's `name_checked_at` to make it eligible again; document in README.) Returns `{"ok": true, "named": [...], "stamped": <count>}`. Idempotent.

- `GET /api/authors` → the last scan result plus live job status: `{"status": "idle"|"running", "started_at": <unix|null>, "progress": <events read so far|null>, ...result fields}`, or `{"status": "idle", "scanned_at": null, "authors": []}` if there has never been one. **Never scans, and never starts one** — a GET that finds an empty cache returns empty forever; only an authenticated POST starts work. Public read, same stance as `/api/banned`: it is derived from public events, and the page is admin-gated as UX rather than as a secret. The result is persisted to `authors-cache.json` under the general persisted-cache rule.

- `POST /api/authors/scan` → body `{"auth": <signed nostr event>, "mode": "recent"|"full"}`. NIP-98 admin auth. Starts the scans in a background thread and returns `202 {"status": "running", "mode": <string>, "started_at": <unix>}` without waiting. On completion the cache is replaced and persisted. Admin-only because it causes work on the operator's live relay, while reading the result does not.

  `mode` names a key of `AUTHOR_SCAN_MODES`; anything else is a 400 with no scan and no fallback default. The filter is the constant stored under that key, serialized as-is — always with an explicit limit, never an empty filter, never a `kinds` array from the request.

  **Single-flight.** A second POST arriving while a scan is running does NOT start another and does NOT 409: it returns the SAME `202 {"status": "running", ...}` shape the original POST returned, so a POST-then-GET from one client never sees a state its own POST could not have produced.

  **`recent` vs `full`, and why both exist.** `recent` reads the newest 20,000 events of ALL kinds: fast, and the honest answer to "what is happening on my relay right now," gift wraps included. `full` reads every event of `AUTHOR_SCAN_KINDS` up to `AUTHOR_SCAN_FULL_LIMIT`, which on the measured relay is the entire non-giftwrap history and is therefore a genuinely complete author list rather than a window. That is possible only because gift wraps are excluded: they are 64.8% of the measured database and their authors are single-use keys, so dropping them removes most of the work and none of the moderation value.

  **`full` is complete until it saturates, and must announce the transition.** When the scan returns exactly `AUTHOR_SCAN_FULL_LIMIT` results, the relay has outgrown the constant: the result is now a newest-first window. Set `saturated: true`, and the page must switch its own language from "all authors" to "the most recent 1,500,000 events of these kinds."

  **`full` is complete only for enumerated kinds.** Nostr filters cannot express "not kind 1059," so `AUTHOR_SCAN_KINDS` is a hand-maintained allowlist and an event of an unlisted kind is invisible to this scan. The page therefore says "all moderation kinds," never "everything"; true completeness is a command block (`scan '{}'`) and deliberately not a page.

  **Streaming and memory.** Read stdout line by line, take `pubkey`, `kind`, and `created_at`, discard the parsed event immediately; never accumulate event bodies, never hold more than one line at a time. The accumulated state is two dicts — per-pubkey `{count, last_seen}` and per-kind counts — both bounded by distinct pubkeys and kinds rather than by events read. Memory stays flat at any limit; the limit constant is what bounds the time.

  **Any scan that streams event bodies tallies everything cheap in the same pass.** Parsing a line to read `pubkey` already costs the parse; taking `kind` from the same object and folding it into a dict costs one more lookup. So no scan in this project reads events and reports only the one number it was asked for: `full` and `recent` both return `kinds` and `singleton_kinds` alongside the author tally, and the command generator's histogram intent returns the total event count, the per-kind histogram, the distinct-author count, and the gift-wrap share from ONE pass.

  **Deadline is a failure, not a result.** A scan still reading at `AUTHOR_SCAN_DEADLINE` terminates the subprocess, DISCARDS what it read, preserves the previous cache, and returns `warning: "scan exceeded 240s in mode full — nothing changed"`. It must never return the partial tally: the traversal order of a truncated scan is not guaranteed to be the newest-first order `limit` provides, so a partial tally is not a smaller window but an unknown one, and rendering it under a provenance line that claims a span would be a lie the page cannot detect. There is no `partial` field anywhere in this design; the only truncation the API reports is `saturated`, which is honest because the window it describes is the newest-first one `limit` guarantees. If an operator hits the deadline repeatedly, the answer is `recent` mode or a terminal command, not a longer deadline.

  **Reports tally**: a second scan, `scan '{"kinds":[1984],"limit":REPORT_SCAN_LIMIT}'`, tallying `p` tags to produce, per pubkey, the number of DISTINCT reporter pubkeys — never the raw report count, which one determined actor can inflate to any number. Report authorship is not restricted to the admin here: this is "who has anyone accused," a signal to look at, never itself a reason to ban. Merge the two tallies as a UNION: a pubkey with reports but no events in the window appears with `count: 0` rather than being dropped, since default-ranking by reports must not hide someone who was reported and then stopped posting. If the report scan fails, the field is null everywhere, the default sort falls back to event count, and `warning` says so — never render zeros that look like an absence of reports.

  **Saturation is reported, not implied**: whenever a scan reads exactly its limit, older matching events exist and were not counted. Set `reports_saturated: true` when the report scan hits `REPORT_SCAN_LIMIT`. `REPORT_SCAN_LIMIT` is 5,000 because that is comfortably above what any relay running this today produces — but a tally that has quietly become a floor while still being rendered as a total is exactly the kind of dishonesty this project refuses elsewhere. The page must say the cap was reached, in words, wherever the numbers are shown.

  **Names**: resolve through the shared name cache, batched, capped at `NAME_RESOLVE_MAX` and taken from the top of the default sort — nobody reads name labels on the 900th-ranked author. **Skip single-event authors entirely**: an author with exactly one event in the window is overwhelmingly a throwaway key with no kind-0 to find, and including them would spend the whole cap on the least useful rows. All three scans are paid for by the same admin press; nothing here touches purplepag.es.

  Response: `{"scanned_at": <unix>, "mode": <string>, "limit": <int>, "saturated": <bool>, "events_read": <int>, "span_start": <unix>, "span_end": <unix>, "kinds": {"<kind>": <count>}, "singleton_kinds": {"<kind>": <count>}, "reports_saturated": <bool>, "reports_scanned": <int>, "warning": <string or null>, "authors": [{"pubkey", "npub", "name", "nip05", "count", "last_seen", "reporters"}]}`, sorted by `reporters` descending then `count` descending. `span_start` is the OLDEST `created_at` actually seen and `span_end` the newest — the honest description of what the number covers, since 20,000 events is six hours on one relay and four months on another.

  **`singleton_kinds`** counts, per kind, the authors whose FINAL tally is exactly 1 — computed in one pass over the per-pubkey dict after the read, costing nothing.

  **What this list is**: in `recent` mode, the authors among the newest 20,000 events — a window, never everyone. In `full` mode, every author of every `AUTHOR_SCAN_KINDS` event in the database, until `saturated` says otherwise. Neither mode covers gift-wrap authors, by design. The question "who has ever posted anything at all, including kind 1059" remains a terminal command.

- `POST /api/recipients` → body `{"auth": <signed nostr event>}`. NIP-98 admin auth. Storage accounting for gift wraps: scans `{"kinds":[1059],"limit":RECIPIENT_SCAN_LIMIT}` and tallies the `p` tag of each event, returning `{"scanned_at", "events_read", "span_start", "span_end", "saturated", "recipients": [{"pubkey", "npub", "name", "nip05", "count"}]}`, sorted by count descending. Same async path, single-flight lock, deadline, persistence, and name resolution as the author scan; `GET /api/recipients` serves the cache and never scans.

  **Why this exists.** Kind 1059 is the majority of the measured relay's events and disk, and an operator cannot make a retention decision about most of their disk without knowing who it belongs to. The `p` tag says exactly that, is unencrypted, is already on the operator's disk, and is readable with one `strfry scan` whether or not this endpoint exists.

  **Retention exemption — the reason this endpoint pays for itself.** Nostr filters have no negation, so "delete every gift wrap except my subscribers'" cannot be written directly. It can be enumerated: take the recipient tally, subtract the pubkeys returned by `/api/subscribers`, and emit `strfry delete --filter '{"kinds":[1059],"until":<now-90d>,"#p":[<every recipient who is NOT a subscriber>]}'`. The page renders this as a copyable command block, never as a button. When the recipient list is large enough that the enumerated filter becomes unwieldy, chunk it into several commands rather than falling back to the blanket version.

  **What it must not become.** Gift-wrap CONTENT is encrypted and the sender key is single-use and unlinkable; nothing here reveals who is talking to whom, and nothing may be added that tries. Three hard rules: admin-only and never reachable logged-out; recipient counts are for retention and capacity decisions and are NEVER a moderation signal, since receiving DMs is not conduct; and no view is added that correlates recipients with senders, timing, or each other. Rate-limiting kind 1059 in the write policy is the legitimate abuse control; a recipient leaderboard is not.

- `POST /api/subscribers` → body `{"auth": <signed nostr event>}`. NIP-98 admin auth. Same async path and persistence as the other two. Scans `{"kinds":[10050],"limit":SUBSCRIBER_SCAN_LIMIT}` and returns every pubkey whose DM-relay-list names THIS relay: `{"scanned_at", "relay_url", "saturated", "subscribers": [{"pubkey", "npub", "name", "nip05", "listed_at", "giftwrap_count"}]}`. Also runs the same query over kind 10002 (general relay lists) and returns those separately — a pubkey listing this relay for general use is a different relationship from one listing it for DMs. `GET /api/subscribers` serves the cache and never scans.

  **Matching is host-only.** Compare the parsed hostname of each `relay` tag against the relay's own hostname, case-insensitively, ignoring scheme, port, trailing slash, and any path. `wss://relay.example`, `wss://relay.example/`, and `relay.example` are one relay; substring matching on the raw tag would match `relay.example.evil.com`. The relay's own URL comes from `relay.info` in `strfry.conf`; if absent, the endpoint returns `relay_url: null` and an empty list rather than guessing, and the page says the URL is unconfigured.

  This result is an INPUT TO A DESTRUCTIVE COMMAND — the retention purge exempts exactly these pubkeys — so it must never be silently empty because a restart cleared it. An empty list would generate a purge command that deletes the DMs of every subscriber, the single worst outcome this document is trying to prevent. **When the cache is absent or older than 7 days, the purge generator refuses to emit the subscriber-exempt form and says why.**

  This is public data — these pubkeys published a signed event specifically to announce that this relay is their DM inbox — so surfacing it discloses nothing.

- `POST /api/reason` → body `{"auth": <signed nostr event>, "pubkeys": ["<hex>", ...], "reason": "<string>", "mode": "replace"|"append"}`. NIP-98 admin auth. Sets or extends the `reason` on existing blacklist entries in bulk. Returns `{"ok": true, "updated": [{"pubkey", "old_reason", "new_reason"}, ...], "skipped": [...]}`.

  - **Edits `reason` and NOTHING else.** `report_type`, `report_event_id`, and `banned_at` are provenance derived from the admin's signed kind-1984 event; rewriting them would make the entry assert something the signed event never said. `reason` is free text and is empty on every jumble-originated ban, which is exactly why this endpoint exists: the report type is a fixed six-item vocabulary, so without editable reasons an operator's entire taxonomy collapses into two categories.
  - A pubkey not currently banned is SKIPPED, never created. This endpoint cannot ban anyone.
  - `mode: "append"` joins to a non-empty existing reason with `" — "` and behaves as `replace` when the existing reason is empty. Reject a reason over `REASON_MAX_LEN` rather than truncating.
  - Reload `blacklist.json` from disk before writing, exactly as `/api/names` does, and write back atomically.
  - **The reason is PUBLIC.** It renders on `bans.html` for logged-out visitors. Bulk-setting a reason is bulk-publishing it, and the page must say so next to the control.
  - `old_reason` is returned for every updated entry so the client can build an undo.

- `POST /api/profile` → body `{"auth": <signed nostr event>, "pubkey": "<npub or 64-hex>"}`. NIP-98 admin auth. Everything server86 can say about ONE pubkey, from the local database plus state it already holds. Admin-only because it scans; it needs no button, because opening `/profile?npub=…` IS the deliberate act.

  Three subprocesses, each bounded by the single author or by a constant:
  - `--count` on `{"authors":[X]}` → lifetime event total. The one number here that covers the whole database.
  - ONE scan `{"authors":[X],"limit":PROFILE_EVENT_LIMIT}` → per-kind tally plus the `PROFILE_PREVIEW_MAX` newest events kept as previews (`kind`, `created_at`, and `content` truncated server-side to 280 chars). This is the read that answers "is this spam or a busy human."
  - `{"kinds":[1984],"#p":[X],"limit":PROFILE_REPORT_LIMIT}` → reports AGAINST this pubkey: reporter pubkey and npub, report type, content, time.

  Plus, with no scan at all: ban status and full blacklist entry if banned, name/nip05 from the shared cache, and rank/count/last_seen from the author-scan cache if the pubkey appears there.

  Response: `{"pubkey", "npub", "name", "nip05", "profile": {"about", "picture", "website", "lud16"} or null, "total_events": <int>, "kinds": {...}, "kinds_window": <int>, "kinds_saturated": <bool>, "previews": [...], "reports": [...], "reports_saturated": <bool>, "banned": <bool>, "ban": {...} or null, "scan_rank": <int|null>, "scan_count": <int|null>, "warning": <string|null>}`.

  **`kinds` describes only the most recent `PROFILE_EVENT_LIMIT` events, never the lifetime** — `total_events` is the lifetime number and the two must never be presented as the same measurement. Set `kinds_saturated` when the scan returned exactly its limit, and label it on the page. Every string in `profile` is attacker-authored; `picture` and `website` are rendered as PLAIN TEXT URLs and never as `<img>` or `<a href>`.

- `POST /api/pubkeys/lookup` → body `{"auth": <signed nostr event>, "pubkeys": ["<hex>", ...], "domain": "<string>"}`. NIP-98 admin auth. Answers "what do you know about these pubkeys" for a roster the browser fetched from a `.well-known/nostr.json`. Reject a body with more than `DOMAIN_LOOKUP_MAX` pubkeys outright — never silently truncate a list the operator is about to act on. Reject any malformed pubkey the same way rather than skipping it: a roster row that vanished between fetch and render is worse than an error.

  The posted list IS the authors bound. One batched kind-0 scan for pubkeys not in the name cache, capped at `NAME_RESOLVE_MAX`; everything else is dict lookups against the blacklist, the name cache, and the author-scan cache.

  Response: `{"domain": "<echoed>", "results": [{"pubkey", "npub", "name", "nip05", "banned", "ban_reason", "scan_count", "claims_domain": <bool>}]}`.

  **`claims_domain`** is the cross-check that makes this page worth building: true when the pubkey's own kind-0 `nip05` ends in `@<domain>`. A pubkey the domain lists that does not claim it back is a stale roster entry; a pubkey claiming the domain that the roster omits is an impersonator — and the author list's nip05 filter shows you the latter while this page shows you the former. Neither the roster nor the kind-0 claim is cryptographic proof of anything; `claims_domain` compares two unverified claims and is displayed as exactly that.

### Reference measurements

Every performance figure in this document comes from here. Any figure quoted elsewhere states whether it is DB-wide or from a window; the two differ by a lot and conflating them has already caused one wrong constant.

Reference relay: **2,628,121 events, of which 1,702,655 are kind 1059 (64.8% DB-wide)**.

| Measurement | Result |
|---|---|
| Non-giftwrap events in DB | 925,466 |
| `full` scan with the current allowlist | 909,056 events in **137s** (~6,625/s) — 98.2% coverage, 1.8% gap |
| Kind-indexed scan throughput | ~7,240 events/sec |
| Unfiltered `scan '{}'` throughput | ~4,814 events/sec — 2,628,121 events in **546s** (~9 min) |
| `full` scan at `AUTHOR_SCAN_FULL_LIMIT` | ~226s |
| Earlier twelve-kind allowlist | 514,127 events in 71s — missing 411,339 events, 44% of the moderation-relevant database |

`AUTHOR_SCAN_FULL_LIMIT` is 1,500,000 because the relay is already at 909,056: a 500,000 cap would have shipped ALREADY SATURATED, and 1,000,000 left only 10% growth — roughly a year — before the completeness claim quietly lapsed. The constant must sit well above present reality, not just above it. At the cap the scan is ~226s, which is why `AUTHOR_SCAN_DEADLINE` is 240, and the ordering matters: saturation (honest, labelled) is reached before the deadline (failure), so growth degrades the claim rather than breaking the feature.

**~1,500,000 non-giftwrap events is the ceiling of this design**, not a number that can keep rising. Past it the scan runs into `AUTHOR_SCAN_DEADLINE` before it saturates, which inverts the ordering above: the feature starts failing instead of honestly narrowing. At that point `full` mode must become an operator-run terminal command feeding a file server86 reads — a larger change than raising a constant. Whoever finds `full` mode timing out should read this paragraph rather than increasing the deadline again.

**Composition, measured on a 20,000-event `recent` window** (strfry version pinned in the same comment as the `--count` verification):

```
all kinds:  1059:17588  5:858  1:459  20001:387  7:312  30078:83  6:42  0:38
singletons: 1059:17588  5:803  7:41   1:40      30166:13 30078:12 10002:10 10050:9
```

Kind 1059 is **17,588 events across 17,588 authors — a 1.000 singleton ratio, not an approximation**. NIP-17 gift wraps are signed by a freshly generated key per message by specification, so the ratio is structural and will hold on every relay carrying DM traffic. Three consequences the design depends on:

1. **Those authors cannot be banned in any meaningful sense.** The key is used once and never returns; a blacklist entry for it blocks nothing that was ever going to arrive. This is the justification for the single-event hide defaulting to ON and for skipping name resolution on singletons — the rows are inert, not merely noisy.
2. **The visible moderation surface is far smaller than `events_read` suggests.** In this window, 88% of events and ~93% of authors were gift wraps. An operator reading "20,000 events" reasonably assumes a 20,000-event view of their relay's behaviour, and on a DM-carrying relay that is off by nearly an order of magnitude. The composition line must therefore also report the multi-event figures. This is also the entire justification for `full` mode: excluding gift wraps is what turns a 546-second whole-database walk into a 137-second complete author list.
3. **The hidden set is not homogeneous and must be itemised.** Kind 5 contributed 803 singleton authors in the same window — accounts whose only activity was a deletion request, which are real accounts, not throwaway keys. Hiding them by default is still correct, but the hide control must state the breakdown by kind, or it silently conceals a category the operator did not consent to hiding.

Kind 9735 zap receipts remain the theoretical runner-up for singleton bulk but cluster on a few zapper pubkeys instead of scattering, and did not appear in the measured window.

**Gift-wrap share is measured DB-wide, never extrapolated from a window.** On the reference relay 1059 is 88% of the recent 20,000-event sample but 64.8% of the database — the recent share is higher because DM traffic is growing.

### Auditing the allowlist — a required release step, not a suggestion

`AUTHOR_SCAN_KINDS` is hand-maintained and WILL drift as Nostr adds kinds, and its failure mode is silent: authors simply do not appear, and the page keeps calling the list complete. So the gap is computed, not assumed:

```
gap = scan --count '{}'  −  scan --count '{kinds: AUTHOR_SCAN_KINDS}'  −  scan --count '{"kinds":[1059]}'
```

All three terms are index counts answered in seconds. **A gap above 2% of the non-giftwrap total means the allowlist is stale**; run the histogram intent in the command generator to identify what is missing and extend the tuple. This check found the original twelve-kind list missing 411,339 events — including kind 2003 (NIP-35 torrents) at 258,290, more numerous than every kind-1 note on the relay, whose authors were entirely invisible.

**A recent-window sample cannot substitute for this.** A kind used heavily two years ago and since abandoned holds its events forever and appears in no recent scan; the 20,000-event sample that seeded the original list showed neither 2003 nor 30166 at meaningful volume. Only a whole-database count can find them.

**Known open item**: kind 20001 appears at 387 events in the measured window above and is NOT in `AUTHOR_SCAN_KINDS`. Either it belongs in the tuple or there is a reason it doesn't; resolve it the next time the gap check runs against a live database.

### NIP-98 auth

Required on `/api/unban`, `/api/ban`, `/api/reason`, `/api/authors/scan`, `/api/recipients`, `/api/subscribers`, `/api/names`, `/api/profile`, and `/api/pubkeys/lookup` — ALL checks must pass, else 401 JSON error:

1. Signature valid per BIP-340 over the NIP-01 serialized event id (use `lib86/bip340.py`; recompute the event id and check it matches `auth.id` before verifying the sig).
2. `auth.pubkey == admin_pubkey_hex`.
3. `auth.kind == 27235`.
4. `method` tag is `POST`; the `u` tag's path matches the endpoint being called exactly (lenient on host/origin — reverse proxies change it). An auth event signed for one endpoint is never accepted at another.
5. `abs(created_at - now) <= 60`.

No sessions, cookies, or tokens.

## Client pages — `bans.html`, `authors.html`, `profile.html`, `domain.html`, `common86.js`

Four pages. **The rule was never "two pages" — it is that the ban list and the author list must never share a document.** That split is a safety property: the two lists carry destructive controls pointing in OPPOSITE directions — "Unban selected" on one, "Ban selected" on the other — over lists that look identical and share the same filter, select-visible, and live count. On a single page the button you reach after filtering and scrolling depends on which region you are inside, and the failure mode is mass-unbanning people you meant to purge. Separate documents make it structural: `bans.html` has no ban-selected control in its DOM at all.

`profile.html` and `domain.html` do not touch that rule. Neither is a second copy of an existing list: the profile page has ONE subject and no list at all, and the domain page has a roster that comes from outside the relay and carries a ban control only. A page is admissible when it cannot be confused with another page's list; it is not admissible merely because the content is related.

Every page carries the identical `<style>` block, the identical login flow, the identical record lines, and the identical command generator. New pages inherit chrome; they never invent it.

### `common86.js`

Everything the pages share lives in ONE locally-served file, served by server86 from `/common86.js` with `Content-Type: application/javascript`. This is mandatory, not a preference: the duplicated surface would otherwise include NIP-07 login, NIP-98 event construction and signing, the filter, select-visible and its live count, the sort controls, the `textContent` rendering rules, the timestamp formatter, the npub link builder, the profile entry field, the record lines, and the command generator — that is most of the client code and ALL of the security-relevant parts. Two copies of signing logic is how an auth fix lands in one file and not the other. **Anything used on a second page moves into `common86.js` in the same commit that introduces the second use.**

This does not breach "no libraries, no CDN": it is the operator's own file, served from the operator's own server, vendored exactly like `lib86/`. It is a deployable file — in `manifest.json`, in the bundle, hash-checked by the updater like everything else.

All four pages are still raw HTML with no framework, no build step, and no bundler. Each page has its own small `<script>` block for page-specific wiring.

### Shared chrome (identical on every page)

`<meta name="viewport" content="width=device-width, initial-scale=1">`. One `<style>` block per page, byte-identical across all four, containing ONLY these layout rules — nothing else, browser defaults throughout:

```css
body { max-width: 40em; margin: 0 auto; padding: 0 1em; }
li { overflow-wrap: anywhere; }
@media (max-width: 600px) { body { font-size: 1.15em; } }
```

The `padding: 0 1em` is REQUIRED — without it, content sits flush against the screen edge on mobile. The `overflow-wrap: anywhere` is REQUIRED — npubs are 63-char unbreakable strings and will overflow the viewport horizontally without it. Do not remove either in the name of minimalism.

- **Cross-page navigation**: plain `<a>` elements directly under the `<h1>`, separated by a bare `·`. `bans.html` and `authors.html` link to each other; `profile.html` and `domain.html` link to both. No nav bar, no styling, no active-state logic, no breadcrumbs, no history manipulation — `location.href` and the browser's own back button are the whole navigation model.
- **Profile entry field** (admin only, every page, directly above the command generator): one `<input type="search">` accepting an npub or 64-hex pubkey, and a "View profile" button navigating to `/profile?npub=<npub>`. Invalid input sets a plain status line and navigates nowhere. Enter in the field submits it. This is deliberately a typed/pasted entry rather than a link on every row: it keeps npubs copyable as plain text in lists and record lines without turning each one into a touch target.
- **Login, and why remembering it is safe**: `window.nostr.getPublicKey()` on the "Login with extension" button. A successful match stores the admin pubkey in localStorage and every page reads it on load to decide what to reveal, so navigating between pages does not re-prompt. This grants NOTHING: there are no sessions and no tokens, every privileged action is still individually signed and verified server-side, and a user who hand-edits that localStorage value gets a page full of buttons that all fail at the server. It is a UI hint, not a credential. A "log out" control clears it.
- **Timestamps**: render unix times as `YYYY-MM-DD@HH:MM UTC` (derive from `toISOString()`, replace the `T` with `@`, drop seconds/milliseconds and the `Z`, append " UTC"), italicized via an `<i>` wrapper. Bold/italic come from semantic tags with browser default styling — no CSS additions.
- **Untrusted string rule**: profile names, nip05 identifiers, and report reasons are all attacker-influenced. Build `<b>`/`<i>`/`<a>` elements with `createElement` and set `textContent`; never `innerHTML`, on any page, ever. nip05 is display text only: never linkified, never verified.
- **Filter field**: an `<input type="search">` on each page. On every `input` event, hide (via `style.display`) each `<li>` in THAT PAGE'S list whose `textContent` does not contain the query as a case-insensitive substring — no debounce, no server round-trip, no re-fetch. Matching on `textContent` means the filter covers everything rendered. Re-apply after every list re-render and after a name lookup fills names. An empty query shows everything. Scope every handler to its own list container rather than looping the document: `<details>` content stays in the DOM when collapsed and would otherwise be filtered too.
- **"Select visible" checkbox** (labelled exactly that, never "Select all"): toggles the checkboxes of only the entries the filter currently SHOWS (hidden `<li>`s untouched) — filter-then-select-visible is the bulk workflow on every list. The behaviour has always been filter-scoped; the old label was lying by omission on a control that mass-unbans. Resets to unchecked on every list re-render, re-sort, and filter change. Entries with no checkbox (already-banned authors, the admin's own pubkey) are skipped.
- **Live count**: a `<b>` reading "<n> selected of <m> shown", empty at zero so it renders nothing while logged out. Both numbers are required — "12 selected" on a filtered list of 4,000 is the sentence that should have read "12 selected of 12 shown". Updated on every checkbox change, select-visible toggle, filter input, re-sort, and list re-render.
- **Sort controls** (wherever a list has more than one meaningful order): a plain row of `<button>`s reading `sort: <field> · <field> · …`, the active one wrapped in `<b>`, a second click on the active field reversing direction and appending ` ↑`/` ↓`. Sorting is `array.sort()` over the in-memory result followed by re-appending the existing nodes — no re-fetch, no re-scan, no server round-trip, no table element. Three rules, all safety rather than polish: **re-apply the filter after every re-sort**, **clear the checked set and reset select-visible after every re-sort** (a selection carried across a re-order is the mass-unban failure mode in its purest form), and never sort a list whose rows the user cannot see the sort key for.
- **`RENDER_MAX`**: no list renders more than `RENDER_MAX` rows. Beyond that, render the first `RENDER_MAX` after sorting, plus a line reading `showing top 500 of 12,481 — filter to narrow`. Truncation happens AFTER sort and filter, so filtering always searches the full result set.
- **"Copy purge command" button** (admin only, every page with a list): operates on the checked set. Builds `strfry delete --filter '{"authors":["<hex>", ...]}'` and copies it to the clipboard — it does NOT run anything. Bans are forward-looking; deleting a user's existing events is destructive and irreversible, and belongs in a terminal where the command can be read before it is run. Also render the command in a `<pre>` below the button, so the clipboard is a convenience rather than the only path. Disabled with nothing checked. It is NOT absorbed into the command generator, because it operates on the current checked set, which the generator has no access to.
- **Command generator** (admin only, every page, inside a `<details>`): ONE `<select>` of intents, ONE input that appears only when the selected intent needs one, ONE `<pre>` holding the rendered command, ONE copy button. This replaced a wall of static blocks: nine `<pre>`s stop being read, and a command block nobody reads is worse than absent, because the terminal is where this project sends everything it refuses to do itself.

  | Intent | Input |
  |---|---|
  | Count all events | — |
  | Whole-database report: total, kind histogram, author count, gift-wrap share (~9 min) | — |
  | Event kinds by author | pubkey |
  | Delete all events by author | pubkey |
  | Gift-wrap retention purge | days (default 90) |
  | Who lists this relay as their DM inbox | — |
  | Fetch a domain's nostr.json | domain |

  Rules:
  - **Never substitute unvalidated text into a rendered command.** A pubkey is decoded and re-encoded to canonical hex before it reaches the `<pre>`; a domain is hostname-validated; days is an integer. Nothing here executes, but a moderation tool that renders whatever it is handed teaches a habit that is wrong everywhere else.
  - **Destructive intents render their test first.** When the selection is a `delete`, render the equivalent `scan --count` line with the IDENTICAL filter directly above it, labelled as the count. The operator sees how many events the command destroys before copying the command that destroys them. This is not optional.
  - The gift-wrap purge renders the subscriber-exempt form whenever `/api/recipients` and `/api/subscribers` have both been run this session, with the blanket form below it and visibly blunter. If the subscriber cache is absent or older than 7 days, it refuses the exempt form and says why.
  - Nothing here is executed, nothing is fetched, no output ever returns to the page.

  **Flags blurb**, four plain lines above the generator, because these four facts explain every command it emits: `--config` is mandatory or strfry reads the wrong database; `--count` returns a number without streaming event bodies, which is why counting is seconds and the histogram is minutes; `scan` reads and `delete` destroys while taking the SAME filter syntax, so any filter can and should be tested with `scan --count` before being run with `delete`; and a filter is one shell argument in single quotes, so its inner quotes are never escaped.

- **Record lines** (admin only, every page, rendered from shared code over the same localStorage): a persistent log of admin actions.

  **Element order within each line is fixed**: the "↩" dismiss button LEFTMOST, then the label, then the "Undo" button, then the **full npub as plain text, last**. The npub wraps onto a second line naturally — that wrap is the design, not a defect to style away. This ordering keeps ↩ and Undo apart with the label between them and leaves Undo flanked by inert text on BOTH sides; previously Undo was the line's last element and sat adjacent to the next record's ↩ across the line break. The npub is NOT a link — the profile entry field is how you go look at one, so a record line has exactly two touch targets no matter how long it grows. The locked CSS block permits no margin, padding, or gap rules, so DOM order is the only lever; flex and gap were considered here and rejected. Never render a record line with an empty label, since the label supplies the separation.

  **Both buttons carry `type="button"`, a `title`, and a matching `aria-label`** — a bare ↩ glyph has no accessible name at all, and `title` does nothing on touch, so neither attribute is sufficient alone.

  **Names are resolved at RENDER time, not capture time.** localStorage stores only `pubkey`, action, timestamp, and (for unbans) the original reason; `name` and `nip05` are looked up by pubkey against the current `/api/banned` payload every time the lines are drawn. Lines already sitting undismissed in an admin's browser therefore gain names the moment a build ships — no stored-key backfill, no migration. Unban records are the one exception and must snapshot `name`/`nip05` at capture, because the server no longer knows those pubkeys after the unban; older unban records simply lack the fields and render name-only.

  Label shape: the display name in `<b>` when known, the nip05 as plain text after it when known, and neither rendered when unknown — in which case the trailing npub is the only identifier, which is a perfectly acceptable resting state. Four kinds:
  - **Ban records**: after a successful ban from ANY page, one line per banned pubkey: "banned **<name>** <nip05>", then Undo, then the npub. Undo signs and POSTs a single-pubkey `/api/unban`.
  - **Unban records**: after a successful "Unban selected", ONE line per action: "unbanned **<name>** <nip05>" plus the trailing npub for a single pubkey, or "unbanned <n> pubkeys" with no npub for more. The unbanned entries (pubkey, npub, name, nip05, reason) are captured from the list before the re-fetch discards them; Undo re-bans all of them via `/api/ban` with each original reason (re-bans therefore get `report_type = "manual"`).
  - **Reason records**: one line per bulk edit — "set reason on <n> bans" — whose Undo POSTs `/api/reason` again in `replace` mode with each entry's `old_reason`, restoring the exact prior state including empties. The snapshot is stored in localStorage and capped at `REASON_UNDO_MAX`; above that the line reads "set reason on 200 bans — undo unavailable" and renders NO Undo button. A truncated undo is worse than none.
  - **Report records**: derived, not stored — for every entry in `/api/banned` whose `report_event_id` is non-null and whose id (`<report_event_id>:<pubkey>`) is not in the dismissed list: "reported **<name>** <nip05> — <report_type>", then Undo, then the npub. Undo signs and POSTs a single-pubkey `/api/unban`; ↩ adds the id to a localStorage dismissed list (capped at the 1000 newest).

  A failed Undo reports on the record line itself, not only in the page's global status line — by the time the admin has scrolled, a message detached from the row it concerns is a message about nothing.

  **At most the 20 newest record lines of all kinds combined are ever rendered**, newest first; everything older is auto-dismissed — dropped from the rendered list and, for ban/unban/reason records, deleted from localStorage. This is a hard cap, not a "show more" affordance: these lines sit ABOVE the list, in the space reserved for controls that must stay reachable, so a relay with 400 reported bans would otherwise push every control off the screen on first login. Stored records keep the 200 newest; the 1000-id dismissed list stays as a backstop it should never approach. Records survive reloads, sessions, and navigation between pages, and render only when logged in as admin.

### `bans.html` — `GET /`

Public. For everyone, this page loads the ban list and whatever names the server could resolve locally — the only work that causes is one authors-bounded kind-0 read. It has no control that could start anything larger. The one thing reserved for the admin's own logged-in load is the external name-resolution flow below.

Page order is fixed, top to bottom, with ALL non-list UI above the list so controls stay reachable when the list is thousands of entries long:

1. `<h1>strfry-86</h1>`, then the links to `/authors` and `/domain`
2. "Login with extension" button (plus its status text: "this key is not the admin" / "a NIP-07 extension is required"), then the name-resolution status line (admin only, present only when the flow ran this page-load)
3. Profile entry field (admin only, hidden until login)
4. Command generator (`<details>`, admin only, hidden until login)
5. Manual ban form (admin only, hidden until login), followed by the record lines
6. Plain text: "These npubs are banned from this relay." — visible to everyone
7. The appeal line — visible to everyone, present only when `contact_appeal` is non-empty
8. The filter field, placeholder "filter bans" — visible to everyone, like the list it filters
9. "Select visible" checkbox (admin only), then "Unban selected" and "Copy purge command" (admin only), then the live count, then the bulk-reason row on its own line below them
10. The ban list

- Ban list loads for everyone from `/api/banned`: one `<li>` per ban containing, in order: the display name wrapped in `<b>` if known; the nip05 as plain text if known; the npub as a raw `<a href="https://njump.me/<npub>" target="_blank">` link; the ban time wrapped in `<i>`; report type and reason. Omit the name/nip05/type/reason portions cleanly when null or empty (jumble reports always have an empty reason). The npub stays a link HERE, where the list is the page's whole subject and every row is a deliberate reading target — unlike record lines, which sit in the control region above a list and must not accumulate touch targets.
- **Display names and NIP-05** come from the server (`name` and `nip05` in `/api/banned`, either read from the local database or persisted in `blacklist.json` by a past external lookup; the page cannot tell which and does not care). No localStorage name cache — the server is the cache, shared across every browser the admin uses.
- **Name-resolution flow** (admin only; one of exactly two paths by which this project contacts a machine the operator does not run — and it runs in the admin's browser, never in server86):
  - Trigger: after `/api/banned` renders AND the stored login says admin, compute the unresolved set — entries with `name` and `nip05` both null and `name_checked_at` null. The local database has ALREADY been consulted by the time this response arrives, so this set is exactly "not resolvable locally, never asked externally". If the set is empty (the steady state), NOTHING happens: no socket, no POST, no status line. Logged-out visitors never trigger any part of this.
  - If non-empty: open ONE WebSocket to `wss://purplepag.es`, send a single batched REQ `{"kinds":[0],"authors":[<unresolved hex>]}`, collect until EOSE or a 5s timeout; on failure to CONNECT, retry once against `wss://relay.damus.io`. Then POST everything received, verbatim and unparsed, plus the list of pubkeys actually queried, to `/api/names`. Re-fetch `/api/banned`, re-render, re-apply the filter.
  - **"Queried" means a REQ actually reached a relay and completed** (EOSE or timeout). If both connections fail, POST nothing and stamp nothing — the next admin page load simply tries again. Stamping pubkeys that were never really asked about would permanently orphan them as false misses.
  - The flow may run again after a re-fetch (e.g. a manual ban just added an unresolved entry) but never re-queries a pubkey already attempted in this page session; that in-page guard plus the persisted stamps make loops impossible.
  - **Status line**, plain text, rendered whenever the flow ran: `resolved <n> locally, <m> via purplepag.es, <k> unresolved`. The outbound connection must be visible in the page itself, not only in a network tab.
  - Why automatic-on-page-load is acceptable here where the author list requires a button: the set is self-extinguishing. Hits AND misses persist server-side, so in steady state the unresolved set is only the bans since the admin's last visit — usually zero. Kind-1984 bans are pubkeys the admin already reported in a public event, so the query discloses nothing new. **Manual bans have no public report, so this lookup is the first external disclosure of interest in that pubkey** — a single-pubkey query from the admin's own browser and IP, judged an acceptable trade. **This reasoning does NOT transfer to the author list**, which is thousands of pubkeys, regenerates on every scan, and therefore never extinguishes.
- **Appeal contact**: taken from the `contact_appeal` field of the same `/api/banned` response — no second request, no separate endpoint. If it is a non-empty string (after trimming), render one plain line directly below "These npubs are banned from this relay.": the fixed prefix "To appeal, contact:" followed by the operator's value inserted with `textContent`. Never linkified, never `innerHTML` — arbitrary config text gets the same treatment as report reasons, even though the operator owns it. If absent, empty, or whitespace-only, render nothing at all: no empty element, no placeholder, no "not configured" text. Shown identically logged in and logged out.
- **Logged-out state (default)**: checkboxes, "Select visible", "Unban selected", "Copy purge command", the command generator, the profile entry field, the record lines, and the manual-ban form are all hidden (not merely disabled). Only the heading, the navigation links, the explanatory line, the appeal line, the filter field, the ban list, and the "Login with extension" button are visible.
- "Unban selected" → build a kind-27235 event with `u` + `method` tags, `window.nostr.signEvent`, POST `/api/unban`, add one unban record line, re-fetch, re-render.
- **Bulk reason row** (admin only, `bans.html` only): a text input placeholder `reason for the selected bans — shown publicly`, then two buttons, **"Replace reason"** and **"Append to reason"**. Two buttons rather than a mode selector: the difference between overwriting fifty reasons and extending them is exactly the thing that must be legible at the moment of pressing, and a `<select>` you did not look at is not legible. Both disabled with nothing checked or an empty input. On press: sign, POST `/api/reason`, add one record line, re-fetch, re-render, re-apply the filter.

  It sits on its own line BELOW the live count, never inline with "Unban selected". Editing reasons is not destructive and unbanning is; the two must not be neighbours, for the same reason ↩ and Undo are not.

  The workflow this enables, and why it is on the ban page rather than the author page: filter to a substring, "Select visible", type a real reason, replace. Jumble's six-item report vocabulary is the only classification a jumble-originated ban ever carries, so this is how an operator's own taxonomy gets built at all.
- **Manual ban form** (admin only): a text input for one or more npubs (whitespace- or comma-separated; hex also accepted), an optional reason input, and a "Ban" button → sign, POST `/api/ban`, clear the inputs, re-fetch, re-render. **It accepts pubkeys only — never a domain.** Banning every npub in a domain's roster from a single text field, with no preview and no count, is the worst failure mode available in this project; that workflow belongs on `domain.html`.

### `authors.html` — `GET /authors`

Admin only. Find who is currently posting, and ban them in bulk.

1. `<h1>strfry-86 — active authors</h1>`, then the links to `/` and `/domain`
2. "Login with extension" button and status text
3. Profile entry field
4. Command generator (`<details>`)
5. Record lines
6. The scan mode selector and scan button, then the provenance line and the composition line
7. The domains summary (`<details>`)
8. The filter field, placeholder "filter authors — name, nip05, or npub", then the "hide single-event authors" checkbox
9. The sort row
10. "Select visible" checkbox, "Ban selected", "Copy purge command", the optional reason input, and the live count
11. The author list

- **Logged out**: heading, navigation links, and the login button only. No list, no scan control, no filter — nothing that could start work or leak a list. There is no public view of this page.
- **Scan mode selector**: two radio buttons, not a dropdown, because the two modes answer different questions rather than offering more of the same thing — `recent activity (newest 20,000 events, all kinds)` and `full author list (every note, reaction, report and profile — excludes DMs, ~2 minutes)`. The page posts the MODE NAME, never a number or a filter. The scan button's label restates the selection before the press: `build full author list (~2 minutes — you can close this tab)`. **The reassurance is the point**: the result persists server-side and the page only polls for it, so an admin who navigates away loses nothing, and telling them so is what stops them watching a progress line for two minutes.
- **Polling**: while `status` is `running`, disable both scan buttons, poll the `GET` every 3 seconds, and render `scanning… <progress> events read` as plain text where the provenance line will go. On reaching `idle`, render the result normally. A page loaded fresh into a running scan picks up the same polling from the same status field — there is no client-side job state, so nothing to lose or desynchronise. If a poll fails, keep polling and say `connection lost — still scanning` rather than declaring failure; the scan is server-side and unaffected by the browser. When a `full` result comes back `saturated`, the page's own wording drops the word "full" everywhere it appears. Nothing scans on page load, on login, or on a timer.
- **Provenance line**, rendered whenever a result is present, as plain text: `<events_read> events, <span_start> → <span_end>, <n> authors — scanned <scanned_at>`, with all timestamps in the shared format. This line is not optional. It is what makes the list honest: the page shows a window, not "everyone," and the span tells the admin whether that is six hours or four months on their relay.
- **Composition line**, directly below it: the top kinds in the window from `kinds`, then the singleton breakdown from `singleton_kinds`, then the multi-event figures — e.g. `18,516 of 19,004 authors posted exactly 1 event; of those, 17,588 were kind 1059 and 803 were kind 5 — 1,484 events from the 488 authors who posted more than once`. This answers a question the page previously could not: a scan dominated by one-event authors is usually NIP-17 gift wraps, whose keys are fresh per message by design and therefore unbannable. Render the kind numbers plainly and do not editorialise beyond naming well-known kinds (0 profile, 1 note, 1059 gift wrap, 9735 zap receipt, 1984 report).
- **"Hide single-event authors" checkbox, DEFAULT ON**, directly under the filter. On a real relay this is 90%+ of the rows and approximately none of the moderation targets. The count it hides is stated next to it, BROKEN DOWN BY KIND (`hiding 18,516 single-event authors — 17,588 kind 1059, 803 kind 5, 125 other`), so it is never a silent omission; unchecking it is one click.
- **Sort row**: `sort: reports · events · name · last seen`. **Default is reports descending**, then events descending as the tiebreak — the author three different people reported matters more than the author who posted most, and count-only ordering buried them. `name` sorts entries WITH a resolved name first (then alphabetically), which doubles as "show me who is identifiable". Per the shared sort rules: filter re-applied and selection cleared on every re-sort.
- **Reports column**: `<n> reports from <m>` is wrong and must not be rendered — show the DISTINCT reporter count only, as `reported by <m>`. Suppress the phrase entirely at zero rather than rendering `reported by 0`. When `reports_saturated` is true, the composition line must carry `report tally is a FLOOR — the 5,000-event cap was reached and older reports were not counted`, in those terms. When the report scan failed outright, sorting falls back to events and the line says so.
- **Author list**: one `<li>` per author, in current sort order: the display name in `<b>` if known; the nip05 as plain text if known; the npub as plain text (not a link — use the profile entry field, or the njump link on the profile page); the event count; the distinct reporter count when non-zero; the time of that author's most recent event in `<i>`. A checkbox precedes each entry EXCEPT authors already in the ban list (render the plain text "(banned)" instead) and the admin's own pubkey. The page fetches `/api/banned` alongside the scan result to determine this.
- **Name resolution here is bounded by what you are looking at, never by what the list contains.** The server resolves `NAME_RESOLVE_MAX` names locally as part of the scan. For the remainder, the page offers a BUTTON — `look up names for the <n> shown without one` — which queries purplepag.es for at most the visible, non-singleton, unresolved rows and POSTs the results to `/api/names`. This is deliberately not automatic, unlike the ban page: that set self-extinguishes and this one does not, so automatic would mean a bulk query of your relay's author graph, from your IP, on every scan. A moderator resolving the fifty names in front of them is what a normal client does; enumerating eighteen thousand is not.
- **The filter matches name and nip05**, because it matches `textContent` and both are rendered — searching `nostrmag.com` finds every resolved author claiming that domain. Two honest limits, both stated on the page: it only covers rows whose names were actually resolved (`names resolved for top 500 of 12,481 authors`), and **a kind-0 `nip05` is an unverified self-claim**. The filter is for reading a list. For acting on a domain, use `domain.html`.
- **Domains summary** (`<details>`, admin only): the nip05 domains seen among resolved authors, ranked by author count, each a link to `/domain?d=<domain>`. Pure derivation from data already in hand — no scan, no request.
- "Ban selected" → build the entries list from the checked set with the shared reason input's value as each entry's reason, sign, POST `/api/ban`, add ban record lines, re-fetch both, re-render.
- A scan result is held by the server, not the page: a reload re-reads it from `GET /api/authors` without re-scanning. The button is the only thing that scans.

### `profile.html` — `GET /profile?npub=<npub>`

Admin only. Everything the relay knows about ONE pubkey, on one page, so that judging an account does not require leaving for njump. Reads `npub` (or `hex`) from `location.search`, decodes client-side via the shared decoder, signs a kind-27235 event with `u` = `/api/profile`, and renders the response. Bad or missing parameter → a plain error line and the profile entry field, nothing else.

1. `<h1>` with the display name if known, else the truncated npub; then the links to `/`, `/authors`, and `/domain`
2. "Login with extension" button and status text
3. The full npub as plain selectable text, and beside it a single `<a href="https://njump.me/<npub>" target="_blank">njump</a>`
4. Ban status: `(banned)` with reason, report type, and ban time if banned — plus a "Ban" or "Unban" button with the shared reason input, signing the same endpoints every other page uses
5. Profile fields from kind 0: about, website, picture, lud16 — each as PLAIN TEXT, labelled
6. Counts: lifetime event total, the kind tally, distinct reporters
7. Reports against this pubkey
8. Recent event previews
9. Command generator (`<details>`), pre-filled with this pubkey

- **`picture` and `website` are rendered as plain text URLs and NEVER as `<img src>` or `<a href>`.** An `<img>` pointing at an attacker-chosen host is an outbound connection from the admin's browser, disclosing their IP on page load, to a URL supplied by the very account under investigation. That defeats the entire no-outbound design more thoroughly than anything else on any page, and it would happen automatically, invisibly, and to every profile viewed. The URL as text is fully sufficient for a moderator, who can decide to open it.
- **Two different measurements, never conflated**: `total_events` covers the whole database; the kind tally covers only the most recent `PROFILE_EVENT_LIMIT` events. Label them separately and, when `kinds_saturated` is true, say `kind breakdown covers the most recent 500 events, not all <total>`.
- **Reports against this pubkey**: one line each — reporter npub, report type, reason, time — with the reporter's npub as plain text. Show the distinct reporter count above the list, and when `reports_saturated` is true say the cap was reached. **Reports from anyone are a signal to look, never grounds in themselves**: only the admin's own kind-1984 events ban anyone, and a brigade of reports from a hundred fresh keys is a thing this page must let the admin SEE rather than something it acts on.
- **Recent event previews**: kind, time, and the truncated content of each, as plain text with the untrusted-string rule in full force. This is the section that answers the actual question — is this spam, a bot, or a busy human — and is the reason the page exists.
- **Name and nip05 fall back outward**: local database, then the name cache, then, if still unresolved, ONE automatic purplepag.es query for this single pubkey, posted back through `/api/names`. Automatic is correct here and nowhere else in bulk: one profile lookup for a page the admin deliberately opened is exactly what any Nostr client does.
- No list, no filter, no select-visible, no sort. The page has one subject; it cannot be confused with the ban list or the author list, which is what makes a third and fourth page admissible at all.

### `domain.html` — `GET /domain?d=<domain>`

Admin only. The authoritative counterpart to the author list's nip05 filter: a kind-0 `nip05` is a claim BY a pubkey, while `.well-known/nostr.json` is a roster published BY the domain. When the question is "who does this domain vouch for," only the second one answers it.

1. `<h1>strfry-86 — domain roster</h1>`, then the links to `/` and `/authors`
2. "Login with extension" button and status text
3. A domain input (pre-filled from `?d=`) and a "Fetch roster" button
4. A textarea for pasting `nostr.json` contents directly, inside a `<details>` labelled "fetch failed? paste it here"
5. The provenance line
6. The filter field, then the sort row
7. "Select visible", "Ban selected", "Copy purge command", the reason input, the live count
8. The roster list

- **Fetch happens in the browser**, `https://<domain>/.well-known/nostr.json`, on button press only. NIP-05 requires `Access-Control-Allow-Origin: *` and most hosts comply, but when it fails there is nothing to fall back on — hence the paste textarea and the `curl` intent in the command generator. Accept both a bare `names` map and a full NIP-05 document. Validate every value as 64-hex client-side before posting.
- The pubkey list goes to `POST /api/pubkeys/lookup`, capped at `DOMAIN_LOOKUP_MAX`. Over the cap is an error, not a truncation: silently dropping rows from a list the admin is about to bulk-ban is exactly the class of failure this project designs against.
- **Roster list**: one `<li>` per entry — the local part from the roster in `<b>`, the npub as plain text, ban status, the kind-0 name and nip05 if known, the event count if the pubkey appears in the current author-scan cache, and the `claims_domain` marker. Checkbox on every unbanned entry except the admin's own pubkey.
- **`claims_domain` is displayed as a comparison of two unverified claims, not as verification.** `listed, claims this domain` is agreement; `listed, but claims <other>` or `listed, claims nothing` is a stale or unclaimed roster entry. The page must never say "verified." Conversely, someone claiming this domain whom the roster omits is a possible impersonator — the roster cannot show them, so the page carries one line pointing at the author list's filter for that direction.
- **Provenance line**: `<n> pubkeys listed by <domain>, fetched <time> — <m> already banned, <k> seen in the current scan`. When the response contains no `names` map or a single entry, say `this domain does not publish a full directory` rather than reporting zero users, which is a different and misleading claim.
- Sort row: `sort: name · events · banned · claims domain`. Same shared rules — re-filter and clear the selection on every re-sort.
- **This page is why a domain is never accepted in the manual ban form.** Ban-by-domain exists, but it is preview, count, filter, select-visible, then ban — four deliberate steps with the roster on screen — rather than a domain typed into a text box that bans four thousand people on Enter.

## README.md

Must open with the one-command install, in its own fenced code block so GitHub shows the copy button:

```
docker exec -it strfry sh -c 'mkdir -p /config/strfry86 && curl -fsSL https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry-86-updater.py -o /config/strfry86/strfry-86-updater.py && python3 /config/strfry86/strfry-86-updater.py'
```

Immediately followed by the update command (same thing, shorter — updater is already installed):

```
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

Then a "Container has no network?" section with the offline install (two files) and offline update (one file) sequences, each in its own fenced block:

```
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry-86-updater.py
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry86-bundle.tar.gz
docker cp strfry-86-updater.py strfry:/config/strfry86/
docker cp strfry86-bundle.tar.gz strfry:/config/strfry86/
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

```
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry86-bundle.tar.gz
docker cp strfry86-bundle.tar.gz strfry:/config/strfry86/
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

with a note that the curl lines can be replaced by getting the files onto the host by any means — only the `docker cp` and `docker exec` steps matter.

README must also cover, briefly:

- Adjusting the container name if not `strfry`.
- The admin key is asked for once on first run (defaulting to `relay.info.pubkey` from strfry.conf if set) and stored in `/config/strfry86/config.json` — public key only, nsec never leaves your extension.
- `contact_appeal` is optional, asked once (re-asked later only if the key is missing entirely), shown publicly to everyone including logged-out visitors, and editable or blankable by hand at any time with effect on the next page load.
- The compose `ports:` line (`127.0.0.1:8686:8686`, with a note on tailnet/reverse-proxy exposure). No special proxy timeout is needed: every scan is asynchronous and polled, so no request is held open for more than about a second.
- The author page offers a fast recent window and a slower full author list covering every kind except DMs (~2 minutes on a 2.6M-event relay). The tab can be closed while it runs and the result collected later. The list defaults to hiding single-event authors, because on most relays those are NIP-17 gift wraps whose keys are never reused and therefore cannot usefully be banned.
- Add the relay to your write relays in jumble.social so your reports actually reach it.
- strfry-86 deliberately shows no charts or statistics; anything needing a whole-database sweep is offered as a copyable terminal command instead.
- Banned users' profile names are resolved first from the local database and, only for the remainder, via a single batched query to `wss://purplepag.es` made from the admin's logged-in browser (the server itself never opens outbound connections). Results and misses persist in `blacklist.json`, so each pubkey is queried externally at most once ever — hand-deleting an entry's `name_checked_at` makes it eligible for one more try.
- `names.json` is a throwaway cache for non-banned pubkeys and can be deleted at any time at the cost of one rescan. So can the three `*-cache.json` files.
- The profile page shows what the relay itself knows about one pubkey, and renders profile picture and website URLs as plain text on purpose, since loading them would disclose the admin's IP to a host chosen by the account under investigation.
- The domain page reads a domain's own `.well-known/nostr.json` from the admin's browser. Neither that roster nor a profile's nip05 is verified by anything — they are two unverified claims the page compares for you, never proof, and never on their own a reason to ban.
- Bans are forward-looking; existing events are purged with `strfry delete --filter '{"authors":["<hex>"]}'`.
- Where the strfry.conf backups land, that a backup is only written when the file actually changes, and that the updater keeps the 3 newest conf backups and the 1 newest applied bundle, pruning older ones after a successful run (hand-renamed files are never touched).
- The trust model in one honest sentence: the updater executes code from this repo's main branch — don't run someone else's fork blindly.

## Release discipline

`tools/make_bundle.py` regenerates BOTH `manifest.json` (sha256 over every deployable file, sorted keys, trailing newline) and `strfry86-bundle.tar.gz` (every deployable file plus the manifest, deterministic member order). Any commit that changes a deployable file MUST regenerate both — a stale manifest or stale bundle makes the updater skip, reject, or install outdated files. The bundle is committed to the repo so it has one stable raw URL.

test.sh must:

- Verify the manifest matches the working tree AND that the committed bundle's contents hash-match the manifest.
- Pipe crafted JSONL through `python3 plugin86.py` and assert accept/reject for: normal event accepted; banned author rejected; admin 1984 bans its p-tags; non-admin 1984 does not ban; admin pubkey cannot be banned.
- Assert the `AUTHOR_SCAN_KINDS` coverage gap against a live database when one is reachable, skipping cleanly (`SKIP: ...`, not a failure) when no strfry binary is found — detect it the same way `server86.get_strfry_bin()` does. Fail if the gap exceeds 2% of non-giftwrap events. This is the only check that can catch an allowlist that has silently gone stale, and staleness removes authors from a list the UI describes as complete. **It has never run against real data in development — whoever has a real relay should run `bash test.sh` there at least once.**
- Assert the bounds, since every one is a silent failure if it stops holding: a `mode` not in `AUTHOR_SCAN_MODES` is a 400 with no scan and no default fallback; a body carrying a raw `limit` or its own `kinds` array is rejected; a `full` result returning exactly `AUTHOR_SCAN_FULL_LIMIT` rows sets `saturated`; `/api/pubkeys/lookup` over `DOMAIN_LOOKUP_MAX` is an error rather than a truncation; `/api/names` rejects a verified kind-0 whose pubkey is neither banned nor in the scan cache; a second `POST /api/authors/scan` during a running scan returns the same `202 running` shape rather than a 409 or a second scan; and the static route table serves the same bytes for `/profile` and `/profile?npub=<anything>`, including values containing `../`.

## Crypto tests (non-negotiable — this is the whole authorization system)

`lib86/bip340.py` has exactly two callers, both in server86: NIP-98 signature verification, and kind-0 verification in `POST /api/names`. There are no sessions, cookies, or tokens, so the first is the only thing between a stranger and every write endpoint; the second is the only thing between a forged profile name and permanent storage in `blacklist.json`, displayed publicly forever. Both dangerous failure modes are silent: a verifier that wrongly returns `True` breaks nothing visible — it simply opens the admin API to the internet, or serves attacker-chosen names on the public page. Every other test in the suite covers behavior you would notice breaking; this one does not.

- **BIP-340 reference vectors.** Vendor the upstream `test-vectors.csv` at `tests/bip340-vectors.csv` (committed, never fetched at test time — test.sh must pass in a container with no network). Run every verification-path row and assert the expected result EXACTLY, including the failure rows: point not on the curve, `r >= p`, `s >= n`, wrong public key, tampered message, and the negated-parity cases. A run that passes the valid rows and skips the invalid ones tests nothing that matters — accepting everything is the failure being guarded against, and it passes any positive-only suite.
- **NIP-98 auth path, end to end.** bip340 is verification-only by design, so tests cannot sign. Vendor a fixed, pre-signed kind-27235 event as a fixture (`tests/nip98-fixture.json`, generated once by hand and committed with a note on its provenance) and assert it is ACCEPTED against the matching admin pubkey. Then assert each of these mutations is REJECTED, one per case: flipped byte in `sig`; flipped byte in `id`; `pubkey` swapped to a different valid key; `kind` changed from 27235; `method` tag not POST; `u` tag path pointing at a different endpoint than the one being called; `created_at` moved 120s into the past and 120s into the future. Each mutation must fail on its own.
- **kind-0 verification path (`/api/names` intake).** Vendor a fixed, pre-signed kind-0 event as a second fixture (`tests/kind0-fixture.json`, same provenance note) and assert the intake logic ACCEPTS it when its pubkey is in `queried` and banned. Then assert each mutation is REJECTED — dropped silently, never a 500, since a hostile relay must not be able to break the endpoint: flipped byte in `sig`; flipped byte in `id`; tampered `content` (recomputed id no longer matches); `kind` changed from 0; `pubkey` not in the `queried` list; `pubkey` not currently banned. A forged name is not an outage — it is silently wrong data served to the public forever — which is exactly why the negative cases are the test.
- **bech32 round-trip.** Assert `npub_decode(npub_encode(hex)) == hex` over a handful of known-good pairs, and assert that a checksum-corrupted npub, a wrong-HRP bech32 string (e.g. an `nsec`), and a truncated npub all raise rather than returning a plausible-looking wrong hex. A decoder that silently returns garbage puts the wrong pubkey on the blacklist.

Because these fixtures are test-only, they are NOT deployable files: `tests/` is excluded from `manifest.json` and from `strfry86-bundle.tar.gz`.

## Vendored crypto (lib86)

- `bip340.py`: adapt the BIP-340 python reference implementation (verification path only — no signing, and strip anything requiring third-party libs). Pure integer math on secp256k1.
- `bech32.py`: adapt the BIP-173 python reference implementation; expose `npub_encode(hex) -> npub` and `npub_decode(npub) -> hex`.
- Keep upstream attribution comments in both files. These two files are the ONLY cryptography in the project; never hand-roll alternatives elsewhere.