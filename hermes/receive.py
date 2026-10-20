"""hermes receive --key <device>: the receiving side of the door (docs/design.md §3, step 1).

It runs only as an ssh forced command (hermes keys add writes the authorized_keys line) and accepts two commands from
SSH_ORIGINAL_COMMAND:
    ping                                           -> "hermes ok <version>"
    put <route> <name-b64url> <bytes> <sha256>     the file on stdin -> <stage>/in/<id>/{data,meta.json}
The file streams into <stage>/incoming/<id>/data and is hashed as it goes; the transfer stops when more bytes arrive
than were declared or when the drive would drop below a 5 GB margin. A short stream, a long one or a wrong hash sets
the attempt aside in <stage>/failed/<id>/ (a transfer error, not a Janus matter). Only a complete folder, data and
meta.json, is renamed into in/ in one step, so the door never sees half a file. Exit: 0 ok · 1 transfer failed ·
2 refused (bad command, route or name) · 3 the server's own problem (config, disk)."""
import base64
import hashlib
import io
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

from hermes import config, version

MARGIN = 5 * 1024 ** 3        # the drive never drops below this
CHUNK = 1024 * 1024
RECHECK = 64 * CHUNK          # how often the margin is checked again while streaming
NAME_MAX = 200
NAME_BAD = ("Cc", "Cf", "Co", "Cs", "Zl", "Zp")   # controls (C0 and C1), format (bidi overrides, zero-width), private, surrogates, separators
IDLE_TIMEOUT = 120            # seconds without data before a transfer is given up; $HERMES_IDLE_TIMEOUT for tests
SIZE = re.compile(r"[0-9]{1,15}")
ERROR_LOG = "~/.local/share/hermes/log/receive-errors.log"
DEVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
TAILSCALE = "tailscale"       # or $HERMES_TAILSCALE
OK, FAILED, REFUSED, SERVER = 0, 1, 2, 3


class Refused(Exception):
    """The sender's command, route or name is wrong: nothing was written."""
    code = REFUSED


class Failed(Exception):
    """The transfer went wrong: the attempt is set aside."""
    code = FAILED


class ServerProblem(Exception):
    """Hermes's own problem (config, disk); the sender should tell the owner."""
    code = SERVER


def clean_name(b64):
    """The file name from its base64url form, NFC-normalized, or why it is refused: no slash or backslash, no control,
    format (bidi override, zero-width), private-use, surrogate or line-separator characters, no leading dot, no
    trailing space or dot (Windows can't make those), 200 characters at most. The name is shown on the Delivery page
    and later written on another device, so nothing that can disguise or break it gets through."""
    try:
        if not B64URL.match(b64):
            raise ValueError
        name = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise Refused("the name is not base64url-encoded UTF-8")
    name = unicodedata.normalize("NFC", name)    # one name, one spelling
    if not name.strip():
        raise Refused("the name is empty")
    if len(name) > NAME_MAX:
        raise Refused("the name is longer than %d characters" % NAME_MAX)
    if name[0] == ".":
        raise Refused("the name starts with a dot (%r)" % name)
    if name[-1] in " .":
        raise Refused("the name ends with a space or a dot (%r)" % name)
    for c in name:
        if c in "/\\":
            raise Refused("the name holds a slash or backslash (%r)" % name)
        if unicodedata.category(c) in NAME_BAD:
            raise Refused("the name holds a control, invisible or direction-changing character (%r, U+%04X)" % (name, ord(c)))
    return name


def parse(command):
    """SSH_ORIGINAL_COMMAND -> ("ping",) or ("put", route, name, bytes, sha256)."""
    if command is None:
        raise Refused("no command. hermes receive runs only as an ssh forced command: ssh <host> ping, or put ...")
    words = command.split()
    if words == ["ping"]:
        return ("ping",)
    if not words or words[0] != "put":
        raise Refused("unknown command %r; only ping and put <route> <name-b64url> <bytes> <sha256> are accepted"
                      % command[:80])
    if len(words) != 5:
        raise Refused("put takes exactly four arguments: <route> <name-b64url> <bytes> <sha256>")
    route, name, size, digest = words[1:]
    if not config.ROUTE_ID.match(route):
        raise Refused("%r is not a route id" % route)
    if not SIZE.fullmatch(size):
        raise Refused("<bytes> must be a number of plain digits (15 at most), not %r" % size)
    if not SHA256.match(digest.lower()):
        raise Refused("<sha256> must be 64 hex characters")
    return ("put", route, clean_name(name), int(size), digest.lower())


def free_bytes(path):
    return shutil.disk_usage(path).free


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def whois(env):
    """node, user and ip of the sender from tailscale whois on the client address in SSH_CONNECTION. A failure is
    recorded (node and user null, whois_error says why), never fatal: Janus decides what to do with it."""
    ip = (env.get("SSH_CONNECTION") or "").split()[:1]
    info = {"node": None, "user": None, "ip": ip[0] if ip else None}
    if not ip:
        info["whois_error"] = "no SSH_CONNECTION"
        return info
    try:
        r = subprocess.run([env.get("HERMES_TAILSCALE") or TAILSCALE, "whois", "--json", ip[0]],
                           capture_output=True, text=True, timeout=15, env=dict(env))
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or "exit %d" % r.returncode)
        doc = json.loads(r.stdout)
        node = doc["Node"]
        name = node.get("ComputedName") or node.get("Name", "").rstrip(".").split(".")[0]
        info["node"] = name or None
        info["user"] = (doc.get("UserProfile") or {}).get("LoginName") or None
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
        info["whois_error"] = "tailscale whois failed: %s" % (str(e)[:200] or type(e).__name__)
    return info


class Reader:
    """Reads from the sender with an idle timeout. A real stdin is read by its descriptor (select, then os.read, so no
    buffer can hide data from select); a stream without one (a test's BytesIO) is read as it is."""
    def __init__(self, stream, idle):
        self.stream, self.idle = stream, idle
        try:
            self.fd = stream.fileno()
        except (AttributeError, OSError, io.UnsupportedOperation):
            self.fd = None

    def read(self, n):
        if self.fd is None:
            return self.stream.read(n)
        if not select.select([self.fd], [], [], self.idle)[0]:
            raise Failed("no data for %g s; the transfer was given up" % self.idle)
        return os.read(self.fd, n)


def stream_in(stdin, path, declared, stage, idle=IDLE_TIMEOUT):
    """stdin -> path, exactly `declared` bytes, hashed on the way. Raises Failed on a short, long or stalled stream and
    ServerProblem when the drive would drop below the margin."""
    digest = hashlib.sha256()
    got = 0
    reader = Reader(stdin, idle)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never an exec bit
    with os.fdopen(fd, "wb") as f:
        while got < declared:
            chunk = reader.read(min(CHUNK, declared - got))
            if not chunk:
                raise Failed("the stream ended after %d of %d bytes" % (got, declared))
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if got % RECHECK < CHUNK and free_bytes(stage) - (declared - got) < MARGIN:
                raise ServerProblem("the server's drive is nearly full; the file would take it under the %d GB margin"
                                    % (MARGIN // 1024 ** 3))
        if reader.read(1):
            raise Failed("more bytes arrived than the %d declared" % declared)
        f.flush()
        os.fsync(f.fileno())
    return digest.hexdigest()


def put(cfg, device, route_id, name, declared, digest, stdin, env):
    route = cfg.routes.get(route_id)
    if route is None:
        raise Refused("there is no route %r on this server" % route_id)
    if route.outbound:
        raise Refused("route %s goes out to a device; files cannot be put on it" % route_id)
    if not route.enabled:
        raise Refused("route %s is closed" % route_id)
    if route.stage_path is None:
        raise ServerProblem("route %s has no usable stage; the owner should run hermes check-config" % route_id)
    stage = route.stage_path
    if free_bytes(stage) - declared < MARGIN:
        raise ServerProblem("the server's drive is nearly full; a %d-byte file would take it under the %d GB margin"
                            % (declared, MARGIN // 1024 ** 3))
    arrival_id = new_id()
    incoming = stage / "incoming" / arrival_id
    for folder in (incoming, stage / "in", stage / "failed"):
        folder.mkdir(parents=True, exist_ok=True)
    try:
        actual = stream_in(stdin, incoming / "data", declared, stage, idle_timeout(env))
        if actual != digest:
            raise Failed("the file arrived with SHA-256 %s, not the declared %s" % (actual, digest))
    except Exception as e:
        reason = str(e) if isinstance(e, (Failed, ServerProblem)) else "%s: %s" % (type(e).__name__, e)
        set_aside(incoming, stage / "failed" / arrival_id, reason)
        if isinstance(e, (Failed, ServerProblem)):
            raise
        raise Failed(reason)
    meta = {"id": arrival_id, "route": route_id, "direction": "in", "name": name, "bytes": declared, "sha256": digest,
            "sender": {"via": "ssh", "key": device, **whois(env)},
            "received": datetime.now().astimezone().isoformat(timespec="seconds"), "hermes": version()}
    write_json(incoming / "meta.json", meta)
    os.rename(incoming, stage / "in" / arrival_id)      # one step: the door only ever sees a complete folder
    return arrival_id


def idle_timeout(env):
    try:
        return max(0.01, float(env.get("HERMES_IDLE_TIMEOUT") or IDLE_TIMEOUT))
    except ValueError:
        return IDLE_TIMEOUT


def log_error(exc):
    """The traceback goes to the server's own log, never to the sender (sshd hands our stderr to the client)."""
    try:
        path = Path(os.path.expanduser(ERROR_LOG))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s hermes receive\n%s" % (datetime.now().astimezone().isoformat(timespec="seconds"),
                                             "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
    except Exception:
        pass


def set_aside(folder, aside, reason):
    """A failed attempt goes to failed/<id>/ with the reason; nothing is deleted."""
    try:
        (folder / "error.txt").write_text(reason + "\n", encoding="utf-8")
        os.rename(folder, aside)
    except OSError:
        pass


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def receive(device, env=None, stdin=None, out=None):
    """The command: returns the exit status; what the sender should read goes to `out`, problems to stderr."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        os.nice(10)        # it runs under sshd, not as a systemd job with limits
    except OSError:
        pass
    try:
        if not DEVICE.match(device or ""):
            raise ServerProblem("--key %r is not a device name; the authorized_keys line is wrong" % device)
        command = parse(env.get("SSH_ORIGINAL_COMMAND"))
        if command[0] == "ping":
            print("hermes ok " + version(), file=out)
            return OK
        try:
            cfg = config.load(env.get("HERMES_CONFIG"), env.get("JANUS_CONFIG"))
        except config.ConfigError as e:
            raise ServerProblem("the server's routes.json has errors (%s); the owner should run hermes check-config" % e)
        stdin = stdin if stdin is not None else sys.stdin.buffer
        arrival_id = put(cfg, device, *command[1:], stdin, env)
        print("ok " + arrival_id, file=out)
        return OK
    except (Refused, Failed, ServerProblem) as e:
        print("hermes: " + str(e), file=sys.stderr)
        return e.code
    except Exception as e:      # never a traceback to the sender: it would show the server's paths
        log_error(e)
        print("hermes: the server hit an unexpected error; the owner can see it in " + ERROR_LOG, file=sys.stderr)
        return SERVER
