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

Only the `docker cp` and `docker exec` steps matter — get the file(s) onto the host however is convenient. Applied bundles are renamed to `.applied-<timestamp>` inside `/config/strfry86/`; only the most recent one is kept, older ones are pruned automatically after each successful run (see [Retention](#retention) below).

## First run

On first run the updater needs your admin pubkey — the one and only key allowed to ban (via a NIP-56 report, kind `1984`, or manually from the admin page) or unban (via NIP-98). It tries to read `relay.info.pubkey` from your `strfry.conf` first and, if found, asks you to confirm before using it; otherwise it prompts you to paste an `npub` or 64-char hex pubkey. It is never adopted silently. The result is stored as a public key only in `/config/strfry86/config.json` — your `nsec` never leaves your extension, and never touches this server.

Right after the admin pubkey, it also asks once for an optional `contact_appeal` — free text (email, npub, URL, whatever) shown publicly on the admin page, to everyone including logged-out visitors, so a banned user knows where to appeal. Blank is a valid answer. Right after that it asks once for an optional `relay_url` — the relay's public websocket URL (e.g. `wss://relay.example.com`), used to sharpen the audit's "ghost" detection (see below); blank skips that refinement, everything else about the audit still works. If either key is ever missing entirely from `config.json` (e.g. upgrading from an older install), a later update run asks for the missing one(s) once and adds them in — never re-asked once present, even if blank. You can edit or blank either by hand in `config.json` at any time; the admin page picks up the change on its next load, no restart needed.

## The audit

After logging in, the admin page shows an **audit**: plain-language notices about suspicious patterns in stored events, the flagship case being swarms of pubkeys that plant this relay in their kind-10002 relay lists without ever posting anything else. Each notice expands to a pre-ticked list of pubkeys you can ban in one click. Detection is purely local counting over what's already in the strfry database — no network calls, no AI, nothing that needs tuning. `relay_url` in `config.json` sharpens the flagship "ghost" notice but is optional. `GET /api/audit` is a stable, public JSON endpoint — external tooling (scripts, AI review, anything) can consume it and propose bans through the existing `POST /api/ban`.

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

Open the admin page, click "Login with extension" (NIP-07), check the pubkeys you want to unban, click "Unban selected". Each unban is authorized per-request with a freshly signed NIP-98 event — there are no sessions or cookies.

## Manual bans

Logged in as admin, the admin page also shows a ban form: paste one or more npubs or hex pubkeys (space or comma separated), an optional reason, and click "Ban". This is the same trust root as reporting — authorized per-request with NIP-98, no sessions.

## strfry.conf backups

A backup is only written when the updater is actually about to change `/config/strfry.conf` — a run that finds the plugin line already correct (the steady state, i.e. almost every update) touches nothing and leaves no backup. When a change is about to be made, the original is copied first, next to itself, as `strfry.conf.bak-<unix-timestamp>`.

## Retention

The updater keeps the **3 newest** `strfry.conf.bak-*` files and the **1 newest** applied bundle (`strfry86-bundle.tar.gz.applied-*`), pruning older ones after each successful run — the exact counts pruned are printed in the run summary. Pruning is skipped entirely on a run that hit a foreign `writePolicy.plugin` (needs manual resolution) or a self-update hash mismatch, so the fallback files always survive while anything is in question. Files you renamed by hand are never touched — only exact `strfry.conf.bak-<digits>` / `strfry86-bundle.tar.gz.applied-<digits>` matches are ever candidates for deletion.

## Trust model

The updater executes code fetched from this repo's `main` branch on every run — don't point it at someone else's fork unless you trust it as much as you'd trust running their code directly.
