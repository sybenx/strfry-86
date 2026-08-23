"""BIP-340 schnorr *signing*, test-fixture use only.

lib86/bip340.py is deliberately verify-only (see its docstring) — the
plugin/server side of this project only ever checks signatures a NIP-07
extension already produced. This seeder is the one place in the repo that
needs to actually produce a signature: the admin-authored kind-1984 reports
and NIP-98 auth events have to pass strfry's own signature check to reach
plugin86 over the wire, so a real keypair is unavoidable for those few
events (see test-relay/README.md). Everything else the seeder writes goes
in through `strfry import --no-verify` and needs no signature at all.

Built on lib86.bip340's already-vendored point math (G, point_add,
point_mul, tagged_hash) rather than a new curve implementation. Deterministic
aux_rand (all-zero) so re-running the seeder with the same admin key
produces byte-identical signatures — matches the "deterministic seed" goal
for the rest of the seeder.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib86.bip340 import G, n, p, point_add, point_mul, tagged_hash, x, y, has_even_y, bytes_from_int  # noqa: E402

AUX_RAND = b"\x00" * 32


def pubkey_hex(privkey_int):
    P = point_mul(G, privkey_int)
    return bytes_from_int(x(P)).hex()


def sign(msg32, privkey_int, aux_rand=AUX_RAND):
    """Return a 64-byte BIP-340 signature over msg32 (already 32 bytes)."""
    if len(msg32) != 32:
        raise ValueError("msg32 must be 32 bytes")
    d0 = privkey_int
    if not (1 <= d0 <= n - 1):
        raise ValueError("privkey out of range")
    P = point_mul(G, d0)
    d = d0 if has_even_y(P) else n - d0
    t = bytes(a ^ b for a, b in zip(bytes_from_int(d), tagged_hash("BIP0340/aux", aux_rand)))
    k0 = int.from_bytes(tagged_hash("BIP0340/nonce", t + bytes_from_int(x(P)) + msg32), "big") % n
    if k0 == 0:
        raise ValueError("bad nonce, retry with different aux_rand")
    R = point_mul(G, k0)
    k = k0 if has_even_y(R) else n - k0
    e = int.from_bytes(
        tagged_hash("BIP0340/challenge", bytes_from_int(x(R)) + bytes_from_int(x(P)) + msg32), "big"
    ) % n
    sig = bytes_from_int(x(R)) + bytes_from_int((k + e * d) % n)
    return sig


def sha256(data):
    return hashlib.sha256(data).digest()
