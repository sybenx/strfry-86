# Copyright (c) 2020 Pieter Wuille
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""Reference implementation of BIP-340 (Schnorr signatures for secp256k1).

Adapted from the BIP-340 reference Python implementation for strfry-86.
Verification path only: signing is stripped since the plugin/server only
ever need to check signatures produced by NIP-07 browser extensions, and
this must run on stdlib-only pure-Python with no third-party big-int
acceleration.
"""

p = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_FFFFFC2F
n = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_BAAEDCE6_AF48A03B_BFD25E8C_D0364141

G = (
    0x79BE667E_F9DCBBAC_55A06295_CE870B07_029BFCDB_2DCE28D9_59F2815B_16F81798,
    0x483ADA77_26A3C465_5DA4FBFC_0E1108A8_FD17B448_A6855419_9C47D08F_FB10D4B8,
)


def x(P):
    return P[0]


def y(P):
    return P[1]


def point_add(P1, P2):
    if P1 is None:
        return P2
    if P2 is None:
        return P1
    if x(P1) == x(P2) and y(P1) != y(P2):
        return None
    if P1 == P2:
        lam = (3 * x(P1) * x(P1) * pow(2 * y(P1), p - 2, p)) % p
    else:
        lam = ((y(P2) - y(P1)) * pow(x(P2) - x(P1), p - 2, p)) % p
    x3 = (lam * lam - x(P1) - x(P2)) % p
    return (x3, (lam * (x(P1) - x3) - y(P1)) % p)


def point_mul(P, n_):
    R = None
    for i in range(256):
        if (n_ >> i) & 1:
            R = point_add(R, P)
        P = point_add(P, P)
    return R


def bytes_from_int(x_):
    return x_.to_bytes(32, byteorder="big")


def lift_x(b):
    x_ = int.from_bytes(b, byteorder="big")
    if x_ >= p:
        return None
    y_sq = (pow(x_, 3, p) + 7) % p
    y_ = pow(y_sq, (p + 1) // 4, p)
    if pow(y_, 2, p) != y_sq:
        return None
    return (x_, y_ if y_ & 1 == 0 else p - y_)


def tagged_hash(tag, msg):
    import hashlib
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def has_even_y(P):
    return y(P) % 2 == 0


def schnorr_verify(msg, pubkey, sig):
    """Verify a BIP-340 schnorr signature.

    msg: 32-byte message (for Nostr, the sha256 event id).
    pubkey: 32-byte x-only public key.
    sig: 64-byte signature.
    Returns True/False; never raises on malformed input.
    """
    if len(msg) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    P = lift_x(pubkey)
    r = int.from_bytes(sig[0:32], byteorder="big")
    s = int.from_bytes(sig[32:64], byteorder="big")
    if P is None or r >= p or s >= n:
        return False
    e = int.from_bytes(
        tagged_hash("BIP0340/challenge", sig[0:32] + pubkey + msg), byteorder="big"
    ) % n
    R = point_add(point_mul(G, s), point_mul(P, n - e))
    if R is None or not has_even_y(R) or x(R) != r:
        return False
    return True
