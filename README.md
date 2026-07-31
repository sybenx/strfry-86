# strfry-86

A pure-Python moderation sidecar for a [strfry](https://github.com/hoytech/strfry) Nostr relay. Deployed *inside* your existing strfry Docker container — zero image changes, zero pip installs, zero new compose services. ("86" is restaurant slang for banning someone from the establishment — fitting for a relay literally named stir fry.)

Report a user from any normal Nostr client (e.g. [jumble.social](https://jumble.social)) and strfry-86 blacklists them at the relay's write policy. Unban them again from a bare admin web page, logged in with your NIP-07 browser extension.

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

strfry-86 is offline-first: if `strfry86-bundle.tar.gz` sits next to the updater script, the updater installs from it directly and never touches the network. Get both files onto the host by any means (the `curl` lines below are just one option — dragging the files over or `scp` work identically), then `docker cp` them in and run the updater exactly as before.

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

## Add the relay as a write relay

For reports to actually reach strfry-86, add this relay to your write relays in jumble.social (or whatever client you report from) — otherwise your NIP-56 reports go somewhere else and nothing gets banned.

## How bans work

Bans are forward-looking only: banning a pubkey stops it from writing *new* events from that point on, it does not retroactively remove what's already stored. To purge a banned author's existing events, run inside the container:

```
strfry delete --filter '{"authors":["<hex-pubkey>"]}'
```

## Unbanning

Open the ban list page (`/bans`), click "Login with extension" (NIP-07), check the pubkeys you want to unban (a live "n selected" count sits next to the button), click "Unban selected". Each unban is authorized per-request with a freshly signed NIP-98 event — there are no sessions or cookies. Logging in is remembered in this browser (localStorage) so you aren't re-prompted by your extension moving between pages — that changes nothing about the security model, every write is still individually NIP-98 signed and checked server-side; hand-editing that stored value just gets you a page full of buttons that fail.

The public landing page is **Activity** (`/home`) — a live feed of recent events showing only kind, timestamp, and event id (no pubkeys or content). Nav is consistent across pages: home, stats, userlist, report, authors, bans, audit. Search (Ctrl-K / Cmd-K) accepts npub, hex, or domain.

Every admin action leaves a record line in the browser and is also written to the server **Audit** log (`/audit`) — the durable undo list. Browser record lines still offer Undo/↩ on each admin page; Audit survives a new browser and is bounded by retention and auth.

Names are resolved first from the local relay database. For a banned user whose events (including their kind 0) have already been purged, the ban list page automatically queries a fallback chain of public relays (`wss://purplepag.es`, `wss://indexer.coracle.social`, `wss://user.kindpag.es`, `wss://relay.nos.social`, `wss://relay.ditto.pub`, `wss://relay.noswhere.com`, tried in that order until one connects) from your logged-in browser — the server itself never opens an outbound connection. Each pubkey is queried externally at most once, ever: both hits and misses are verified and persisted in `blacklist.json` (never trusted client-side extraction — the server independently checks the event's signature before storing anything), so the result is shared across every browser you log in from and never re-fetched. If you want a pubkey re-checked, hand-delete its `name_checked_at` field in `blacklist.json`.

## Manual bans

Logged in as admin, the ban list page also shows a ban form: paste one or more npubs or hex pubkeys (space or comma separated), an optional reason, and click "Ban". This is the same trust root as reporting — authorized per-request with NIP-98, no sessions.

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

strfry-86 deliberately shows no charts or computed statistics — every `strfry scan` it runs is the direct, bounded result of a button press that says what it's about to do. Anything that needs a database sweep (event counts, per-pubkey counts, purges) is offered instead through the "terminal commands" generator on every admin page: pick an intent from the dropdown, fill in whatever it asks for (a pubkey, a domain, a day count), and copy the rendered `strfry` command. Nothing in that block is ever run by the page itself — destructive intents (like deleting a pubkey's events) always render the equivalent `scan --count` line first, so you see how much a command destroys before you copy the command that destroys it.

## strfry.conf backups

An updater run backs `/config/strfry.conf` up next to the original as `strfry.conf.bak-<unix-timestamp>` — but only when the run actually changes the file; the steady-state run that finds everything already configured writes no backup. The updater keeps the 3 newest backups and the 1 newest applied bundle, pruning older ones after each successful run. Files you've renamed by hand are never touched.

## Trust model

The updater executes code fetched from this repo's `main` branch on every run — don't point it at someone else's fork unless you trust it as much as you'd trust running their code directly.
