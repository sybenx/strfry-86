# Eighty-Six (repo: `strfry-86`)

A moderation sidecar for a [strfry](https://github.com/hoytech/strfry) Nostr relay, written in Python with no pip dependencies. It installs inside the existing strfry Docker container, with no image change and no extra compose service. ("86" is restaurant slang for refusing someone service.)

A report sent from an ordinary Nostr client (for example [jumble.social](https://jumble.social)) adds the reported user to the relay's write-policy blacklist. Bans are lifted from an admin web page that logs in with a NIP-07 browser extension.

## Install

```
docker exec -it strfry sh -c 'mkdir -p /config/strfry86 && curl -fsSL https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry-86-updater.py -o /config/strfry86/strfry-86-updater.py && python3 /config/strfry86/strfry-86-updater.py'
```

## Update

The updater is already installed after the first run — just re-run it:

```
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

Safe to run any number of times.

If your strfry container isn't named `strfry`, substitute your actual container name in both commands above (`docker ps` to check).

## Container has no network?

Eighty-Six is offline-first: if `strfry86-bundle.tar.gz` sits next to the updater script, the updater installs from it directly and never touches the network. Get both files onto the host by any means (the `curl` lines below are just one option — dragging the files over or `scp` work identically), then `docker cp` them in and run the updater exactly as before.

Offline install (two files):

```
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry-86-updater.py
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry86-bundle.tar.gz
docker cp strfry-86-updater.py strfry:/config/strfry86/
docker cp strfry86-bundle.tar.gz strfry:/config/strfry86/
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

Offline update (updater is already installed — one file):

```
curl -LO https://raw.githubusercontent.com/sybenx/strfry-86/main/strfry86-bundle.tar.gz
docker cp strfry86-bundle.tar.gz strfry:/config/strfry86/
docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py
```

Only the `docker cp` and `docker exec` steps matter — get the file(s) onto the host however is convenient. Applied bundles are renamed to `.applied-<timestamp>` inside `/config/strfry86/`; only the most recent one is retained (older applied bundles and all but the 3 newest `strfry.conf.bak-*` files are pruned after each successful run).

## First run

On first run the updater needs your admin pubkey — the one and only key allowed to ban (via a NIP-56 report, kind `1984`, or manually from the admin page) or unban (via NIP-98). It tries to read `relay.info.pubkey` from your `strfry.conf` first and, if found, asks you to confirm before using it; otherwise it prompts you to paste an `npub` or 64-char hex pubkey. It is never adopted silently. The result is stored as a public key only in `/config/strfry86/config.json` — your `nsec` never leaves your extension, and never touches this server.

Right after the admin pubkey, it also asks once for an optional `contact_appeal` — free text (email, npub, URL, whatever) shown publicly on the admin page, to everyone including logged-out visitors, so a banned user knows where to appeal. Blank is a valid answer. If this key is ever missing entirely from `config.json` (e.g. upgrading from an older install), a later update run asks for it once and adds it in — it's otherwise never re-asked. You can edit or blank it by hand in `config.json` at any time; the admin page picks up the change on its next load, no restart needed.

## Expose the admin page

The admin page listens on port 8686 inside the container by default. Add a `ports:` line to your compose file to expose it:

```yaml
services:
  strfry:
    ports:
      - "127.0.0.1:8686:8686"
```

`127.0.0.1:8686:8686` keeps it reachable only from the host itself — put it behind your tailnet or a reverse proxy (with TLS) if you want to reach it from elsewhere. Don't bind it to `0.0.0.0` on the host without something in front of it.

### Keep the admin page up across restarts

strfry only spawns plugin86.py — and, through it, the admin page — lazily, on the relay's first write after container start (confirmed against a real container: this really is a "first event," not "process is up"). On a busy relay that's a non-issue; on a quiet one, `docker restart` or a host reboot can leave the admin page down indefinitely, until *something* actually publishes to the relay. Wire the included `ensure-server86.sh` into your container's own `HEALTHCHECK` — Docker's exec is the one thing guaranteed to run on a fixed interval regardless of relay traffic — and it closes that gap independent of events entirely:

```yaml
services:
  strfry:
    ports:
      - "127.0.0.1:8686:8686"
    healthcheck:
      test: ["CMD", "sh", "/config/strfry86/ensure-server86.sh",
             "/app/strfry", "--config=/etc/strfry.conf", "scan", "--count", "{}"]
      interval: 2s
      timeout: 5s
      retries: 30
```

Replace the trailing `/app/strfry --config=/etc/strfry.conf scan --count {}` with whatever your own image's `HEALTHCHECK` already runs (or leave it as shown — that's `dockurr/strfry`'s own default, and a harmless read either way). The wrapper's exit code is always that wrapped command's own, unmodified — it can never report "healthy" on its own account, and can never get the relay depooled over a problem that isn't the relay's; the only thing it adds is trying to bring the admin page back up as an unrelated side effect. Skip this if you don't use Docker's `HEALTHCHECK` at all — the admin page still comes back on the next event, and on every updater run (which restarts it directly, synchronously).

## Add the relay as a write relay

For reports to actually reach Eighty-Six, add this relay to your write relays in jumble.social (or whatever client you report from) — otherwise your NIP-56 reports go somewhere else and nothing gets banned.

## How bans work

Bans are forward-looking only: banning a pubkey stops it from writing *new* events from that point on, it does not retroactively remove what's already stored. To purge a banned author's existing events, run inside the container:

```
strfry delete --filter '{"authors":["<hex-pubkey>"]}'
```

## Unbanning

Open the ban list page (`/bans`), click "Login with extension" (NIP-07), check the pubkeys you want to unban (a live "n selected" count sits next to the button), click "Unban selected". Each unban is authorized per-request with a freshly signed NIP-98 event — there are no sessions or cookies. Logging in is remembered in this browser (localStorage) so you aren't re-prompted by your extension moving between pages — that changes nothing about the security model, every write is still individually NIP-98 signed and checked server-side; hand-editing that stored value just gets you a page full of buttons that fail.

The public landing page is the **Live Event Feed** (`/`) — recent events showing kind, timestamp, author, content, and result. Nav is consistent across pages: home, stats, users, bans, audit, settings. Search (Ctrl-K / Cmd-K) accepts npub, hex, or domain.

Every admin action leaves a record line in the browser and is also written to the server **Audit** log (`/audit`) — the durable undo list. Browser record lines still offer Undo/↩ on each admin page; Audit survives a new browser and is bounded by retention and auth.

Names are resolved first from the local relay database. For a banned user whose events (including their kind 0) have already been purged, the ban list page automatically queries a fallback chain of public relays (`wss://purplepag.es`, `wss://indexer.coracle.social`, `wss://user.kindpag.es`, `wss://relay.nos.social`, `wss://relay.ditto.pub`, `wss://relay.noswhere.com`, tried in that order until one connects) from your logged-in browser — the server itself never opens an outbound connection. Each pubkey is queried externally at most once, ever: both hits and misses are verified and persisted in `blacklist.json` (never trusted client-side extraction — the server independently checks the event's signature before storing anything), so the result is shared across every browser you log in from and never re-fetched. If you want a pubkey re-checked, hand-delete its `name_checked_at` field in `blacklist.json`.

## Manual bans

Logged in as admin, the ban list page also shows a ban form: paste one or more npubs or hex pubkeys (space or comma separated), an optional reason, and click "Ban". This is the same trust root as reporting — authorized per-request with NIP-98, no sessions.

## Moderating from your phone (NIP-86)

Eighty-Six answers [NIP-86](https://github.com/nostr-protocol/nips/blob/master/86.md), the
relay-management RPC, so a client like Amethyst can ban and unban on this relay without a
browser. Four methods are implemented: `supportedmethods`, `banpubkey`, `unbanpubkey` and
`listbannedpubkeys`. A ban made this way lands in the same `blacklist.json`, with the same
shape, as one made from the ban page or by an admin kind-1984 report — there is no second
list — and shows up on `/audit` marked `(nip86)`.

**Amethyst has no field for the endpoint.** It derives it from the relay's own socket URL
by swapping the scheme: `wss://relay.example.com/` becomes `https://relay.example.com/`,
same host, port and path. So a phone can only reach this through whatever already fronts
your relay on 443 — your reverse proxy — never through `:8686` directly.

Set `canonical_urls` in `config.json` (Settings → Eighty-Six) to the URLs a signed auth
event may name. It is asked once by the updater and never guessed: nothing here can know
your public URL, and a plausible wrong value makes every request fail with what looks like
a signing bug on the phone. **Leave it empty and the endpoint is off** — there is no
separate switch. Hold both spellings:

```json
"canonical_urls": ["https://relay.example.com/", "http://127.0.0.1:8686/"]
```

The public one is what a phone signs for; the local one is what `nak` and any development
testing use. Scheme and a trailing slash don't have to match; host, path and any
non-default port do.

Route it in your proxy on the **Content-Type**, not the path — the request arrives at the
relay's own URL, so the media type is the only thing that distinguishes it from an ordinary
websocket upgrade or a page load. Match it as a prefix so a client or proxy that appends
`; charset=utf-8` still lands here:

```
@nip86 {
    method POST
    header Content-Type application/nostr+json+rpc*
}
handle @nip86 {
    reverse_proxy 127.0.0.1:8686
}
```

Every **rejected** auth attempt is appended to `nip86-auth.jsonl` (rotated to `.1`, two
segments kept) naming which check failed and the one value that fixes it — the configured
and received `u` tags side by side, the expected and received body hashes, the measured
clock skew. That file is the answer to "why won't Amethyst connect"; the error the phone
shows is deliberately terse, so that an unauthenticated caller can't read your
configuration back out of it. A valid signature from a pubkey that isn't the admin returns
403 rather than 401, which is what makes Amethyst show the real reason instead of a generic
"unauthorized".

Clock skew is tolerated ±120s by default (`nip86_auth_max_skew`, Settings). Repeated
*failed* authorizations are rate-limited process-wide; a request that succeeds costs
nothing, so ordinary use never trips it.

## Bulk-editing ban reasons

Reports from jumble (and most clients) never carry a reason — jumble sends the report *type* (spam, nudity, malware, etc.) but an empty content field, so a kind-1984-triggered ban's `reason` is blank by default. To build your own taxonomy: filter the ban list to the pubkeys you want, "Select visible", type a reason in the row below the selected count, and click "Replace reason" (overwrites) or "Append to reason" (joins onto whatever's there with " — "). **The reason is public** — it renders on the ban list for logged-out visitors, same as everything else there — and the control says so next to the input. Each bulk edit leaves one record line ("set reason on N bans") with Undo, same as a ban or unban; above 50 pubkeys in one edit, the snapshot needed for Undo isn't kept and the line says "undo unavailable" instead of offering an undo that could only restore some of them.

## Looking up one pubkey

Every page's header search box (enter an npub, hex pubkey, or domain) opens `/profile?hex=<hex>` or `/domain?d=` — for a profile, everything server86 knows about that one account, on one page: ban status with a Ban/Unban button, its kind-0 profile fields, lifetime event count plus a breakdown of its most recent 500 events by kind, reports filed against it, and its 20 most recent events (truncated) so you can tell spam from a busy human without leaving for njump. `about`/`website`/`picture`/`lud16` are always shown as plain text, never as a clickable link or a loaded image — an account under investigation shouldn't be able to make your browser fetch a URL of its choosing. Admin-only, same as the author list.

## Finding active authors

The **Users** page (`/users` in the header nav) is admin-only, and holds both the member table and the author scan that fills it. It offers two scan depths: **recent activity** (the newest 20,000 events, all kinds, a few seconds) and **full author list** (every note, reaction, report, and profile — every kind except gift-wrap DMs, which use a fresh single-use key per message and so can't usefully be banned — roughly two minutes on a large relay). Either way, single-event authors are hidden by default (that's almost always gift wraps or one-off deletion requests, not moderation targets); uncheck the box to see them. Check the authors you want, optionally add a reason, and "Ban selected" bans them the same way the manual ban form does.

Both scans run in the background and the page just polls for the result — closing the tab mid-scan loses nothing, and a page reload picks the poll back up rather than starting a fresh scan. **This means your reverse proxy or tunnel needs its read timeout raised to at least 300 seconds; Cloudflare's free tier cannot host this page at all**, since its 100-second request cap isn't configurable — direct binding, a tailnet, or self-managed nginx all work fine. Nothing scans on page load, on login, or on a timer — only the scan button starts one, and the result is saved to disk (`authors-cache.json`) so it survives the updater restarting the admin page on every update.

## Checking a domain's roster

The `/domain` page answers a different question than a kind-0 `nip05` field does: a `nip05` is a claim a pubkey makes about itself, while a domain's `.well-known/nostr.json` is the domain's own claim about who it vouches for. Enter a domain and click "Fetch roster" — this happens directly in your browser, and if the domain's server doesn't send the CORS header NIP-05 requires, there's a paste box as a fallback. Every entry is checked both ways: `claims this domain` means the pubkey's own profile agrees with the roster; `claims <something else>` or `claims nothing` flags a stale roster entry. Neither direction is verified by anything cryptographic — it's two unverified claims compared for you, never proof, and the page never claims otherwise. (The reverse case — someone claiming your domain that the roster doesn't list, a possible impersonator — shows up in the Users page's search instead.) From there it's filter, select, ban, same as everywhere else.

## Gift-wrap storage accounting and the retention purge

Two more bounded, background-scanned endpoints: `/api/recipients` tallies who's on the receiving end of your relay's gift wraps (NIP-17 DMs) — useful for storage/retention decisions, never a moderation signal — and `/api/subscribers` finds every pubkey whose DM relay list or general relay list actually names your relay. Results persist to `recipients-cache.json` and `subscribers-cache.json`, exactly like `authors-cache.json` — safe to delete at any time, rebuilt on the next scan.

`/api/subscribers` needs to know your relay's own address, which strfry doesn't otherwise expose to itself. Set it on the **Stats & Console** page, in the field right above the subscriber scan — it's stored in `config.json`, not `strfry.conf`, and strfry itself never reads it. Without it, `/api/subscribers` returns an empty result and says why, rather than guessing.

Both scans feed the "Gift-wrap retention purge" intent in the terminal-commands block (see below): open it, and if either scan hasn't been run in the last 7 days you'll see a "scan now" button right there before it renders anything destructive. Once both are fresh, it renders a `strfry delete` filter that excludes your subscribers' gift wraps — never their messages, just everyone else's — alongside the blunter blanket form for comparison.

The **Stats & Console** page's whole-database walk (the same scan that audits the allowlist) also reports **expired events** — events whose NIP-40 `expiration` tag is already in the past. strfry stops serving these to clients but keeps them on disk until a delete actually runs, so the figure is a measure of purgeable storage sitting alongside the gift-wrap count. It only appears in the walk, not the fast index-count totals: `expiration` isn't a single-letter tag, so nothing but a full pass over the event bodies can find it — which is why it's a byproduct of a walk you were already running, not a scan of its own.

## No charts or statistics

Eighty-Six deliberately shows no charts or computed statistics — every `strfry scan` it runs is the direct, bounded result of a button press that says what it's about to do. Anything that needs a database sweep (event counts, per-pubkey counts, purges) is offered instead through the "terminal commands" generator on every admin page: pick an intent from the dropdown, fill in whatever it asks for (a pubkey, a domain, a day count), and copy the rendered `strfry` command. Nothing in that block is ever run by the page itself — destructive intents (like deleting a pubkey's events) always render the equivalent `scan --count` line first, so you see how much a command destroys before you copy the command that destroys it.

## strfry.conf location and backups

The updater finds `strfry.conf` the same way the admin UI does: an explicit `strfry_conf_path` in `config.json` if set, otherwise the running relay's `--config`, otherwise the first path that exists among `/etc/strfry.conf`, `/etc/strfry/strfry.conf`, `/config/strfry.conf`, `/app/strfry.conf`, and `/usr/local/etc/strfry.conf`.

A backup is written next to that file as `strfry.conf.bak-<unix-timestamp>` **only when the run actually changes the file** (sets `writePolicy.plugin`). Already-configured and refused-overwrite runs write nothing. The updater keeps the 3 newest backups and the 1 newest applied bundle, pruning older ones after each successful run. Files you've renamed by hand are never touched.

If `writePolicy.plugin` is already set to something else, the updater normally leaves it alone and warns rather than clobbering a deliberate choice — with one exception: `dockurr/strfry`'s own shipped default (`/app/write-policy.py`, a no-op) is recognized and replaced outright, backed up first like any other change. Without that, the documented one-line install against that image would complete and print success while moderation stayed silently off.

## Chaining another write-policy plugin

strfry only has one `writePolicy.plugin` slot, so running Eighty-Six and some other plugin (a spam filter, a rate limiter, anything that speaks strfry's own plugin protocol) used to mean picking one — the updater would refuse to overwrite an existing value and just warn. plugin86.py can now chain to that other plugin instead: when it finds `writePolicy.plugin` already set to something else, the updater offers (interactively) to record it as `next_plugin` in `config.json`. Every event Eighty-Six itself would *accept* is then forwarded to that downstream process and its verdict relayed verbatim; an event Eighty-Six *rejects* (a banned pubkey, for instance) short-circuits and never reaches it. If the downstream hangs, dies, or replies with something malformed, plugin86.py falls back to its own verdict, logs it once, and stops forwarding for the rest of that run — it never retries and never blocks the relay's write path waiting on it. The decision log (Stats & Console) records which stage actually made each call. `next_plugin` and its per-event timeout (`next_plugin_timeout`, default 2s) are both editable on the Settings page; both take effect the next time plugin86.py restarts, not immediately on save.

## Sidecar (work in progress, log-only)

`sidecar86.py` is the start of the relay-agnostic version. It connects to any relay as
an ordinary client, reads kind 1984 reports and the admin's moderator list, and for each
report from the admin or a moderator keeps a copy of the reported content (the event,
the author's profile and recent posts) and logs the ban it would make. Reports from
anyone else are logged as requests. It also keeps every Nth ordinary note as a sample
of what normally gets through. It does not ban anything yet: the only mode is
`log-only`, and it holds no key.

```
cp sidecar.example.json sidecar.json   # set relay_url and admin_pubkey
python3 sidecar86.py
```

Output goes to `sidecar-data/`: `sidecar-decisions.jsonl` (every report and what it
would do), `kept-spam.jsonl`, `kept-good.jsonl`, and `sidecar-state.json` (the
checkpoint). The moderator list is a kind 30000 list signed by the admin with
`d` = `eighty-six-moderators`; its `p` tags are the moderators.

A report bans every pubkey and every post it names, because on many relays banning a
pubkey leaves that person's stored posts up. Nobody can ban the admin, and a moderator
cannot ban another moderator. To overrule a moderator, the admin puts the pubkey on a
second kind 30000 list with `d` = `eighty-six-pardons`: after that, moderator reports
against it ban nothing, though the admin's own reports still do.

`tools/spam_rules.py replay corpus.json` scores the draft spam rules against labeled
posts; see `docs/spam-replay.md` for the first run.

## Trust model

The NIP-86 endpoint above is the only part of Eighty-Six meant to be reachable from the
open internet, and the only thing guarding it is a signature check: `lib86/bip340.py`
verifying that a kind-27235 event was signed by `admin_pubkey_hex`. There are no sessions,
tokens or second factors anywhere in this project. That file is 116 lines of vendored
reference code, exercised against every BIP-340 test vector — valid and invalid — on each
`./test.sh` run, one named check per vector.

The updater executes code fetched from this repo's `main` branch on every run — don't point it at someone else's fork unless you trust it as much as you'd trust running their code directly.
