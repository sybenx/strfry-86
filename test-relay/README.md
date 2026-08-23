# test-relay/

Disposable local strfry + Eighty-Six, for anything that measures or deletes.
Never the production relay.

```
bin/up.sh      # docker compose up + real strfry-86-updater.py install (offline bundle mode)
bin/seed.sh    # deterministic corpus: 62k authors, ~65% kind 1059, gap-tail kinds, admin-signed bans
bin/measure.py # /api/authors + /api/userlist, raw vs gzip, vs the production baseline
bin/down.sh    # destroy container + volume
```

Admin UI: `http://127.0.0.1:18686` · relay: `ws://127.0.0.1:17777`. Admin key is
fixed/deterministic (`seed/admin_key.py`) — throwaway, not a real key.

Seeded events are unsigned (`strfry import --no-verify` — confirmed to accept
this; no signing path needed for the bulk corpus). The handful of admin
kind-1984 reports are genuinely signed and published over a real websocket
connection, because that's the only path that reaches plugin86's write
policy and proves the auto-ban actually fires — `import` bypasses it
entirely. See `seed/seed.py`'s docstring for the full shape rationale.

`up.sh` builds the install bundle from a throwaway copy of the repo tree
(uncommitted changes included), never touching the real `manifest.json` /
`strfry86-bundle.tar.gz`. `strfry.conf` here is dockurr/strfry's own default
template with `writePolicy.plugin` blanked — that image ships its own
default plugin pre-wired, and the real updater refuses to overwrite an
already-set plugin line, so installing against its untouched default
silently never wires up plugin86 (confirmed by testing it).
