# tests/

Not deployable — excluded from `manifest.json` and `strfry86-bundle.tar.gz`
(nothing here is in `tools/make_bundle.py`'s `DEPLOYABLE_FILES`).

## `bip340-vectors.csv`

Vendored verbatim from
`https://raw.githubusercontent.com/bitcoin/bips/master/bip-0340/test-vectors.csv`.
`lib86/bip340.py` verifies only fixed 32-byte messages (a Nostr event id is
always a sha256 hash, so that's all this project will ever need) — the
vectors added in December 2022 for variable-length messages (message sizes
0, 1, 17, 100 bytes) test a mode this project deliberately does not
implement, and `test.sh` skips exactly those rows, by message length, not by
index. Every fixed-32-byte-message row is asserted exactly, including the
invalid ones.

## `nip98-fixture.json`

Four pre-signed kind-27235 (NIP-98) events, all from the same throwaway
keypair, used by `test.sh` to exercise `server86.verify_nip98` end to end —
something the BIP-340 vectors above can't do, since those only cover
`schnorr_verify` in isolation:

- `valid` — `u` path `/api/unban`, `method` `POST`. Expected to be accepted.
- `wrong_kind` — identical except `kind` is `1`.
- `wrong_method` — identical except the `method` tag is `GET`.
- `wrong_path` — identical except the `u` tag points at `/api/ban`.

All four are independently signed rather than being one fixture with fields
mutated after the fact: `id` and `sig` cover every other field, so mutating
`kind`/`method`/`u` post-signing would just be caught by the id-mismatch
check and never actually exercise the kind/method/path-specific checks in
`verify_nip98` — "each mutation must fail on its own," per CLAUDE.md, means
each one needs its own validly-signed event. `sig`- and `id`-byte-flip and
wrong-admin-pubkey cases don't need their own fixture: `test.sh` mutates
`valid` directly for those, since a broken signature/id is exactly what
should be caught regardless of which other field triggered it.

**Provenance**: generated once, offline, by a throwaway script (not part of
this repo) that implements the standard BIP-340 sign algorithm on top of the
point-math already vendored in `lib86/bip340.py` — signing itself is not
vendored anywhere in the shipped code, since the plugin/server only ever
need to verify signatures produced by a NIP-07 extension. The keypair is
random, generated solely for these four fixtures; the private key was never
written to disk and is discarded. It has no relation to any real relay's
admin key.

`created_at` is fixed at `1700000000` on all four, and the signatures cover
it, so `test.sh` can't move a fixture's timestamp forward to test staleness
— it instead calls `verify_nip98(..., now=<fixture created_at> ± 120)` to
inject a fake "current time" against `valid` and isolate the freshness
check without touching any signed payload.

To regenerate: re-implement BIP-340 signing (RFC-style, using
`lib86/bip340.py`'s `tagged_hash`/point-math), pick one fresh random
keypair, and build+sign the four events above (same pubkey, same
`created_at`, each with its one field different) with a fresh `sig` on each.

## `kind0-fixture.json`

Two pre-signed kind-0 (profile) events, same approach and same reasoning
as `nip98-fixture.json` above, used to exercise `server86.verify_kind0_event`
— the second (and last) caller of `lib86/bip340.py`, used to verify the
raw kind-0 events a browser POSTs to `/api/names` after querying a
third-party relay for a banned user's profile:

- `valid` — `kind` 0, `content` a JSON profile with `name`, `display_name`,
  and `nip05`. Expected to be accepted when its pubkey is in both the
  `queried` and `banned_pubkeys` sets passed to `verify_kind0_event`.
- `wrong_kind` — identical except `kind` is `1`, independently signed for
  the same reason `nip98-fixture.json`'s `wrong_kind`/`wrong_method`/
  `wrong_path` are: mutating `kind` on a copy of `valid` without re-signing
  would just be caught by the id-mismatch check and never actually
  exercise the `kind == 0` check.

`sig`-byte-flip, `id`-byte-flip, and tampered-`content` are tested by
mutating `valid` directly — exactly what the id/signature check is meant
to catch. "Pubkey not in `queried`" and "pubkey not in `banned_pubkeys`"
need no fixture at all: they're just different set arguments to
`verify_kind0_event` at test time.

Provenance and regeneration: same as `nip98-fixture.json` — a throwaway,
never-committed signing script on top of `lib86/bip340.py`'s vendored
point-math, one fresh random keypair used only for these two fixtures,
private key discarded after generation.
