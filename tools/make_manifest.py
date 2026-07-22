#!/usr/bin/env python3
"""Regenerates manifest.json: sha256 of every deployable file.

Run before every release commit. Deployable files are what the updater
installs into /config/strfry86/ — not README.md, test.sh, tools/, or
manifest.json itself.
"""

import hashlib
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "manifest.json")

DEPLOYABLE_FILES = [
    "strfry-86-updater.py",
    "plugin86.py",
    "server86.py",
    "admin.html",
    "lib86/__init__.py",
    "lib86/bip340.py",
    "lib86/bech32.py",
    "lib86/blacklist.py",
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    manifest = {}
    for rel_path in DEPLOYABLE_FILES:
        abs_path = os.path.join(REPO_ROOT, rel_path)
        manifest[rel_path] = sha256_file(abs_path)

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"wrote {MANIFEST_PATH} with {len(manifest)} entries")


if __name__ == "__main__":
    main()
