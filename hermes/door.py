"""hermes door: the delivering side (docs/design.md §3 step 2, docs/janus-contract.md, docs/files.md).

hermes-door.path starts it when any stage's in/ is not empty. For every arrival <stage>/in/<id>/ (data + meta.json,
as hermes receive leaves it) the door asks `janus check --json` and acts on the verdict:
    pass       -> the route's `to` (one rename; copy + fsync + rename + remove the staged copy only across drives),
                  the route's conflict rule, exec bits off, the folder's SELinux label, the route's doorbell;
                  the arrival's meta.json and verdict.json go to <stage>/delivered/<id>/
    quarantine -> <stage>/quarantine/<id>/ (data mode 0400, meta.json, verdict.json)   duplicate -> <stage>/skipped/<id>/
    Janus missing, failed, timed out, or an answer that doesn't match its exit code -> <stage>/held/<id>/ (held.txt),
                  retried on every run and by hermes retry; nothing is ever delivered without a pass
Every arrival leaves in/, so the path unit never loops into systemd's trigger limit. One log line per outcome. The door
exits non-zero only when Hermes itself fails (config, disk); a quarantine is Janus doing his job. A run interrupted
halfway is finished by the next one: verdict.json is written into the arrival before anything moves, so a verdict is
never asked twice and a file is never delivered twice. Nothing is deleted: only renames into the set-aside folders or
the dated set-aside of hermes discard."""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from hermes import config, log, receive, version

JANUS = "janus"                      # or $HERMES_JANUS
JANUS_TIMEOUT = 30                   # seconds, plus one per 20 MB of file; $HERMES_JANUS_TIMEOUT overrides (tests)
JANUS_RATE = 20 * 1024 * 1024
VERDICTS = {0: "pass", 10: "quarantine", 11: "duplicate"}
FOLDERS = ("in", "held", "quarantine", "skipped", "replaced", "delivered", "failed", "outbox", "undelivered")
TAILSCALE = "tailscale"              # or $HERMES_TAILSCALE
SEND_TIMEOUT = 60                    # seconds for tailscale file cp before the device counts as asleep, plus one per MB
RETRY_HOURS = 24                     # how long an outbound item waits for a sleeping device when the route says nothing
SET_ASIDE = "~/.local/share/hermes"  # hermes discard moves to <here>/.set-aside-YYYYMMDD-HHMMSS/<id>/
REASON = re.compile(r"[A-Za-z0-9_-]{1,40}")
CONFLICT_SUFFIX = re.compile(r" \((\d+)\)$")


class DoorError(Exception):
    """Hermes's own failure (disk, a folder it cannot write): the door exits non-zero."""


class Held(Exception):
    """Janus did not give a verdict: the arrival waits in held/, never delivered unchecked."""


class HermesVerdict(Exception):
    """Hermes itself quarantines: the arrival is malformed in a way Janus should not have to judge."""


# ---------- Janus ----------
def janus_timeout(nbytes, env):
    try:
        return float(env["HERMES_JANUS_TIMEOUT"])
    except (KeyError, ValueError):
        return JANUS_TIMEOUT + (nbytes or 0) / JANUS_RATE


def ask_janus(arrival, nbytes, env):
    """janus check --json <arrival> -> (verdict, its JSON). Raises Held when there is no trustworthy verdict."""
    binary = env.get("HERMES_JANUS") or JANUS
    timeout = janus_timeout(nbytes, env)
    try:
        r = subprocess.run([binary, "check", "--json", str(arrival)], capture_output=True, text=True, timeout=timeout, env=dict(env))
    except FileNotFoundError:
        raise Held("Janus is not installed (%s not found); the arrival waits" % binary)
    except subprocess.TimeoutExpired:
        raise Held("janus check gave no answer in %g s; the arrival waits" % timeout)
    except OSError as e:
        raise Held("janus check could not run (%s); the arrival waits" % e)
    verdict = VERDICTS.get(r.returncode)
    if verdict is None:
        raise Held("janus check failed (exit %d: %s); the arrival waits" % (r.returncode, (r.stderr or r.stdout).strip()[:200] or "no output"))
    try:
        doc = json.loads(r.stdout)
        if not isinstance(doc, dict) or doc.get("verdict") != verdict or not isinstance(doc.get("reasons"), list):
            raise ValueError
    except ValueError:
        raise Held("janus check exited %d but its answer is not a matching verdict; the arrival waits" % r.returncode)
    return verdict, doc


def hermes_verdict(meta, reason):
    return {"verdict": "quarantine", "reasons": [reason], "sha256": meta.get("sha256"), "janus": None,
            "checks": {"route": "ok", "kind": "ok", "shape": "fail", "integrity": "ok"}, "hermes": version()}


# ---------- where a pass goes ----------
def destination(route, meta):
    """The folder a passed arrival goes to: the route's `to`, plus the arrival's subpath on a subpath route. Every
    name is re-checked here, and the result must sit inside `to`; the door is the boundary and trusts no earlier check."""
    try:
        name = receive.check_name(meta.get("name"))
    except receive.Refused as e:
        raise HermesVerdict("Hermes refused the file's name: %s." % e)
    folder = route.to
    sub = meta.get("subpath")
    if sub:
        if not route.subpath:
            raise HermesVerdict("The arrival names a folder under the destination, but route %s does not allow that." % route.id)
        if str(sub)[:1] in ("/", "\\") or ":" in str(sub):
            raise HermesVerdict("The arrival's folder must be a relative path under the destination, not %r." % sub)
        parts = [p for p in str(sub).replace("\\", "/").split("/") if p]
        try:
            parts = [receive.check_name(p) for p in parts]
        except receive.Refused as e:
            raise HermesVerdict("Hermes refused the folder under the destination: %s." % e)
        folder = route.to.joinpath(*parts)
    if folder != route.to and route.to not in folder.resolve().parents and folder.resolve() != route.to:
        raise HermesVerdict("The arrival's folder would land outside the route's destination.")
    return folder, name


def free_name(folder, name):
    """name, then name (2), name (3) ... until nothing is in the way."""
    final = folder / name
    n = 1
    while final.exists() or final.is_symlink():
        n += 1
        stem, suffix = os.path.splitext(name)
        final = folder / ("%s (%d)%s" % (CONFLICT_SUFFIX.sub("", stem), n, suffix))
    return final


def place(src, dest, same_drive):
    """src -> dest: one rename on the same drive; across drives copy, fsync, rename into place, then remove the staged
    copy (the file exists once, at its destination, before anything is removed)."""
    if same_drive:
        os.rename(src, dest)
        return
    part = dest.with_name(".hermes-%s.part" % src.parent.name)
    with open(src, "rb") as f, open(part, "wb") as g:
        shutil.copyfileobj(f, g, 1024 * 1024)
        g.flush()
        os.fsync(g.fileno())
    os.rename(part, dest)
    os.remove(src)


def file_mode():
    """What a file made here would get (umask), exec bits off."""
    old = os.umask(0)
    os.umask(old)
    return 0o666 & ~old & ~0o111


def selinux_on():
    return os.path.exists("/sys/fs/selinux/enforce") and shutil.which("chcon") is not None


def relabel(folder, path):
    """A moved file keeps its old SELinux label, and containers (qBittorrent, Jellyfin) can't read it: copy the
    destination folder's label onto it. Never fatal."""
    if not selinux_on():
        return
    try:
        subprocess.run(["chcon", "--reference", str(folder), str(path)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def ring(cfg, bell, env, out):
    """The route's doorbell, from a fixed list; config never names a command. Failures are said, never fatal."""
    try:
        DOORBELLS[bell](cfg, env)
    except KeyError:
        print("hermes door: no doorbell named %r" % bell, file=out)
    except Exception as e:
        print("hermes door: doorbell %s failed: %s" % (bell, str(e)[:200]), file=out)


def ring_jellyfin(cfg, env):
    """POST /Library/Refresh on the Jellyfin of fetch.jellyfin-themes (its key_file holds the API key)."""
    job = cfg.fetch.get("jellyfin-themes") or {}
    base = (job.get("jellyfin") or "").rstrip("/")
    key_file = job.get("key_file")
    if not base or not key_file:
        raise ValueError("fetch.jellyfin-themes needs jellyfin and key_file in routes.json")
    key = Path(os.path.expanduser(key_file)).read_text(encoding="utf-8").strip()
    req = urllib.request.Request(base + "/Library/Refresh", method="POST", headers={"X-Emby-Token": key}, data=b"")
    urllib.request.urlopen(req, timeout=10).read()


DOORBELLS = {"jellyfin-refresh": ring_jellyfin}


# ---------- outbound: Taildrop ----------
def taildrop_send(path, device, nbytes, env):
    """tailscale file cp <path> <device>: -> (sent, why not). A device that is asleep makes cp fail or hang."""
    timeout = SEND_TIMEOUT + (nbytes or 0) / (1024 * 1024)
    try:
        r = subprocess.run([env.get("HERMES_TAILSCALE") or TAILSCALE, "file", "cp", str(path), device + ":"],
                           capture_output=True, text=True, timeout=timeout, env=dict(env))
    except FileNotFoundError:
        return False, "tailscale is not installed"
    except subprocess.TimeoutExpired:
        return False, "tailscale file cp gave no answer in %g s; %s is probably asleep" % (timeout, device)
    except OSError as e:
        return False, "tailscale file cp could not run: %s" % e
    if r.returncode == 0:
        return True, ""
    return False, "tailscale file cp failed (exit %d): %s" % (r.returncode, (r.stderr or r.stdout).strip()[:200] or "no output")


def first_try(arrival):
    try:
        stamp = (arrival / "tries.txt").read_text(encoding="utf-8").split(None, 1)[0]
        return datetime.fromisoformat(stamp)
    except (OSError, ValueError, IndexError):
        return None


def deliver_out(stage, arrival, meta, route, doc, env, out, outcome="sent", **more):
    """A passed outbound arrival to its device by Taildrop, as <prefix><name>. Sent: Hermes removes its own staging
    link and the folder goes to delivered/. Not sent (the device is asleep): the folder waits in outbox/ and
    hermes collect tries again on every run, for the route's retry_hours; then it is undelivered. Returns the word."""
    name = route.prefix + receive.check_name(meta.get("name"))
    staged = arrival / name
    if not staged.exists() and (arrival / "data").exists():
        os.rename(arrival / "data", staged)                  # Taildrop sends a file under its own name
    if staged.exists():
        sent, why = taildrop_send(staged, route.device, meta.get("bytes"), env)
    else:
        sent, why = True, ""                                 # an interrupted run removed the link after the send
    where = "taildrop:" + route.device
    if sent:
        if staged.exists():
            os.remove(staged)                                # Hermes's own link, after Tailscale confirmed the send
        if not log.seen(meta.get("id"), outcome):
            log.write(entry(meta, outcome, to=where, name=name, verdict=doc.get("verdict"), reasons=doc.get("reasons") or [], **more))
        move(arrival, stage / "delivered" / arrival.name)
        return outcome
    note(arrival, "tries.txt", why)
    hours = route.retry_hours or RETRY_HOURS
    since = first_try(arrival) or datetime.now().astimezone()
    if (datetime.now().astimezone() - since).total_seconds() > hours * 3600:
        note(arrival, "tries.txt", "%s was unreachable for %g hours." % (route.device, hours))
        move(arrival, stage / "undelivered" / arrival.name)
        log.write(entry(meta, "undelivered", to=where, name=name, verdict=doc.get("verdict"),
                        reasons=[why, "%s was unreachable for %g hours." % (route.device, hours)]))
        return "undelivered"
    if arrival.parent.name != "outbox":
        move(arrival, stage / "outbox" / arrival.name)
        log.write(entry(meta, "waiting", to=where, name=name, verdict=doc.get("verdict"), reasons=[why]))
    return "waiting"


# ---------- one arrival ----------
def read_meta(arrival):
    doc = log.read_json(arrival / "meta.json")
    return doc if isinstance(doc, dict) and isinstance(doc.get("route"), str) else None


def move(src, dest):
    """One rename into a set-aside folder (made first). Raises DoorError when the drive refuses."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dest)
    except OSError as e:
        raise DoorError("cannot move %s to %s: %s" % (src, dest, e))


def note(folder, name, text):
    with open(folder / name, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (log.now(), text))


def entry(meta, outcome, **more):
    sender = meta.get("sender") or {}
    return {"id": meta.get("id"), "route": meta.get("route"), "dir": meta.get("direction", "in"),
            "from": sender.get("key") or sender.get("fetch") or sender.get("via"), "to": None, "name": meta.get("name"),
            "bytes": meta.get("bytes"), "verdict": None, "outcome": outcome, "reasons": [], **more}


def deliver(cfg, stage, arrival, meta, route, doc, env, out, outcome="delivered", **more):
    """A passed (or released) arrival into its destination, then the folder into delivered/. Idempotent: a run that
    was interrupted after the data moved finishes here without moving or logging twice."""
    folder, name = destination(route, meta)
    data = arrival / "data"
    final = None
    if data.exists():
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / name
        if final.exists() or final.is_symlink():
            if route.conflict == "skip":
                move(arrival, stage / "skipped" / arrival.name)
                log.write(entry(meta, "skipped", to=config.short(final), verdict=doc.get("verdict"),
                                reasons=["A file of that name is already there; the route skips it."]))
                return "skipped"
            if route.conflict == "rename":
                final = free_name(folder, name)
            else:                                            # replace: the older copy is set aside first, never deleted
                move(final, stage / "replaced" / arrival.name / name)
        place(data, final, route.same_drive is not False)
        os.chmod(final, file_mode())
        relabel(folder, final)
        if route.after:
            ring(cfg, route.after, env, out)
    else:
        final = folder / name                                # the data moved in an interrupted run
    if not log.seen(meta.get("id"), outcome):
        log.write(entry(meta, outcome, to=config.short(final), verdict=doc.get("verdict"), reasons=doc.get("reasons") or [], **more))
    move(arrival, stage / "delivered" / arrival.name)
    return outcome


def process(cfg, stage, arrival, env, out):
    """One arrival in <stage>/in/<id>/: a verdict (Janus's, a saved one, or Hermes's own), then the outcome.
    Returns the outcome word; raises DoorError only for Hermes's own failures."""
    aid = arrival.name
    meta = read_meta(arrival)
    if meta is None:
        note(arrival, "error.txt", "no readable meta.json; not an arrival Hermes made")
        move(arrival, stage / "failed" / aid)
        log.write({"id": aid, "route": None, "dir": "in", "from": None, "to": None, "name": None, "bytes": None,
                   "verdict": None, "outcome": "failed", "reasons": ["The arrival has no readable meta.json."]})
        return "failed"
    route = cfg.routes.get(meta["route"])
    doc = log.read_json(arrival / "verdict.json")
    if not isinstance(doc, dict) or doc.get("verdict") not in VERDICTS.values():
        try:
            if route is None:
                raise HermesVerdict("There is no route %r on this server any more." % meta["route"])
            if route.outbound != (meta.get("direction") == "out"):
                raise HermesVerdict("The arrival goes %s, but route %s goes %s." % (
                    "out" if meta.get("direction") == "out" else "in", route.id, "out to a device" if route.outbound else "to a folder here"))
            if route.outbound:                               # a bad name is quarantined before Janus works on it
                try:
                    receive.check_name(meta.get("name"))
                except receive.Refused as e:
                    raise HermesVerdict("Hermes refused the file's name: %s." % e)
            else:
                destination(route, meta)
            verdict, doc = ask_janus(arrival, meta.get("bytes"), env)
        except Held as e:
            note(arrival, "held.txt", str(e))
            move(arrival, stage / "held" / aid)
            log.write(entry(meta, "held", reasons=[str(e)]))
            return "held"
        except HermesVerdict as e:
            doc = hermes_verdict(meta, str(e))
        receive.write_json(arrival / "verdict.json", doc)   # from here on, a crash never asks Janus again
    verdict = doc["verdict"]
    if verdict == "pass":
        if route.outbound:
            return deliver_out(stage, arrival, meta, route, doc, env, out)
        return deliver(cfg, stage, arrival, meta, route, doc, env, out)
    if verdict == "quarantine":
        if (arrival / "data").exists() and meta.get("direction") != "out":   # an outbound data is a hardlink to the original
            os.chmod(arrival / "data", 0o400)
        move(arrival, stage / "quarantine" / aid)
        log.write(entry(meta, "quarantined", verdict=verdict, reasons=doc.get("reasons") or []))
        return "quarantined"
    move(arrival, stage / "skipped" / aid)
    log.write(entry(meta, "skipped", verdict=verdict, reasons=doc.get("reasons") or []))
    return "skipped"


# ---------- a run ----------
def load(env):
    try:
        return config.load(env.get("HERMES_CONFIG"), env.get("JANUS_CONFIG"))
    except config.ConfigError as e:
        raise DoorError("%s; nothing was delivered" % e)


def run(env=None, out=None, only=None, cfg=None, quiet=False):
    """hermes door: every arrival in every stage's in/ (held/ first, for another try), or just the ids in `only`.
    One door per stage at a time (a lock file), so hermes send, hermes collect and the path unit never race.
    Returns the exit status."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = cfg or load(env)
    except DoorError as e:
        print("hermes door: %s" % e, file=sys.stderr)
        return 1
    outcomes, failures = Counter(), 0
    for stage in cfg.stages.values():
        try:
            for name in FOLDERS:
                (stage / name).mkdir(exist_ok=True)
            lock = open(stage / ".door.lock", "w")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError as e:
            print("hermes door: cannot make the folders in %s: %s" % (stage, e), file=sys.stderr)
            failures += 1
            continue
        try:
            outcomes_here, failures_here = run_stage(cfg, stage, env, out, only)
        finally:
            lock.close()
        outcomes.update(outcomes_here)
        failures += failures_here
    log.update_alert(cfg)
    words = ", ".join("%d %s" % (n, w) for w, n in sorted(outcomes.items())) or "nothing waiting"
    if not quiet:
        print("hermes door: %s%s" % (words, "; %d failure%s" % (failures, "" if failures == 1 else "s") if failures else ""), file=out)
    return 1 if failures else 0


def run_stage(cfg, stage, env, out, only):
    outcomes, failures = Counter(), 0
    if True:
        for held in sorted((stage / "held").iterdir()):
            if held.is_dir() and (only is None or held.name in only) and not (stage / "in" / held.name).exists():
                try:
                    os.rename(held, stage / "in" / held.name)
                except OSError as e:
                    print("hermes door: cannot retry %s: %s" % (held.name, e), file=sys.stderr)
        for arrival in sorted((stage / "in").iterdir()):
            if only is not None and arrival.name not in only:
                continue
            if not arrival.is_dir():                         # a stray file is not an arrival; it still leaves in/
                try:
                    move(arrival, stage / "failed" / arrival.name)
                except DoorError as e:
                    print("hermes door: %s" % e, file=sys.stderr)
                    failures += 1
                continue
            try:
                outcomes[process(cfg, stage, arrival, env, out)] += 1
            except Exception as e:                           # Hermes's own failure: say so, hold the arrival, go on
                failures += 1
                if not isinstance(e, DoorError):
                    log.error(e, "door")
                print("hermes door: %s: %s" % (arrival.name, e if isinstance(e, DoorError) else "unexpected error, see the door log"), file=sys.stderr)
                try:
                    if arrival.exists():
                        note(arrival, "held.txt", "Hermes failed: %s" % e)
                        os.rename(arrival, stage / "held" / arrival.name)
                except OSError:
                    pass
    return outcomes, failures


# ---------- retry, release, discard ----------
def find(cfg, aid, folders):
    """-> (stage, folder, path) of an arrival that sits in one of the folders. DoorError when it is nowhere."""
    for stage in cfg.stages.values():
        for folder in folders:
            if (stage / folder / aid).is_dir():
                return stage, folder, stage / folder / aid
    raise DoorError("no arrival %s in %s" % (aid, ", ".join(folders)))


def retry(aid, env=None, out=None):
    """hermes retry <id>: a held arrival goes through the door again now."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = load(env)
        stage, folder, path = find(cfg, aid, ("held",))
    except DoorError as e:
        print("hermes retry: %s" % e, file=sys.stderr)
        return 1
    return run(env, out, only={aid})


def release(aid, reason, env=None, out=None):
    """hermes release <id> --reason <word>: deliver a quarantined (or held) arrival anyway, and log who did it."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        if not REASON.fullmatch(reason or ""):
            raise DoorError("--reason must be one word (letters, digits, - or _), e.g. --reason checked-by-hand")
        cfg = load(env)
        stage, folder, path = find(cfg, aid, ("quarantine", "held"))
        meta = read_meta(path)
        if meta is None:
            raise DoorError("%s has no readable meta.json; it cannot be delivered" % aid)
        route = cfg.routes.get(meta["route"])
        if route is None or route.outbound != (meta.get("direction") == "out"):
            raise DoorError("route %r does not fit arrival %s (direction %s); it cannot be delivered" % (meta["route"], aid, meta.get("direction")))
        try:
            if route.outbound:
                receive.check_name(meta.get("name"))
            else:
                destination(route, meta)
        except (HermesVerdict, receive.Refused) as e:
            raise DoorError("%s cannot be released: %s" % (aid, e))
        w = receive.whois(env)                                # who released it: the ssh user, and whois on the connection
        who = {"user": env.get("USER") or env.get("LOGNAME"), "reason": reason, "node": w["node"], "login": w["user"], "ip": w["ip"]}
        doc = log.read_json(path / "verdict.json") or hermes_verdict(meta, "released without a verdict")
        doc["released"] = {"at": log.now(), **who}
        receive.write_json(path / "verdict.json", doc)
        if (path / "data").exists() and meta.get("direction") != "out":
            os.chmod(path / "data", 0o600)
        if route.outbound:
            outcome = deliver_out(stage, path, meta, route, doc, env, out, outcome="released", released_by=who)
        else:
            outcome = deliver(cfg, stage, path, meta, route, doc, env, out, outcome="released", released_by=who)
        log.update_alert(cfg)
        print("hermes release: %s %s (%s)" % (aid, outcome, reason), file=out)
        return 0
    except Exception as e:
        if not isinstance(e, DoorError):
            log.error(e, "door")
        print("hermes release: %s" % (e if isinstance(e, DoorError) else "unexpected error, see the door log"), file=sys.stderr)
        return 1


def discard(aid, env=None, out=None):
    """hermes discard <id>: a set-aside arrival moves to ~/.local/share/hermes/.set-aside-<date>-<time>/<id>/.
    Nothing is deleted; Poseidon's cleanup looks there later."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        cfg = load(env)
        stage, folder, path = find(cfg, aid, ("quarantine", "held", "skipped", "failed", "replaced", "outbox", "undelivered"))
        meta = read_meta(path) or {"id": aid, "route": None}
        aside = Path(os.path.expanduser(SET_ASIDE)) / time.strftime(".set-aside-%Y%m%d-%H%M%S") / aid
        move(path, aside)
        log.write(entry(meta, "discarded", to=config.short(aside), reasons=["Moved out of %s by hermes discard." % folder]))
        log.update_alert(cfg)
        print("hermes discard: %s moved to %s" % (aid, config.short(aside)), file=out)
        return 0
    except Exception as e:
        if not isinstance(e, DoorError):
            log.error(e, "door")
        print("hermes discard: %s" % (e if isinstance(e, DoorError) else "unexpected error, see the door log"), file=sys.stderr)
        return 1
