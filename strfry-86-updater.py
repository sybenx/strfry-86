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

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
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
PLUGIN_PATH = "/config/strfry86/plugin86.py"
SERVER_SCRIPT = "/config/strfry86/server86.py"
# How long to wait for a killed server86.py to actually exit before spawning
# its replacement (it enforces singleton by port bind, so a replacement
# started too early just dies silently on EADDRINUSE), and how long to poll
# for the new one to come up before giving up and warning.
SERVER_KILL_WAIT = 5.0
SERVER_VERIFY_WAIT = 3.0
PROC_DIR = "/proc"
CONFIG_JSON_PATH = os.path.join(INSTALL_DIR, "config.json")
LOCAL_MANIFEST_PATH = os.path.join(INSTALL_DIR, "manifest.json")
DEFAULT_PORT = 8686
DEFAULT_BIND = "0.0.0.0"

# strfry.conf is NOT at a fixed path (same order of authority as server86):
# config.json override → running relay's --config → first candidate that
# exists. Hardcoding /config/strfry.conf made the updater no-op on packages
# that keep the conf under /etc. Tests may pin STRFRY_CONF_PATH to force a
# single path (including a missing one, so the pubkey prompt stays manual).
STRFRY_CONF_PATH = None
STRFRY_CONF_CANDIDATES = (
    "/etc/strfry.conf",
    "/etc/strfry/strfry.conf",
    "/config/strfry.conf",
    "/app/strfry.conf",
    "/usr/local/etc/strfry.conf",
)
STRFRY_BIN_CANDIDATES = (
    "/app/strfry", "/usr/local/bin/strfry", "/usr/bin/strfry", "/strfry",
)

# Files never managed by the manifest / never overwritten by updates.
OPERATOR_OWNED = {"config.json", "blacklist.json"}

# dockurr/strfry — the image the README's one-line install targets — ships
# its own relay.writePolicy.plugin already set to this no-op script. The old
# refuse-to-overwrite behavior left that documented install path completing
# with a WARNING and moderation silently never wired in. These are not
# values an operator wrote by hand, so they're safe to replace outright
# (still backed up first, like any other change this function makes).
KNOWN_DEFAULT_PLUGINS = {"/app/write-policy.py"}

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
# strfry.conf location (mirrors server86.strfry_conf_status, standalone)
# --------------------------------------------------------------------------

def _relay_conf_from_args(args):
    """Pull --config from a relay process argv (`--config X` or `--config=X`)."""
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def _strfry_bin_names():
    names = {"strfry"}
    for candidate in STRFRY_BIN_CANDIDATES:
        names.add(os.path.basename(candidate))
    which = shutil.which("strfry")
    if which:
        names.add(os.path.basename(which))
    return names


def locate_relay_conf():
    """Return the absolute path of the RUNNING relay's --config, or None.

    Same idea as server86.locate_relay_process: the process is the authority
    on which conf is active. Best-effort via /proc; silent None when /proc
    is missing or the relay is not up yet."""
    bin_names = _strfry_bin_names()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    args = [a.decode("utf-8", "replace") for a in f.read().split(b"\x00") if a]
            except OSError:
                continue
            if not args or os.path.basename(args[0]) not in bin_names:
                continue
            if "relay" not in args[1:]:
                continue
            conf = _relay_conf_from_args(args)
            if not conf:
                continue
            if not os.path.isabs(conf):
                try:
                    cwd = os.readlink(f"/proc/{entry}/cwd")
                except OSError:
                    continue
                conf = os.path.normpath(os.path.join(cwd, conf))
            return conf
    except OSError:
        pass
    return None


def resolve_strfry_conf():
    """Locate the active strfry.conf.

    Returns {path, source, exists} where source is one of:
      'pin'           — STRFRY_CONF_PATH set (tests / explicit pin)
      'config.json'   — strfry_conf_path override in config.json
      'relay process' — running relay's --config
      'default location' — first STRFRY_CONF_CANDIDATES entry that exists
      None            — nothing found
    """
    # Pin wins always so tests and a deliberate operator override of the
    # module constant stay deterministic.
    if STRFRY_CONF_PATH is not None:
        path = STRFRY_CONF_PATH
        return {
            "path": path,
            "source": "pin",
            "exists": os.path.isfile(path),
        }

    override = None
    try:
        with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh).get("strfry_conf_path")
        if isinstance(raw, str) and raw.strip():
            override = raw.strip()
    except (OSError, ValueError):
        pass
    if override:
        return {
            "path": override,
            "source": "config.json",
            "exists": os.path.isfile(override),
        }

    detected = locate_relay_conf()
    if detected and os.path.isfile(detected):
        return {"path": detected, "source": "relay process", "exists": True}

    for candidate in STRFRY_CONF_CANDIDATES:
        if os.path.isfile(candidate):
            return {
                "path": candidate,
                "source": "default location",
                "exists": True,
            }

    return {
        "path": detected,
        "source": None,
        "exists": False,
    }


def get_strfry_conf_path():
    """Path of an existing strfry.conf, or None."""
    status = resolve_strfry_conf()
    return status["path"] if status.get("exists") else None


# --------------------------------------------------------------------------
# first-run config
# --------------------------------------------------------------------------

def find_relay_info_pubkey():
    """Best-effort scan of strfry.conf for the relay.info.pubkey field."""
    conf_path = get_strfry_conf_path()
    if not conf_path:
        return None
    try:
        with open(conf_path, "r") as f:
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


def find_relay_info_url():
    """Best-effort scan of strfry.conf for the relay.info.url field."""
    conf_path = get_strfry_conf_path()
    if not conf_path:
        return None
    try:
        with open(conf_path, "r") as f:
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
        m = re.search(r'url\s*=\s*"([^"]*)"', line)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
        if depth <= 0:
            in_info_block = False
    return None


# The direct admin-port URL, always seeded alongside whatever public URL the
# operator gives. Without it nak and every development test against
# 127.0.0.1:8686 fail on day one with an auth error that looks like a signing
# problem.
LOCAL_CANONICAL_URL = f"http://127.0.0.1:{DEFAULT_PORT}/"


def prompt_canonical_urls():
    """Ask for the public URLs a NIP-86 management client may sign auth for.

    Deliberately ASKED, never derived. This is the highest-consequence value
    in the NIP-86 feature — get it wrong and every request fails with an auth
    error that looks like a client-side signing problem — and nothing here can
    know the operator's public URL. relay.info.url is offered as a suggestion
    the operator CONFIRMS, because guessing from it silently would produce a
    plausible wrong answer, which is worse than no answer at all.

    Returns a list; blank leaves only the local URL, which disables NIP-86
    from the outside without disabling development against it."""
    urls = []
    found = find_relay_info_url()
    suggestion = None
    if found:
        # wss:// -> https:// is exactly the derivation Amethyst performs on
        # the relay's own socket URL to find this endpoint.
        suggestion = re.sub(r'^ws(s?)://', r'http\1://', found.strip(), count=1)
        if "://" not in suggestion:
            suggestion = "https://" + suggestion
    if suggestion:
        answer = input(
            f"Found relay.info.url {found} in strfry.conf — allow NIP-86 auth signed "
            f"for {suggestion}? [Y/n] "
        ).strip().lower()
        if answer in ("", "y", "yes"):
            urls.append(suggestion)
    if not urls:
        print(
            "  Public https:// URL a NIP-86 client (Amethyst) will reach this relay on.\n"
            "  Amethyst has no endpoint field: it swaps wss://host/path to https://host/path,\n"
            "  so this must be the relay's own URL, not the admin port's."
        )
        raw = input("  URL (comma-separated for more than one, blank for none): ").strip()
        for tok in raw.replace(",", " ").split():
            if "://" not in tok:
                tok = "https://" + tok
            if tok not in urls:
                urls.append(tok)
    if LOCAL_CANONICAL_URL not in urls:
        urls.append(LOCAL_CANONICAL_URL)
    return urls


def prompt_contact_appeal():
    return input(
        "Optional appeal contact, shown publicly on the admin page (email, "
        "npub, URL, or any free text) — blank for none: "
    ).strip()


def prompt_admin_pubkey():
    """Ask for the admin pubkey, auto-detecting relay.info.pubkey from
    strfry.conf as a one-keystroke default and falling back to manual entry
    (looped until valid). Shared by ensure_config()'s fresh-install path and
    its existing-config top-up path, so a config.json somehow missing this
    key is asked for it exactly the same way a fresh install would be —
    never a second, slightly-different prompt to maintain."""
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
                return candidate_hex

    admin_pubkey = None
    while admin_pubkey is None:
        raw = input("Enter admin pubkey (npub or 64-char hex): ").strip()
        try:
            admin_pubkey = parse_pubkey_input(raw)
        except ValueError as e:
            print(f"  invalid: {e}")
    return admin_pubkey


# Every piece of information a fresh install ever prompts the operator for,
# as (key, prompt_fn) — the single list an update walks to top up whatever
# an EXISTING config.json is missing. A field added here later (the way
# contact_appeal was added after this project's initial release) is
# automatically both asked on a fresh install and topped up on every
# existing one — no second "if key not in cfg" block to remember to write
# alongside it, the exact gap that left contact_appeal as a one-off special
# case before this refactor.
INSTALLER_PROMPTED_FIELDS = (
    ("admin_pubkey_hex", prompt_admin_pubkey),
    ("contact_appeal", prompt_contact_appeal),
    ("canonical_urls", prompt_canonical_urls),
)

# Installer-prompted fields that a non-interactive first run may skip rather
# than block on, and the value it writes instead. admin_pubkey_hex is
# deliberately absent: it has no usable default and is always prompted.
# canonical_urls falls back to the local URL alone, which leaves NIP-86
# working for nak and closed from the outside until an operator sets it.
INSTALLER_OPTIONAL_DEFAULTS = {
    "contact_appeal": "",
    "canonical_urls": [LOCAL_CANONICAL_URL],
}


def _write_config(cfg):
    os.makedirs(INSTALL_DIR, exist_ok=True)
    tmp_path = CONFIG_JSON_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, CONFIG_JSON_PATH)


def ensure_config():
    if not os.path.exists(CONFIG_JSON_PATH):
        cfg = {"port": DEFAULT_PORT, "bind": DEFAULT_BIND}
        for key, prompt_fn in INSTALLER_PROMPTED_FIELDS:
            # The optional fields skip their prompt on a non-interactive first
            # run rather than blocking it. admin_pubkey_hex has no such
            # default and is always prompted, exactly as before.
            if key in INSTALLER_OPTIONAL_DEFAULTS and not sys.stdin.isatty():
                cfg[key] = INSTALLER_OPTIONAL_DEFAULTS[key]
            else:
                cfg[key] = prompt_fn()
        _write_config(cfg)
        print(f"config.json written with admin {npub_encode(cfg['admin_pubkey_hex'])}")
        return "created"

    # config.json already exists — top up whichever installer-prompted
    # fields are missing from it (contact_appeal was the first; won't be
    # the last) and leave every other key/value untouched. Requires a tty,
    # same as a fresh install's own prompts — an unattended update just
    # leaves the gap for the next interactive run rather than blocking or
    # guessing a value.
    try:
        with open(CONFIG_JSON_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: failed to read existing config.json ({e}) — leaving it untouched.")
        return "unchanged"

    missing = [(key, prompt_fn) for key, prompt_fn in INSTALLER_PROMPTED_FIELDS if key not in cfg]
    if not missing:
        return "unchanged"
    if not sys.stdin.isatty():
        return "unchanged"

    added = []
    for key, prompt_fn in missing:
        print(f"config.json is missing {key!r} —")
        cfg[key] = prompt_fn()
        added.append(key)
    _write_config(cfg)
    return ", ".join(added) + " added"


def set_next_plugin_config(value):
    """Merge next_plugin into the existing config.json — never a full
    rewrite from a stale in-memory copy, since ensure_config() may have
    already changed other keys earlier in this same run."""
    try:
        with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"
    cfg["next_plugin"] = value
    try:
        _write_config(cfg)
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    return True, None


# --------------------------------------------------------------------------
# strfry.conf edit
#
# A real strfry.conf whole-document parse, not a line-scan near where the
# plugin line is expected to be. That line-scan approach broke on several
# real shapes: a same-line `writePolicy { plugin = "" }` (rewriting the
# whole matched line ate the block's own closing brace or the surrounding
# relay{}'s), Allman-style `writePolicy` with `{` on its own following line
# (not recognised at all, so the fallback appended a second, top-level
# writePolicy block outside relay{} — a key strfry never reads), and an
# unterminated block (crashed with TypeError instead of failing cleanly).
# This tokenizes the whole file once, offsets included, and only ever
# splices the original text at those offsets — comments, spacing and
# everything else the operator wrote survives untouched, the same guarantee
# server86.py's own apply_strfry_conf_edits() makes for the Settings page.
# --------------------------------------------------------------------------

class ConfParseError(Exception):
    pass


_CONF_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'   # quoted string (escapes not interpreted, kept raw)
    r'|[{}=]'              # structural characters
    r'|[^\s{}=#"]+'        # bareword: identifier, number, true/false, ...
)


def _tokenize_conf(text):
    """(token, start, end) triples over strfry.conf-shaped text. Whitespace
    is skipped; '#' outside a quoted string starts a comment that runs to
    end-of-line. A '#' or brace INSIDE a quoted string is not structural —
    the quoted-string alternative in _CONF_TOKEN_RE is tried first and
    consumes the whole literal, braces and all."""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        m = _CONF_TOKEN_RE.match(text, i)
        if not m:
            raise ConfParseError(f"unrecognized character {c!r} at offset {i}")
        tokens.append((m.group(0), m.start(), m.end()))
        i = m.end()
    return tokens


def _parse_conf_tree(tokens):
    """Recursive-descent parse into a nested dict: each key maps to either
    ('block', subtree, open_end, close_start) — open_end/close_start are the
    text offsets just inside the block's '{' and just before its matching
    '}', the two splice points an edit ever needs — or
    ('value', raw_token, start, end). Raises ConfParseError on anything that
    doesn't fit the key=value / key{...} grammar, including a block that
    never closes, instead of a bare crash on malformed input."""
    idx = 0

    def parse_block(top_level):
        nonlocal idx
        node = {}
        while idx < len(tokens):
            tok, start, end = tokens[idx]
            if tok == "}":
                if top_level:
                    raise ConfParseError(f"unexpected '}}' at offset {start}")
                idx += 1
                return node
            key = tok
            idx += 1
            if idx >= len(tokens):
                raise ConfParseError(f"unexpected end of file after '{key}'")
            nxt, nstart, nend = tokens[idx]
            if nxt == "{":
                open_end = nend
                idx += 1
                child = parse_block(False)
                close_start = tokens[idx - 1][1]  # the '}' just consumed
                node[key] = ("block", child, open_end, close_start)
            elif nxt == "=":
                idx += 1
                if idx >= len(tokens):
                    raise ConfParseError(f"unexpected end of file after '{key} ='")
                vtok, vstart, vend = tokens[idx]
                idx += 1
                node[key] = ("value", vtok, vstart, vend)
            else:
                raise ConfParseError(f"unexpected token {nxt!r} after '{key}'")
        if not top_level:
            raise ConfParseError("unterminated block — missing '}'")
        return node

    root = parse_block(top_level=True)
    if idx != len(tokens):
        raise ConfParseError("trailing tokens after top-level parse")
    return root


def _conf_unquote(vtok):
    if vtok.startswith('"') and vtok.endswith('"') and len(vtok) >= 2:
        return vtok[1:-1]
    return vtok


def edit_strfry_conf():
    """Point relay.writePolicy.plugin at plugin86.py.

    Returns (status, conf_path). conf_path is the file we resolved (even when
    missing or left untouched) so the caller can prune backups next to it;
    None only when resolution found nothing at all. A backup is written only
    when this call is about to change the file's bytes — already-configured
    and refused-overwrite paths never touch disk."""
    status = resolve_strfry_conf()
    conf_path = status.get("path")
    if not status.get("exists") or not conf_path:
        tried = ", ".join(STRFRY_CONF_CANDIDATES)
        where = conf_path or f"candidates: {tried}"
        print(f"WARNING: strfry.conf not found ({where}) — skipping strfry.conf edit.")
        return "not found", conf_path

    source = status.get("source") or "unknown"
    print(f"strfry.conf: using {conf_path} ({source})")

    # newline='' disables universal-newline translation on both read and
    # write, so a file's own \r\n vs \n convention is read back verbatim
    # instead of being silently collapsed to \n (which text mode's default
    # translate-on-read does — the very thing the newline-detection below
    # exists to preserve).
    with open(conf_path, "r", newline="") as f:
        original = f.read()
    newline = "\r\n" if "\r\n" in original else "\n"

    try:
        tokens = _tokenize_conf(original)
        root = _parse_conf_tree(tokens)
    except ConfParseError as e:
        print(f"WARNING: strfry.conf failed to parse ({e}) — skipping strfry.conf edit.")
        return f"conf parse error: {e}", conf_path

    relay_entry = root.get("relay")
    if relay_entry is None or relay_entry[0] != "block":
        print("WARNING: strfry.conf has no relay{} block — skipping strfry.conf edit.")
        return "no relay block", conf_path
    _, relay_children, relay_open_end, relay_close_start = relay_entry

    wp_entry = relay_children.get("writePolicy")
    if wp_entry is not None and wp_entry[0] != "block":
        print("WARNING: strfry.conf's writePolicy is not a block — skipping strfry.conf edit.")
        return "writePolicy is not a block", conf_path

    if wp_entry is not None:
        _, wp_children, wp_open_end, wp_close_start = wp_entry
        plugin_entry = wp_children.get("plugin")
    else:
        plugin_entry = None

    if plugin_entry is not None and plugin_entry[0] != "value":
        print("WARNING: strfry.conf's writePolicy.plugin is not a value — skipping strfry.conf edit.")
        return "plugin is not a value", conf_path

    plugin_value = _conf_unquote(plugin_entry[1]) if plugin_entry is not None else None

    if plugin_value == PLUGIN_PATH:
        print("strfry.conf: writePolicy.plugin already set to strfry-86 — no changes made.")
        return "already configured", conf_path

    replacing_default = plugin_value in KNOWN_DEFAULT_PLUGINS
    if plugin_value not in (None, "") and not replacing_default:
        print(
            f"strfry.conf writePolicy.plugin is already set to '{plugin_value}' — NOT overwriting it."
        )
        # This used to be a dead end: run strfry-86 or your other plugin,
        # never both, since strfry only has the one plugin slot. plugin86.py
        # can now chain to a downstream plugin on its own stdin/stdout, so
        # offer to wire the existing value in as that downstream rather than
        # just leaving it disconnected — interactive only, same as every
        # other prompt in this file, so an unattended run stays a no-op.
        if sys.stdin.isatty():
            answer = input(
                f"Chain it after plugin86.py instead? Every event plugin86 accepts "
                f"will be forwarded to it and its verdict used; an event plugin86 "
                f"itself rejects will never reach it. [Y/n] "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                ok, err = set_next_plugin_config(plugin_value)
                if ok:
                    print(
                        f"config.json: next_plugin set to {plugin_value!r} — plugin86.py "
                        f"will chain to it the next time it restarts."
                    )
                else:
                    print(f"WARNING: failed to record next_plugin in config.json: {err}")
            else:
                print("Leaving it unconfigured — plugin86.py will not chain to it.")
        else:
            print(
                "Not chaining (non-interactive run). Set next_plugin manually in "
                "config.json, or re-run the updater interactively, if you want "
                "strfry-86 to forward accepted events to it."
            )
        return "existing plugin left untouched", conf_path

    new_plugin_value = f'"{PLUGIN_PATH}"'
    appended_block = False
    if plugin_entry is not None:
        # Splice only the value token — everything else on and around this
        # line (indentation, trailing comment) is untouched.
        _, _, vstart, vend = plugin_entry
        new_content = original[:vstart] + new_plugin_value + original[vend:]
    elif wp_entry is not None:
        # writePolicy{} exists but has no plugin key (e.g. `writePolicy {}`)
        # — insert one just before its closing brace, wherever that is.
        insert = f'{newline}        plugin = {new_plugin_value}{newline}    '
        new_content = original[:wp_close_start] + insert + original[wp_close_start:]
    else:
        # No writePolicy block at all — insert a whole new one just before
        # relay{}'s own closing brace, so it lands inside relay{} regardless
        # of whether relay{} (or anything else in the file) uses K&R or
        # Allman brace style; nothing here depends on line layout.
        insert = (
            f'{newline}    writePolicy {{{newline}'
            f'        plugin = {new_plugin_value}{newline}'
            f'    }}{newline}'
        )
        new_content = original[:relay_close_start] + insert + original[relay_close_start:]
        appended_block = True

    if new_content == original:
        print("strfry.conf: writePolicy.plugin already set to strfry-86 — no changes made.")
        return "already configured", conf_path

    # Re-parse the edited text before it ever touches disk. The splices
    # above only ever insert well-formed, self-balanced fragments or replace
    # a brace-free value token, so this should never fail — it's the same
    # defense-in-depth server86.py's write_strfry_conf() applies: never
    # trust an edit was correct just because the code that produced it looks
    # right.
    try:
        _parse_conf_tree(_tokenize_conf(new_content))
    except ConfParseError as e:
        print(f"WARNING: edited strfry.conf failed to reparse ({e}) — nothing written.")
        return f"internal error: edit failed to reparse ({e})", conf_path

    backup_path = f"{conf_path}.bak-{int(time.time())}"
    shutil.copy2(conf_path, backup_path)
    print(f"strfry.conf backed up to {backup_path}")

    tmp_path = conf_path + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        f.write(new_content)
    try:
        os.replace(tmp_path, conf_path)
    except OSError as e:
        # A single-file bind mount (Docker: `-v host/strfry.conf:/etc/strfry.conf`,
        # confirmed against a real container) refuses to have its mountpoint
        # replaced — os.replace raises EBUSY here even though writing the
        # file's CONTENTS in place is fine. Fall back to that: not atomic
        # against a mid-write crash, but the .bak above already exists, and
        # this is the only way to land the edit at all on that layout.
        if e.errno != errno.EBUSY:
            raise
        with open(conf_path, "w", newline="") as f:
            f.write(new_content)
        os.remove(tmp_path)

    if appended_block:
        print("strfry.conf: no writePolicy block found — appended a new one inside relay{}.")
    if replacing_default:
        print(
            f"strfry.conf: replaced known-default no-op plugin '{plugin_value}' "
            f"with {PLUGIN_PATH}"
        )
    print(f"strfry.conf: writePolicy.plugin set to {PLUGIN_PATH}")
    return "set", conf_path


# --------------------------------------------------------------------------
# process management
# --------------------------------------------------------------------------

def chmod_plugin():
    if os.path.exists(PLUGIN_PATH):
        st = os.stat(PLUGIN_PATH)
        os.chmod(PLUGIN_PATH, st.st_mode | 0o111)


def find_server86_pids():
    """PIDs of processes actually running SERVER_SCRIPT under a python
    interpreter — not just any process whose argv happens to contain that
    path as a substring, which previously matched e.g. `nano
    /config/strfry86/server86.py` open in another shell and killed it."""
    if not os.path.isdir(PROC_DIR):
        return []
    pids = []
    for entry in os.listdir(PROC_DIR):
        if not entry.isdigit():
            continue
        cmdline_path = os.path.join(PROC_DIR, entry, "cmdline")
        try:
            with open(cmdline_path, "rb") as f:
                args = [a.decode("utf-8", errors="replace")
                        for a in f.read().split(b"\x00") if a]
        except OSError:
            continue
        if len(args) < 2 or SERVER_SCRIPT not in args[1:]:
            continue
        if not os.path.basename(args[0]).startswith("python"):
            continue
        pids.append(int(entry))
    return pids


def _server86_port_bound():
    """True if something is actively accepting on the configured admin
    port. server86.py exits 0 silently on EADDRINUSE and on any startup
    crash (main(), server86.py ~5556), so a PID existing for a moment after
    spawn is not evidence the admin UI actually came up — only a bound
    port is."""
    try:
        with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    port = cfg.get("port", DEFAULT_PORT)
    bind = cfg.get("bind", DEFAULT_BIND)
    host = "127.0.0.1" if bind in ("0.0.0.0", "", None) else bind
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def restart_server86():
    """Kill any running server86.py and unconditionally spawn its
    replacement, verifying it comes back up. Delegating the respawn to
    plugin86 (which only checks once per hour, or on the next event) could
    leave the admin page down indefinitely on a quiet relay after every
    update — and on a brand new install, nothing has ever spawned it at
    all until the relay's first event — so this does it here, synchronously,
    with a visible outcome every run.

    Returns "restarted" or a failure description."""
    pids = find_server86_pids()

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    if pids:
        # server86 enforces singleton by port bind and exits 0 silently on
        # EADDRINUSE, so a replacement spawned before the old process
        # actually releases the port just dies without a word — wait for
        # it to be gone.
        deadline = time.monotonic() + SERVER_KILL_WAIT
        remaining = [pid for pid in pids if os.path.isdir(f"/proc/{pid}")]
        while remaining and time.monotonic() < deadline:
            time.sleep(0.2)
            remaining = [pid for pid in remaining if os.path.isdir(f"/proc/{pid}")]

        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if remaining:
            time.sleep(0.5)

    try:
        subprocess.Popen(
            ["python3", SERVER_SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=INSTALL_DIR,
        )
    except Exception as e:
        return f"did NOT come back up — check stderr (failed to spawn: {e})"

    verify_deadline = time.monotonic() + SERVER_VERIFY_WAIT
    while time.monotonic() < verify_deadline:
        if find_server86_pids() and _server86_port_bound():
            return "restarted"
        time.sleep(0.2)
    return "did NOT come back up — check stderr"


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

    # Must happen right after sync, before ensure_config(): sync_files()
    # writes plugin86.py via a fresh temp file + os.replace, so the old
    # inode's exec bit is gone the moment it lands. ensure_config() can then
    # block indefinitely on an interactive prompt — strfry re-execs its
    # write-policy plugin on mtime change and needs it executable the whole
    # time this run is in progress, not just once main() finishes.
    chmod_plugin()

    config_status = ensure_config()

    conf_status, conf_path = edit_strfry_conf()

    restart_status = restart_server86()
    if restart_status == "restarted":
        print("admin page restarted")
    else:
        print(f"WARNING: admin page {restart_status}")

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
    # files must survive to the next attempt). Conf backups live next to
    # whatever conf we actually resolved, not a hardcoded /config path.
    pruned_conf = 0
    pruned_bundles = 0
    safe_to_prune = conf_status != "existing plugin left untouched" and not self_update_mismatch
    if safe_to_prune:
        if conf_path:
            conf_dir = os.path.dirname(conf_path) or "."
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
