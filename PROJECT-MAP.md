# PROJECT-MAP.md — strfry-86

A from-the-code map of what this project actually does, as of the current tree.
Read-only survey; every citation is `file:line`. Nothing here proposes changes.
Where something could not be determined from this repo, that is stated explicitly
rather than guessed.

---

## 1. Runtime topology

Four processes total, three of them live in this repo.

**strfry** (the relay binary itself — not in this repo) invokes `plugin86.py` as its
`writePolicy.plugin` subprocess, per the line the updater writes into `strfry.conf`
(`edit_strfry_conf()`, [strfry-86-updater.py:708-782](strfry-86-updater.py#L708-L782)).
It pipes one JSON event per line to the plugin's stdin and reads one accept/reject JSON
line back per line — this is strfry's own plugin protocol, implemented on this side by
`plugin86.py`'s `for line in sys.stdin:` loop
([plugin86.py:227](plugin86.py#L227), docstring [plugin86.py:1-7](plugin86.py#L1-L7)).

**Confirmed against a real container (2026-08-23), not just inferred**: strfry spawns
that plugin subprocess *lazily*, on the first inbound event — never eagerly at relay
startup. Verified by pointing a real `dockurr/strfry` container's `writePolicy.plugin`
at a probe script that logs its own start time, restarting the container, and observing
no such process for as long as no event is published; the marker only appears the
moment a signed event is actually sent over the websocket. This matters concretely
because `plugin86.py` is what spawns (and, as of the same date, continuously
liveness-probes) `server86.py` — a quiet relay's admin page genuinely does not come
back on its own after a `docker restart` or host reboot until *something* publishes to
it, confirming the updater's own `restart_server86()` docstring
([strfry-86-updater.py:987-996](strfry-86-updater.py#L987-L996)), which was previously
the only (indirect) evidence for this. README's "Keep the admin page up across
restarts" section and the optional `ensure-server86.sh` Docker `HEALTHCHECK` wrapper
close that gap for operators who wire it in, independent of relay traffic entirely;
Stats & Console states the caveat in-page for operators who don't.

**plugin86.py** — strfry's hot-path write-policy plugin.
- Reads `config.json` (admin pubkey, port/bind, `next_plugin`/`next_plugin_timeout` —
  `load_config()`/`admin_pubkey_from_config()`, [plugin86.py:174-191](plugin86.py#L174-L191))
  and `blacklist.json` (ban check, [plugin86.py:368](plugin86.py#L368)) via
  `lib86.blacklist`.
- Writes `decisions.jsonl` / `decisions.jsonl.1`, each record now also carrying a
  `stage` field (`"plugin86"` or `"next_plugin"` — see below)
  ([plugin86.py:73-171](plugin86.py#L73-L171)).
- Runs a daemon liveness-probe thread for the life of the process
  (`_server_liveness_loop`, [plugin86.py:247-256](plugin86.py#L247-L256),
  `SERVER_LIVENESS_PROBE_INTERVAL = 5.0`) that spawns `server86.py` only when a
  non-blocking TCP connect to the configured admin port fails
  (`admin_port_bound()`/`ensure_server_running()`,
  [plugin86.py:219-244](plugin86.py#L219-L244)) — replacing a blind, unconditional
  `Popen` checked only once per hour and only inside the stdin loop. Also reachable
  standalone via `python3 plugin86.py --ensure-server86`
  ([plugin86.py:511-522](plugin86.py#L511-L522)), a one-shot probe/spawn meant to be
  invoked from outside the stdin loop entirely (see `ensure-server86.sh`). Still **no
  PID check** before spawning — relies on `server86.py`'s own singleton-by-port-bind
  behavior to make a redundant spawn a no-op, same as before.
- Optionally chains a second write-policy plugin (`config.json`'s `next_plugin`,
  spawned once at startup and kept alive for the process lifetime,
  [plugin86.py:282-350](plugin86.py#L282-L350)): every event plugin86 itself would
  ACCEPT is forwarded to it verbatim and its verdict relayed; a plugin86 REJECT
  short-circuits and is never forwarded. A hang, dead process, or malformed/mismatched
  reply (`next_plugin_timeout`, config-editable, default 2s) disables forwarding
  permanently for the rest of the process's life and logs once — the same
  never-retry, never-block discipline `record_decision()`'s own failure path already
  uses. The updater's "existing plugin left untouched" branch
  ([strfry-86-updater.py:848-878](strfry-86-updater.py#L848-L878)) now offers,
  interactively, to record that existing value as `next_plugin` instead of just
  warning.
- Must never crash the stdin loop (docstring [plugin86.py:6-7](plugin86.py#L6-L7)) —
  every internal exception is caught and substituted with a default accept
  ([plugin86.py:484-486](plugin86.py#L484-L486)); see §7 for the logging-failure
  behavior, which is stricter (permanent disable, not fail-open).

**This is how bans originate in normal use.** `process_event()`
([plugin86.py:167-218](plugin86.py#L167-L218)) checks, in order: is the pubkey already
banned → reject ([plugin86.py:173-174](plugin86.py#L173-L174)); else, **is this event a
kind-1984 NIP-56 report authored by the configured admin pubkey**
([plugin86.py:176](plugin86.py#L176))? If so, for every `"p"` tag on that report with a
valid 64-hex pubkey, plugin86.py calls `blacklist.add(...)` directly
([plugin86.py:192-216](plugin86.py#L192-L216)) — writing `banned_at`, `report_event_id`,
`reason` (the report's content), and `report_type` straight into `blacklist.json` from
inside strfry's write-policy hot path, before the admin's own report event is itself
accepted ([plugin86.py:218](plugin86.py#L218)). No HTTP request, no NIP-98 signature,
and no trip through server86.py is involved in this path at all — `POST /api/ban`
(§2) is the *manual* ban path; a report filed from any ordinary Nostr client (the
relay's own front door) is the one this project is built around, per
[README.md](README.md)'s opening paragraph. Name/nip05 fields are deliberately left
unresolved here (comment [plugin86.py:203-206](plugin86.py#L203-L206)) — no network
lookup happens in this hot path; resolution happens later from the admin's browser.

**server86.py** — the admin HTTP server.
- Entry point `main()` ([server86.py:5546](server86.py#L5546)), run from
  `if __name__ == "__main__"` ([server86.py:5580-5581](server86.py#L5580-L5581)).
- Loads `config.json` (`load_config()`, [server86.py:289-301](server86.py#L289-L301))
  for `admin_pubkey_hex`, `port` (default 8686), `bind` (default `0.0.0.0`).
- Binds `ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)`
  ([server86.py:5554](server86.py#L5554)); on `EADDRINUSE` it exits 0 silently
  ([server86.py:5556-5557](server86.py#L5556-L5557)) — the singleton mechanism
  `plugin86.py`'s blind respawn depends on.
- Before `serve_forever()` ([server86.py:5577](server86.py#L5577)): seeds the live feed
  with one bounded `strfry scan` (`_live_seed_recent()`, [server86.py:5567](server86.py#L5567)),
  starts two daemon threads (`_live_poll_loop`, `_giftwrap_retention_loop`,
  [server86.py:5570-5571](server86.py#L5570-L5571)).
- Reads at module-import time (not inside `main()`): every `*-cache.json`,
  `audit-log.json`, `domain-bans.json`, `author-mode-durations.json` — see §5 for the
  full file table.
- Reads `strfry.conf` lazily, never a fixed path — resolved per-call by
  `strfry_conf_status()` ([server86.py:479](server86.py#L479)): explicit `config.json`
  override → the running relay's own `--config` argv via `/proc`
  (`locate_relay_process()`, [server86.py:422](server86.py#L422)) →
  `STRFRY_CONF_CANDIDATES` ([server86.py:77-83](server86.py#L77-L83)).
- Reads `decisions.jsonl`(+`.1`) on every live-feed poll/render
  ([server86.py:657-658](server86.py#L657-L658), read at
  [server86.py:692](server86.py#L692), [720](server86.py#L720), [748](server86.py#L748))
  — **never writes it**; that channel is plugin86.py's alone.
- Invokes `strfry scan` / `strfry count` / `strfry delete` / `strfry compact` /
  `strfry export` as subprocesses on demand throughout (never on a schedule beyond the
  two loops in §4).

**strfry-86-updater.py** — separate, manually invoked
(`docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py`, per
[README.md:8-21](README.md#L8-L21)). **Not scheduled anywhere in this repo** — no
cron/systemd/timer file exists for it.
- Offline-first source selection (`main()`, [strfry-86-updater.py:918-950](strfry-86-updater.py#L918-L950)):
  local `strfry86-bundle.tar.gz` → network fetch from
  `https://raw.githubusercontent.com/sybenx/strfry-86/main/` → last-resort local
  `manifest.json` (network/bundle both unavailable — refuses to fetch anything new in
  that mode).
- Syncs deployable files by SHA-256 diff against `manifest.json`
  (`sync_files()`, [strfry-86-updater.py:311-361](strfry-86-updater.py#L311-L361)) —
  see §5 and §7 for what this does *not* do (remove stale files).
- Patches `strfry.conf`'s `writePolicy.plugin` line
  (`edit_strfry_conf()`, [strfry-86-updater.py:708-782](strfry-86-updater.py#L708-L782)),
  backing up first only when a byte actually changes.
- Writes/tops-up `config.json` (`ensure_config()`, [strfry-86-updater.py:621-662](strfry-86-updater.py#L621-L662)).
- Self-updates last, deliberately, so it never replaces itself mid-run
  (`self_update()`, [strfry-86-updater.py:364-397](strfry-86-updater.py#L364-L397)).
- **Kills and synchronously restarts `server86.py`** every run
  (`restart_server86()`, [strfry-86-updater.py:814-868](strfry-86-updater.py#L814-L868))
  — SIGTERM, poll for exit, SIGKILL stragglers, spawn fresh, poll for a new PID.
  **Never restarts or signals strfry itself** — strfry re-reads its own config live.

### Who reads/writes what (summary — full table in §5)

| Process | Reads | Writes |
|---|---|---|
| strfry | `strfry.conf` | (not in this repo) |
| plugin86.py | `config.json`, `blacklist.json` | `decisions.jsonl`(+`.1`), `blacklist.json` (auto-ban path, see below) |
| server86.py | `config.json`, `strfry.conf`, all `*-cache.json`, `audit-log.json`, `domain-bans.json`, `names.json`, `decisions.jsonl`(+`.1`, read-only) | `config.json`, `strfry.conf`(+`.bak`), `blacklist.json`, `names.json`, all `*-cache.json`, `audit-log.json`, `domain-bans.json` |
| strfry-86-updater.py | `strfry86-bundle.tar.gz`, `manifest.json`, `config.json`, `strfry.conf`, `/proc` | every synced deployable file, `config.json`, `strfry.conf`(+`.bak-<ts>`), local `manifest.json`, itself |

---

## 2. HTTP endpoints

Two dispatchers: `do_GET` ([server86.py:4824](server86.py#L4824)) and `do_POST`
([server86.py:4989](server86.py#L4989)).

**Auth mechanism (POST only, one shared gate before path dispatch)**:
route allowlist check ([server86.py:4991-5004](server86.py#L4991-L5004), 404 otherwise)
→ body-size cap `POST_BODY_MAX` = 2 MiB
([server86.py:5008-5017](server86.py#L5008-L5017), 413 if exceeded) → body parsed as a
JSON dict ([server86.py:5018-5027](server86.py#L5018-L5027)) → `auth = body.get("auth")`
([server86.py:5029](server86.py#L5029)) — **the NIP-98 event travels inside the JSON
body, not an `Authorization` header** → `verify_nip98()`
([server86.py:4657](server86.py#L4657)) checks event-id recompute, BIP-340 signature,
admin-pubkey match, `kind==27235`, `method=="POST"`, `u` tag origin+path match,
`payload` tag = SHA-256 of the body minus `auth`, freshness within
`NIP98_MAX_SKEW`=60s, and replay via `nip98_mark_used()` keyed by event id
(`NIP98_REPLAY_TTL`=120s). 401 with `err` on any failure
([server86.py:5043-5045](server86.py#L5043-L5045)). **Every POST endpoint below
requires this by construction — there is no per-endpoint auth check.**

### GET endpoints

| Path | Lines | Auth | Reads | Starts job? | Mutates? |
|---|---|---|---|---|---|
| `/home`,`/report`,`/authors`,`/userlist` | [4829-4833](server86.py#L4829-L4833) | none | `LEGACY_REDIRECTS` ([114-119](server86.py#L114-L119)) | no | no (302 only) |
| `/`,`/stats`,`/users`,`/bans`,`/audit`,`/settings`,`/profile`,`/domain`,`/common86.js`,`/favicon.ico` | [4835-4849](server86.py#L4835-L4849) | none | `STATIC_ROUTES` allowlist ([97-108](server86.py#L97-L108)), no path join from request | no | no |
| `/api/live` | [4851-4876](server86.py#L4851-L4876) | none | in-memory live ring + decision log (SSE) | no | no |
| `/api/stats` | [4878-4880](server86.py#L4878-L4880) | none | `get_stats_snapshot()` (cached) | no | no |
| `/api/userlist` | [4882-4884](server86.py#L4882-L4884) | none | joined authors/recipients/reports caches | no | no |
| `/api/reports` | [4886-4888](server86.py#L4886-L4888) | none | `reports-cache.json` | no | no |
| `/api/metrics` | [4890-4892](server86.py#L4890-L4892) | none | cached report + live state + one `os.scandir` (not a scan) | no | no |
| `/api/giftwrap-retention/purge` | [4894-4900](server86.py#L4894-L4900) | none | job status | no | no |
| `/api/ephemeral/purge` | [4902-4905](server86.py#L4902-L4905) | none | job status | no | no |
| `/api/db-compact` | [4907-4910](server86.py#L4907-L4910) | none | job status | no | no |
| `/api/domain-bans` | [4912-4914](server86.py#L4912-L4914) | none | `domain-bans.json` | no | no |
| `/api/audit` | [4914-4920](server86.py#L4914-L4920) | — | **always 401** — `"auth required — POST /api/audit with NIP-98"` | no | no |
| `/api/banned` | [4922-4965](server86.py#L4922-L4965) | none | `blacklist.json`, `resolve_profiles()` (possible bounded scan for uncached names), decision-log tally | possibly (bounded) | no |
| `/api/authors` | [4967-4969](server86.py#L4967-L4969) | none | `authors-cache.json` — "never scans" per docstring | no | no |
| `/api/recipients` | [4971-4973](server86.py#L4971-L4973) | none | `recipients-cache.json` | no | no |
| `/api/subscribers` | [4975-4977](server86.py#L4975-L4977) | none | `subscribers-cache.json` | no | no |
| `/api/relay-url` | [4979-4981](server86.py#L4979-L4981) | none | `config.json` | no | no |
| `/api/report` | [4983-4985](server86.py#L4983-L4985) | none | `report-cache.json` | no | no |
| anything else | [4987](server86.py#L4987) | — | — | 404 | — |

### POST endpoints (all require NIP-98)

| Path | Lines | Starts job? | Mutates |
|---|---|---|---|
| `/api/authors/scan` | [5047-5054](server86.py#L5047-L5054) | yes — bounded author scan | `authors-cache.json`, `author-mode-durations.json` |
| `/api/recipients` | [5056-5059](server86.py#L5056-L5059) | yes — bounded kind-1059 scan | `recipients-cache.json` |
| `/api/subscribers` | [5061-5064](server86.py#L5061-L5064) | yes — bounded kind-10050/10002 scan | `subscribers-cache.json` |
| `/api/relay-url` | [5066-5073](server86.py#L5066-L5073) | no | `config.json` |
| `/api/report/totals` | [5075-5078](server86.py#L5075-L5078) | yes — 3× `strfry scan --count` | `report-cache.json` |
| `/api/report/walk` | [5080-5083](server86.py#L5080-L5083) | yes — the one **unbounded** scan (full DB stream) | `report-cache.json` |
| `/api/reports` | [5085-5088](server86.py#L5085-L5088) | yes — bounded kind-1984 scan | `reports-cache.json` |
| `/api/console` | [5090-5107](server86.py#L5090-L5107) | synchronous, takes the global job lock | none directly — `CONSOLE_VERBS` (`scan`,`info`,`export`) is a read-only allowlist |
| `/api/audit` | [5109-5121](server86.py#L5109-L5121) | no | none — the actual (authenticated) audit read |
| `/api/undo` | [5123-5192](server86.py#L5123-L5192) | no | `blacklist.json` (via add/remove/set_reasons depending on undo type), audit log |
| `/api/settings` | [5194-5196](server86.py#L5194-L5196) | no | none — read of `strfry.conf`+`config.json`+job statuses |
| `/api/strfry-conf-path` | [5198-5211](server86.py#L5198-L5211) | no | none |
| `/api/giftwrap-retention/estimate` | [5213-5220](server86.py#L5213-L5220) | no | none |
| `/api/giftwrap-retention/purge` | [5222-5239](server86.py#L5222-L5239) | yes — count then `strfry delete` | **deletes** kind-1059 events older than the retention window; audit log |
| `/api/ephemeral/estimate` | [5241-5248](server86.py#L5241-L5248) | no | none |
| `/api/ephemeral/purge` | [5250-5259](server86.py#L5250-L5259) | yes — count then `strfry delete` | **deletes** all kinds 20000-29999; audit log |
| `/api/db-compact` | [5261-5270](server86.py#L5261-L5270) | yes — `strfry compact` | writes `<db>/data.mdb.compacted`; audit log |
| `/api/settings/save` | [5272-5318](server86.py#L5272-L5318) | no | `strfry.conf`(+`.bak`) or `config.json`; audit log |
| `/api/domain-unban` | [5320-5336](server86.py#L5320-L5336) | no | `domain-bans.json`, `blacklist.json`; audit log |
| `/api/reason` | [5338-5358](server86.py#L5338-L5358) | no | `blacklist.json` reasons; audit log |
| `/api/profile` | [5360-5375](server86.py#L5360-L5375) | synchronous bounded scans (not job machinery) | none |
| `/api/profile/day` | [5377-5387](server86.py#L5377-L5387) | synchronous bounded scan | none |
| `/api/profile/new` | [5389-5399](server86.py#L5389-L5399) | synchronous bounded scan | none |
| `/api/pubkeys/lookup` | [5401-5407](server86.py#L5401-L5407) | possibly bounded | none |
| `/api/names` | [5409-5451](server86.py#L5409-L5451) | no | `blacklist.json` names and/or `names.json` |
| `/api/unban` | [5453-5478](server86.py#L5453-L5478) | no | `blacklist.json`; audit log |
| `/api/ban` | [5480-5543](server86.py#L5480-L5543) | no | `blacklist.json`; optionally `domain-bans.json`; audit log |

`/api/ban` is reached by **fallthrough** — it is not gated by its own explicit
`if path == "/api/ban":`; it's the final unconditional block after every other branch
returns. Given the route allowlist ([server86.py:4991-5002](server86.py#L4991-L5002))
and that every other listed path has its own earlier `if`/`return`, this fallthrough
only ever executes for `/api/ban`. Flagged here so it isn't mistaken for dead code.

---

## 3. Every page

Shared facts across all 8 pages, stated once here rather than per-page:
- **Only 3 controls anywhere have a `window.confirm()` dialog** — gift-wrap retention
  purge, DB compact, and ephemeral purge, all on `/settings`
  ([settings.html:997-1000](settings.html#L997-L1000),
  [1034-1041](settings.html#L1034-L1041), [1366-1371](settings.html#L1366-L1371)).
  Every other mutating control on every page — ban, unban, bulk ban, reason
  replace/append, domain-unban, settings field Save, per-pubkey ban/unban on
  `/profile`, ban on `/domain`, undo on `/audit` — has **no** confirm dialog.
- The only safety net for most of those un-confirmed actions is the 45-second
  flash-undo (`s86ShowFlashUndo`, [common86.js:2686-2694](common86.js#L2686-L2694)) —
  which covers **ban/unban only**, not reason edits: the function returns immediately
  unless `record.type === 'ban'` or `'unban'`
  ([common86.js:2690-2692](common86.js#L2690-L2692), verified verbatim:
  `if (record.type !== 'ban' && record.type !== 'unban') { return; }`). Reason edits
  get no flash-undo at all — their only undo path is the permanent
  `POST /api/undo` ([server86.py:5174-5190](server86.py#L5174-L5190), which restores
  each pubkey's prior reason from the snapshot taken at edit time). Settings Save and
  the three confirm-gated purge/compact actions have **no** undo path of any kind,
  confirmed or not.
- SSE (`GET /api/live`) is used only by `/` and `/stats`. The other 6 pages are pure
  fetch-on-load / fetch-on-action, no live layer.

### `/` — Live Event Feed ([home.html](home.html))
- **On load**: `GET /api/banned` ([home.html:797](home.html#L797)) for admin pubkey +
  auto-login; `s86ConnectLive(renderFeed, onDisconnect)`
  ([home.html:805](home.html#L805)) opens SSE — the sole feed data source.
- **Renders**: live pill, feed table (Time/Kind/Author/Content/Result), Result column
  sourced solely from the decision log via SSE, never a scan
  ([home.html:541-546](home.html#L541-L546)).
- **Controls**: pubkey filter, kind filter, sortable headers — all client-side re-render
  only, no fetch. Login button (chrome). Global search → `/profile` or `/domain`.
- **Destructive controls**: none.

### `/stats` — Relay Statistics & Console ([stats.html](stats.html))
- **On load (public)**: `GET /api/banned`, `GET /api/stats`, `GET /api/metrics`
  (polled every 15000ms — `METRICS_INTERVAL`, [stats.html:1311](stats.html#L1311)), SSE
  `GET /api/live`.
- **On login (admin)**: `GET /api/report`, `GET /api/recipients`,
  `GET /api/subscribers`, `GET /api/relay-url`.
- **Action-triggered**: refresh-totals → `POST /api/report/totals`; run-walk →
  `POST /api/report/walk`; scan-recipients → `POST /api/recipients`;
  scan-subscribers → `POST /api/subscribers`; relay-url Save →
  `POST /api/relay-url`; console Run → `POST /api/console`.
- **Renders**: 6 headline tiles, database totals + walk scan panels, gift-wrap
  recipients + subscribers panels, write-policy decision log (last 40 lines, SSE-fed),
  console output log.
- **Destructive controls**: none — the console is read-only-verb-enforced server-side
  (`scan`/`info`/`export`); scans/report jobs are non-destructive reads.
- **Polling**: `setInterval(loadMetrics, 15000)`; 3s job-status poll while any panel is
  `running`/`blocked_by`; SSE re-pulls totals when `live.baseline_at` changes.

### `/users` — Users ([users.html](users.html))
- **On load**: `GET /api/banned`.
- **On login**: `POST /api/strfry-conf-path`, `GET /api/authors`,
  `GET /api/userlist` — the actual member-table rows.
- **Action-triggered**: scan button → `POST /api/authors/scan {mode}` (polled 1s while
  running, 3s while blocked); "look up names…" → external relay query then
  `POST /api/names`; "Ban selected" / per-row Ban → `POST /api/ban`.
- **Renders**: member table (User/NIP-05/Events/Reports/Gift-wraps/Last active/Ban),
  scan provenance + composition lines, command generator (clipboard-only).
- **Destructive controls**: **Ban selected** (bulk) and **per-row Ban** →
  `POST /api/ban`, neither has a confirm dialog. "Copy purge command" is
  clipboard-only, never executes.

### `/bans` — Banlist ([bans.html](bans.html))
- **On load**: `GET /api/banned`, `GET /api/domain-bans` (public, renders for
  logged-out visitors too); `maybeResolveNamesExternally()` runs **automatically**
  (not button-gated) inside every render when unresolved names exist and admin is
  logged in, self-limiting via a self-extinguishing attempted-set.
- **Action-triggered**: ban form → `POST /api/ban`; "Unban selected" →
  `POST /api/unban`; bulk reason Replace/Append → `POST /api/reason`; per-domain
  Unban → `POST /api/domain-unban`.
- **Renders**: Banned Pubkeys table, Banned NIP-05 Domains table ("n of m still
  banned"), command generator, appeal-contact line.
- **Destructive controls**: Ban, Unban selected, bulk reason Replace/Append,
  per-domain Unban — **none** have a confirm dialog. Flash-undo shown after ban/unban.

### `/audit` — Audit Log ([audit.html](audit.html))
- **On load**: `GET /api/banned`; on login, `POST /api/audit {q, offset:0}` — signed
  even though it's a read, because "admin action history is not a public cache"
  ([audit.html:714-715](audit.html#L714-L715)).
- **Action-triggered**: filter input debounced 300ms, re-signs and re-fetches each
  time; per-row **Undo** → `POST /api/undo`.
- **Renders**: Time/Admin/Action/Detail/Undo table; "showing newest N of TOTAL" when
  the server total exceeds returned records.
- **Destructive controls**: **Undo** — explicitly called out in-page
  ([audit.html:536-537](audit.html#L536-L537)) as "the ONLY undo authority in the
  project" (i.e. the permanent one, vs. the 45s flash-undo elsewhere). No confirm
  dialog.

### `/settings` — Settings ([settings.html](settings.html))
- **On load**: `GET /api/banned`; on login, `POST /api/settings` (loads both
  `strfry.conf` and `config.json` payloads plus any in-flight purge/compact/ephemeral
  job state); estimate calls fire on section build.
- **Action-triggered**: per-section Save → `POST /api/settings/save`; gift-wrap
  retention "Purge now" → `POST /api/giftwrap-retention/purge`; "Compact" →
  `POST /api/db-compact`; ephemeral "Purge ephemeral kinds" →
  `POST /api/ephemeral/purge`; "Refresh estimate" buttons → the matching `/estimate`
  endpoint.
- **Renders**: Relay section (strfry.conf fields, file-order, "Advanced" details,
  raw-edit fallback), Admin-console section (config.json, same shape), gift-wrap
  retention slider + estimate + Purge/Compact, ephemeral-kinds estimate + Purge.
- **Destructive controls**:
  - **Save** (per section) → `POST /api/settings/save` — rewrites `strfry.conf` or
    `config.json` in place. **No confirm dialog. Not undoable via `/api/undo`.**
  - **Purge now** (gift-wrap retention) → `POST /api/giftwrap-retention/purge`. **Has**
    a confirm dialog: "Delete all gift wraps older than N days now? This runs
    immediately and cannot be undone from this UI."
  - **Compact** → `POST /api/db-compact`. **Has** a confirm dialog explaining it writes
    a compacted copy only — does not shrink the live DB without a manual stop+swap.
  - **Purge ephemeral kinds** → `POST /api/ephemeral/purge`. **Has** a confirm dialog:
    "Delete every event of kind 20000–29999 now? … Cannot be undone from this UI."
  - Purge and Compact share the global job lock, mutually disabling each other and
    other scan jobs while one runs.
- **Polling**: 3s job-status poll for each of the three purge/compact jobs, each
  resuming automatically on page load if already in-flight from a previous visit.

### `/profile?hex=` or `?npub=` — pubkey detail ([profile.html](profile.html))
Reached from search/rows only, not the nav.
- **On load**: `GET /api/banned`; on login, `POST /api/strfry-conf-path` then
  `POST /api/profile {pubkey}` — the whole profile payload. Also, **unauthenticated,
  direct-from-browser**: `maybeFetchNip05Relays()`
  ([profile.html:1032-1091](profile.html#L1032-L1091)) fetches
  `https://<nip05-domain>/.well-known/nostr.json`, silent on failure, no CORS
  fallback. `maybeResolveNameExternally()` auto-fires once per load for this one
  subject pubkey only (justified in-page as safe, unlike bulk lookups).
- **Action-triggered**: Ban/Unban toggle → `POST /api/ban`/`POST /api/unban`; inline
  reason edit → `POST /api/reason`; date navigation → `POST /api/profile/day` or
  `POST /api/profile/new` (incremental delta, not a full re-scan); "look up
  names for the N reporter(s)…" → external relay query then `POST /api/names`.
- **Renders**: identity + ban-status line; `about`/`website`/`picture`/`lud16` as
  **plain text only, never `<img>`/`<a href>`** — explicitly to avoid leaking the
  admin's IP to an attacker-chosen host on fetch
  ([profile.html:891-893](profile.html#L891-L893)); 5 tiles; relay lists; reports
  against; recent/day-view events; command generator prefilled with this pubkey.
- **Destructive controls**: Ban/Unban toggle, Set/Change reason — neither has a confirm
  dialog. Flash-undo after ban/unban.

### `/domain?d=` — domain roster detail ([domain.html](domain.html))
Reached from search/rows only, not the nav.
- **On load**: `GET /api/banned`; on login, if `?d=` was prefilled,
  **auto-fetches the roster** — the one page where the third-party fetch fires
  automatically rather than requiring a click, gated on the URL param being present.
  `fetchRoster()` ([domain.html:738-761](domain.html#L738-L761)) is an
  **unauthenticated, direct-from-browser** fetch of
  `https://<domain>/.well-known/nostr.json`, not proxied through server86.
- **Action-triggered**: "Fetch roster" (same as above); "Use pasted roster" (parses
  pasted JSON client-side, no network call); roster lookup →
  `POST /api/pubkeys/lookup {pubkeys, domain}`; "Ban selected" →
  `POST /api/ban` (with `domain` attached, so `/bans` can group them).
- **Renders**: 3 tiles (Listed / Claim-this-domain % / Already banned), provenance
  line, roster table, curl-hint fallback command for when the browser fetch fails
  (CORS), command generator.
- **Destructive controls**: **Ban selected** → `POST /api/ban`, no confirm dialog,
  flash-undo shown after. No per-row Unban here (that lives on `/bans`' Domain table
  via `/api/domain-unban`).

---

## 4. Background loops / scheduled jobs

| Loop | Trigger | Interval | Evicts / deletes | Bound |
|---|---|---|---|---|
| `_live_poll_loop` | daemon thread, started [server86.py:5570](server86.py#L5570) | `LIVE_POLL_INTERVAL`=3s ([1104](server86.py#L1104)) | ring-buffer entries via `_live_append_row_unlocked` ([895](server86.py#L895), `del ring[:-CAP]` at [900](server86.py#L900)/[903](server86.py#L903)) | `LIVE_RECENT_MAX`=500 (durable), `LIVE_EPHEMERAL_MAX`=100 (ephemeral); one bounded `strfry scan {since,limit:LIVE_POLL_LIMIT}` per tick |
| `_giftwrap_retention_loop` | daemon thread, started [server86.py:5571](server86.py#L5571) | sleeps `GIFTWRAP_RETENTION_INTERVAL`=3600s **first**, then runs ([1457](server86.py#L1457)) | **deletes** kind-1059 events older than the retention window, under the shared scan lock; skips (no retry) that cycle if the lock is held elsewhere | `GIFTWRAP_RETENTION_TIMEOUT`=3600s subprocess deadline; `GIFTWRAP_RETENTION_MIN`=7/`MAX`=30 day clamp ([2575-2577](server86.py#L2575-L2577)) |

Non-thread, on-access bounds worth listing alongside the two real loops — the rest of
the project's eviction/pruning logic that isn't a timer-driven loop:
- Decision-log tail read cap — `_read_log_segment_tail()` ([661](server86.py#L661)),
  `DECISION_READ_BYTES`=1 MiB per segment, further sliced to `DECISION_TAIL_MAX`=500
  records ([681](server86.py#L681)/[709](server86.py#L709)).
- NIP-98 replay-id pruning — `_nip98_prune_used()` ([4568](server86.py#L4568)), inline
  on every successful auth, not timer-driven.
- Audit-log cap — `audit_append()` ([2285](server86.py#L2285)), trims to
  `AUDIT_LOG_MAX`=500 on every append ([2296-2297](server86.py#L2296-L2297)).
- Name-cache eviction — `lib86/namecache.py`'s `_evict()`
  ([lib86/namecache.py:144-151](lib86/namecache.py#L144-L151)) drops the
  oldest-`checked_at` entries down to 90% of `max_entries` whenever a write would
  exceed it, called from `set_local()` ([lib86/namecache.py:189](lib86/namecache.py#L189))
  and `set_external()` ([lib86/namecache.py:231](lib86/namecache.py#L231)), which
  server86.py invokes at [server86.py:2231](server86.py#L2231) and
  [server86.py:5446](server86.py#L5446). This silently discards persisted name-cache
  data once `NAME_CACHE_MAX` (20,000) is exceeded — not just a cap on growth, an actual
  eviction of previously-resolved names.

`plugin86.py`'s hourly server86 respawn check
([plugin86.py:223-230](plugin86.py#L223-L230), `SERVER_RESPAWN_INTERVAL`=3600.0) is a
periodic mechanism too, distinct from server86's own two loops.

**No periodic scan scheduler exists** for the authors/recipients/subscribers/report
caches. server86.py's own docstring states it directly
([server86.py:220-222](server86.py#L220-L222)): only the matching `POST` endpoint ever
starts one of those scans. Nothing refreshes them on a timer or because they went
stale.

---

## 5. On-disk state files

| File | Owner (who writes) | Recoverable if lost? | Survives restart? |
|---|---|---|---|
| `config.json` | updater (`ensure_config`), server86 (`set_relay_url`, `write_app_config`) | **No** — admin identity, port/bind, relay URL, retention settings are operator-entered, not derivable | Confirmed as observed fact (see below) |
| `blacklist.json` | plugin86.py hot path (`blacklist.add`), server86 ban/unban/reason/undo endpoints | **No** — the actual ban list | Confirmed |
| `audit-log.json` | server86 (`audit_append`, `audit_mark_undone`) | **No** — admin action history, the undo source | Confirmed |
| `domain-bans.json` | server86 (`domain_ban_record`, `domain_ban_remove`) | **No** — domain-ban receipts, no cap found | Confirmed |
| `decisions.jsonl` + `.1` | **plugin86.py only** (server86.py only reads) | Bounded/lossy by design — only channel that ever records a REJECT; capped at 2000 lines/segment, 2 segments retained via `os.replace` rotation. Losing it loses reject history permanently (a fresh file just starts empty) | Confirmed |
| `names.json`(+`.lock`) | server86 (`namecache.set_local`, `set_external`) | Yes — cache, rebuildable | Confirmed |
| `authors-cache.json`, `recipients-cache.json`, `subscribers-cache.json`, `report-cache.json`, `reports-cache.json` | server86, after the matching scan completes | Yes — "safe to delete at any time, rebuilt on the next scan" | Confirmed |
| `author-mode-durations.json` | server86, after a successful author scan | Yes — measurement history only | Confirmed |
| `manifest.json` (local copy) | updater, every run | Yes — rebuilt from bundle/network manifest | Confirmed |
| `strfry86-bundle.tar.gz.applied-<unixtime>` | updater — a consumed offline bundle is renamed (not deleted) here after a successful offline install ([strfry-86-updater.py:966-970](strfry-86-updater.py#L966-L970)), then pruned down to `KEEP_APPLIED_BUNDLES`=1 ([strfry-86-updater.py:71](strfry-86-updater.py#L71), pruning at [strfry-86-updater.py:997](strfry-86-updater.py#L997)) | Yes — it's a spent install artifact, not needed for correctness | N/A — written and later pruned, not a persistent state file |
| `strfry.conf` | not owned by this project — the relay's own config | N/A (external) | N/A |

**Restart survival — confirmed as observed fact, not just a docs claim.** The
operator's own listing of the running install directory shows `recipients-cache.json`
dated 2026-08-01 alongside `authors-cache.json` dated 2026-08-23 — state has persisted
across multiple updater runs/restarts over three weeks. Stated positively; the caveat
that remains true is that **nothing in server86.py or the updater enforces or checks**
that the install directory is a durable mount — the persistence above is an
operational fact about this deployment, not a guarantee the code makes.

**Deployed install directory carries dead files this repo does not have.** The
operator's listing of the running `/config/strfry86` includes `activity.html`,
`admin.html`, `authors.html`, `report.html`, `userlist.html` — pages from an earlier
project layout, all since removed from this repo — plus a `favicon.ico` still at the
old 15,406-byte version. Cause: `sync_files()`
([strfry-86-updater.py:311-361](strfry-86-updater.py#L311-L361)) only adds/updates
files *present* in the current `manifest.json`; nothing in the updater ever deletes a
file that has left the manifest since a prior version. Every page ever deleted from
this project is therefore still sitting in every existing install, indefinitely.
Consequence is inert today — `STATIC_ROUTES` ([server86.py:97-108](server86.py#L97-L108))
is an allowlist, so none of these stray files are routable — but it is a real,
unbounded updater gap invisible from reading this repo alone; only an operator's live
directory listing reveals it (see also §7).

**Two distinct backup mechanisms exist for `strfry.conf`, not one.** Server86's own
Settings-page save writes a single, non-timestamped `strfry.conf.bak`
([server86.py:2554](server86.py#L2554), `open(path + ".bak", ...)`, overwritten on
every save) via `write_strfry_conf()`. The **updater's own** `strfry.conf` patch writes
a separately-named, timestamped `strfry.conf.bak-<unixtime>`
([strfry-86-updater.py:772-774](strfry-86-updater.py#L772-L774)), retaining the 3
newest. Same target file, two different code paths, two different naming schemes — an
operator asking "which .bak is the real one" has to know both exist.

---

## 6. Constants

Every module-level constant that bounds something, with its definition line and every
enforcing use site. `server86.py`'s constants are **not** in one block despite
CLAUDE.md's claim — see §7, item 1.

### server86.py

As of the 2026-08-23 cap rework, every module-level bound in this file is explicitly
marked HARD or CONFIGURABLE in a comment at its own definition — that block
([server86.py:127-259](server86.py#L127-L259)) is the source of truth for what each
one protects, in the caller's own words; this table gives def-lines and values only,
not a re-derived "enforced at" site list (the old version of this table cited exact
enforcement line numbers, which drift on every edit — not repeated here to avoid
re-asserting stale precision next time this file changes without this table changing
with it).

**HARD** — a subprocess deadline or a process-lifetime in-memory buffer; raising one
requires a measurement, not just a decision.

| Constant | Value | Def. |
|---|---|---|
| `AUTHOR_SCAN_KINDS` | the SHIPPED default allowlist — not what a scan actually uses; see `extra_author_scan_kinds` in the CONFIGURABLE table below | [155-163](server86.py#L155-L163) |
| `AUTHOR_SCAN_MODES` | `"recent"` (limit 20000) / `"full"` (no limit) | [164-169](server86.py#L164-L169) |
| `AUTHOR_SCAN_DEADLINE` | 240s | [178](server86.py#L178) — the only bound left on the full author scan, the recipient scan, and the subscriber scan, now that `AUTHOR_SCAN_FULL_LIMIT`/`RECIPIENT_SCAN_LIMIT`/`SUBSCRIBER_SCAN_LIMIT` are gone (see §7 note below) |
| `REPORT_WALK_DEADLINE` | 1800s | [179](server86.py#L179) — unlike the deadline above, still an all-or-nothing FAILURE timeout: `compute_report_walk` raises and discards on timeout |
| `REPORT_AUTHOR_KEY_BYTES` | 8 | [180](server86.py#L180) |
| `RATE_SMOOTHING_WINDOW` | 10s | [181](server86.py#L181) |
| `SCAN_TIMEOUT` | 10s | [182](server86.py#L182) |
| `NAME_RESOLVE_SCAN_TIMEOUT` | 60s | [189](server86.py#L189) |
| `GAP_NOTICE_SHARE` | 0.02 | [194](server86.py#L194) |
| `KIND_ALARM_SHARE` | 0.005 | [195](server86.py#L195) |
| `CONSOLE_VERBS` | `("scan","info","export")` | [197](server86.py#L197) |
| `CONSOLE_STDOUT_MAX` / `CONSOLE_STDERR_MAX` | 256 KiB / 64 KiB | [198-199](server86.py#L198-L199) |
| `CONSOLE_RESPONSE_STDOUT_MAX` / `_STDERR_MAX` | 32 KiB / 8 KiB | [206-207](server86.py#L206-L207) |
| `MEMORY_SOFT_BYTES` | 400 MiB | [227](server86.py#L227) |
| `CACHE_LIST_MAX` | 50,000 | [228](server86.py#L228) — became MORE load-bearing after the cap rework: it's now the only thing bounding an uncapped author/recipient scan's retained row list |
| `STREAM_STDERR_MAX` | 64 KiB | [229](server86.py#L229) |
| `POST_BODY_MAX` | 2 MiB | [230](server86.py#L230) |
| `LIVE_POLL_INTERVAL` | 3s | [240](server86.py#L240) |
| `LIVE_BUFFER_MAX` | 500 | [246](server86.py#L246) — replaces the old `LIVE_POLL_LIMIT`/`LIVE_RECENT_MAX` aliases-of-`RENDER_MAX`; bounds the durable-kind ring AND the poll's own scan limit, deliberately split from the display-only `RENDER_PAGE_MAX` below (see §7 note) |
| `LIVE_EPHEMERAL_MAX` | 100 | [251](server86.py#L251) |
| `LIVE_FEED_SEED` | 50 | [252](server86.py#L252) |
| `LIVE_FEED_SEED_SCAN_MAX` | 2,000 | [255](server86.py#L255) |
| `LIVE_CONTENT_MAX` | 280 chars | [256](server86.py#L256) |
| `DECISION_TAIL_MAX` | 500 | [258](server86.py#L258) — no longer aliased to the live-buffer/render constants; same value by convention only |
| `DECISION_READ_BYTES` | 1 MiB | [259](server86.py#L259) |
| `NIP98_KIND` | 27235 | [122](server86.py#L122) |
| `NIP98_MAX_SKEW` | 60s | [123](server86.py#L123) |
| `NIP98_REPLAY_TTL` | =`NIP98_MAX_SKEW*2`=120s | [124](server86.py#L124) |
| `NAME_CACHE_TTL` | 24×3600s | [125](server86.py#L125) |
| `STDERR_TAIL_CHARS` | 300 | [602](server86.py#L602) |
| `CONTACT_APPEAL_CHECK_INTERVAL` | 1.0s | [293](server86.py#L293) |
| `CONSOLE_TIMEOUT` | 30s | [1851](server86.py#L1851) |
| `COMPACT_TIMEOUT` | 3600s | [1852](server86.py#L1852) |
| `COMPACT_OUTPUT_NAME` | `"data.mdb.compacted"` | [1853](server86.py#L1853) |
| `GIFTWRAP_RETENTION_INTERVAL` | 3600s | [2815](server86.py#L2815) |
| `GIFTWRAP_RETENTION_TIMEOUT` | 3600s | [2816](server86.py#L2816) |
| `EPHEMERAL_KIND_MIN` / `MAX` | 20000 / 29999 | [2823-2824](server86.py#L2823-L2824) |
| `EPHEMERAL_PURGE_TIMEOUT` | 3600s | [2826](server86.py#L2826) |

**Display-only** — bounds one response's row count, not a memory/subprocess cost:

| Constant | Value | Def. |
|---|---|---|
| `RENDER_PAGE_MAX` | 500 | [192](server86.py#L192) — split out of the old `RENDER_MAX` specifically so raising it can never again silently multiply the `LIVE_BUFFER_MAX`/`DECISION_TAIL_MAX` buffers that used to alias it |
| `PROFILE_PREVIEW_MAX` | 20 | [190](server86.py#L190) — pure in-memory slice of an already-fetched list |

**CONFIGURABLE** — an operator preference in `config.json` (Settings page, `int` fields),
with the value below as the factory default only; each has a `get_<name>()` accessor
and a `MIN`/`MAX` clamp in `CONFIG_INT_BOUNDS` ([server86.py:2923-2934](server86.py#L2923-L2934)).
None of these feed a subprocess timeout or a process-lifetime buffer of their own —
what actually bounds each one stays a fixed HARD constant from the table above
(`SCAN_TIMEOUT` for the profile/report fields, nothing at all for the pure
size/length fields).

| Constant | Default | Def. | Accessor |
|---|---|---|---|
| `report_scan_limit` | 5,000 | [2834](server86.py#L2834) | `get_report_scan_limit()` [3140](server86.py#L3140) |
| `name_resolve_max` | 500 | [2837](server86.py#L2837) | `get_name_resolve_max()` [3144](server86.py#L3144) |
| `name_cache_max` | 20,000 | [2840](server86.py#L2840) | `get_name_cache_max()` [3148](server86.py#L3148) |
| `domain_lookup_max` | 1,000 | [2843](server86.py#L2843) | `get_domain_lookup_max()` [3152](server86.py#L3152) |
| `profile_event_limit` | 500 | [2846](server86.py#L2846) | `get_profile_event_limit()` [3156](server86.py#L3156) |
| `profile_report_limit` | 100 | [2849](server86.py#L2849) | `get_profile_report_limit()` [3160](server86.py#L3160) |
| `profile_day_events_max` | 50 | [2852](server86.py#L2852) | `get_profile_day_events_max()` [3164](server86.py#L3164) |
| `reason_max_len` | 500 chars | [2855](server86.py#L2855) | `get_reason_max_len()` [3168](server86.py#L3168) |
| `reason_undo_max` | 50 | [2858](server86.py#L2858) | `get_reason_undo_max()` [3172](server86.py#L3172) |
| `audit_log_max` | 500 | [2866](server86.py#L2866) | `get_audit_log_max()` [3176](server86.py#L3176) — the one entry with a floor (`AUDIT_LOG_MAX_MIN`=100), not just a default: this is the only undo authority in the project, so a value set too low destroys undo capability for actions already taken, not just a view |
| `giftwrap_retention_days` | 30 (min 7, max 30) | [2812-2814](server86.py#L2812-L2814) | `get_giftwrap_retention_days()` [3120](server86.py#L3120) — pre-existing, unchanged by this rework |
| `extra_author_scan_kinds` | `[]` (a `"kind_list"` type — comma-separated non-negative ints, not an int itself, no `CONFIG_INT_BOUNDS` entry) | [2930-2935](server86.py#L2930-L2935) | `get_extra_author_scan_kinds()` [3199](server86.py#L3199); `get_effective_author_scan_kinds()` [3222](server86.py#L3222) unions it with `AUTHOR_SCAN_KINDS` — never a replacement, an operator can only widen coverage |
| `next_plugin_timeout` | 2s (min 1, max 30) | near `NEXT_PLUGIN_TIMEOUT_DEFAULT`, server86.py | `get_next_plugin_timeout()` — display/edit only; plugin86.py reads `config.json` itself and applies this timeout, server86.py never enforces it directly. `next_plugin` itself (the downstream command) is a plain `"string"` field, not in this int table |

**Removed in the 2026-08-23 cap rework** (see PROJECT-MAP §7's newest note for detail):
`AUTHOR_SCAN_FULL_LIMIT`, `RECIPIENT_SCAN_LIMIT`, `SUBSCRIBER_SCAN_LIMIT` (all three
scans now run until they finish or `AUTHOR_SCAN_DEADLINE` binds — no separate count
cap), and `FIGURE_HEAD_MAX` (dead code in this file; the load-bearing copy was always
`common86.js`'s independently-hardcoded `S86_FIGURE_HEAD_MAX`).

### plugin86.py

| Constant | Value | Def. | Enforced at |
|---|---|---|---|
| `DECISION_LOG_PATH` | `decisions.jsonl` | [38](plugin86.py#L38) | [60](plugin86.py#L60), [64](plugin86.py#L64), [68-69](plugin86.py#L68-L69), [106](plugin86.py#L106) |
| `DECISION_LOG_PREV_PATH` | `decisions.jsonl.1` | [39](plugin86.py#L39) | [68](plugin86.py#L68), [106](plugin86.py#L106) |
| `DECISION_LOG_MAX_LINES` | 2,000 | [40](plugin86.py#L40) | [66](plugin86.py#L66), [104](plugin86.py#L104) |
| `DECISION_CONTENT_PREVIEW` | 160 chars | [41](plugin86.py#L41) | [99](plugin86.py#L99) |
| `SERVER_LIVENESS_PROBE_INTERVAL` | 5.0s | [48](plugin86.py#L48) | [256](plugin86.py#L256) — replaces the old `SERVER_RESPAWN_INTERVAL`/3600s blind-Popen check |
| `NEXT_PLUGIN_TIMEOUT_DEFAULT` | 2.0s | [58](plugin86.py#L58) | [444](plugin86.py#L444) — fallback only; `config.json`'s `next_plugin_timeout` (CONFIGURABLE, server86.py) wins when set, and the two defaults must match |

### common86.js — a separate, independently-hardcoded set

These are **not fetched from any endpoint** — a change to a server-side constant (e.g.
`RENDER_PAGE_MAX`) does not propagate to its client twin without a manual edit in both
places. `S86_RENDER_MAX` maps to the new `RENDER_PAGE_MAX` (display-only), not to
`LIVE_BUFFER_MAX` — the server-side split that separated those two had no client-side
counterpart to split, since the live-feed ring buffer only ever existed server-side.

`S86_RENDER_MAX`=500 ([common86.js:17](common86.js#L17)),
`S86_FIGURE_HEAD_MAX`=10 ([common86.js:16](common86.js#L16)),
`S86_REASON_UNDO_MAX`=50 ([common86.js:15](common86.js#L15)),
`S86_MAX_RECORDS_STORED`=200, `S86_MAX_RECORDS_RENDERED`=20, `S86_RECORDS_INLINE`=3,
`S86_MAX_DISMISSED`=1000 ([common86.js:8-14](common86.js#L8-L14)),
`S86_FLASH_UNDO_MS`=45000 ([common86.js:21](common86.js#L21)),
`S86_GIFTWRAP_PURGE_CHUNK_SIZE`=200 (used [common86.js:2088-2101](common86.js#L2088-L2101)).

`FIGURE_HEAD_MAX` is the one constant in this report with a documented server-side
definition and **zero server-side enforcement** — see §7, item 6.

---

## 7. Surprises

Most concrete first.

1. **CLAUDE.md contradicts itself on endpoint auth.** Its Endpoints section states
   "`GET /api/audit` (paged, newest first) and `POST /api/undo` back `/audit`" — but
   `GET /api/audit` in the actual code unconditionally returns 401
   ([server86.py:4914-4920](server86.py#L4914-L4920), verified verbatim: `"auth
   required — POST /api/audit with NIP-98"`); the real paged read is
   `POST /api/audit` ([server86.py:5109-5121](server86.py#L5109-L5121)), confirmed
   independently by `audit.html`'s `loadAudit()`
   ([audit.html:712-738](audit.html#L712-L738)), which POSTs with NIP-98 auth. This
   matches `grok-phase.md`'s Phase 3 ("Gate audit log behind NIP-98 (POST)") — a
   security-hardening pass that landed after this CLAUDE.md line was written and was
   never reconciled with it.

2. **CLAUDE.md's own Header section contradicts its own Pages section on nav
   composition.** The Header section lists nav tabs, in order, as "home · stats ·
   userlist · report · authors · bans · audit" (7 items, as if `report`/`authors`/
   `userlist` were still separate pages) — but the Pages section, in the same file,
   says "Six, and only six. `/report` and `/authors` are gone as pages... Both old
   paths, plus `/userlist`, 302 to their new home via LEGACY_REDIRECTS." The actual
   code matches the Pages section, not the Header line: `common86.js`'s
   `S86_NAV_LINKS` ([common86.js:624-631](common86.js#L624-L631), verified verbatim)
   has exactly 6 entries — Live Feed (`/`), Stats & Console (`/stats`), Users
   (`/users`), Banlist (`/bans`), Audit Log (`/audit`), Settings (`/settings`) — no
   separate report/authors/userlist tabs — and `server86.py`'s `LEGACY_REDIRECTS`
   ([server86.py:114-119](server86.py#L114-L119), verified verbatim) confirms those
   three paths 302-redirect rather than render.

3. **CLAUDE.md's constants rule is false as written.** "Constants — `server86.py`, one
   block, every bound auditable in one place" — the actual constants live in at least
   six separate locations ([126-211](server86.py#L126-L211),
   [245](server86.py#L245), [554](server86.py#L554), [1784-1786](server86.py#L1784-L1786),
   [2272](server86.py#L2272), [2575-2589](server86.py#L2575-L2589)), and CLAUDE.md's
   own documented list of 16 constants omits real bounding constants with genuine
   safety implications: `GIFTWRAP_RETENTION_MIN/MAX/DEFAULT`, `EPHEMERAL_KIND_MIN/MAX`,
   `AUDIT_LOG_MAX`, `REASON_MAX_LEN`, `COMPACT_TIMEOUT`, `AUTHOR_SCAN_FULL_LIMIT`,
   `NIP98_MAX_SKEW`/`REPLAY_TTL`, and more (full list in §6). Every constant CLAUDE.md
   *does* list matches its code value exactly — the drift is completeness and
   location, not correctness.

4. **README.md and PHASES.md are substantially stale** relative to current code, and
   contradict CLAUDE.md's current page/endpoint model in the same ways as item 2 (old
   `/report` page, old `POST /api/authors/scan` shape, "command block" language
   superseded by the command *generator*). Not a CLAUDE.md contradiction per se, but
   material for reconstructing "what's current" — CLAUDE.md plus the actual code are
   the sources of truth here, not README/PHASES.

5. **Two backup mechanisms for the same file, `strfry.conf`.** See §5 for the
   full writeup — one non-timestamped `.bak` from server86's Settings page, one
   timestamped, retained-3, `.bak-<unixtime>` set from the updater's own conf patch.

6. **`FIGURE_HEAD_MAX` is dead in server86.py.** Defined once
   ([server86.py:168](server86.py#L168)), confirmed to be its **only** occurrence in
   the file — zero enforcement sites. Its only real enforcement is the independently
   hardcoded client twin, `S86_FIGURE_HEAD_MAX`
   ([common86.js:16](common86.js#L16)), used inside
   `s86BuildRankedFigureTable` ([common86.js:1164](common86.js#L1164)).

7. **Duplicated reports-tally logic, acknowledged in-code.** `compute_authors()`
   ([server86.py:3211-3229](server86.py#L3211-L3229)) and standalone
   `compute_reports()` ([server86.py:3513-3533](server86.py#L3513-L3533)) both scan
   `{"kinds":[1984],"limit":REPORT_SCAN_LIMIT}` and build an identical
   pubkey→reporter-set map. A comment
   ([server86.py:3498-3504](server86.py#L3498-L3504)) explains this is deliberate —
   the userlist must render the reporters column before any author scan has run —
   real duplication, but documented and intentional.

8. **Four independent reimplementations of "is this a 64-char hex string."**
   `plugin86.py`'s `is_valid_hex_pubkey`
   ([plugin86.py:133-141](plugin86.py#L133-L141)), `lib86/blacklist.py`'s `_as_hex64`
   ([lib86/blacklist.py:50-58](lib86/blacklist.py#L50-L58)),
   `lib86/namecache.py`'s `_as_hex64`
   ([lib86/namecache.py:47-54](lib86/namecache.py#L47-L54)), and
   `strfry-86-updater.py`'s `is_hex64`
   ([strfry-86-updater.py:174-181](strfry-86-updater.py#L174-L181), verified
   verbatim) — near-identical, none shared. The updater's copy is justified (must run
   before `lib86` exists); the other three are not.

9. **`common86.js` has two vestigial/dead functions.** `s86UndoReportBan`
   ([common86.js:2654-2669](common86.js#L2654-L2669), verified — no call sites found
   across any of the 8 pages) and `s86RenderRecords`
   ([common86.js:2725-2727](common86.js#L2725-L2727), verified verbatim) — the latter's
   own comment admits it: "Legacy name kept so pages that 'refresh records' on load
   stay valid" — its entire body is now just `s86ClearFlashUndo(container)`.

10. **`cmpStrEmptyLast` reimplemented byte-for-byte in 5 pages**
    ([home.html:657-662](home.html#L657-L662),
    [users.html:756-761](users.html#L756-L761),
    [bans.html:621-626](bans.html#L621-L626),
    [audit.html:561-566](audit.html#L561-L566),
    [domain.html:773-778](domain.html#L773-L778), verified verbatim) — and
    **`KNOWN_KINDS` kind-label maps defined separately and inconsistently in 4
    pages**: `home.html`/`stats.html` carry 12 entries each
    ([home.html:547-551](home.html#L547-L551), verified verbatim: 0, 1, 3, 5, 6, 7,
    1059, 1984, 9735, 10002, 10050, 30023), `users.html`/`profile.html` carry
    only 5 ([users.html:710](users.html#L710), verified verbatim) — a shared lookup
    that drifted into two different versions rather than one common86.js export.

11. **`lib86/blacklist.py:282`'s `set_reasons()` has a no-op conditional.**
    `skipped.append(pk if isinstance(pk, str) else pk)` — both branches evaluate to
    the same value; harmless, but not real type-branching logic.

12. **Only 3 of roughly 20 mutating controls project-wide have a confirm dialog**
    (the three settings.html purge/compact actions) — see §3's shared-facts intro for
    the full picture. Stated as fact for review, not a recommendation.

13. **`/api/ban`'s handler is unreachable-looking but live** — reached by fallthrough
    after every other `if path==...` branch returns
    ([server86.py:5480-5543](server86.py#L5480-L5543)), verified: `/api/ban` is not
    gated by its own explicit check. Flagged so it isn't mistaken for dead code.

14. **Every HTTP response server86.py has ever sent is uncompressed.** `_send_json`
    ([server86.py:4812-4818](server86.py#L4812-L4818), verified verbatim) sets
    `Content-Type` and `Content-Length` and writes raw bytes straight to `self.wfile`;
    there is no `Content-Encoding` header and no gzip anywhere in the file (zero
    matches file-wide). On the operator's live relay, two observed responses measured
    11.9 MB and 13.5 MB uncompressed.

15. **The updater never removes files that leave `manifest.json`** — see §5's writeup
    on the deployed install directory carrying five dead pages (`activity.html`,
    `admin.html`, `authors.html`, `report.html`, `userlist.html`) plus a stale
    `favicon.ico`, none of which exist in this repo anymore. Inert (not routable) but
    unbounded — every historical page this project has ever deleted is still
    physically present in every existing install, forever, and this is not visible
    from the repo alone.

16. **The 2026-08-23 cap rework changed what "saturated" means for three scans, not
    just their numbers.** `AUTHOR_SCAN_FULL_LIMIT`, `RECIPIENT_SCAN_LIMIT`, and
    `SUBSCRIBER_SCAN_LIMIT` were removed outright rather than raised — each of those
    three scans is backed by `run_strfry_scan_streaming`, which never buffers more
    than one event, so the count cap was never protecting memory; it was only making
    a "full" scan quietly stop being full once a relay's matching-event count passed
    it. `AUTHOR_SCAN_DEADLINE` (240s) is now the sole bound on all three, and
    `run_strfry_scan_streaming` grew a `timeout_ok` parameter
    ([server86.py:2164](server86.py#L2164)) so hitting that deadline is a completed
    "ran out of time" result (fresh `scanned_at`, `saturated`/`timed_out` True, a
    `warning` naming the exact shortfall) rather than the failure it used to be —
    `compute_report_walk`'s own deadline (`REPORT_WALK_DEADLINE`) deliberately did
    NOT get this treatment, since a partial database walk isn't a usable totals
    record the way a partial author/recipient/subscriber list still is. Ten other
    constants (`REPORT_SCAN_LIMIT`, `NAME_RESOLVE_MAX`, `NAME_CACHE_MAX`,
    `DOMAIN_LOOKUP_MAX`, `PROFILE_EVENT_LIMIT`, `PROFILE_REPORT_LIMIT`,
    `PROFILE_DAY_EVENTS_MAX`, `REASON_MAX_LEN`, `REASON_UNDO_MAX`, `AUDIT_LOG_MAX`)
    moved from module constants to `config.json`-backed operator settings, following
    the pre-existing `giftwrap_retention_days` pattern; see §6's new CONFIGURABLE
    table. `RENDER_MAX` was split into `RENDER_PAGE_MAX` (display-only) and
    `LIVE_BUFFER_MAX` (the process-lifetime live-feed ring buffer it used to alias
    without saying so), specifically so raising a page-size constant can never again
    silently triple a persistent server-wide buffer. This makes §6, item 3's point
    about this file's own claimed "one block" constants organization more true than
    it was, not less — the bounds block ([server86.py:127-259](server86.py#L127-L259))
    now explicitly marks every entry HARD or CONFIGURABLE inline, but the
    CONFIGURABLE defaults/accessors still live near `CONFIG_EDITABLE_FIELDS`, ~2700
    lines later, matching the pre-existing `giftwrap_retention_days` layout rather
    than consolidating everything into one physical location.

17. **`AUTHOR_SCAN_KINDS` is a floor, not the whole allowlist, as of the same rework.**
    The gap-alarm ladder (`GAP_NOTICE_SHARE`/`KIND_ALARM_SHARE`,
    [server86.py:194-195](server86.py#L194-L195)) was deliberately kept — verified
    against a seeded test-relay's real 300-kind unlisted tail, all four ladder states
    render as designed — but its `stale` state used to terminate outside the UI
    (edit `AUTHOR_SCAN_KINDS` in server86.py, redeploy). `extra_author_scan_kinds`
    ([server86.py:2930-2935](server86.py#L2930-L2935)) closes that loop: a new
    `"kind_list"` CONFIG_EDITABLE_FIELDS type, additive only —
    `get_effective_author_scan_kinds()` ([server86.py:3222](server86.py#L3222)) is
    the shipped default UNIONED with operator additions, and every scan/count/
    comparison that means "the allowlist" reads that function now, never
    `AUTHOR_SCAN_KINDS` directly (the two exceptions are the constant's own
    definition and the `AUTHOR_SCAN_KINDS` gap-drift check in test.sh, which now
    audits the effective set instead). Adding a kind does not retroactively cover
    an already-completed author-scan cache — `_authors_cache_kinds_stale()`
    ([server86.py:4008](server86.py#L4008)) compares a cached FULL-mode scan's
    recorded `scan_kinds` against the current effective set and surfaces
    `authors_kinds_stale` on `/api/userlist`, so the Users page says so rather than
    presenting stale coverage as current.
