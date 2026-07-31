#!/usr/bin/env python3
"""Copy tools/style86.css into every page's <style> block, verbatim.

CLAUDE.md requires the style block to be duplicated character-for-character in
every page, and calls a page that differs a bug. That rule used to be kept by
hand over five lines of CSS; it is not keepable by hand now. This script is the
enforcement:

    python3 tools/stamp_style.py          # rewrite every page from style86.css
    python3 tools/stamp_style.py --check  # exit 1 if any page has drifted

`--check` is the assertion — it reads every page and fails on the first one
whose block is not byte-identical to the canonical file.
"""

import os
import re
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
CSS_PATH = os.path.join(TOOLS_DIR, "style86.css")

PAGES = [
    "home.html", "stats.html", "users.html", "bans.html",
    "audit.html", "settings.html", "profile.html", "domain.html",
]

BLOCK = re.compile(r"<style>.*?</style>", re.DOTALL)


def main(argv):
    check_only = "--check" in argv
    with open(CSS_PATH, encoding="utf-8") as fh:
        css = fh.read().rstrip("\n")
    block = "<style>\n" + css + "\n</style>"

    drifted, rewritten = [], []
    for name in PAGES:
        path = os.path.join(ROOT, name)
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        found = BLOCK.search(html)
        if not found:
            print(f"{name}: no <style> block found", file=sys.stderr)
            return 1
        if found.group(0) == block:
            continue
        drifted.append(name)
        if not check_only:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html[: found.start()] + block + html[found.end():])
            rewritten.append(name)

    if check_only:
        if drifted:
            print("style block drifted: " + ", ".join(drifted), file=sys.stderr)
            return 1
        print(f"style block identical across {len(PAGES)} pages")
        return 0
    print(f"stamped {len(rewritten)} of {len(PAGES)} pages"
          + (": " + ", ".join(rewritten) if rewritten else " (all already current)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
