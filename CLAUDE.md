# CLAUDE.md — strfry-86

## What this project is

**strfry-86** is a pure-Python moderation sidecar for a [strfry](https://github.com/hoytech/strfry) Nostr relay (repo: https://github.com/sybenx/strfry-86), deployed *inside* the operator's existing strfry Docker container with zero image changes, zero pip installs, and zero new compose services. ("86" is restaurant slang for banning someone from the establishment — fitting for a relay literally named stir fry.)

How it works, end to end:

1. The admin reports a user from any normal Nostr client (primarily jumble.social). The client publishes a NIP-56 report (`kind 1984`).
2. The strfry write-policy plugin (`plugin86.py`) sees the report. If — and only if — it is authored by the admin pubkey, every pubkey in its `p` tags is added to the blacklist. From then on, all events authored by blacklisted pubkeys are rejected.
3. A tiny stdlib web server (`server86.py`) serves a bare HTML page listing bans. The admin logs in with a NIP-07 extension and unbans via checkboxes. Unbans are authorized per-request with NIP-98 signed events — no sessions. Statistics, charts, and history reporting are explicitly NOT features: anything that would require sweeping the database is surfaced instead as a copyable `strfry` or `nak` command block for the operator to run in a terminal, where a ten-minute operation is visible, interruptible, and harmless.
4. A single self-contained installer/updater (`strfry-86-updater.py`) handles first install, config, strfry.conf rewrite, and all future updates, run via one `docker exec` command.

## Hard environment constraints (do not violate)

- **Python 3 stdlib ONLY.** No pip, no venv, no third-party imports anywhere. The target is the Python that ships in the operator's strfry container. Schnorr verification and bech32 are vendored (see below).
- **Everything lives in `/config/strfry86/`** inside the container — this is on the operator's permanent `strfry_config` named volume, so it survives container recreation. Nothing is written anywhere else except the strfry.conf edit and its backup.
- **No custom Docker image, no new compose service, no entrypoint changes.** The only compose change the operator makes by hand is adding a `ports:` line for the admin page (document in README).
- **`plugin86.py` writes nothing but protocol JSON to stdout** (stderr for all logging) and never crashes on bad input — a dead plugin can wedge the relay.
- Only the admin pubkey can ban (via kind 1984, or manually via NIP-98 `/api/ban`) or unban (via NIP-98). No other trust roots.
- The admin pubkey can never end up in the blacklist (silent no-op on any attempt).
- **No unbounded or background database work, ever.** No scan on server start, on a timer, or because a cache went stale. Every scan is either bounded by an authors list and issued in service of a request that is being answered right now, or it is triggered by a labeled button that says what it is about to do before it is pressed. The `limit`-bounded scan on `/api/authors/scan` is button-triggered and admin-only because it reads a constant-sized slice of the whole relay; the authors-bounded kind-0 name lookup behind `/api/banned` is not, because its size is fixed by the ban list itself and it is answering the request in front of it. That distinction is the rule — not "page load" as such, which is merely where the old streaming design happened to go wrong.
- **The server never opens an outbound network connection. Ever.** All third-party contact (today: profile lookup against `wss://purplepag.es`) happens from the admin's logged-in browser, only for banned pubkeys that could not be resolved from the local database, and each pubkey is queried externally at most once, ever — results AND misses are persisted in `blacklist.json`. Results come back through ONE authenticated endpoint (`POST /api/names`) which cryptographically verifies every displayable claim before storing it. Names are display sugar: both pages must work fully, forever, with that socket blocked and no name ever resolved externally.
- **Every scan must be bounded before it is issued, by one of exactly two mechanisms**: an explicit `limit` whose value is a constant in `server86.py` (never a value taken from a request), or an explicit `authors` list assembled from data server86 already holds. Nothing else counts as a bound — not `since`/`until`, which bound the time range but not the result size, and not "it is probably small." A scan whose result size is not knowable in advance from a constant or a finite list must not be issued at all. That single rule is what the earlier streaming design broke, and it is the only rule needed to keep it from recurring.
- Page styling is limited to the exact CSS block in the Client pages section (identical on both pages) (centering, edge padding, npub wrapping, one mobile font-size media query). No fonts, no colors, no frameworks, no CDN.

## Repo layout

```
strfry-86-updater.py   # installer + updater, the only file the operator ever runs
plugin86.py            # strfry write-policy plugin (stdin/stdout JSONL)
server86.py            # stdlib http.server admin server, spawned by plugin86
lib86/__init__.py      # empty
lib86/bip340.py        # vendored BIP-340 schnorr verification (pure python reference impl)
lib86/bech32.py        # vendored bech32/npub encode+decode (pure python reference impl)
lib86/blacklist.py     # shared blacklist load/save/add/remove, atomic writes, mtime reload; missing entry keys (incl. name fields) read as null
bans.html              # public ban list page, served at /            (was admin.html)
authors.html           # admin-only active-author page, served at /authors
common86.js            # shared client code for both pages — login, NIP-98 signing, filter, select-all, records
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
  common86.js
  manifest.json
  config.json        # OPERATOR-OWNED: admin pubkey (hex), port, bind, contact_appeal. Never in manifest; the updater may only ADD a missing key, never change an existing value.
  blacklist.json     # OPERATOR-OWNED: the ban list, incl. resolved profile names. Never in manifest, never overwritten.
```

## strfry-86-updater.py

Single file, self-contained (may not import lib86 — it must run before lib86 exists). Idempotent: safe to run any number of times.

**Source selection (offline-first):** on startup, look for `strfry86-bundle.tar.gz` in the updater's own directory. If present → OFFLINE MODE: the bundle is the source; read `manifest.json` from inside it and extract files from it instead of downloading. If absent → NETWORK MODE: fetch from the repo raw URL as described below. All downstream logic (diffing, verification, config, strfry.conf, server restart, self-update) is identical in both modes.

Offline-mode specifics:
- Open with stdlib `tarfile`. Before extracting ANYTHING, validate every member name: reject absolute paths, `..` components, and links; abort loudly on violation.
- Extract each needed file to `<name>.tmp`, verify sha256 against the bundle's manifest, then `os.replace` into place — same atomicity as network mode. Only extract files that are missing or hash-differ locally (same diffing).
- After a fully successful run, rename the bundle to `strfry86-bundle.tar.gz.applied-<unixtime>` so a re-run without a fresh bundle cleanly no-ops (it falls back to comparing local files against the local `manifest.json` and reports "unchanged"). Older applied bundles are removed by the end-of-run prune (see Retention), never here.
- Self-update in offline mode: if the bundle contains a changed updater, extract it LAST (after the rename step is queued), replace atomically, print "updater updated — effective next run."

Network-mode flow:

1. **Fetch `manifest.json`** from `https://raw.githubusercontent.com/sybenx/strfry-86/main/` (repo base URL is a constant at the top of the file). Manifest maps relative path → sha256 for every deployable file. `config.json` and `blacklist.json` are NEVER in the manifest.
2. **Diff against local**: sha256 each local file; download only missing/changed files. Download to `<name>.tmp` then `os.replace` (atomic). Verify sha256 of each download against the manifest before installing; abort loudly on mismatch.
3. **First-run config**: if `config.json` doesn't exist, determine the admin pubkey and write `config.json` with `{"admin_pubkey_hex": ..., "port": 8686, "bind": "0.0.0.0", "contact_appeal": ""}`. To determine the pubkey: first try to read `relay.info.pubkey` from `/config/strfry.conf` (the NIP-11 admin contact); if found and it parses as a valid pubkey, offer it as the default — "Found relay.info.pubkey <npub...> in strfry.conf — use as admin? [Y/n]". On decline, or if the field is absent/invalid, prompt for a paste (accept npub or 64-hex; decode npub via inline bech32 — small enough to duplicate in the updater). Never silently adopt the strfry.conf value without confirmation: this key is the sole root of trust for banning. (Bind inside the container must be 0.0.0.0 for the compose port mapping to work; the README tells the operator to scope exposure via the compose `ports:` line, e.g. `127.0.0.1:8686:8686`.)

   **`contact_appeal`**: an optional free-text string shown publicly on the ban list page so a banned user knows where to appeal. Prompted for once on first run, right after the admin pubkey — "Optional appeal contact, shown publicly on the admin page (email, npub, URL, or any free text) — blank for none:". An empty answer is a valid answer and stores `""`.

   On EVERY run, if `config.json` exists but has no `contact_appeal` key at all, ask the same question once and merge the answer in. Merging means: load the existing JSON, add the one key, write back via `<n>.tmp` + `os.replace`, preserving every other key and value verbatim — including keys this updater doesn't know about. A key that is present but empty is an answered question; never re-prompt on it. This top-up is the ONLY circumstance in which the updater writes to an existing `config.json`, and it never alters a value already there.

   If stdin is not a TTY, skip both prompts silently: write `""` on first run, and on top-up leave the existing file untouched rather than half-writing it.
4. **strfry.conf edit**: locate `/config/strfry.conf` (constant, documented). Decide what the edit will be FIRST; only if a byte will actually change, copy to `strfry.conf.bak-<unixtime>` and then write. A run that determines the plugin line is already correct (the steady state, i.e. almost every update) must produce NO backup file at all. Then:
   - If `writePolicy.plugin` already points at `/config/strfry86/plugin86.py` → no-op.
   - If it is empty/unset → set it to `plugin = "/config/strfry86/plugin86.py"`.
   - If it points at some OTHER plugin → do NOT touch it; print a loud warning telling the operator to resolve manually.
   - Edit conservatively with line-oriented matching on the `writePolicy` block; do not reformat the rest of the file.
   - When detecting whether a plugin is already configured, ignore comment lines (lines whose first non-whitespace character is `#`) — a commented-out `# plugin = ...` line is NOT an active plugin and must not trigger the refuse-to-touch path. Never modify, remove, or uncomment any comment line anywhere in the file.
5. **chmod +x** `plugin86.py` (it has a `#!/usr/bin/env python3` shebang; strfry executes it directly).
6. **Restart the web server**: after any successful update, find and kill any running `server86.py` (match on cmdline via `/proc`, no pgrep dependency), then **spawn the replacement directly** — `python3 /config/strfry86/server86.py`, fully detached (`start_new_session=True`, stdin/stdout/stderr to devnull), exactly as plugin86 spawns it. Do NOT delegate the respawn to plugin86: the plugin is blocked on `stdin.readline()` and its once-per-hour check only fires when an event actually arrives, so on a quiet relay the admin page would stay down indefinitely after every update, with nothing indicating why.
   - **Wait for the old process to exit before spawning.** server86 enforces singleton by port bind and exits 0 silently on EADDRINUSE, so a replacement started while the old one still holds the port dies without a word — reproducing the exact outage this step exists to prevent. Poll `/proc/<pid>` until the killed pid disappears, up to ~5s, before spawning.
   - Kill by SIGTERM first; only escalate if the pid is still present at the end of the wait.
   - Verify the result: after spawning, poll for a `server86.py` cmdline in `/proc` for a couple of seconds and report `admin page restarted` or a loud `admin page did NOT come back up — check stderr` — never silently assume success. A failed spawn is a warning, not an abort; plugin86's hourly check remains as the backstop it was always meant to be.
   - Print what was done either way.
7. **Self-update**: if the manifest shows the updater itself changed, download it LAST, replace atomically, and print "updater updated — already effective next run."
8. **Prune** per the Retention rules below — this is the LAST action of the run, in both modes, after self-update, and only if everything above succeeded.
9. Exit with a clear summary: installed/updated/unchanged file counts, config status, strfry.conf status, pruned-file count, and the hint "if in doubt: docker restart <container>".

**Retention (applies to both artifact families the updater creates):**

- `strfry.conf.bak-<unixtime>` in the strfry.conf directory → **keep the 3 newest, delete older**.
- `strfry86-bundle.tar.gz.applied-<unixtime>` in `/config/strfry86/` → **keep the 1 newest, delete older**.

Rules that make this safe:

- Prune ONLY at the very end of a fully successful run. A run that aborted, warned about a foreign plugin, or hit a hash mismatch prunes nothing — never destroy the fallback while the current state is in question.
- Match with a strict regex anchored to the exact patterns above (`^strfry\.conf\.bak-\d+$`, `^strfry86-bundle\.tar\.gz\.applied-\d+$`). Anything the operator renamed by hand, any non-matching neighbor, and any directory is invisible to the pruner. Never glob loosely, never recurse, never follow symlinks.
- Order by the integer parsed from the filename, not mtime — `docker cp` and volume restores rewrite mtimes.
- Deletion failures are non-fatal: log to stderr, continue, still exit 0.
- Print exactly what was removed (`pruned 4 old conf backups, 2 old applied bundles`), so the operator sees it rather than discovering files vanished.
- The counts are constants at the top of the file (`KEEP_CONF_BACKUPS = 3`, `KEEP_APPLIED_BUNDLES = 1`), not prompts or config keys — this is housekeeping, not policy.

Combined with the conditional-backup rule in step 4, the steady state for an operator who updates weekly is: one applied bundle, and conf backups only from runs that genuinely rewrote the file.

## plugin86.py — strfry write policy

strfry spawns the plugin once and writes one JSON object per line to stdin; the plugin answers one JSON per line on stdout.

Input (relevant fields): `{ "type": "new", "event": { "id", "pubkey", "kind", "tags", "content", "sig", "created_at" }, ... }`
Output: `{"id": "<event id>", "action": "accept"}` or `{"id": "<event id>", "action": "reject", "msg": "blocked: banned pubkey"}`

Logic per event, in order:

1. Parse; on failure log to stderr, continue.
2. Author blacklisted → reject `"blocked: banned pubkey"`.
3. `kind == 1984` and `pubkey == admin` → for every valid `p` tag (64-char lowercase hex, validate, skip malformed), add to blacklist with `banned_at = created_at`, `report_event_id = id`, `reason = content`, `name`/`nip05`/`name_checked_at` all null (name resolution happens later, from the admin's browser — NEVER in this hot path: strfry blocks its entire write pipeline waiting on the plugin's reply, so a network lookup here would stall the relay), and `report_type` resolved as: the p tag's own third element if present, otherwise the third element of the first `e` (or `a`) tag that has one, otherwise null. This dual lookup is required by how jumble builds reports (verified against its source): profile reports carry the type on the p tag (`["p", <pubkey>, <type>]`), but note reports carry it on the e tag (`["e", <id>, <type>]`) with a bare p tag. Jumble's type options are `nudity`, `malware`, `profanity`, `illegal`, `spam`, `other` (NIP-56 also defines `impersonation`; store whatever string arrives, don't whitelist). Note: jumble always sends `content: ""`, so `reason` will be empty for jumble reports — the type is the only signal. Then accept. Do NOT verify the signature here — strfry has already verified it, and this is the hot path.
4. Otherwise accept.

On startup (and once per hour thereafter), plugin86 ensures `server86.py` is running: spawn `python3 /config/strfry86/server86.py` fully detached (`start_new_session=True`, stdin/stdout/stderr to devnull — the plugin's stdout is sacred). server86 enforces singleton by port-bind: if the bind fails with EADDRINUSE it exits 0 silently, so repeated spawns are harmless.

Use unbuffered/line-flushed stdout. Reload the blacklist on mtime change, checked at most once per second (see lib86/blacklist.py).

## server86.py — page server + ban/unban/scan API

stdlib `http.server` (ThreadingHTTPServer). Routes:

- **Static routes are an explicit allowlist**, never a filesystem path join: `/` → `bans.html`, `/authors` → `authors.html`, `/common86.js` → `common86.js` (`Content-Type: application/javascript`). Anything else 404s. There is no directory serving and no path derived from the request, so path traversal is not mitigated here — it is impossible. `config.json` and `blacklist.json` sit in the same directory as these files and must never be reachable over HTTP.
- `GET /api/banned` → `{"admin": "<hex>", "contact_appeal": "<string>", "banned": [{"pubkey", "npub", "banned_at", "reason", "report_type", "report_event_id", "name", "nip05", "name_checked_at"}]}`. `report_event_id` is the id of the admin's kind-1984 report that caused the ban (null for manual bans) — it is already-public event data and lets the page derive the report record lines. Public read is fine. `contact_appeal` is echoed verbatim from `config.json`, or `""` if the key is absent, null, or not a string — this endpoint must never 500 over a hand-edited config. Re-read it from `config.json` on mtime change (the same cheap once-per-second mtime check `lib86/blacklist.py` uses) so an operator's hand-edit takes effect on the next page load without restarting anything.
  **Name resolution, in two layers.** If the blacklist entry already carries a `name`/`nip05` (written there by `POST /api/names`), serve those and do nothing else for that pubkey. For every other entry, resolve from the LOCAL strfry database exactly as before: run `strfry --config /config/strfry.conf scan '{"kinds":[0],"authors":[<uncached hex>]}'` via subprocess (see **strfry scan execution rules** below; scan is a read-only LMDB read, safe while the relay runs, and bounded by the authors list per the hard constraint), parse each event's `content` JSON, take `display_name || name` for `name` and the `nip05` string for `nip05` (same event, same scan — `nip05` is served as-is and NEVER verified; verification would require outbound HTTP to arbitrary domains). Cache results in an in-memory dict `{pubkey: ({"name", "nip05"}, checked_at)}`; re-query only pubkeys that are uncached or were full misses (both fields null) older than 24h, and batch all of them into ONE scan call per request. Local results stay in memory and are NOT written to `blacklist.json` — the local database is already the source of truth for them, and re-reading it is cheap. If the subprocess fails for any reason (binary missing, bad path, timeout of a few seconds), log to stderr and return `name: null, nip05: null` — the endpoint must never break because name lookup broke. This layering is what makes the page identical for logged-out visitors before and after the external-lookup feature exists.
  `name_checked_at` is served verbatim from the blacklist entry and means one thing only: an EXTERNAL lookup was attempted for this pubkey. It is set by `POST /api/names` and by nothing else — never by a local scan, hit or miss. An entry whose `name` and `nip05` are both null AND whose `name_checked_at` is null is the unresolved set the admin's browser may take to purplepag.es; everything else it leaves alone. Entries written before these fields existed simply lack the keys, and a missing key reads as null — no migration step, ever.
  **strfry scan execution rules** (every scan subprocess server86 ever issues — today that is the `/api/banned` name lookup and `POST /api/authors/scan`, and nothing else):
  - `--count` is NOT documented in strfry's public README (which shows only body-printing `strfry scan '<filter>'`); it was verified against the source, where it prints the number of matching events read from the index without streaming bodies. Pin the strfry version this was verified against in a comment, and treat a `--count` invocation that returns non-numeric output as a failure of that scan rather than parsing it loosely.
  - The strfry BINARY path is DISCOVERED once at startup, not assumed on PATH: use `shutil.which("strfry")`, else the first path in `("/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry")` that exists and is executable — dockurr/strfry ships the binary at `/app/strfry`, which is NOT on PATH for a detached process, while the official image installs to `/usr/local/bin`. Cache the discovered path for the process lifetime. If no candidate is found, don't crash: the `/api/banned` name lookup returns `name: null` as usual, and `/api/authors/scan` returns its previous cache with a `warning` stating `strfry binary not found` and listing the candidates tried.
  - Invoke as an argv LIST with `shell=False`: `[<discovered binary>, "--config", "/config/strfry.conf", "scan", <optional "--count">, json.dumps(<filter>, separators=(",", ":"))]`. The filter is exactly ONE argv element containing raw compact JSON. Never build the command as a single string, never `.split()` a command string (json.dumps output contains no argv-safe boundaries), never wrap the filter in quotes, never backslash-escape its quotes (escaped quotes reach strfry verbatim and it exits 1), never `shell=True`.
  - Every scan subprocess MUST pass `cwd=`: `strfry.conf`'s `db` is conventionally a path relative to wherever the relay process itself was launched (e.g. dockurr/strfry runs `./strfry` from `/app` with `db = "./strfry-db/"`), which is NOT server86's own cwd (plugin86 spawns server86 with `cwd=SCRIPT_DIR`), so a scan with no cwd override fails with `mdb_env_open: No such file or directory`. Resolve the cwd by locating the running strfry relay process via `/proc` (match on the discovered binary's basename plus a `relay` argv element) and reading `/proc/<pid>/cwd`; fall back to the discovered binary's parent directory if no relay process is found. Cache the discovered cwd for the process lifetime, re-deriving only if the located pid disappears.
  - Capture stderr and include its TAIL (last ~300 chars) in failure messages — strfry's loguru output puts a startup banner first and the actual error (tao::json parse or LMDB env open) as the LAST line, so truncating from the head discards the error itself; always slice from the end.
- `POST /api/unban` → body `{"auth": <signed nostr event>, "pubkeys": ["<hex>", ...]}` → removes each (the whole entry, including any resolved name and `name_checked_at` — a later re-ban starts fresh and earns one new lookup), returns `{"ok": true, "removed": [...]}`.
- `POST /api/ban` → body `{"auth": <signed nostr event>, "entries": [{"pubkey": "<npub or 64-hex>", "reason": "<optional>"}, ...]}` → for each entry: decode npub via `lib86/bech32.py` if needed, validate, skip malformed; add to blacklist with `banned_at = now`, `reason` (empty string if omitted), `report_type = "manual"`, no `report_event_id`. Admin pubkey is silently skipped (per the hard constraint). New entries get `name`/`nip05`/`name_checked_at` null. Returns `{"ok": true, "added": [...], "skipped": [...]}`.
- `POST /api/names` → body `{"auth": <signed nostr event>, "queried": ["<hex>", ...], "events": [<raw kind-0 events exactly as received>, ...]}`. NIP-98 admin auth. The ONLY way externally-fetched profile data enters the system, and the server trusts NONE of it on arrival. **The browser posts signed events, never extracted strings** — a name detached from its signature would be an unverifiable claim stored forever, whereas a kind-0 is self-certifying regardless of the route it traveled (admin's browser, third-party relay, or anywhere else). For each posted event: recompute the NIP-01 event id and require it to match, verify the BIP-340 signature via `lib86/bip340.py`, require `kind == 0`, require `pubkey` to be in `queried` AND currently banned. Drop anything that fails (a dropped event is not an error — a hostile relay must not be able to 500 this endpoint), keep only the newest `created_at` per pubkey, and extract `display_name || name` and `nip05` SERVER-SIDE from the verified event's `content`. Then RELOAD `blacklist.json` from disk before writing — the lookup took seconds and the admin may have banned or unbanned someone meanwhile; never write back a pre-lookup copy — set `name`/`nip05` on surviving hits, and stamp `name_checked_at = now` on EVERY pubkey in `queried` that is still banned, hits and misses alike. The miss stamp is what makes "once, max" true: a pubkey with no profile anywhere is never auto-requeried. (A miss is unverifiable by nature — you cannot sign an absence — and is accepted because it is only an admin-authenticated timestamp, not displayable data. The operator can hand-delete an entry's `name_checked_at` to make it eligible again; document in README.) Returns `{"ok": true, "named": [...], "stamped": <count>}`. Idempotent: re-posting the same events is a harmless overwrite.
- `GET /api/authors` → the last scan result from an IN-MEMORY cache, or `{"scanned_at": null, "authors": []}` if there has been no scan since server86 started. **Never scans.** Public read, same stance as `/api/banned`: it is derived from public events, and the page is admin-gated as UX rather than as a secret. Not persisted to disk — a rescan is one bounded scan, and a state file is not worth the drift.
- `POST /api/authors/scan` → body `{"auth": <signed nostr event>}`. NIP-98 admin auth. Runs ONE scan, replaces the cache, and returns the same shape as `GET /api/authors`. Admin-only because it causes work on the operator's live relay, while reading the result does not.

  The scan is `strfry scan '{"limit": AUTHOR_SCAN_LIMIT}'` with `AUTHOR_SCAN_LIMIT = 20000` as a constant in `server86.py` — never a value from the request, per the bounded-scan rule. Read stdout line by line, take `pubkey` and `created_at`, discard the parsed event immediately; never accumulate event bodies, never hold more than one line at a time. Tally per pubkey, then resolve names for the result set through the same in-memory name cache and the same single batched kind-0 scan `/api/banned` uses. Both scans are paid for by the same admin press; nothing here touches purplepag.es or writes to `blacklist.json`.

  Response: `{"scanned_at": <unix>, "events_read": <int>, "span_start": <unix>, "span_end": <unix>, "warning": <string or null>, "authors": [{"pubkey", "npub", "name", "nip05", "count", "last_seen"}]}`, sorted by `count` descending. `span_start` is the OLDEST `created_at` actually seen and `span_end` the newest — the honest description of what the number covers, since 20,000 events is six hours on one relay and four months on another.

  This depends on `limit` returning the NEWEST matching events, as NIP-01 specifies for relay REQs. Verify that against the target strfry version before relying on it: if `limit` is oldest-first, this returns the relay's most ancient authors and the feature is actively misleading rather than merely approximate.

  Single-flight: a scan already in progress returns the existing cache with a `warning` rather than starting a second one. Bounded by the same per-subprocess timeout as every other scan; on failure the previous cache survives and `warning` carries the exception type and message.

  **What this list is**: the most prolific authors among the most recent `AUTHOR_SCAN_LIMIT` events. It is NOT everyone who has ever posted — that would require enumerating the whole database and an accumulated index, which this project does not do. For moderation the bounded set is the right one anyway: nobody needs to ban an account that posted twice in 2023.

NIP-98 auth checks for `/api/unban`, `/api/ban`, `/api/authors/scan`, and `/api/names` — ALL must pass, else 401 JSON error:

1. Signature valid per BIP-340 over the NIP-01 serialized event id (use `lib86/bip340.py`; recompute the event id and check it matches `auth.id` before verifying the sig).
2. `auth.pubkey == admin_pubkey_hex`.
3. `auth.kind == 27235`.
4. `method` tag is `POST`; `u` tag's path matches the endpoint being called (`/api/unban`, `/api/ban`, `/api/authors/scan`, or `/api/names`; lenient on host/origin — reverse proxies change it). The path match is exact per endpoint — an auth event signed for one endpoint is never accepted at another.
5. `abs(created_at - now) <= 60`.

No sessions, cookies, or tokens.

## Client pages — `bans.html`, `authors.html`, `common86.js`

Two pages, not one, and not three. The split is a safety property, not a layout preference: the two lists carry destructive controls that point in OPPOSITE directions — "Unban selected" on one, "Ban selected" and "Copy purge command" on the other — over lists that look identical, with the same filter box, the same select-all, and the same live count. On a single page (collapsed sections included) the button you reach after filtering and scrolling depends on which region you are inside, and the failure mode is mass-unbanning people you meant to purge. Separate documents make it structural: `bans.html` has no ban-selected control in its DOM at all, and the public page contains no scan trigger.

The rejected third page (`/tools`) is a `<details>` block on both pages instead. It was static text with one input; purge commands are relevant to both lists.

### `common86.js`

Everything both pages share lives in ONE locally-served file, served by server86 from `/common86.js` with `Content-Type: application/javascript`. This is mandatory, not a preference: the duplicated surface would otherwise include NIP-07 login, NIP-98 event construction and signing, the filter, select-all and its live count, the `textContent` rendering rules, the timestamp formatter, the npub link builder, the record lines, and the command blocks — that is most of the client code and ALL of the security-relevant parts. Two copies of signing logic is how an auth fix lands in one file and not the other.

This does not breach "no libraries, no CDN": it is the operator's own file, served from the operator's own server, vendored exactly like `lib86/`. It is a deployable file — in `manifest.json`, in the bundle, hash-checked by the updater like everything else.

Both pages are still raw HTML with no framework, no build step, and no bundler. Each page has its own small `<script>` block for page-specific wiring; anything used twice moves into `common86.js` rather than being copied.

### Shared chrome (identical on both pages)

`<meta name="viewport" content="width=device-width, initial-scale=1">`. One `<style>` block per page containing ONLY these layout rules — nothing else, browser defaults throughout:

```css
body { max-width: 40em; margin: 0 auto; padding: 0 1em; }
li { overflow-wrap: anywhere; }
@media (max-width: 600px) { body { font-size: 1.15em; } }
```

The `padding: 0 1em` is REQUIRED — without it, content sits flush against the screen edge on mobile (max-width centering does nothing when the viewport is narrower than the max-width). The `overflow-wrap: anywhere` is REQUIRED — npubs are 63-char unbreakable strings and will overflow the viewport horizontally without it. Do not remove either in the name of minimalism; they are the minimum.

- **Cross-page navigation**: one plain `<a>` on each page pointing at the other (`/` ↔ `/authors`), rendered directly under the `<h1>`. No nav bar, no styling, no active-state logic.
- **Login, and why remembering it is safe**: `window.nostr.getPublicKey()` on the "Login with extension" button. Navigating between pages would otherwise re-prompt on every page load, which some NIP-07 extensions surface as a dialog — miserable across two pages. So a successful match stores the admin pubkey in localStorage and both pages read it on load to decide what to reveal. This grants NOTHING: there are no sessions and no tokens, every privileged action is still individually signed and verified server-side, and a user who hand-edits that localStorage value gets a page full of buttons that all fail at the server. It is a UI hint, not a credential. A "log out" control clears it.
- **Timestamps**: render unix times as `YYYY-MM-DD@HH:MM UTC` (e.g. `2026-07-23@14:35 UTC`; derive from `toISOString()`, replace the `T` with `@`, drop seconds/milliseconds and the `Z`, append " UTC"), italicized via an `<i>` wrapper. Bold/italic come from semantic tags with browser default styling — no CSS additions.
- **Untrusted string rule**: profile names, nip05 identifiers, and report reasons are all attacker-influenced. Build `<b>`/`<i>`/`<a>` elements with `createElement` and set `textContent`; never `innerHTML`, on either page, ever. nip05 is display text only: never linkified, never verified.
- **Filter field**: an `<input type="search">` on each page. On every `input` event, hide (via `style.display`) each `<li>` in THAT PAGE'S list whose `textContent` does not contain the query as a case-insensitive substring — no debounce, no server round-trip, no re-fetch. Matching on `textContent` means the filter covers everything rendered. Re-apply after every list re-render and after a name lookup fills names. An empty query shows everything. Scope every handler to its own list container rather than looping the document: `<details>` content stays in the DOM when collapsed and would otherwise be filtered too.
- **Select all checkbox**: toggles the checkboxes of only the entries the filter currently SHOWS (hidden `<li>`s untouched) — filter-then-select-all is the bulk workflow on both pages. Resets to unchecked on every list re-render. Entries with no checkbox (already-banned authors, the admin's own pubkey) are skipped.
- **Live count**: a `<b>` reading "<n> selected", empty at zero so it renders nothing while logged out. Updated on every checkbox change, select-all toggle, and list re-render.
- **"Copy purge command" button** (admin only, both pages): operates on the checked set. Builds `strfry delete --filter '{"authors":["<hex>", ...]}'` and copies it to the clipboard — it does NOT run anything, and server86 has no delete endpoint of any kind. Bans are forward-looking; deleting a user's existing events is destructive and irreversible, and belongs in a terminal where the command can be read before it is run. Also render the command in a `<pre>` below the button, so the clipboard is a convenience rather than the only path. Disabled with nothing checked.
- **Command blocks** (admin only, both pages): a `<details>` block of copyable `strfry` and `nak` invocations for the things strfry-86 deliberately does not do — event counts, per-kind counts, purges, any other database sweep. Each is a `<pre>` with one line of plain text saying what it does. One text input for a pubkey (npub or hex) fills the placeholder in whichever blocks take one, via `textContent` on re-render. Nothing here is executed, nothing here is fetched, no block's output ever returns to the page. `<details>`/`<summary>` is native HTML: no JavaScript, no CSS, no change to the locked style block.
- **Record lines** (admin only, both pages, rendered from shared code over the same localStorage): a persistent log of admin actions. Element order within each line is fixed: the "↩" dismiss button LEFTMOST, then the plain-text label, then the "Undo" button as the rightmost element. The two buttons sit at opposite ends with the label between them because they are not equally consequential — ↩ only hides a line in this browser's localStorage, while Undo changes server state. The dangerous one gets the isolated position and the harmless one absorbs the mis-taps. DOM order is the ONLY available lever: the locked CSS block permits no margin, padding, or gap rules, so buttons cannot be separated by spacing. Since the label supplies the separation, never render a record line with an empty label. Three kinds:
  - **Ban records**: after a successful ban from EITHER page, one line per banned pubkey (added after the re-fetch, so name/npub are available): "banned <name> <npub>". Undo signs and POSTs a single-pubkey `/api/unban`.
  - **Unban records**: after a successful "Unban selected", ONE line per action: "unbanned <name> <npub>" for a single pubkey, "unbanned <n> pubkeys" for more. The unbanned entries (pubkey, npub, name, reason) are captured from the list before the re-fetch discards them; Undo re-bans all of them via `/api/ban` with each original reason (re-bans therefore get `report_type = "manual"`).
  - **Report records**: derived, not stored — for every entry in `/api/banned` whose `report_event_id` is non-null and whose id (`<report_event_id>:<pubkey>`) is not in the dismissed list: "reported <name> <npub> — <report_type>". Undo signs and POSTs a single-pubkey `/api/unban` (the line then disappears with the ban); ↩ adds the id to a localStorage dismissed list (capped at the 1000 newest).

  **At most the 20 newest record lines of all three kinds combined are ever rendered**, newest first; everything older is auto-dismissed — dropped from the rendered list and, for ban/unban records, deleted from localStorage. This is a hard cap, not a "show more" affordance: these lines sit ABOVE the list, in the space reserved for controls that must stay reachable, so a relay with 400 reported bans would otherwise push every control off the screen on first login. The cap also retires the dismissed-id list's failure mode — report records were previously suppressed by a list capped at 1000 ids, so past that point evicted ids let long-dismissed lines reappear. With only 20 lines rendered the dismissed list is bounded by what can be on screen; keep the 1000 cap as a backstop it should never approach. Ban and unban records persist in localStorage (200 newest stored, 20 rendered) until their ↩ is clicked, their Undo succeeds, or they age out of the visible 20 — they survive reloads, sessions, and navigation between the two pages. A successful Undo removes/dismisses the line, re-fetches, re-renders; a failed Undo re-enables the button and reports via the status line. Record lines render only when logged in as admin (localStorage is per-browser UI state, not server state — nothing about records is served).

### `bans.html` — `GET /`

Public. For everyone, this page loads the ban list and whatever names the server could resolve locally — the only work that causes is one authors-bounded kind-0 read, which is exactly what it was before. It has no control that could start anything larger. The one thing reserved for the admin's own logged-in load is the external name-resolution flow below, which never runs for a logged-out visitor.

Page order is fixed, top to bottom, with ALL non-list UI above the list so controls stay reachable when the list is thousands of entries long:

1. `<h1>strfry-86</h1>`, then the link to `/authors`
2. "Login with extension" button (plus its status text: "this key is not the admin" / "a NIP-07 extension is required"), then the name-resolution status line (admin only, present only when the resolution flow ran this page-load)
3. Command blocks (`<details>`, admin only, hidden until login)
4. Manual ban form (admin only, hidden until login), followed by the record lines
5. Plain text: "These npubs are banned from this relay." — visible to everyone
6. The appeal line — visible to everyone, present only when `contact_appeal` is non-empty
7. The filter field, placeholder "filter bans" — visible to everyone, like the list it filters
8. "Select all" checkbox (admin only), then "Unban selected" and "Copy purge command" (admin only), then the live count. These sit directly above the list they operate on; the admin scrolls down, ticks boxes, and scrolls back up to these rather than to the top of the page
9. The ban list

- Ban list loads for everyone on page load from `/api/banned`: one `<li>` per ban containing, in order: the display name wrapped in `<b>` if known; the nip05 as plain text if known; the npub as a raw `<a href="https://njump.me/<npub>" target="_blank">` link; the ban time wrapped in `<i>`; report type and reason. Omit the name/nip05/type/reason portions cleanly when null or empty (jumble reports always have an empty reason).
- **Display names and NIP-05**: from the server (`name` and `nip05` in `/api/banned` — either read from the local database or persisted in `blacklist.json` by a past external lookup; the page cannot tell which, and does not care). No localStorage name cache — the server is the cache, shared across every browser the admin uses. Entries with no name show the npub only — a perfectly acceptable resting state.
- **Name-resolution flow** (admin only; the ONLY path by which this project ever contacts a machine the operator does not run — and it runs in the admin's browser, never in server86):
  - Trigger: after `/api/banned` renders AND the stored login says admin, compute the unresolved set — entries with `name` and `nip05` both null and `name_checked_at` null. The local database has ALREADY been consulted by the time this response arrives, so this set is exactly "not resolvable locally, never asked externally" — no extra round-trip is needed to establish it, and a name that resolves locally never leaves the machine. If the set is empty (the steady state), NOTHING happens: no socket, no POST, no status line. Logged-out visitors never trigger any part of this.
  - If non-empty: open ONE WebSocket to `wss://purplepag.es`, send a single batched REQ `{"kinds":[0],"authors":[<unresolved hex>]}`, collect until EOSE or a 5s timeout; on failure to CONNECT, retry once against `wss://relay.damus.io`. Then POST everything received, verbatim and unparsed, plus the list of pubkeys actually queried, to `/api/names`. Re-fetch `/api/banned`, re-render, re-apply the filter (a fill can change whether an entry matches).
  - **"Queried" means a REQ actually reached a relay and completed** (EOSE or timeout). If both connections fail, POST nothing and stamp nothing — the next admin page load simply tries again. Stamping pubkeys that were never really asked about would permanently orphan them as false misses.
  - The flow may run again after a re-fetch (e.g. a manual ban just added an unresolved entry) but never re-queries a pubkey already attempted in this page session; that in-page guard plus the persisted stamps make loops impossible.
  - **Status line**, plain text, rendered whenever the flow ran: `queried purplepag.es for <n>, resolved <m>`. The outbound connection must be visible in the page itself, not only in a network tab.
  - Why automatic-on-page-load is acceptable here where the old design gated it behind a labeled button: the set is self-extinguishing. Hits AND misses persist server-side, so in steady state it is only the bans since the admin's last visit — usually zero — not the permanently-unresolvable purged list, re-leaked in bulk on every press, that the old design implied. Kind-1984 bans are pubkeys the admin already reported in a public event, so the query discloses nothing new. **Manual bans have no public report, so this lookup is the first external disclosure of interest in that pubkey** — a single-pubkey query from the admin's own browser and IP, judged an acceptable trade. That is a documented property of the design, not an accident.
  - This is display sugar. Both pages must work fully, forever, with the socket blocked and no name ever resolved externally.
  - **"Queried" means a REQ actually reached a relay and completed** (EOSE or timeout). If both connections fail, POST nothing and stamp nothing — the next admin page load simply tries again. Stamping pubkeys that were never really asked about would permanently orphan them as false misses.
  - The flow may run again after a re-fetch (e.g. a manual ban just added an unresolved entry) but never re-queries a pubkey already attempted in this page session; that in-page guard plus the persisted stamps make loops impossible.
  - **Status line**, plain text, rendered whenever the flow ran: `resolved <n> locally, <m> via purplepag.es, <k> unresolved`. The outbound connection must be visible in the page itself, not only in a network tab.
  - Why automatic-on-page-load is acceptable here where the old design gated it behind a labeled button: the set is self-extinguishing. Hits AND misses persist server-side, so in steady state the unresolved set is only the bans since the admin's last visit — usually zero — not the permanently-unresolvable purged list, re-leaked in bulk on every press, that the old design implied. Kind-1984 bans are pubkeys the admin already reported in a public event, so the query discloses nothing new. **Manual bans have no public report, so this lookup is the first external disclosure of interest in that pubkey** — a single-pubkey query from the admin's own browser and IP, judged an acceptable trade. That is a documented property of the design, not an accident.
  - This is display sugar. Both pages must work fully, forever, with the socket blocked and no name ever resolved.
- **Appeal contact**: taken from the `contact_appeal` field of the same `/api/banned` response that populates the list — no second request, no separate endpoint. If it is a non-empty string (after trimming), render one plain line directly below "These npubs are banned from this relay.": the fixed prefix "To appeal, contact:" followed by the operator's value inserted with `textContent`. Never linkified, never `innerHTML` — arbitrary config text gets the same treatment as report reasons, even though the operator owns it. If absent, empty, or whitespace-only, render nothing at all: no empty element, no placeholder, no "not configured" text. Shown identically logged in and logged out.
- **Logged-out state (default)**: checkboxes, "Select all", "Unban selected", "Copy purge command", the command blocks, the record lines, and the manual-ban form are all hidden (not merely disabled). Only the heading, the navigation link, the explanatory line, the appeal line, the filter field, the ban list, and the "Login with extension" button are visible.
- "Unban selected" → build a kind-27235 event with `u` + `method` tags, `window.nostr.signEvent`, POST `/api/unban`, add one unban record line, re-fetch, re-render.
- **Manual ban form** (admin only): a text input for one or more npubs (whitespace- or comma-separated; hex also accepted), an optional reason input, and a "Ban" button → sign, POST `/api/ban`, clear the inputs, re-fetch, re-render.

### `authors.html` — `GET /authors`

Admin only. This is the third core function: find who is currently posting, and ban them in bulk.

1. `<h1>strfry-86 — active authors</h1>`, then the link back to `/`
2. "Login with extension" button and status text
3. Command blocks (`<details>`, admin only)
4. Record lines (admin only)
5. The scan button and the provenance line
6. The filter field, placeholder "filter authors"
7. "Select all" checkbox, "Ban selected", "Copy purge command", the optional reason input, and the live count
8. The author list

- **Logged out**: heading, navigation link, and the login button only. No list, no scan control, no filter — nothing that could start work or leak a list. There is no public view of this page.
- **Scan button**: label states what it will do before it is pressed — `scan recent activity (reads the most recent <AUTHOR_SCAN_LIMIT> events)`. On press: sign a kind-27235 event with `u` = `/api/authors/scan`, POST it, disable the button while in flight, then render the response. Nothing scans on page load, on login, or on a timer.
- **Provenance line**, rendered whenever a result is present, as plain text: `<events_read> events, <span_start> → <span_end>, <n> authors — scanned <scanned_at>`, with all four timestamps in the shared format. This line is not optional. It is what makes the list honest: the page shows the most recent N events, not "everyone," and the span tells the admin whether that is six hours or four months on their relay.
- **Author list**: one `<li>` per author, sorted by count descending: the display name in `<b>` if known; the nip05 as plain text if known; the npub as an njump link; the event count; the time of that author's most recent event in `<i>`. A checkbox precedes each entry EXCEPT for authors already in the ban list (render the plain text "(banned)" instead) and the admin's own pubkey (which the server refuses to ban anyway). The page fetches `/api/banned` alongside the scan result to determine this.
- "Ban selected" → build the entries list from the checked set with the shared reason input's value as each entry's reason, sign, POST `/api/ban`, add ban record lines, re-fetch both, re-render.
- A scan result is held by the server, not the page: a reload re-reads it from `GET /api/authors` without re-scanning. The button is the only thing that scans.

## README.md

Must open with the one-command install, in its own fenced code block so GitHub shows the copy button (that IS the one-click copy — no HTML tricks needed):

```
docker exec -it strfry sh -c 'mkdir -p /config/strfry86 && curl -fsSL https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry-86-updater.py -o /config/strfry86/strfry-86-updater.py && python3 /config/strfry86/strfry-86-updater.py'
```

Immediately followed by the update command (same thing, shorter — updater is already installed):

```
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

README must include a "Container has no network?" section with the offline install (two files) and offline update (one file) command sequences, each in its own fenced code block for one-click copy:

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

with a note that the curl lines can be replaced by downloading/dragging the files onto the host by any means — only the `docker cp` and `docker exec` steps matter, and applied bundles are renamed to `.applied-<timestamp>` inside `/config/strfry86/`, where only the most recent one is retained (older applied bundles and all but the 3 newest `strfry.conf.bak-*` files are pruned after each successful run).

README must also cover, briefly: adjusting the container name if not `strfry`; that the admin key is asked for once on first run (defaulting to relay.info.pubkey from strfry.conf if set) and stored in `/config/strfry86/config.json`, public key only, nsec never leaves your extension; that `contact_appeal` in config.json is optional and asked for once (re-asked on a later update only if the key is missing entirely), is shown publicly on the admin page to everyone including logged-out visitors, and can be edited or blanked by hand at any time with effect on the next page load; the compose `ports:` line (`127.0.0.1:8686:8686`, with a note on tailnet/reverse-proxy exposure); adding the relay to your write relays in jumble.social so your reports actually reach it; that strfry-86 deliberately shows no charts or statistics, and that anything needing a database sweep is offered as a copyable command to run in a terminal instead; that banned users' profile names are resolved first from the local database and, only for the remainder, via a single batched query to `wss://purplepag.es` made from the admin's logged-in browser (the server itself never opens outbound connections), with results and misses persisted in `blacklist.json` so each pubkey is queried externally at most once ever — and that hand-deleting an entry's `name_checked_at` makes it eligible for one more try; that bans are forward-looking and existing events are purged with `strfry delete --filter '{"authors":["<hex>"]}'`; where the strfry.conf backups land, that a backup is only written when the file is actually changed, and that the updater keeps the 3 newest conf backups and the 1 newest applied bundle, pruning older ones after a successful run (hand-renamed files are never touched); and the trust model in one honest sentence (the updater executes code from this repo's main branch — don't run someone else's fork blindly).

## Release discipline

`tools/make_bundle.py` regenerates BOTH `manifest.json` (sha256 over every deployable file, sorted keys, trailing newline) and `strfry86-bundle.tar.gz` (every deployable file plus the manifest, deterministic member order). Any commit that changes a deployable file MUST regenerate both — a stale manifest or stale bundle makes the updater skip, reject, or install outdated files. The bundle is committed to the repo so it has one stable raw URL. test.sh should verify that the manifest matches the working tree AND that the committed bundle's contents hash-match the manifest, in addition to piping crafted JSONL through `python3 plugin86.py` and asserting accept/reject for: normal event accepted; banned author rejected; admin 1984 bans its p-tags; non-admin 1984 does not ban; admin pubkey cannot be banned.

## Crypto tests (non-negotiable — this is the whole authorization system)

`lib86/bip340.py` has exactly two callers, both in server86: NIP-98 signature verification, and kind-0 verification in `POST /api/names`. There are no sessions, cookies, or tokens, so the first is the only thing between a stranger and `/api/unban`, `/api/ban`, `/api/authors/scan`, and `/api/names`; the second is the only thing between a forged profile name and permanent storage in `blacklist.json`, displayed publicly forever. Both dangerous failure modes are silent: a verifier that wrongly returns `True` breaks nothing visible — it simply opens the admin API to the internet, or serves attacker-chosen names on the public page. Every other test in the suite covers behavior you would notice breaking; this one does not. It is therefore required, not optional.

- **BIP-340 reference vectors.** Vendor the upstream `test-vectors.csv` at `tests/bip340-vectors.csv` (committed, never fetched at test time — test.sh must pass in a container with no network). Run every verification-path row and assert the expected result EXACTLY, including the failure rows: point not on the curve, `r >= p`, `s >= n`, wrong public key, tampered message, and the negated-parity cases. A run that passes the valid rows and skips the invalid ones tests nothing that matters — accepting everything is the failure being guarded against, and it passes any positive-only suite.
- **NIP-98 auth path, end to end.** bip340 is verification-only by design, so tests cannot sign. Vendor a fixed, pre-signed kind-27235 event as a fixture (`tests/nip98-fixture.json`, generated once by hand and committed with a note on its provenance) and assert it is ACCEPTED against the matching admin pubkey. Then assert each of these mutations is REJECTED, one per case: flipped byte in `sig`; flipped byte in `id`; `pubkey` swapped to a different valid key; `kind` changed from 27235; `method` tag not POST; `u` tag path pointing at a different endpoint than the one being called; `created_at` moved 120s into the past and 120s into the future. Each mutation must fail on its own — a test that only checks the happy path plus one bad signature leaves the other four checks unverified.
- **kind-0 verification path (`/api/names` intake).** Vendor a fixed, pre-signed kind-0 event as a second fixture (`tests/kind0-fixture.json`, generated once by hand and committed with the same provenance note) and assert the intake logic ACCEPTS it when its pubkey is in `queried` and banned. Then assert each mutation is REJECTED — dropped silently, never a 500, since a hostile relay must not be able to break the endpoint: flipped byte in `sig`; flipped byte in `id`; tampered `content` (recomputed id no longer matches); `kind` changed from 0; `pubkey` not in the `queried` list; `pubkey` not currently banned. A forged name is not an outage — it is silently wrong data served to the public forever — which is exactly why the negative cases are the test.
- **bech32 round-trip.** Assert `npub_decode(npub_encode(hex)) == hex` over a handful of known-good pairs, and assert that a checksum-corrupted npub, a wrong-HRP bech32 string (e.g. an `nsec`), and a truncated npub all raise rather than returning a plausible-looking wrong hex. A decoder that silently returns garbage puts the wrong pubkey on the blacklist.

Because these fixtures are test-only, they are NOT deployable files: `tests/` is excluded from `manifest.json` and from `strfry86-bundle.tar.gz`.

## Vendored crypto (lib86)

- `bip340.py`: adapt the BIP-340 python reference implementation (verification path only — no signing, and strip anything requiring third-party libs). Pure integer math on secp256k1.
- `bech32.py`: adapt the BIP-173 python reference implementation; expose `npub_encode(hex) -> npub` and `npub_decode(npub) -> hex`.
- Keep upstream attribution comments in both files. These two files are the ONLY cryptography in the project; never hand-roll alternatives elsewhere.