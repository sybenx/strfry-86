#!/usr/bin/env python3
"""strfry-86 installer/updater.

Single self-contained file — must run before lib86 exists, so it may NOT
import lib86. Safe to run any number of times (idempotent). Run via:

    docker exec -it strfry python3 /config/strfry86/strfry-86-updater.py

Source selection is offline-first: if strfry86-bundle.tar.gz sits next to
this script, it is the source of truth (no network needed at all). Otherwise
this falls back to fetching from the repo's raw URL, and if that's also
unreachable, to the local manifest.json already installed from a previous
run (a clean no-op — nothing to fetch means nothing has changed).

See README.md for the one-command curl install and the offline install.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tarfile
import time
import urllib.error
import urllib.request

REPO_BASE_URL = "https://raw.githubusercontent.com/sybenx/strfry-86/main/"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_FILENAME = "strfry86-bundle.tar.gz"
BUNDLE_PATH = os.path.join(SCRIPT_DIR, BUNDLE_FILENAME)
INSTALL_DIR = "/config/strfry86"
STRFRY_CONF_PATH = "/config/strfry.conf"
PLUGIN_PATH = "/config/strfry86/plugin86.py"
CONFIG_JSON_PATH = os.path.join(INSTALL_DIR, "config.json")
LOCAL_MANIFEST_PATH = os.path.join(INSTALL_DIR, "manifest.json")
DEFAULT_PORT = 8686
DEFAULT_BIND = "0.0.0.0"

# Files never managed by the manifest / never overwritten by updates.
OPERATOR_OWNED = {"config.json", "blacklist.json"}

# Retention: housekeeping constants, not prompts or config keys.
KEEP_CONF_BACKUPS = 3
KEEP_APPLIED_BUNDLES = 1
CONF_BACKUP_RE = re.compile(r'^strfry\.conf\.bak-(\d+)$')
APPLIED_BUNDLE_RE = re.compile(r'^strfry86-bundle\.tar\.gz\.applied-(\d+)$')


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


# --------------------------------------------------------------------------
# source selection: offline bundle / network / local-fallback
# --------------------------------------------------------------------------

def open_bundle(bundle_path):
    try:
        return tarfile.open(bundle_path, "r:gz")
    except tarfile.TarError as e:
        print(f"ERROR: failed to open bundle {bundle_path}: {e}")
        sys.exit(1)


def validate_bundle_members(tar):
    """Reject absolute paths, '..' components, and links before anything is
    extracted — a malicious or corrupt bundle must never be allowed to write
    outside INSTALL_DIR."""
    for member in tar.getmembers():
        name = member.name
        if os.path.isabs(name) or name.startswith("/"):
            print(f"ERROR: bundle member '{name}' has an absolute path — aborting.")
            sys.exit(1)
        parts = name.replace("\\", "/").split("/")
        if ".." in parts:
            print(f"ERROR: bundle member '{name}' contains '..' — aborting.")
            sys.exit(1)
        if member.issym() or member.islnk():
            print(f"ERROR: bundle member '{name}' is a link — aborting.")
            sys.exit(1)


def load_manifest_from_bundle(tar):
    try:
        member = tar.getmember("manifest.json")
    except KeyError:
        print("ERROR: bundle does not contain manifest.json — aborting.")
        sys.exit(1)
    f = tar.extractfile(member)
    if f is None:
        print("ERROR: manifest.json in bundle is not a regular file — aborting.")
        sys.exit(1)
    try:
        return json.loads(f.read().decode("utf-8"))
    except ValueError as e:
        print(f"ERROR: bundle manifest.json is not valid JSON: {e}")
        sys.exit(1)


def read_from_bundle(tar, rel_path):
    member = tar.getmember(rel_path)
    f = tar.extractfile(member)
    if f is None:
        raise RuntimeError(f"'{rel_path}' is not a regular file in bundle")
    return f.read()


def try_fetch_manifest_network():
    """Returns the manifest dict, or None if the network is unreachable."""
    url = REPO_BASE_URL + "manifest.json"
    try:
        raw = fetch_url(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"WARNING: failed to fetch manifest.json from {url}: {e}")
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        print(f"ERROR: manifest.json is not valid JSON: {e}")
        sys.exit(1)


def read_from_network(rel_path):
    return fetch_url(REPO_BASE_URL + rel_path)


def load_local_manifest():
    if not os.path.exists(LOCAL_MANIFEST_PATH):
        return None
    try:
        with open(LOCAL_MANIFEST_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_local_manifest(manifest):
    """Record what's currently installed so a future run with no network and
    no bundle has something to compare against instead of hard-failing."""
    os.makedirs(INSTALL_DIR, exist_ok=True)
    tmp_path = LOCAL_MANIFEST_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, LOCAL_MANIFEST_PATH)


def refuse_to_fetch(rel_path):
    raise RuntimeError("no network and no bundle available to fetch this file")


# --------------------------------------------------------------------------
# file sync
# --------------------------------------------------------------------------

def sync_files(manifest, fetch_bytes, source_label):
    """Install missing/changed deployable files via fetch_bytes(rel_path).
    The updater itself is deferred to the very end and handled by
    self_update(). Returns (installed, updated, unchanged)."""
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
        try:
            raw = fetch_bytes(rel_path)
        except Exception as e:
            print(f"ERROR: failed to read {rel_path} from {source_label}: {e}")
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


def self_update(manifest, fetch_bytes, source_label):
    """Fetch strfry-86-updater.py last, if changed.

    Returns (updated, hit_mismatch): updated is True if the file was
    replaced; hit_mismatch is True if a fetch or sha256 verification failed,
    which the caller treats the same as any other hash-mismatch — nothing
    gets pruned this run."""
    rel_path = "strfry-86-updater.py"
    if rel_path not in manifest:
        return False, False
    expected_sha = manifest[rel_path]
    local_path = os.path.abspath(__file__)
    if os.path.exists(local_path) and sha256_file(local_path) == expected_sha:
        return False, False

    try:
        raw = fetch_bytes(rel_path)
    except Exception as e:
        print(f"ERROR: failed to read updated {rel_path} from {source_label}: {e}")
        return False, True

    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        print(
            f"ERROR: sha256 mismatch for {rel_path} "
            f"(expected {expected_sha}, got {actual_sha}) — not self-updating."
        )
        return False, True

    tmp_path = local_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, local_path)
    return True, False


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


def prompt_contact_appeal():
    return input(
        "Optional appeal contact, shown publicly on the admin page (email, "
        "npub, URL, or any free text) — blank for none: "
    ).strip()


def prompt_relay_url():
    return input(
        "Relay URL as clients dial it (e.g. wss://relay.example.com), used "
        "by the audit page — blank to skip: "
    ).strip()


# Optional config keys prompted for once on first run (in this order) and
# topped up on any later run where an existing config.json lacks the key
# entirely. Present-but-empty is an answered question and is never re-asked.
OPTIONAL_CONFIG_PROMPTS = [
    ("contact_appeal", prompt_contact_appeal),
    ("relay_url", prompt_relay_url),
]


def ensure_config():
    if not os.path.exists(CONFIG_JSON_PATH):
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

        is_tty = sys.stdin.isatty()
        cfg = {
            "admin_pubkey_hex": admin_pubkey,
            "port": DEFAULT_PORT,
            "bind": DEFAULT_BIND,
        }
        for key, prompt_fn in OPTIONAL_CONFIG_PROMPTS:
            cfg[key] = prompt_fn() if is_tty else ""

        os.makedirs(INSTALL_DIR, exist_ok=True)
        tmp_path = CONFIG_JSON_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, CONFIG_JSON_PATH)
        print(f"config.json written with admin {npub_encode(admin_pubkey)}")
        return "created"

    # config.json already exists — the only thing we may do is top up any
    # optional keys missing entirely. Every other key/value is left
    # untouched.
    try:
        with open(CONFIG_JSON_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: failed to read existing config.json ({e}) — leaving it untouched.")
        return "unchanged"

    missing = [key for key, _ in OPTIONAL_CONFIG_PROMPTS if key not in cfg]
    if not missing:
        return "unchanged"

    if not sys.stdin.isatty():
        return "unchanged"

    added = []
    for key, prompt_fn in OPTIONAL_CONFIG_PROMPTS:
        if key in missing:
            cfg[key] = prompt_fn()
            added.append(key)

    tmp_path = CONFIG_JSON_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, CONFIG_JSON_PATH)
    return ", ".join(added) + " added"


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

    new_lines = list(lines)
    appended_block = False
    if plugin_idx is not None:
        new_lines[plugin_idx] = new_line
    elif block_start is not None:
        new_lines.insert(block_end, new_line)
    else:
        if new_lines and not new_lines[-1].endswith(("\n", "\r\n")):
            new_lines[-1] = new_lines[-1] + newline
        new_lines.append(newline)
        new_lines.append(f"writePolicy {{{newline}")
        new_lines.append(new_line)
        new_lines.append(f"}}{newline}")
        appended_block = True

    new_content = "".join(new_lines)
    if new_content == original:
        # Nothing actually changes byte-for-byte — no backup, no write.
        print("strfry.conf: writePolicy.plugin already set to strfry-86 — no changes made.")
        return "already configured"

    backup_path = f"{STRFRY_CONF_PATH}.bak-{int(time.time())}"
    shutil.copy2(STRFRY_CONF_PATH, backup_path)
    print(f"strfry.conf backed up to {backup_path}")

    with open(STRFRY_CONF_PATH, "w") as f:
        f.write(new_content)

    if appended_block:
        print("strfry.conf: no writePolicy block found — appended a new one.")
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
# retention
# --------------------------------------------------------------------------

def prune_old_files(directory, pattern, keep):
    """Delete all but the `keep` newest files in `directory` whose basename
    fully matches `pattern` (which must capture the sortable integer in
    group 1). No recursion, no globbing, no following symlinks — anything
    hand-renamed or otherwise not an exact match is left alone. Ordered by
    the integer parsed from the filename, not mtime (docker cp / volume
    restores rewrite mtimes). Deletion failures are logged to stderr and
    otherwise non-fatal. Returns the count actually removed."""
    try:
        entries = os.listdir(directory)
    except OSError as e:
        print(f"WARNING: failed to list {directory} for pruning: {e}", file=sys.stderr)
        return 0

    matches = []
    for name in entries:
        m = pattern.match(name)
        if not m:
            continue
        path = os.path.join(directory, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        matches.append((int(m.group(1)), path))

    matches.sort(key=lambda t: t[0], reverse=True)

    removed = 0
    for _, path in matches[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            print(f"WARNING: failed to prune {path}: {e}", file=sys.stderr)
    return removed


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    os.makedirs(INSTALL_DIR, exist_ok=True)

    tar = None

    if os.path.exists(BUNDLE_PATH):
        mode = "offline"
        source_label = f"bundle {BUNDLE_FILENAME}"
        tar = open_bundle(BUNDLE_PATH)
        validate_bundle_members(tar)
        manifest = load_manifest_from_bundle(tar)
        fetch_bytes = lambda rel: read_from_bundle(tar, rel)
    else:
        manifest = try_fetch_manifest_network()
        if manifest is not None:
            mode = "network"
            source_label = REPO_BASE_URL
            fetch_bytes = read_from_network
        else:
            local_manifest = load_local_manifest()
            if local_manifest is None:
                print(
                    "ERROR: no bundle present, manifest.json unreachable over "
                    "the network, and no local manifest.json to fall back on. "
                    f"Provide network access or drop {BUNDLE_FILENAME} next to "
                    "the updater."
                )
                sys.exit(1)
            mode = "local-fallback"
            source_label = "local manifest.json (no network, no bundle)"
            manifest = local_manifest
            fetch_bytes = refuse_to_fetch
            print(
                "no network reachable and no bundle present — falling back to "
                "the local manifest.json; files will report unchanged."
            )

    installed, updated, unchanged = sync_files(manifest, fetch_bytes, source_label)

    config_status = ensure_config()

    conf_status = edit_strfry_conf()

    chmod_plugin()

    killed = kill_server86()
    if killed:
        print(f"stopped {killed} running server86.py process(es); will respawn with fresh code.")

    self_update_mismatch = False
    if mode == "offline":
        applied_path = f"{BUNDLE_PATH}.applied-{int(time.time())}"
        os.rename(BUNDLE_PATH, applied_path)
        print(f"bundle applied — renamed to {os.path.basename(applied_path)}")
        updater_self_updated, self_update_mismatch = self_update(manifest, fetch_bytes, source_label)
        self_update_msg = "updater updated — effective next run."
    elif mode == "network":
        updater_self_updated, self_update_mismatch = self_update(manifest, fetch_bytes, source_label)
        self_update_msg = "updater updated — already effective next run."
    else:
        updater_self_updated = False
        self_update_msg = ""

    if tar is not None:
        tar.close()

    save_local_manifest(manifest)

    # Prune only at the very end of a fully successful run: a foreign
    # writePolicy plugin or a self-update hash mismatch leaves the current
    # state in question, so nothing gets pruned this run (the fallback
    # files must survive to the next attempt).
    pruned_conf = 0
    pruned_bundles = 0
    safe_to_prune = conf_status != "existing plugin left untouched" and not self_update_mismatch
    if safe_to_prune:
        conf_dir = os.path.dirname(STRFRY_CONF_PATH) or "."
        pruned_conf = prune_old_files(conf_dir, CONF_BACKUP_RE, KEEP_CONF_BACKUPS)
        pruned_bundles = prune_old_files(INSTALL_DIR, APPLIED_BUNDLE_RE, KEEP_APPLIED_BUNDLES)

    print()
    print("=== strfry-86 update summary ===")
    print(f"mode: {mode}")
    print(f"files installed: {installed}, updated: {updated}, unchanged: {unchanged}")
    print(f"config.json: {config_status}")
    print(f"strfry.conf: {conf_status}")
    if updater_self_updated:
        print(self_update_msg)
    if safe_to_prune:
        print(f"pruned {pruned_conf} old conf backups, {pruned_bundles} old applied bundles")
    else:
        print("pruning skipped this run (state in question)")
    print("if in doubt: docker restart <container>")


if __name__ == "__main__":
    main()
