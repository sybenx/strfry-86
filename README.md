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

Open the ban list page (`/`), click "Login with extension" (NIP-07), check the pubkeys you want to unban (a live "n selected" count sits next to the button), click "Unban selected". Each unban is authorized per-request with a freshly signed NIP-98 event — there are no sessions or cookies. Logging in is remembered in this browser (localStorage) so you aren't re-prompted by your extension moving between the ban list and the active-authors page — that changes nothing about the security model, every write is still individually NIP-98 signed and checked server-side; hand-editing that stored value just gets you a page full of buttons that fail.

Every admin action leaves a record line — manual bans, unbans, and bans triggered by your kind-1984 reports — each with an "Undo" button and a "↩" dismiss button. Records persist in your browser (localStorage) across reloads until you dismiss them or undo the action; they are display-only and nothing about them is stored on the server. At most the 20 newest are ever shown.

Names are resolved first from the local relay database. For a banned user whose events (including their kind 0) have already been purged, the ban list page automatically queries `wss://purplepag.es` (falling back to `wss://relay.damus.io`) from your logged-in browser — the server itself never opens an outbound connection. Each pubkey is queried externally at most once, ever: both hits and misses are verified and persisted in `blacklist.json` (never trusted client-side extraction — the server independently checks the event's signature before storing anything), so the result is shared across every browser you log in from and never re-fetched. If you want a pubkey re-checked, hand-delete its `name_checked_at` field in `blacklist.json`.

## Manual bans

Logged in as admin, the ban list page also shows a ban form: paste one or more npubs or hex pubkeys (space or comma separated), an optional reason, and click "Ban". This is the same trust root as reporting — authorized per-request with NIP-98, no sessions.

## Finding active authors

The `/authors` page (linked from the ban list) is admin-only. It offers two scan depths: **recent activity** (the newest 20,000 events, all kinds, a few seconds) and **full author list** (every note, reaction, report, and profile — every kind except gift-wrap DMs, which use a fresh single-use key per message and so can't usefully be banned — roughly two minutes on a large relay). Either way, single-event authors are hidden by default (that's almost always gift wraps or one-off deletion requests, not moderation targets); uncheck the box to see them. Check the authors you want, optionally add a reason, and "Ban selected" bans them the same way the manual ban form does.

Both scans run in the background and the page just polls for the result — closing the tab mid-scan loses nothing, and a page reload picks the poll back up rather than starting a fresh scan. **This means your reverse proxy or tunnel needs its read timeout raised to at least 300 seconds; Cloudflare's free tier cannot host this page at all**, since its 100-second request cap isn't configurable — direct binding, a tailnet, or self-managed nginx all work fine. Nothing scans on page load, on login, or on a timer — only the scan button starts one, and the result is saved to disk (`authors-cache.json`) so it survives the updater restarting the admin page on every update.

## No charts or statistics

## No charts or statistics

strfry-86 deliberately shows no charts or computed statistics — every `strfry scan` it runs is the direct, bounded result of a button press that says what it's about to do. Anything that needs a database sweep (event counts, per-pubkey counts, purges) is offered instead as copyable `strfry` commands in a "terminal commands" block on both pages, for you to read and run yourself.

## strfry.conf backups

An updater run backs `/config/strfry.conf` up next to the original as `strfry.conf.bak-<unix-timestamp>` — but only when the run actually changes the file; the steady-state run that finds everything already configured writes no backup. The updater keeps the 3 newest backups and the 1 newest applied bundle, pruning older ones after each successful run. Files you've renamed by hand are never touched.

## Trust model

The updater executes code fetched from this repo's `main` branch on every run — don't point it at someone else's fork unless you trust it as much as you'd trust running their code directly.
