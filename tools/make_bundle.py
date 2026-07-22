#!/usr/bin/env python3
"""Regenerates manifest.json and strfry86-bundle.tar.gz.

Run before every release commit. Deployable files are what the updater
installs into /config/strfry86/ — not README.md, test.sh, tools/, or these
two outputs themselves. manifest.json IS bundled alongside the deployables
(so offline installs get a local copy at /config/strfry86/manifest.json),
but it is never a key inside its own dict — computing its own hash before
its content is final is circular, so the updater writes it out directly
instead of sha-verifying it like the rest.

Both outputs are built deterministically (sorted keys, fixed member order,
zeroed timestamps) so re-running this with no source changes reproduces the
exact same bytes.
"""

import gzip
import hashlib
import io
import json
import os
import tarfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "manifest.json")
BUNDLE_PATH = os.path.join(REPO_ROOT, "strfry86-bundle.tar.gz")

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


def write_manifest():
    manifest = {}
    for rel_path in DEPLOYABLE_FILES:
        abs_path = os.path.join(REPO_ROOT, rel_path)
        manifest[rel_path] = sha256_file(abs_path)

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    return manifest


def write_bundle():
    """manifest.json is written to disk first by write_manifest(), so bundling
    it here just packs the final file — no circularity."""
    members = sorted(DEPLOYABLE_FILES) + ["manifest.json"]

    raw_fileobj = open(BUNDLE_PATH, "wb")
    try:
        # filename="" suppresses the FNAME header field, and mtime=0 zeroes
        # the gzip timestamp — both would otherwise make identical content
        # produce different bytes on every run.
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw_fileobj, mtime=0)
        try:
            with tarfile.open(fileobj=gz, mode="w|", format=tarfile.GNU_FORMAT) as tar:
                for rel_path in members:
                    abs_path = os.path.join(REPO_ROOT, rel_path)
                    with open(abs_path, "rb") as f:
                        data = f.read()
                    info = tarfile.TarInfo(name=rel_path)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
        finally:
            gz.close()
    finally:
        raw_fileobj.close()


def main():
    manifest = write_manifest()
    write_bundle()
    print(f"wrote {MANIFEST_PATH} with {len(manifest)} entries")
    print(f"wrote {BUNDLE_PATH} with {len(manifest) + 1} members")


if __name__ == "__main__":
    main()
