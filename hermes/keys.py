"""hermes keys add|list|remove <device> [pubkey-file]: the Hermes-only ssh keys in ~/.ssh/authorized_keys
(docs/design.md §3, docs/keys.md). Each sending device has its own key, and on this server that key can do one thing:
run `hermes receive --key <device>`. Only lines tagged hermes:<device> are ever touched; every change first saves a
dated backup, authorized_keys.set-aside-YYYYMMDD-HHMM, next to the file."""
import base64
import hashlib
import os
import shlex
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

from hermes.receive import DEVICE

TAILNET_RANGES = "100.64.0.0/10,fd7a:115c:a1e0::/48"   # private-ok: Tailscale's documented ranges, no device of ours
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
TAG = "hermes:"


class KeyError_(Exception):
    pass


def authorized_keys():
    return Path(os.path.expanduser("~/.ssh/authorized_keys"))


def hermes_bin():
    return os.path.expanduser("~/.local/bin/hermes")


def options(device):
    return 'restrict,from="%s",command="%s receive --key %s"' % (TAILNET_RANGES, hermes_bin(), device)


def line_for(device, keytype, blob):
    return "%s %s %s %s%s" % (options(device), keytype, blob, TAG, device)


def parse_pubkey(text):
    """(keytype, base64 blob) from one public key line, or KeyError_ saying what is wrong with it."""
    if "PRIVATE KEY" in text:
        raise KeyError_("that is a private key. Hermes needs the public half (the .pub file); the private key stays on "
                        "the sending device.")
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if len(lines) != 1:
        raise KeyError_("expected one public key line (ssh-ed25519 AAAA... comment), found %d." % len(lines))
    words = lines[0].split()
    if len(words) < 2 or words[0] not in KEY_TYPES:
        raise KeyError_("the line does not start with a key type Hermes knows (%s)." % ", ".join(KEY_TYPES))
    keytype, blob = words[0], words[1]
    try:
        raw = base64.b64decode(blob, validate=True)
        n = struct.unpack(">I", raw[:4])[0]
        if raw[4:4 + n].decode() != keytype:
            raise ValueError
    except (ValueError, struct.error, UnicodeDecodeError):
        raise KeyError_("the key data is not a valid %s public key." % keytype)
    return keytype, blob


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")


def tagged(line):
    """The device a line is tagged for (hermes:<device> as its comment), else None."""
    for word in line.split():
        if word.startswith(TAG) and DEVICE.match(word[len(TAG):]):
            return word[len(TAG):]
    return None


def key_of(line):
    """(keytype, blob) of any authorized_keys line, options or not; None for comments and blanks."""
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    try:
        words = shlex.split(line)
    except ValueError:
        words = line.split()
    for i, w in enumerate(words[:-1]):
        if w in KEY_TYPES:
            return w, words[i + 1]
    return None


def read_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def backup(path):
    """authorized_keys.set-aside-YYYYMMDD-HHMM next to the file, never overwritten."""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M")
    aside = path.with_name(path.name + ".set-aside-" + stamp)
    n = 1
    while aside.exists():
        n += 1
        aside = path.with_name("%s.set-aside-%s-%d" % (path.name, stamp, n))
    shutil.copy2(path, aside)
    return aside


def write_lines(path, lines):
    """Replace the file in one step, mode 600, in a 700 ~/.ssh."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".authorized_keys.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in lines))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_device(device):
    if not DEVICE.match(device or ""):
        raise KeyError_("%r is not a device name: letters, digits, dots and dashes, 63 characters at most (e.g. %s)."
                        % (device, "{{PC}}"))


def add(device, pubkey_text, path=None):
    """Add (or replace) the device's line. Returns what was done, in words."""
    check_device(device)
    path = path or authorized_keys()
    keytype, blob = parse_pubkey(pubkey_text)
    lines = read_lines(path)
    for line in lines:
        k = key_of(line)
        if k and k[1] == blob and tagged(line) != device:
            raise KeyError_("this key is already in %s on a line that is not Hermes's for %s. A Hermes key is its own "
                            "key, made for one device and nothing else (ssh-keygen -t ed25519 -f hermes_%s)."
                            % (path, device, device))
    new = line_for(device, keytype, blob)
    kept = [l for l in lines if tagged(l) != device]
    replaced = len(lines) - len(kept)
    aside = backup(path)
    write_lines(path, kept + [new])
    return "%s hermes:%s (%s %s)%s" % ("replaced" if replaced else "added", device, keytype, fingerprint(blob),
                                       "; the old file is " + aside.name if aside else "")


def remove(device, path=None):
    check_device(device)
    path = path or authorized_keys()
    lines = read_lines(path)
    kept = [l for l in lines if tagged(l) != device]
    if len(kept) == len(lines):
        raise KeyError_("no line tagged hermes:%s in %s; hermes keys list shows the devices there." % (device, path))
    aside = backup(path)
    write_lines(path, kept)
    return "removed hermes:%s (%d line%s); the old file is %s" % (device, len(lines) - len(kept),
                                                                 "" if len(lines) - len(kept) == 1 else "s", aside.name)


def listing(path=None):
    """One line per Hermes key: device, key type, fingerprint, and whether the line is exactly what Hermes writes."""
    path = path or authorized_keys()
    out = []
    for line in read_lines(path):
        device = tagged(line)
        if device is None:
            continue
        k = key_of(line)
        if not k:
            out.append("%s: a tagged line with no key in it; check %s" % (device, path))
            continue
        state = "ok" if line == line_for(device, *k) else "options differ from what hermes keys add writes; check it"
        out.append("%s  %s  %s  %s" % (device, k[0], fingerprint(k[1]), state))
    return out


def main(args):
    """hermes keys ...: returns the exit status."""
    path = Path(args.file) if args.file else None
    try:
        if args.action == "list":
            lines = listing(path)
            print("\n".join(lines) if lines else "no Hermes keys in %s" % (path or authorized_keys()))
        elif args.action == "add":
            if not args.device:
                raise KeyError_("hermes keys add <device> [pubkey-file]: which device is this key for?")
            text = Path(args.pubkey).read_text(encoding="utf-8") if args.pubkey else sys.stdin.read()
            print(add(args.device, text, path))
        elif args.action == "remove":
            if not args.device:
                raise KeyError_("hermes keys remove <device>: which device's key?")
            print(remove(args.device, path))
        return 0
    except (KeyError_, OSError) as e:
        print("hermes keys: %s" % e, file=sys.stderr)
        return 1
