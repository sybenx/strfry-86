# Relay support

Probe results for the relay-agnostic rework (step 1). Checked 2026-11-24.

Method: live relays were probed read-only: the NIP-11 document, an
unauthenticated NIP-86 `supportedmethods` POST, and a `REQ` for kind 1984.
Nothing was published, banned or deleted on any live relay. Ban semantics and
authorization come from reading each relay's source at the version noted.

Terms used below:

- **Ban blocks / deletes**: whether `banpubkey` only refuses new writes, or
  also removes the pubkey's stored events.
- **Second key**: whether a key other than the owner's can be allowed to call
  the ban methods. The sidecar needs this, because it must not hold the
  admin's nsec.

## Relays Aaron runs

The question per relay is not "who may publish" but "can strangers' content
end up stored here and served": through direct writes, through a mentions or
inbox tier, or through the relay fetching and importing it. Every public relay
below except Pyramid's main write path takes in strangers' content one of
those ways.

"Advertises 86" means NIP-11 lists it. Several relays advertise NIP-86 without
wiring a single method (khatru's default list), so the column below says what
the source actually implements.

| Relay | Software | How strangers' content gets in | Served to | Ban tools that exist | NIP-86 (real) | Fit |
|---|---|---|---|---|---|---|
| `wss://syb.lol` | Ditto Relay 2.5.1 | Anyone may write any kind. Reports: 300 recent from 62 authors. | Everyone | Write-policy plugin (strfry protocol) blocks new writes. `ditto-relay delete --filter` on the host removes stored events. | No (POST returns 405; nothing planned in 2.5.2 or MRs) | **Needs Eighty-Six.** `plugin86.py` can sit behind its plugin hook. |
| `wss://relay.vantis.ninja` | Chronicle 0.7.0+ca20f18 (sybenx fork) | 1. Direct: replies, reactions, zaps in threads you're in, from your web of trust (follows + anyone 3 of them follow). 2. **Fetchers write straight to the store, skipping the accept check:** whole threads you touch, re-polled daily/weekly/monthly, keeping WoT authors and anyone a fetched event p-tags. 3. **Quoted events (q tags) are stored with no trust check at all**, with their reactions, zaps and media. 4. Gift wraps tagging you (`DM_ACCEPT_ALL`), readable by you only. | Everyone (except gift wraps) | None. No ban list, no plugin. Removal = kind 5 or a code change. | No. Advertises 86, wires no methods. | **Needs a filter, in code.** Bans must also gate the fetchers, or they re-ingest. |
| `wss://bothy-relay.sybenx.workers.dev` | bothy v0.31.1 | Today (`follows`): only you, members and follows. With mention support (`mentions` tier): anyone who mentions or replies to you. Backfill pulls your own events only. | Everyone | `banpubkey` (refused under every tier), `banevent` (deletes + tombstones). | Yes, full set incl. `unbanpubkey`. Owner and `addadmin` admins, so a separate sidecar key works. | **Needs Eighty-Six once `mentions` ships.** Native NIP-86 covers everything; no adapter. |
| `wss://haven.aaro.cc` | HAVEN c8b61d2 (**upstream archived 2026-11-17**) | 1. Inbox: any kind except 4 that p-tags you, from your WoT. `WOT_DEPTH` and `WOT_MINIMUM_FOLLOWERS` are unset, so depth 3 with **no follower floor** (the `CHAT_RELAY_WOT_*` settings are ignored by this version). 2. Importer: a live subscription on 22 public relays pulls every event tagging you into the inbox, same WoT rule. 3. Chat: group/channel messages and gift wraps; gift wraps from the importer skip the WoT check. | Inbox: everyone. Chat: any WoT member who authenticates. Outbox, private, Blossom: yours only. | Static `BLACKLISTED_NPUBS_FILE`, read at startup; blocks inbox, chat and import, deletes nothing. | No. Advertises 86, wires no methods. | **Treated as retired** (2026-11-25) until decided otherwise; out of scope for Eighty Six. |
| `wss://herbstmeister.com` (= `pyramid.aaro.cc`) | Pyramid v1.4.5 | Notes: members only. **Reports: non-members may post kind 1984** if the reported pubkey is a member or the event is stored there. | Everyone | `banpubkey` drops a member from the invite tree (events stay), `banevent` deletes. | Yes, but no `unbanpubkey`; a separate key must be a root member. | Membership covers notes. Only exposure is stranger reports about members, which is what Eighty-Six's request queue would read anyway. |
| nostr-rs-relay (Umbrel app, xumbrel `:4848`; also installed on ambrel) | nostr-rs-relay 0.10.0 | **Open writes** (`restricted_writes: false`; config whitelist broken, runs on defaults). | Not public: `personal.aaro.cc` sits behind a login. | Config whitelist only (currently broken). | No | Not public today. If it is ever exposed, it is fully open. Retirement was already recommended (2026-10-20 audit). |
| strfry + castle-steward (ambrel Dockge stack `strfry`) | strfry, `plugin86.py` | Whatever the plugin allows. | — | Plugin blocks; `strfry delete` removes. | No | **Stopped** (nothing answers on 7777/8686/8787). `castle.syb.lol` is behind a login; `86.syb.lol` is a dead route. |

Not relays, ruled out: the parked Pyramid stack on ambrel (image gone,
not running), `om.aarobh.net` (an Open-Meteo HTTP proxy), the Plex Relay and
bun processes on Took (no Nostr relay found there). racknerd runs no relay.

## Other relay software (bench list)

From source only (relays listed in `0ceanSlim/nostr-relay-bench`). "All five"
means `supportedmethods`, `banpubkey`, `allowpubkey`, `listbannedpubkeys`,
`banevent`.

| Relay | NIP-86 | Who may call / second key | `banpubkey` deletes stored? | Kind 1984 handling |
|---|---|---|---|---|
| zooid (khatru) | all five + roles | owner, relay key, plus keys granted methods via `assignmethod` | yes | none |
| vespa-relay | all five (quartz) | `RELAY_ADMIN_PUBKEYS` list | yes | none |
| hubstr | no `banevent` | any tenant (`allowpubkey` adds one, with full management) | yes | none |
| nosfry | all five | `admins` list in config | no, hidden at read time | none |
| newlay | all five + ~50 extensions | tiers with `can_admin`, grantable via `assigntier` | no (`banevent` purges) | reads limited to moderators when `[buzz]` is on |
| nostrfy | all five + extensions | `admin_pubkey` or token; others via `assignmethod` | not traced | none |
| wisp | all five | `admin_pubkeys` list | no | none |
| haven (khatru), upstream HEAD | all five (the deployed c8b61d2 above wires none) | owner only | no (`banevent` deletes) | none |
| grain | no `banevent` | owner only | no | none |
| orly | all but `unbanpubkey`, only in managed ACL mode | `ORLY_OWNERS` list | no | Neo4j backend only |
| nostream | none (own admin web UI) | own login | unknown | reports from `trustedModerators` can hide targets |
| nostr-rs-relay, rnostr, swarm, wot-relay, magnitude, gc_index_relay, mattn-nostr-relay | none | n/a | n/a | none (mattn: optional push alert) |

## What this means for the plan

- **Ban semantics are not uniform.** On about half the NIP-86 relays,
  `banpubkey` leaves stored events in place. The sidecar should treat
  `banpubkey` as "block future writes" and use `banevent` for the reported
  event when one is tagged. It cannot promise removal of a pubkey's history
  through NIP-86 alone.
- **There is no `unbanpubkey` on Pyramid or orly**, so undo is not
  available everywhere through NIP-86.
- **Authorizing a second key is relay-specific**: an admin list in config
  (vespa, nosfry, wisp, orly), a runtime grant (zooid, nostrfy, newlay,
  bothy's `addadmin`), or a role in a tree (Pyramid). The sidecar's key has to
  be authorized by hand on each relay; there is no standard call for it.
- **Of the relays that take strangers' content (syb.lol, Chronicle, bothy,
  plus HAVEN, now treated as retired), only bothy has working NIP-86.**
  syb.lol has a strfry-style plugin hook, so the strfry adapter covers it.
  Chronicle and HAVEN have no hook at all; a filter there is a code change
  (Chronicle is already a fork; HAVEN would need one). The NIP-86 sidecar and
  the static UI only reach the others if an adapter also exposes a NIP-86
  endpoint, as `server86.py` does now.
- **Fetching relays need bans applied at ingest, not just at publish.**
  Chronicle's fetchers and HAVEN's importer bring content in without going
  through the write path, so a ban that only gates publishes does not hold
  there.
