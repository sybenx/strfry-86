"""The test relay's one fixed admin keypair.

Deterministic and throwaway: derived from a fixed label, not random, so
every seeder run signs byte-identical events and the admin pubkey is the
same across `up`/`seed` cycles without persisting a key file anywhere. Has
no relation to any real relay's admin key — never use this key for
anything but this local disposable relay.
"""

import hashlib

from bip340_sign import n, pubkey_hex

_SEED_LABEL = b"strfry-86 test-relay admin key -- throwaway, not real"
PRIVKEY_INT = int(hashlib.sha256(_SEED_LABEL).hexdigest(), 16) % n
if PRIVKEY_INT == 0:
    PRIVKEY_INT = 1

PUBKEY_HEX = pubkey_hex(PRIVKEY_INT)

if __name__ == "__main__":
    print(PUBKEY_HEX)
