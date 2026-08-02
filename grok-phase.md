# Grok review fix phases

Ordered work from the whole-project code review (2026-08-02). Each phase
is a complete, tested state. Not a deployable file — not in `manifest.json`,
not in the bundle. Historical rollout record lives in `PHASES.md`; this file
tracks the review remediation only.

## Why phased

The review surface spans auth, ban enforcement, DoS, UI panel rules, and
supply chain. Landing everything at once would mix enforcement bugs with
API redesign. Order is **correctness of enforcement first**, then auth
binding, then disclosure, then ops/UI contract drift.

## Phases

- [x] **Phase 1 — pubkey lowercase + banlist write safety.**
  Normalize every pubkey to lowercase at ban/config/auth boundaries so a
  mixed-case ban cannot silently fail to match event pubkeys. Harden
  `lib86/blacklist.py` (and the same write shape in `namecache.py`):
  cross-process `fcntl` lock around read-modify-write, unique temp files
  (no shared `.tmp`), preserve last-good in-memory cache on corrupt JSON
  instead of replacing with `{}`. Tests for case-insensitive ban store and
  locked write path.

- [x] **Phase 2 — NIP-98 origin + payload hash (+ replay store).**
  Bind `u` to scheme+host (configured public origin), not path alone.
  Hash the non-auth request body into the signed event; reject missing/
  mismatch. Remember accepted auth event ids for ≥2× skew.

- [x] **Phase 3 — Authenticate admin-shaped GETs.**
  Gate audit log behind NIP-98 (POST). Document what stays public.

- [ ] **Phase 4 — Console under global job lock.**
  Server refuses or queues console while a scan/purge/compact holds the
  lock; Stats UI disables Run and shows “waiting on …”. Aligns CLAUDE.md.

- [ ] **Phase 5 — Users panel + report soft-memory UX.**
  Users author-scan provenance matches `s86BuildScanPanel` (failed ≠
  result). Poll continues while `blocked_by` is set. Report start surfaces
  soft-memory refusal from the POST body.

- [ ] **Phase 6 — Small correctness fixes.**
  `REASON_MAX_LEN` on `/api/ban`; decision-log line counter on open;
  `s86SignAndPost` never lets `extraBody` clobber `auth`.

- [ ] **Phase 7 — Resource caps if UI is not localhost-only.**
  Max SSE subscribers, POST body read timeout / connection caps.

- [ ] **Phase 8 — Updater authenticity (optional).**
  Tag-pinned URLs and/or detached signature for network install.

## Phase 1 — landed

**What changed**

- `lib86/blacklist.py`: `_as_hex64` / `_normalize_keys` on every read and
  write; exclusive `fcntl` flock on `blacklist.json.lock`; unique
  `.blacklist-*.tmp` temps + fsync before `os.replace`; corrupt JSON keeps
  the last good in-memory map instead of `{}`.
- `lib86/namecache.py`: same lock / unique-temp / preserve-on-corrupt /
  lowercase-key shape as blacklist.
- `server86.py`: `as_hex64()`; `load_config` and settings raw save force
  lowercase `admin_pubkey_hex`; ban/unban/profile/reason/lookup/NIP-98
  compare paths normalize; mixed-case event id accepted for verify after
  lowercasing for BIP-340.
- `plugin86.py`: admin key and ban targets lowercased; `is_valid_hex_pubkey`
  accepts any case (callers store lower).
- `test.sh`: eight assertions for store-as-lower, mixed-case `is_banned`,
  legacy uppercase on disk, corrupt-file preserve, `as_hex64`.
- Bundle regenerated (`tools/make_bundle.py`) so deployable hashes match.

**Not in this phase:** NIP-98 origin/payload (phase 2), public audit auth
(phase 3), console job lock (phase 4).

## Phase 2 — landed

**What changed**

- `verify_nip98` takes `expected_origin`, `payload_sha256`, `consume_id`.
  `u` must match scheme://host (and path); phishing host rejected.
  `payload` tag must equal sha256 of canonical non-auth JSON body.
  Successful live auths mark the event id used for `NIP98_REPLAY_TTL`.
- `do_POST` always supplies origin (config `public_origin` or Host /
  X-Forwarded-Proto) and body hash; fails closed if origin unknown.
- Optional Settings field `public_origin` for reverse-proxy / TLS setups.
- `common86.js` `s86SignAndPost`: canonical sorted JSON + SHA-256 payload
  tag; `auth` always written last so it cannot be clobbered.
- Tests: origin mismatch, port-exact origin, payload hash stability,
  missing payload tag, replay within TTL.

**Ops note:** Behind a TLS-terminating proxy, set `public_origin` in
`config.json` (or Settings) to the browser-visible `https://host[:port]`.

## Phase 3 — landed

**What changed**

- `GET /api/audit` → 401 (no longer public).
- `POST /api/audit` with NIP-98: body `{q, offset}` returns the same
  paged records shape as before.
- `audit.html` loads via `s86SignAndPost`; filter input debounced 300ms
  so each keystroke does not hammer the extension.

**Still public (intentional)**

- Ban list (`GET /api/banned`) — enforcement is public by design; admin
  pubkey is needed for login compare.
- Userlist / authors / recipients / subscribers / report caches — derived
  relay intelligence; CLAUDE “logged out empty” is client-only UI.
- Live feed / metrics / decision-log content — public landing.

Revisit any of the above only if deployment exposes the UI beyond the
operator’s trust boundary and wants a stricter perimeter.
