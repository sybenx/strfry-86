#!/usr/bin/env python3
"""strfry-86 installer/updater.

Single self-contained file — must run before lib86 exists, so it may NOT
import lib86. Safe to run any number of times (idempotent). Run via:

    docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py

See README.md for the one-command curl install.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
import urllib.error
import urllib.request

REPO_BASE_URL = "https://raw.githubusercontent.com/sybenx/strfry-86/main/"
INSTALL_DIR = "/config/strfry86"
STRFRY_CONF_PATH = "/config/strfry.conf"
PLUGIN_PATH = "/config/strfry86/plugin86.py"
CONFIG_JSON_PATH = os.path.join(INSTALL_DIR, "config.json")
DEFAULT_PORT = 8686
DEFAULT_BIND = "0.0.0.0"

# Files never managed by the manifest / never overwritten by updates.
OPERATOR_OWNED = {"config.json", "blacklist.json"}


# --------------------------------------------------------------------------
# Minimal inline bech32 (BIP-173), duplicated from lib86/bech32.py because
# this file must run standalone before lib86 is installed.
# --------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify_checksum(hrp, data):
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp, data):
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_encode(hrp, data):
    combined = data + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in combined)


def _bech32_decode(bech):
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        return None, None
    if bech.lower() != bech and bech.upper() != bech:
        return None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return None, None
    if not all(c in _BECH32_CHARSET for c in bech[pos + 1:]):
        return None, None
    hrp = bech[:pos]
    data = [_BECH32_CHARSET.find(c) for c in bech[pos + 1:]]
    if not _bech32_verify_checksum(hrp, data):
        return None, None
    return hrp, data[:-6]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def npub_encode(pubkey_hex):
    raw = bytes.fromhex(pubkey_hex)
    data = _convertbits(list(raw), 8, 5, True)
    return _bech32_encode("npub", data)


def npub_decode(npub):
    hrp, data = _bech32_decode(npub)
    if hrp != "npub" or data is None:
        raise ValueError("invalid npub")
    decoded = _convertbits(data, 5, 8, False)
    if decoded is None or len(decoded) != 32:
        raise ValueError("invalid npub payload length")
    return bytes(decoded).hex()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def is_hex64(s):
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def parse_pubkey_input(raw):
    """Accept npub1... or 64-char hex; return lowercase hex or raise ValueError."""
    raw = raw.strip()
    if raw.startswith("npub1"):
        return npub_decode(raw)
    if is_hex64(raw.lower()):
        return raw.lower()
    raise ValueError("not a valid npub or 64-char hex pubkey")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_url(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "strfry-86-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_manifest():
    url = REPO_BASE_URL + "manifest.json"
    try:
        raw = fetch_url(url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: failed to fetch manifest.json from {url}: {e}")
        sys.exit(1)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        print(f"ERROR: manifest.json is not valid JSON: {e}")
        sys.exit(1)


# --------------------------------------------------------------------------
# file sync
# --------------------------------------------------------------------------

def sync_files(manifest):
    """Download missing/changed deployable files. Updater itself is deferred
    to the very end and handled by self_update(). Returns (installed, updated, unchanged)."""
    installed = 0
    updated = 0
    unchanged = 0

    for rel_path, expected_sha in sorted(manifest.items()):
        if rel_path in OPERATOR_OWNED:
            continue
        if rel_path == "strfry-86-updater.py":
            continue  # handled last by self_update()

        local_path = os.path.join(INSTALL_DIR, rel_path)
        if os.path.exists(local_path):
            local_sha = sha256_file(local_path)
            if local_sha == expected_sha:
                unchanged += 1
                continue
            action = "updated"
        else:
            action = "installed"

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        url = REPO_BASE_URL + rel_path
        try:
            raw = fetch_url(url)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"ERROR: failed to download {rel_path}: {e}")
            sys.exit(1)

        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != expected_sha:
            print(
                f"ERROR: sha256 mismatch for {rel_path} "
                f"(expected {expected_sha}, got {actual_sha}) — aborting."
            )
            sys.exit(1)

        tmp_path = local_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, local_path)

        if action == "installed":
            installed += 1
        else:
            updated += 1
        print(f"{action}: {rel_path}")

    return installed, updated, unchanged


def self_update(manifest):
    """Download strfry-86-updater.py last, if changed. Returns True if updated."""
    rel_path = "strfry-86-updater.py"
    if rel_path not in manifest:
        return False
    expected_sha = manifest[rel_path]
    local_path = os.path.abspath(__file__)
    if os.path.exists(local_path) and sha256_file(local_path) == expected_sha:
        return False

    url = REPO_BASE_URL + rel_path
    try:
        raw = fetch_url(url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: failed to download updated {rel_path}: {e}")
        return False

    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        print(
            f"ERROR: sha256 mismatch for {rel_path} "
            f"(expected {expected_sha}, got {actual_sha}) — not self-updating."
        )
        return False

    tmp_path = local_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, local_path)
    return True


# --------------------------------------------------------------------------
# first-run config
# --------------------------------------------------------------------------

def find_relay_info_pubkey():
    """Best-effort scan of strfry.conf for the relay.info.pubkey field."""
    if not os.path.exists(STRFRY_CONF_PATH):
        return None
    try:
        with open(STRFRY_CONF_PATH, "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    in_info_block = False
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not in_info_block:
            if re.match(r'^\s*info\s*\{', line):
                in_info_block = True
                depth = line.count("{") - line.count("}")
            continue
        depth += line.count("{") - line.count("}")
        m = re.search(r'pubkey\s*=\s*"([^"]*)"', line)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
        if depth <= 0:
            in_info_block = False
    return None


def ensure_config():
    if os.path.exists(CONFIG_JSON_PATH):
        return "unchanged"

    admin_pubkey = None
    found = find_relay_info_pubkey()
    if found:
        candidate_hex = None
        try:
            candidate_hex = parse_pubkey_input(found)
        except ValueError:
            candidate_hex = None
        if candidate_hex:
            try:
                npub = npub_encode(candidate_hex)
            except ValueError:
                npub = candidate_hex
            answer = input(
                f"Found relay.info.pubkey {npub} in strfry.conf — use as admin? [Y/n] "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                admin_pubkey = candidate_hex

    while admin_pubkey is None:
        raw = input("Enter admin pubkey (npub or 64-char hex): ").strip()
        try:
            admin_pubkey = parse_pubkey_input(raw)
        except ValueError as e:
            print(f"  invalid: {e}")

    cfg = {
        "admin_pubkey_hex": admin_pubkey,
        "port": DEFAULT_PORT,
        "bind": DEFAULT_BIND,
    }
    os.makedirs(INSTALL_DIR, exist_ok=True)
    tmp_path = CONFIG_JSON_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, CONFIG_JSON_PATH)
    print(f"config.json written with admin {npub_encode(admin_pubkey)}")
    return "created"


# --------------------------------------------------------------------------
# strfry.conf edit
# --------------------------------------------------------------------------

def is_comment_line(line):
    return line.strip().startswith("#")


def find_write_policy_block(lines):
    depth = 0
    block_start = None
    block_end = None
    for i, line in enumerate(lines):
        if is_comment_line(line):
            continue
        if block_start is None:
            m = re.match(r'^\s*writePolicy\s*\{', line)
            if m:
                block_start = i
                depth = line.count("{") - line.count("}")
                if depth <= 0:
                    block_end = i
                    break
            continue
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            block_end = i
            break
    return block_start, block_end


def find_plugin_line(lines, block_start, block_end):
    if block_start is None:
        return None, None
    for i in range(block_start, block_end + 1):
        if is_comment_line(lines[i]):
            continue
        m = re.search(r'plugin\s*=\s*"([^"]*)"', lines[i])
        if m:
            return i, m.group(1)
    return None, None


def edit_strfry_conf():
    if not os.path.exists(STRFRY_CONF_PATH):
        print(f"WARNING: {STRFRY_CONF_PATH} not found — skipping strfry.conf edit.")
        return "not found"

    with open(STRFRY_CONF_PATH, "r") as f:
        original = f.read()

    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)

    backup_path = f"{STRFRY_CONF_PATH}.bak-{int(time.time())}"
    shutil.copy2(STRFRY_CONF_PATH, backup_path)
    print(f"strfry.conf backed up to {backup_path}")

    block_start, block_end = find_write_policy_block(lines)
    plugin_idx, plugin_value = find_plugin_line(lines, block_start, block_end)

    if plugin_value == PLUGIN_PATH:
        print("strfry.conf: writePolicy.plugin already set to strfry-86 — no changes made.")
        return "already configured"

    if plugin_value not in (None, ""):
        print(
            f"WARNING: strfry.conf writePolicy.plugin is already set to "
            f"'{plugin_value}' — NOT overwriting. Configure manually if you "
            f"want strfry-86's plugin86.py to take over."
        )
        return "existing plugin left untouched"

    new_line = f'    plugin = "{PLUGIN_PATH}"{newline}'

    if plugin_idx is not None:
        lines[plugin_idx] = new_line
    elif block_start is not None:
        lines.insert(block_end, new_line)
    else:
        if lines and not lines[-1].endswith(("\n", "\r\n")):
            lines[-1] = lines[-1] + newline
        lines.append(newline)
        lines.append(f"writePolicy {{{newline}")
        lines.append(new_line)
        lines.append(f"}}{newline}")
        print("strfry.conf: no writePolicy block found — appended a new one.")

    with open(STRFRY_CONF_PATH, "w") as f:
        f.write("".join(lines))

    print(f"strfry.conf: writePolicy.plugin set to {PLUGIN_PATH}")
    return "set"


# --------------------------------------------------------------------------
# process management
# --------------------------------------------------------------------------

def chmod_plugin():
    if os.path.exists(PLUGIN_PATH):
        st = os.stat(PLUGIN_PATH)
        os.chmod(PLUGIN_PATH, st.st_mode | 0o111)


def kill_server86():
    killed = 0
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return killed
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmdline_path = os.path.join(proc_dir, entry, "cmdline")
        try:
            with open(cmdline_path, "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if "server86.py" in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    os.makedirs(INSTALL_DIR, exist_ok=True)

    manifest = fetch_manifest()

    installed, updated, unchanged = sync_files(manifest)

    config_status = ensure_config()

    conf_status = edit_strfry_conf()

    chmod_plugin()

    killed = kill_server86()
    if killed:
        print(f"stopped {killed} running server86.py process(es); will respawn with fresh code.")

    updater_self_updated = self_update(manifest)

    print()
    print("=== strfry-86 update summary ===")
    print(f"files installed: {installed}, updated: {updated}, unchanged: {unchanged}")
    print(f"config.json: {config_status}")
    print(f"strfry.conf: {conf_status}")
    if updater_self_updated:
        print("updater updated — already effective next run.")
    print("if in doubt: docker restart <container>")


if __name__ == "__main__":
    main()
