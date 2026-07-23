# CLAUDE.md — strfry-86

## What this project is

**strfry-86** is a pure-Python moderation sidecar for a [strfry](https://github.com/hoytech/strfry) Nostr relay (repo: https://github.com/sybenx/strfry-86), deployed *inside* the operator's existing strfry Docker container with zero image changes, zero pip installs, and zero new compose services. ("86" is restaurant slang for banning someone from the establishment — fitting for a relay literally named stir fry.)

How it works, end to end:

1. The admin reports a user from any normal Nostr client (primarily jumble.social). The client publishes a NIP-56 report (`kind 1984`).
2. The strfry write-policy plugin (`plugin86.py`) sees the report. If — and only if — it is authored by the admin pubkey, every pubkey in its `p` tags is added to the blacklist. From then on, all events authored by blacklisted pubkeys are rejected.
3. A tiny stdlib web server (`server86.py`) serves a bare HTML page listing bans. The admin logs in with a NIP-07 extension and unbans via checkboxes. Unbans are authorized per-request with NIP-98 signed events — no sessions.
4. A single self-contained installer/updater (`strfry-86-updater.py`) handles first install, config, strfry.conf rewrite, and all future updates, run via one `docker exec` command.

## Hard environment constraints (do not violate)

- **Python 3 stdlib ONLY.** No pip, no venv, no third-party imports anywhere. The target is the Python that ships in the operator's strfry container. Schnorr verification and bech32 are vendored (see below).
- **Everything lives in `/config/strfry86/`** inside the container — this is on the operator's permanent `strfry_config` named volume, so it survives container recreation. Nothing is written anywhere else except the strfry.conf edit and its backup.
- **No custom Docker image, no new compose service, no entrypoint changes.** The only compose change the operator makes by hand is adding a `ports:` line for the admin page (document in README).
- **`plugin86.py` writes nothing but protocol JSON to stdout** (stderr for all logging) and never crashes on bad input — a dead plugin can wedge the relay.
- Only the admin pubkey can ban (via kind 1984, or manually via NIP-98 `/api/ban`) or unban (via NIP-98). No other trust roots.
- The admin pubkey can never end up in the blacklist (silent no-op on any attempt).
- admin.html styling is limited to the exact CSS block in the admin.html section (centering, edge padding, npub wrapping, one mobile font-size media query). No fonts, no colors, no frameworks, no CDN.

## Repo layout

```
strfry-86-updater.py   # installer + updater, the only file the operator ever runs
plugin86.py            # strfry write-policy plugin (stdin/stdout JSONL)
server86.py            # stdlib http.server admin server, spawned by plugin86
lib86/__init__.py      # empty
lib86/bip340.py        # vendored BIP-340 schnorr verification (pure python reference impl)
lib86/bech32.py        # vendored bech32/npub encode+decode (pure python reference impl)
lib86/blacklist.py     # shared blacklist load/save/add/remove, atomic writes, mtime reload
admin.html
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
  admin.html
  manifest.json
  config.json        # OPERATOR-OWNED: admin pubkey (hex), port, bind. Never in manifest, never overwritten.
  blacklist.json     # OPERATOR-OWNED: the ban list. Never in manifest, never overwritten.
```

## strfry-86-updater.py

Single file, self-contained (may not import lib86 — it must run before lib86 exists). Idempotent: safe to run any number of times.

**Source selection (offline-first):** on startup, look for `strfry86-bundle.tar.gz` in the updater's own directory. If present → OFFLINE MODE: the bundle is the source; read `manifest.json` from inside it and extract files from it instead of downloading. If absent → NETWORK MODE: fetch from the repo raw URL as described below. All downstream logic (diffing, verification, config, strfry.conf, server restart, self-update) is identical in both modes.

Offline-mode specifics:
- Open with stdlib `tarfile`. Before extracting ANYTHING, validate every member name: reject absolute paths, `..` components, and links; abort loudly on violation.
- Extract each needed file to `<name>.tmp`, verify sha256 against the bundle's manifest, then `os.replace` into place — same atomicity as network mode. Only extract files that are missing or hash-differ locally (same diffing).
- After a fully successful run, rename the bundle to `strfry86-bundle.tar.gz.applied-<unixtime>` so a re-run without a fresh bundle cleanly no-ops (it falls back to comparing local files against the local `manifest.json` and reports "unchanged"). Never delete applied bundles.
- Self-update in offline mode: if the bundle contains a changed updater, extract it LAST (after the rename step is queued), replace atomically, print "updater updated — effective next run."

Network-mode flow:

1. **Fetch `manifest.json`** from `https://raw.githubusercontent.com/sybenx/strfry-86/main/` (repo base URL is a constant at the top of the file). Manifest maps relative path → sha256 for every deployable file. `config.json` and `blacklist.json` are NEVER in the manifest.
2. **Diff against local**: sha256 each local file; download only missing/changed files. Download to `<name>.tmp` then `os.replace` (atomic). Verify sha256 of each download against the manifest before installing; abort loudly on mismatch.
3. **First-run config**: if `config.json` doesn't exist, determine the admin pubkey and write `config.json` with `{"admin_pubkey_hex": ..., "port": 8686, "bind": "0.0.0.0"}`. To determine the pubkey: first try to read `relay.info.pubkey` from `/config/strfry.conf` (the NIP-11 admin contact); if found and it parses as a valid pubkey, offer it as the default — "Found relay.info.pubkey <npub...> in strfry.conf — use as admin? [Y/n]". On decline, or if the field is absent/invalid, prompt for a paste (accept npub or 64-hex; decode npub via inline bech32 — small enough to duplicate in the updater). Never silently adopt the strfry.conf value without confirmation: this key is the sole root of trust for banning. (Bind inside the container must be 0.0.0.0 for the compose port mapping to work; the README tells the operator to scope exposure via the compose `ports:` line, e.g. `127.0.0.1:8686:8686`.)
4. **strfry.conf edit**: locate `/config/strfry.conf` (constant, documented). Before any modification, copy to `strfry.conf.bak-<unixtime>`. Then:
   - If `writePolicy.plugin` already points at `/config/strfry86/plugin86.py` → no-op.
   - If it is empty/unset → set it to `plugin = "/config/strfry86/plugin86.py"`.
   - If it points at some OTHER plugin → do NOT touch it; print a loud warning telling the operator to resolve manually.
   - Edit conservatively with line-oriented matching on the `writePolicy` block; do not reformat the rest of the file.
   - When detecting whether a plugin is already configured, ignore comment lines (lines whose first non-whitespace character is `#`) — a commented-out `# plugin = ...` line is NOT an active plugin and must not trigger the refuse-to-touch path. Never modify, remove, or uncomment any comment line anywhere in the file.
5. **chmod +x** `plugin86.py` (it has a `#!/usr/bin/env python3` shebang; strfry executes it directly).
6. **Restart the web server**: after any successful update, find and kill any running `server86.py` (match on cmdline via `/proc`, no pgrep dependency). The next event through the plugin respawns it with fresh code. Print what was done.
7. **Self-update**: if the manifest shows the updater itself changed, download it LAST, replace atomically, and print "updater updated — already effective next run."
8. Exit with a clear summary: installed/updated/unchanged file counts, config status, strfry.conf status, and the hint "if in doubt: docker restart <container>".

## plugin86.py — strfry write policy

strfry spawns the plugin once and writes one JSON object per line to stdin; the plugin answers one JSON per line on stdout.

Input (relevant fields): `{ "type": "new", "event": { "id", "pubkey", "kind", "tags", "content", "sig", "created_at" }, ... }`
Output: `{"id": "<event id>", "action": "accept"}` or `{"id": "<event id>", "action": "reject", "msg": "blocked: banned pubkey"}`

Logic per event, in order:

1. Parse; on failure log to stderr, continue.
2. Author blacklisted → reject `"blocked: banned pubkey"`.
3. `kind == 1984` and `pubkey == admin` → for every valid `p` tag (64-char lowercase hex, validate, skip malformed), add to blacklist with `banned_at = created_at`, `report_event_id = id`, `reason = content`, and `report_type` resolved as: the p tag's own third element if present, otherwise the third element of the first `e` (or `a`) tag that has one, otherwise null. This dual lookup is required by how jumble builds reports (verified against its source): profile reports carry the type on the p tag (`["p", <pubkey>, <type>]`), but note reports carry it on the e tag (`["e", <id>, <type>]`) with a bare p tag. Jumble's type options are `nudity`, `malware`, `profanity`, `illegal`, `spam`, `other` (NIP-56 also defines `impersonation`; store whatever string arrives, don't whitelist). Note: jumble always sends `content: ""`, so `reason` will be empty for jumble reports — the type is the only signal. Then accept. Do NOT verify the signature here — strfry has already verified it, and this is the hot path.
4. Otherwise accept.

On startup (and once per hour thereafter), plugin86 ensures `server86.py` is running: spawn `python3 /config/strfry86/server86.py` fully detached (`start_new_session=True`, stdin/stdout/stderr to devnull — the plugin's stdout is sacred). server86 enforces singleton by port-bind: if the bind fails with EADDRINUSE it exits 0 silently, so repeated spawns are harmless.

Use unbuffered/line-flushed stdout. Reload the blacklist on mtime change, checked at most once per second (see lib86/blacklist.py).

## server86.py — admin page + unban API

stdlib `http.server` (ThreadingHTTPServer). Routes:

- `GET /` → `admin.html`.
- `GET /api/banned` → `{"admin": "<hex>", "banned": [{"pubkey", "npub", "banned_at", "reason", "report_type", "name"}]}`. Public read is fine. `name` is resolved server-side from the LOCAL strfry database: run `strfry --config /config/strfry.conf scan '{"kinds":[0],"authors":[<uncached hex>]}'` via subprocess (binary path and conf path are constants at the top of the file; scan is a read-only LMDB read, safe while the relay runs), parse each event's `content` JSON, take `display_name || name`. Cache results in an in-memory dict `{pubkey: (name, checked_at)}`; re-query only pubkeys that are uncached or were misses older than 24h, and batch all of them into ONE scan call per request. If the subprocess fails for any reason (binary missing, bad path, timeout of a few seconds), log to stderr and return `name: null` — the endpoint must never break because name lookup broke.
- `POST /api/unban` → body `{"auth": <signed nostr event>, "pubkeys": ["<hex>", ...]}` → removes each, returns `{"ok": true, "removed": [...]}`.
- `POST /api/ban` → body `{"auth": <signed nostr event>, "entries": [{"pubkey": "<npub or 64-hex>", "reason": "<optional>"}, ...]}` → for each entry: decode npub via `lib86/bech32.py` if needed, validate, skip malformed; add to blacklist with `banned_at = now`, `reason` (empty string if omitted), `report_type = "manual"`, no `report_event_id`. Admin pubkey is silently skipped (per the hard constraint). Returns `{"ok": true, "added": [...], "skipped": [...]}`.

NIP-98 auth checks for `/api/unban` and `/api/ban` — ALL must pass, else 401 JSON error:

1. Signature valid per BIP-340 over the NIP-01 serialized event id (use `lib86/bip340.py`; recompute the event id and check it matches `auth.id` before verifying the sig).
2. `auth.pubkey == admin_pubkey_hex`.
3. `auth.kind == 27235`.
4. `method` tag is `POST`; `u` tag's path matches the endpoint being called (`/api/unban` or `/api/ban`; lenient on host/origin — reverse proxies change it).
5. `abs(created_at - now) <= 60`.

No sessions, cookies, or tokens.

## admin.html

Raw HTML, one vanilla `<script>` block, no libraries. `<meta name="viewport" content="width=device-width, initial-scale=1">`. One `<style>` block containing ONLY these layout rules — nothing else, browser defaults throughout:

```css
body { max-width: 40em; margin: 0 auto; padding: 0 1em; }
li { overflow-wrap: anywhere; }
@media (max-width: 600px) { body { font-size: 1.15em; } }
```

The `padding: 0 1em` is REQUIRED — without it, content sits flush against the screen edge on mobile (max-width centering does nothing when the viewport is narrower than the max-width). The `overflow-wrap: anywhere` is REQUIRED — npubs are 63-char unbreakable strings and will overflow the viewport horizontally without it. Do not remove either in the name of minimalism; they are the minimum.

Content and behavior — page order is fixed, top to bottom, with ALL non-list UI above the ban list so controls stay reachable when the list is thousands of entries long:

1. `<h1>strfry-86</h1>`
2. "Login with extension" button (plus its status text: "this key is not the admin" / "a NIP-07 extension is required")
3. Manual ban form (admin only, hidden until login)
4. "Unban selected" button (admin only, hidden until login) — yes, ABOVE the list it operates on; the admin scrolls down, ticks boxes, scrolls back up
5. Plain text: "These npubs are banned from this relay." — visible to everyone
6. The ban list

Behavior:

- Ban list loads for everyone on page load from `/api/banned`: one `<li>` per ban containing, in order: the display name (see below) wrapped in `<b>` if known; the npub as a raw `<a href="https://njump.me/<npub>" target="_blank">` link; the ban time wrapped in `<i>`; report type and reason. Omit the name/type/reason portions cleanly when null or empty (jumble reports always have an empty reason). Insert all user-influenced strings via `textContent` (report reasons AND profile names are attacker-influenced) — build the `<b>`/`<i>`/`<a>` elements with `createElement` and set their `textContent`, never innerHTML.
- **Timestamps**: render `banned_at` as `YYYY-MM-DD@HH:MM UTC` (e.g. `2026-07-23@14:35 UTC`; derive from `toISOString()`, replace the `T` with `@`, drop seconds/milliseconds and the `Z`, append " UTC"), italicized via the `<i>` wrapper. `created_at` is unix time, so these are inherently UTC. Bold/italic come from the semantic tags with browser default styling — no CSS additions.
- **Display names**: names come primarily from the server (`name` field in `/api/banned`, resolved from the local strfry DB). The client WebSocket lookup is a FALLBACK only, for entries where `name` is null (typical after the operator purges a banned user's events with `strfry delete`, which deletes their kind 0 too). For those: keep a localStorage cache mapping pubkey hex → `{name, checked_at}`; collect null-name pubkeys with no cache entry (or cached misses older than 24h), open ONE WebSocket to `wss://purplepag.es` and send a single batched REQ `{"kinds":[0],"authors":[<all uncached hex>]}`; on failure to connect, retry once against `wss://relay.damus.io`. For each kind-0 received, parse `content` JSON and take `display_name || name`; close the socket on EOSE or a 5s timeout. Cache every queried pubkey — including misses (`name: null`) — so each npub is queried at most once (misses re-checked at most daily). Fill names into the already-rendered list via `textContent` inside the same `<b>` wrapper used for server-provided names; entries with no name anywhere show the npub only. This is display sugar — the page must work fully with the socket blocked.
- **Logged-out state (default)**: checkboxes, the "Unban selected" button, and the manual-ban form are all hidden (not merely disabled). Only the heading, the explanatory line, the ban list, and the "Login with extension" button are visible.
- "Login with extension" button → `window.nostr.getPublicKey()`. Match admin → reveal the manual-ban form, "Unban selected", and the checkboxes (in that page order); mismatch → plain text "this key is not the admin"; no `window.nostr` → plain text "a NIP-07 extension is required".
- "Unban selected" button → build kind-27235 event with `u` + `method` tags, `window.nostr.signEvent`, POST `/api/unban`, re-fetch, re-render.
- **Manual ban form** (admin only): a text input for one or more npubs (whitespace- or comma-separated; hex also accepted), an optional reason text input, and a "Ban" button → build kind-27235 event with `u` tag for `/api/ban` + `method` tag, `window.nostr.signEvent`, POST `/api/ban`, clear the inputs, re-fetch, re-render.

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

with a note that the curl lines can be replaced by downloading/dragging the files onto the host by any means — only the `docker cp` and `docker exec` steps matter, and applied bundles are renamed to `.applied-<timestamp>` inside `/config/strfry86/`.

README must also cover, briefly: adjusting the container name if not `strfry`; that the admin key is asked for once on first run (defaulting to relay.info.pubkey from strfry.conf if set) and stored in `/config/strfry86/config.json`, public key only, nsec never leaves your extension; the compose `ports:` line (`127.0.0.1:8686:8686`, with a note on tailnet/reverse-proxy exposure); adding the relay to your write relays in jumble.social so your reports actually reach it; that bans are forward-looking and existing events are purged with `strfry delete --filter '{"authors":["<hex>"]}'`; where the strfry.conf backups land; and the trust model in one honest sentence (the updater executes code from this repo's main branch — don't run someone else's fork blindly).

## Release discipline

`tools/make_bundle.py` regenerates BOTH `manifest.json` (sha256 over every deployable file, sorted keys, trailing newline) and `strfry86-bundle.tar.gz` (every deployable file plus the manifest, deterministic member order). Any commit that changes a deployable file MUST regenerate both — a stale manifest or stale bundle makes the updater skip, reject, or install outdated files. The bundle is committed to the repo so it has one stable raw URL. test.sh should verify that the manifest matches the working tree AND that the committed bundle's contents hash-match the manifest, in addition to piping crafted JSONL through `python3 plugin86.py` and asserting accept/reject for: normal event accepted; banned author rejected; admin 1984 bans its p-tags; non-admin 1984 does not ban; admin pubkey cannot be banned.

## Vendored crypto (lib86)

- `bip340.py`: adapt the BIP-340 python reference implementation (verification path only — no signing, and strip anything requiring third-party libs). Pure integer math on secp256k1.
- `bech32.py`: adapt the BIP-173 python reference implementation; expose `npub_encode(hex) -> npub` and `npub_decode(npub) -> hex`.
- Keep upstream attribution comments in both files. These two files are the ONLY cryptography in the project; never hand-roll alternatives elsewhere.