#!/usr/bin/env python3
"""Re-runs chat 04's measurement against the test relay: /api/authors and
/api/userlist, raw and gzipped. urllib sends no Accept-Encoding by default,
so the raw measurement has to open its own connection without the header
and the gzip measurement has to send it explicitly — otherwise the "raw"
number is meaningless (you'd just be re-measuring the gzip path twice) and
gzip looks like it did nothing when it isn't even engaging.

Production baseline for comparison (11,858,247 / 13,506,329 bytes) came
from a naive urllib measurement with no Accept-Encoding header at all — the
uncompressed number is the one that lines up with it.
"""

import sys
import urllib.request

BASE = "http://127.0.0.1:18686"
ENDPOINTS = ["/api/authors", "/api/userlist"]
BASELINE_RAW = {"/api/authors": 11_858_247, "/api/userlist": 13_506_329}


def fetch(path, gzip_accept):
    headers = {"Accept-Encoding": "gzip"} if gzip_accept else {}
    req = urllib.request.Request(BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        encoding = resp.headers.get("Content-Encoding")
    return len(body), encoding


def main():
    print(f"{'endpoint':<18}{'raw bytes':>14}{'gzip bytes':>14}{'gzip encoding?':>16}{'prod baseline (raw)':>22}")
    for path in ENDPOINTS:
        raw_len, raw_enc = fetch(path, gzip_accept=False)
        gz_len, gz_enc = fetch(path, gzip_accept=True)
        baseline = BASELINE_RAW.get(path)
        baseline_str = f"{baseline:,}" if baseline else "n/a"
        print(f"{path:<18}{raw_len:>14,}{gz_len:>14,}{str(gz_enc == 'gzip'):>16}{baseline_str:>22}")
        if raw_enc == "gzip":
            print(f"  WARNING: the no-Accept-Encoding request still came back gzip-encoded ({path})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        print("is the test relay up? bin/up.sh && bin/seed.sh first.", file=sys.stderr)
        sys.exit(1)
