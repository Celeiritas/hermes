"""Hermes · Delivery, the Elaia server plugin `delivery` (docs/design.md §6, issue #11).

Reads use Hermes's own code (hermes.log, hermes.config) from the installed one-file hermes, which is a zip that Python can
import, so the page and `hermes status` never disagree about what a delivery is. Reads only read files: no subprocess.
Every write runs a fixed command with fixed arguments (never a shell) and returns what it said: `hermes message`,
`hermes ack`, or `systemctl --user start` for an allowlisted job. The plugin never decides and never releases
(issue #27 adds Release through Zeus).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
JOBS = {"hermes-taildrop": "Collect phone mail", "hermes-decks": "Fetch decks now", "hermes-github": "Check now",
        "hermes-jellyfin": "Fetch theme songs now"}      # the only jobs the page may start (docs/design.md §6)
MESSAGE_MAX = 4096
RECENT = 60            # log lines the page gets, newest first


def hermes_bin():
    """The hermes command: $HERMES_BIN (tests), else on PATH, else ~/.local/bin/hermes (Elaia's unit may have a short PATH)."""
    return os.environ.get("HERMES_BIN") or shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")


_lib = None


def lib():
    """(hermes.config, hermes.log) from $HERMES_LIB (a checkout, in tests) or the installed hermes zipapp.
    Python caches the zip's contents: a new hermes is read after Elaia restarts (the install does that)."""
    global _lib
    if _lib:
        return _lib
    src = os.environ.get("HERMES_LIB") or hermes_bin()
    if not os.path.exists(src):
        raise ImportError("Hermes is not installed on this machine (no %s)." % src)
    if src not in sys.path:
        sys.path.insert(0, src)
    from hermes import config, log
    _lib = (config, log)
    return _lib


def _meta(item):
    try:
        doc = json.loads((Path(item["path"]) / "meta.json").read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _waiting(item):
    """One set-aside item for the page: no server paths, plus the SHA-256 from its meta.json."""
    out = {k: item.get(k) for k in ("id", "stage", "folder", "route", "name", "bytes", "from", "since", "reasons")}
    out["sha256"] = _meta(item).get("sha256")
    return out


def _routes(config, cfg):
    """The open routes the send box offers, each with where it goes in plain words."""
    out = []
    for rid, r in cfg.routes.items():
        if not r.enabled:
            continue
        where = ("%s by Taildrop" % r.device) if r.device else config.short(r.to) if r.to else ""
        out.append({"id": rid, "to": where})
    return out


def _fetchers(cfg, recent):
    """The jobs the page can start: phone mail always, then each fetcher in routes.json whose job is allowlisted,
    with the newest delivery on its route."""
    out = [{"job": "hermes-taildrop", "label": JOBS["hermes-taildrop"], "what": "Picks up the phones' Taildrops and retries the outbox.", "last": None}]
    for name, f in (cfg.fetch or {}).items():
        job = f.get("job") if isinstance(f, dict) else None
        if job in JOBS and job != "hermes-taildrop":
            last = next((e for e in recent if e.get("route") == f.get("route")), None)
            out.append({"job": job, "label": JOBS[job], "what": "%s, into route %s." % (name, f.get("route")),
                        "last": {"t": last.get("t"), "name": last.get("name"), "outcome": last.get("outcome")} if last else None})
    return out


def snapshot():
    """Everything the page shows, from Hermes's files. Nothing is written."""
    try:
        config, log = lib()
    except ImportError as e:
        return {"ok": False, "problem": str(e), "line": "Hermes is not installed here"}
    try:
        cfg = config.load()
    except config.ConfigError as e:
        return {"ok": False, "problem": "routes.json has errors: %s" % str(e).strip().splitlines()[0][:300], "line": "Hermes's routes.json has errors"}
    st = log.status(cfg)
    recent = list(reversed(log.entries(limit=RECENT, last_files=2)))
    keep = ("t", "id", "route", "dir", "from", "to", "name", "bytes", "outcome", "reasons")
    return {"ok": True, "hermes": st.get("hermes"), "at": st.get("at"), "line": log.summary(st), "today": st.get("today"),
            "recent": [{k: e.get(k) for k in keep} for e in recent],
            **{f: [_waiting(i) for i in st.get(f) or []] for f in ("quarantine", "held", "undelivered", "outbox", "failed")},
            "routes": _routes(config, cfg), "fetchers": _fetchers(cfg, recent),
            "alert": ({"message": str(st["alert"].get("message") or "")[:300], "since": st["alert"].get("since")} if st.get("alert") else None)}


def home_status():
    """Elaia's Home (status v2): the line, and "needs you" while something quarantined or held is unseen."""
    s = snapshot()
    if not s["ok"]:
        return {"line": s["line"], "needs": False, "action": ""}
    n = len(s["quarantine"]) + len(s["held"]) + len(s["undelivered"])
    return {"line": s["line"], "needs": bool(s["alert"]), "count": n,
            "action": ("Look at %d set aside" % n if n != 1 else "Look at the set-aside one") if s["alert"] else ""}


# ---- Elaia's entry points --------------------------------------------------------------------------------------------
def get(path, query, ctx):
    if path == "":
        return 200, snapshot()
    if path == "status":
        return 200, home_status()
    return 404, {"ok": False, "msg": "not found"}


def run(args, stdin=None):
    """Run a command with fixed arguments (a list, never a shell). Returns (exit code, what it said)."""
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, timeout=120, stdin=None if stdin is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "%s is not installed on this machine." % Path(args[0]).name
    except subprocess.TimeoutExpired:
        return 124, "No answer within two minutes. Look at the list again in a moment; nothing is retried."
    said = "\n".join(x.decode("utf-8", "replace").strip() for x in (p.stdout, p.stderr) if x and x.strip())
    return p.returncode, said[-800:]


def post(path, body, ctx):
    if path == "send":
        route, text = str(body.get("route", "")), body.get("text")
        if not ROUTE.match(route) or not isinstance(text, str):
            return 400, {"ok": False, "msg": "bad route or text"}
        data = text.strip().encode("utf-8")
        if not data:
            return 400, {"ok": False, "msg": "Type a note, a link or a magnet link first."}
        if len(data) > MESSAGE_MAX:
            return 400, {"ok": False, "msg": "A message is %d bytes at most; this one is %d." % (MESSAGE_MAX, len(data))}
        h = getattr(ctx, "headers", None) or {}
        user = (h.get("Tailscale-User-Login") or "").strip()[:100]
        code, said = run([hermes_bin(), "message", route] + (["--user", user] if user else []), stdin=data)
    elif path == "run":
        job = str(body.get("job", ""))
        if job not in JOBS:
            return 400, {"ok": False, "msg": "That job is not one the page may start."}
        code, said = run([os.environ.get("HERMES_SYSTEMCTL") or "systemctl", "--user", "start", "--no-block", job + ".service"])
    elif path == "seen":
        code, said = run([hermes_bin(), "ack"])
    else:
        return 404, {"ok": False, "msg": "not found"}
    return 200, {"ok": code == 0, "code": code, "msg": said}
