"""Hermes's records (docs/files.md): one JSON line per outcome in ~/.local/share/hermes/log/deliveries-YYYY-MM.jsonl,
`hermes log` and `hermes status --json` over them and over the stage folders, the Argus alert file while quarantined
or held items are unseen, and `hermes ack` that clears it. Nothing here deletes a delivery."""
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path

from hermes import config, version

LOG_DIR = "~/.local/share/hermes/log"
STATE = "~/.local/state/hermes/seen.json"
ALERT = "~/.local/state/argus-ui/alerts/hermes.json"
LISTED = ("quarantine", "held", "undelivered", "outbox", "failed", "skipped")   # what the Delivery page shows
ALERTING = ("quarantine", "held", "undelivered")
WORDS = {"quarantine": "quarantined", "held": "held", "undelivered": "undelivered", "outbox": "waiting to be sent",
         "failed": "failed transfers"}
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?([+-]\d\d:\d\d|Z)? ")


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_dir():
    return Path(os.path.expanduser(LOG_DIR))


def write(entry):
    """Append one outcome line; returns the entry with its time."""
    entry = {"t": now(), **entry}
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ("deliveries-%s.jsonl" % entry["t"][:7]), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return entry


def files():
    return sorted(log_dir().glob("deliveries-????-??.jsonl"))


def entries(limit=None, last_files=None):
    """The log lines, oldest first (the last `limit` of them, over the last `last_files` months)."""
    out = []
    for path in files()[-last_files:] if last_files else files():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:] if limit else out


def seen(arrival_id, outcome):
    """Was this outcome already logged for the arrival? (A run interrupted after logging must not log it twice.)"""
    return any(e.get("id") == arrival_id and e.get("outcome") == outcome for e in entries(last_files=2))


def error(exc, command):
    """A traceback to ~/.local/share/hermes/log/<command>-errors.log; writing it never fails the command."""
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / (command + "-errors.log"), "a", encoding="utf-8") as f:
            f.write("%s hermes %s\n%s" % (now(), command, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
    except Exception:
        pass


def size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def line(e):
    """One log entry for people: time, route, from → to, name, size, outcome and the reasons."""
    where = e.get("to") or ""
    s = "%s  %-14s %s → %s  %s  %s  %s" % (e.get("t", "")[:16].replace("T", " "), e.get("route") or "?",
                                           e.get("from") or "?", where, e.get("name") or "", size(e.get("bytes")), e.get("outcome"))
    if e.get("reasons"):
        s += "  (" + " ".join(e["reasons"]) + ")"
    return s


# ---------- status ----------
def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def items(cfg, folder):
    """What waits in <stage>/<folder>/ across the stages: id, route, name, bytes, since and the reasons."""
    out = []
    for stage_name, stage in cfg.stages.items():
        d = stage / folder
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_dir():
                continue
            meta = read_json(p / "meta.json") or {}
            verdict = read_json(p / "verdict.json") or {}
            reasons = list(verdict.get("reasons") or [])
            note = {"held": "held.txt", "failed": "error.txt", "outbox": "tries.txt", "undelivered": "tries.txt"}.get(folder)
            if note and (p / note).is_file():         # the last note, without its time stamp
                lines = [STAMP.sub("", l) for l in (p / note).read_text(encoding="utf-8").splitlines() if l.strip()]
                reasons += lines[-1:]
            out.append({"id": p.name, "stage": stage_name, "folder": folder, "route": meta.get("route"),
                        "name": meta.get("name"), "bytes": meta.get("bytes"),
                        "from": (meta.get("sender") or {}).get("key") or (meta.get("sender") or {}).get("via"),
                        "since": meta.get("received") or datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                        "reasons": reasons, "path": str(p)})
    return out


def status(cfg):
    """What hermes status --json prints: today's counts, every set-aside item, and the alert."""
    today = now()[:10]
    counts = {}
    for e in entries(last_files=1):
        if e.get("t", "").startswith(today):
            counts[e.get("outcome")] = counts.get(e.get("outcome"), 0) + 1
    return {"hermes": version(), "at": now(), "today": counts,
            **{folder: items(cfg, folder) for folder in LISTED},
            "alert": read_json(os.path.expanduser(ALERT))}


def summary(st):
    """The status line the Delivery page and Elaia's Home page show."""
    parts = ["%d %s today" % (n, word) for word, n in sorted(st["today"].items())] or ["nothing today"]
    for folder in ALERTING + ("outbox", "failed"):
        if st[folder]:
            parts.append("%d %s" % (len(st[folder]), WORDS[folder]))
    return " · ".join(parts)


# ---------- the alert file ----------
def seen_ids():
    doc = read_json(os.path.expanduser(STATE)) or {}
    return set(doc.get("ids") or [])


def write_seen(ids):
    path = Path(os.path.expanduser(STATE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": sorted(ids), "at": now()}) + "\n", encoding="utf-8")


def update_alert(cfg):
    """~/.local/state/argus-ui/alerts/hermes.json while quarantine/ or held/ hold items the owner hasn't seen
    (hermes ack marks them seen); removed when there are none. Returns the alert or None."""
    waiting = [i for folder in ALERTING for i in items(cfg, folder)]
    seen = seen_ids() & {i["id"] for i in waiting}
    write_seen(seen)                       # forget ids that left the folders
    unseen = [i for i in waiting if i["id"] not in seen]
    path = Path(os.path.expanduser(ALERT))
    if not unseen:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None
    parts = []
    for folder, fallback in (("quarantine", "see the Delivery page"), ("held", "Janus did not answer"), ("undelivered", "the device stayed asleep")):
        these = [i for i in unseen if i["folder"] == folder]
        if these:
            parts.append("%d deliver%s %s: %s" % (len(these), "y" if len(these) == 1 else "ies", WORDS[folder],
                                                  these[0]["reasons"][-1] if these[0]["reasons"] else fallback))
    alert = {"job": "hermes-door", "level": "alert", "since": min(i["since"] for i in unseen), "message": "; ".join(parts), "link": "delivery"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alert) + "\n", encoding="utf-8")
    return alert


def ack(cfg):
    """hermes ack: everything waiting now counts as seen; the alert goes away until something new arrives."""
    waiting = {i["id"] for folder in ALERTING for i in items(cfg, folder)}
    write_seen(waiting)
    update_alert(cfg)
    return len(waiting)
