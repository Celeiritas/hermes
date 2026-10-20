"""Both ways (docs/design.md §3 steps 3–5, docs/both-ways.md): hermes send (this server -> a device by Taildrop),
hermes message (a note, link or magnet as a small arrival) and hermes collect (hermes-taildrop.timer: retry the
outbox, then pick up the phones' Taildrops). Everything goes through the same door and the same Janus check."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from hermes import config, door, log, receive, version

MESSAGE_MAX = 4096
MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9A-Za-z]{32,40})", re.I)
URL = re.compile(r"^https?://\S+$")
TAILDROP_DIR = "taildrop"            # <home stage>/incoming/taildrop/: where tailscale file get puts the phones' files
GET_TIMEOUT = 3600                   # a phone's video can be big


def tailscale(env):
    return env.get("HERMES_TAILSCALE") or door.TAILSCALE


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_arrival(route, name, src=None, data=None, sender=None, take=False, **more):
    """A new arrival in the route's stage: the file as a hardlink to src (a copy across drives; the original is never
    touched), moved in when take=True (the taildrop pickup folder is Hermes's own), or the given bytes; then meta.json;
    then one rename into in/. Returns the id."""
    stage = route.stage_path
    aid = receive.new_id()
    incoming = stage / "incoming" / aid
    incoming.mkdir(parents=True)
    (stage / "in").mkdir(exist_ok=True)
    target = incoming / "data"
    if src is not None:
        try:
            os.rename(src, target) if take else os.link(src, target)
        except OSError:                                      # another drive: copy, fsync
            with open(src, "rb") as f, open(target, "wb") as g:
                shutil.copyfileobj(f, g, 1 << 20)
                g.flush()
                os.fsync(g.fileno())
            if take:
                os.remove(src)
    else:
        with open(target, "wb") as g:
            g.write(data)
            g.flush()
            os.fsync(g.fileno())
    meta = {"id": aid, "route": route.id, "direction": "out" if route.outbound else "in", "name": name,
            "bytes": os.path.getsize(target), "sha256": sha256_of(target), "sender": sender,
            "received": log.now(), "hermes": version(), **more}
    receive.write_json(incoming / "meta.json", meta)
    os.rename(incoming, stage / "in" / aid)
    return aid


def local_sender(env, user=None):
    return {"via": "local", "key": None, "node": None, "user": user or env.get("USER") or env.get("LOGNAME")}


def report(cfg, ids, out):
    """After the door ran: one line per arrival saying where it ended up. Returns how many did not go through."""
    bad = 0
    for aid, name in ids.items():
        try:
            stage, folder, path = door.find(cfg, aid, ("delivered", "outbox", "quarantine", "held", "undelivered", "skipped", "failed", "in"))
        except door.DoorError:
            folder, path = "lost", None
        meta = door.read_meta(path) if path else None
        route = cfg.routes.get(meta["route"]) if meta else None
        verdict = log.read_json(path / "verdict.json") if path else None
        reasons = " ".join((verdict or {}).get("reasons") or [])
        if folder == "delivered":
            word = "sent to %s" % route.device if route and route.outbound else "delivered"
        elif folder == "outbox":
            word = "waiting for %s (asleep?); hermes collect keeps trying for %g hours" % (route.device, route.retry_hours or door.RETRY_HOURS)
        elif folder == "quarantine":
            word, bad = "quarantined: " + reasons, bad + 1
        elif folder == "held":
            word, bad = "held: " + " ".join(log.items(cfg, "held")[0]["reasons"][-1:]) if log.items(cfg, "held") else "held", bad + 1
        elif folder == "skipped":
            word, bad = "skipped: " + reasons, bad + 1
        else:
            word, bad = folder, bad + 1
        print("%s: %s" % (name, word), file=out)
    return bad


# ---------- hermes send ----------
def send(route_id, files, env=None, out=None):
    """hermes send <route> <files...>: stage each file (a hardlink; the original is never touched) and run the door."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = door.load(env)
        route = cfg.routes.get(route_id)
        if route is None:
            raise door.DoorError("there is no route %r; hermes check-config lists them" % route_id)
        if not route.outbound:
            raise door.DoorError("route %s delivers on this server; hermes send is for routes that go out to a device" % route_id)
        if not route.enabled:
            raise door.DoorError("route %s is closed" % route_id)
        if not files:
            raise door.DoorError("nothing to send: hermes send <route> <files...>")
    except door.DoorError as e:
        print("hermes send: %s" % e, file=sys.stderr)
        return 2
    ids, bad = {}, 0
    for f in files:
        p = Path(f)
        if not p.is_file():
            print("hermes send: %s is not a file; skipped" % f, file=sys.stderr)
            bad += 1
            continue
        try:
            name = receive.check_name(p.name)
        except receive.Refused as e:
            print("hermes send: %s refused: %s" % (f, e), file=sys.stderr)
            bad += 1
            continue
        try:
            ids[stage_arrival(route, name, src=p.resolve(), sender=local_sender(env), source=str(p.resolve()))] = name
        except OSError as e:
            print("hermes send: %s: cannot stage it: %s" % (f, e), file=sys.stderr)
            bad += 1
    if ids:
        door.run(env, out, only=set(ids), cfg=cfg, quiet=True)
        bad += report(cfg, ids, out)
    return 1 if bad else 0


# ---------- hermes message ----------
def message(route_id, text, env=None, out=None, user=None):
    """hermes message <route>, the text on stdin (4 KB at most): a magnet link becomes <hash8>.magnet, a URL
    link-<time>.txt, anything else note-<time>.txt; then the door, like any file."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = door.load(env)
        route = cfg.routes.get(route_id)
        if route is None:
            raise door.DoorError("there is no route %r; hermes check-config lists them" % route_id)
        if not route.enabled:
            raise door.DoorError("route %s is closed" % route_id)
        if len(text) > MESSAGE_MAX:
            raise door.DoorError("a message is %d bytes at most; this one is %d" % (MESSAGE_MAX, len(text)))
        try:
            s = text.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise door.DoorError("a message is UTF-8 text")
        if not s:
            raise door.DoorError("the message is empty")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if s.lower().startswith("magnet:?"):
            m = MAGNET_HASH.search(s)
            kind, name = "magnet", (m.group(1).lower() if m else hashlib.sha256(s.encode()).hexdigest())[:8] + ".magnet"
        elif "\n" not in s and URL.match(s):
            kind, name = "link", "link-%s.txt" % stamp
        else:
            kind, name = "note", "note-%s.txt" % stamp
        aid = stage_arrival(route, name, data=(s + "\n").encode("utf-8"), sender=local_sender(env, user), message={"type": kind})
    except door.DoorError as e:
        print("hermes message: %s" % e, file=sys.stderr)
        return 2
    door.run(env, out, only={aid}, cfg=cfg, quiet=True)
    return 1 if report(cfg, {aid: name}, out) else 0


# ---------- hermes collect ----------
def taildrop_route(cfg, filename):
    """The route a phone's file takes: taildrop.by_kind by its ending, else taildrop.default."""
    kinds = cfg.taildrop.get("by_kind") or {}
    rid = kinds.get(os.path.splitext(filename)[1].lower()) or cfg.taildrop.get("default")
    route = cfg.routes.get(rid)
    return route if route and route.enabled and not route.outbound else None


def retry_outbox(cfg, env, out):
    counts = Counter()
    for stage in cfg.stages.values():
        outbox = stage / "outbox"
        if not outbox.is_dir():
            continue
        for arrival in sorted(outbox.iterdir()):
            if not arrival.is_dir():
                continue
            meta = door.read_meta(arrival)
            route = cfg.routes.get(meta["route"]) if meta else None
            try:
                if meta is None or route is None or not route.outbound:
                    door.note(arrival, "tries.txt", "the route is gone from routes.json")
                    door.move(arrival, stage / "undelivered" / arrival.name)
                    log.write(door.entry(meta or {"id": arrival.name}, "undelivered", reasons=["The route is gone from routes.json."]))
                    counts["undelivered"] += 1
                    continue
                doc = log.read_json(arrival / "verdict.json") or {}
                counts[door.deliver_out(stage, arrival, meta, route, doc, env, out)] += 1
            except Exception as e:
                log.error(e, "collect")
                print("hermes collect: %s: %s" % (arrival.name, e if isinstance(e, door.DoorError) else "unexpected error, see the collect log"), file=sys.stderr)
                counts["failures"] += 1
    return counts


def collect(env=None, out=None):
    """hermes collect (hermes-taildrop.timer, Collect now): retry the outbox, then `tailscale file get` without --wait
    (it returns at once when nothing waits) and hand every file to the door. A run with nothing waiting writes nothing."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = door.load(env)
    except door.DoorError as e:
        print("hermes collect: %s" % e, file=sys.stderr)
        return 1
    counts = retry_outbox(cfg, env, out)
    home = cfg.stages.get("home") or next(iter(cfg.stages.values()))
    pickup = home / "incoming" / TAILDROP_DIR
    failures = counts.pop("failures", 0)
    try:
        pickup.mkdir(parents=True, exist_ok=True)              # made once; stays, so a quiet run writes nothing
        r = subprocess.run([tailscale(env), "file", "get", "--conflict=rename", str(pickup)],
                           capture_output=True, text=True, timeout=GET_TIMEOUT, env=dict(env))
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip()[:200] or "exit %d" % r.returncode)
    except (OSError, subprocess.SubprocessError) as e:
        print("hermes collect: tailscale file get failed: %s" % (e if not isinstance(e, FileNotFoundError) else "tailscale is not installed"), file=sys.stderr)
        failures += 1
    ids = {}
    for f in sorted(pickup.iterdir()) if pickup.is_dir() else []:
        if not f.is_file():
            continue
        route = taildrop_route(cfg, f.name)
        if route is None:
            aid = receive.new_id()
            door.move(f, home / "failed" / aid / "data")
            door.note(home / "failed" / aid, "error.txt", "no taildrop route for %r and no default; set taildrop.default in routes.json" % os.path.splitext(f.name)[1])
            log.write({"id": aid, "route": None, "dir": "in", "from": "taildrop", "to": None, "name": f.name, "bytes": None,
                       "verdict": None, "outcome": "failed", "reasons": ["No route for a Taildrop of that kind."]})
            failures += 1
            continue
        sender = {"via": "taildrop", "key": None, "node": None, "user": None}
        ids[stage_arrival(route, f.name, src=f, sender=sender, take=True)] = f.name
    if ids:
        door.run(env, out, only=set(ids), cfg=cfg, quiet=True)
        for aid in ids:
            try:
                folder = door.find(cfg, aid, ("delivered", "quarantine", "held", "skipped", "failed"))[1]
                counts[{"quarantine": "quarantined"}.get(folder, folder)] += 1
            except door.DoorError:
                counts["lost"] += 1
    if ids or counts or failures:
        log.update_alert(cfg)
    words = ", ".join("%d %s" % (n, w) for w, n in sorted(counts.items()))
    print("hermes collect: %s%s" % (words or "nothing waiting", "; %d failure%s" % (failures, "" if failures == 1 else "s") if failures else ""), file=out)
    return 1 if failures else 0
