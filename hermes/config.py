"""routes.json: where each route goes (docs/design.md §2). Load it, expand `~` and {{PLACEHOLDERS}}, validate it, and
explain it in plain sentences (`hermes check-config`). Who may send what on a route is Janus's file; Hermes only reads
it to warn about routes Janus doesn't know, because every arrival on such a route would be quarantined."""
import json
import os
import re
from pathlib import Path

CONFIG = "~/.config/hermes/routes.json"        # or $HERMES_CONFIG
JANUS_CONFIG = "~/.config/janus/routes.json"   # or $JANUS_CONFIG
REALM = "~/.config/leafy/realm.json"           # or $LEAFY_REALM: the same file Mentor reads
REALM_DEFAULTS = {"SERVER": "homeserver", "PC": "desktop", "PHONE": "phone", "SSH": "server",
                  "HOME_PC": "C:\\Users\\you", "HOME_SERVER": "~", "REPO_URL": "https://github.com/you/argus-ui"}
PLACEHOLDER = re.compile(r"\{\{\s*(" + "|".join(REALM_DEFAULTS) + r")\s*\}\}")
ROUTE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
CONFLICTS = {"rename": "the new file gets (2)", "skip": "the new file is set aside", "replace": "the older copy is set aside"}
DOORBELLS = ("jellyfin-refresh",)              # `after`: a fixed list in code; config never names a command
ROUTE_KEYS = ("to", "stage", "conflict", "subpath", "after", "prefix", "retry_hours", "enabled")
TAILDROP = "taildrop:"


def config_path():
    return Path(os.path.expanduser(os.environ.get("HERMES_CONFIG") or CONFIG))


def janus_path():
    return Path(os.path.expanduser(os.environ.get("JANUS_CONFIG") or JANUS_CONFIG))


def realm_path():
    return Path(os.path.expanduser(os.environ.get("LEAFY_REALM") or REALM))


def realm(path=None):
    """The placeholder values: realm.json over the public defaults. A missing or broken file means the defaults."""
    try:
        with open(path or realm_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    vals = dict(REALM_DEFAULTS)
    for k, v in (doc.items() if isinstance(doc, dict) else []):
        if str(k).upper() in vals and isinstance(v, (str, int, float)) and str(v).strip():
            vals[str(k).upper()] = str(v)
    return vals


def fill(text, vals, as_json=False):
    """Put the realm's values where {{SERVER}} etc. sit. as_json: text is JSON, so each value goes in escaped."""
    if "{{" not in text:
        return text
    return PLACEHOLDER.sub(lambda m: json.dumps(vals[m.group(1)])[1:-1] if as_json else vals[m.group(1)], text)


def device_of(path):
    """The drive (filesystem) a path is on. A rename works only within one drive."""
    return os.stat(path).st_dev


def short(path):
    """A path for people: the home folder shown as ~."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


class ConfigError(Exception):
    """routes.json has errors; `hermes check-config` lists them."""
    def __init__(self, config):
        super().__init__("%s: %d error%s; run `hermes check-config`" % (short(config.path), len(config.errors),
                                                                         "" if len(config.errors) == 1 else "s"))
        self.config = config


class Route:
    """One route after validation: `to` is the resolved destination folder, or `device` is set for a taildrop: route."""
    def __init__(self, id, raw):
        self.id, self.raw = id, raw
        self.to = self.device = self.stage_path = self.same_drive = None
        self.stage = raw.get("stage")
        self.conflict = raw.get("conflict", "rename")
        self.subpath = raw.get("subpath") is True
        self.after = raw.get("after")
        self.prefix = raw.get("prefix") or ""
        self.retry_hours = raw.get("retry_hours")
        self.enabled = raw.get("enabled") is not False

    @property
    def outbound(self):
        return self.device is not None

    def sentence(self):
        """One plain sentence: where the route's files go."""
        go = "files would go to" if not self.enabled else "files go to"
        s = self.id + ": " + ("closed; " if not self.enabled else "")
        if self.device:
            s += "%s %s by Taildrop" % (go, self.device)
            if self.prefix:
                s += " as %s<name>" % self.prefix
            if self.retry_hours:
                s += ", retried for %g hours while the device sleeps" % self.retry_hours
        elif self.to:
            s += "%s %s%s" % (go, short(self.to), "/<subpath>" if self.subpath else "")
            drive = {True: "on the same drive", False: "on another drive", None: ""}[self.same_drive]
            s += " (staged in %s %s; same name: %s)" % (self.stage, drive, CONFLICTS.get(self.conflict, "?"))
            if self.after:
                s += ", then rings %s" % self.after
        else:
            s += "broken, see the errors"
        return s + "."


class Config:
    """A read routes.json: stages, routes, taildrop and fetch sections, plus the errors and warnings found."""
    def __init__(self, path):
        self.path = path
        self.stages, self.routes, self.taildrop, self.fetch = {}, {}, {}, {}
        self.errors, self.warnings = [], []

    @property
    def ok(self):
        return not self.errors

    def explain(self):
        return [r.sentence() for r in self.routes.values()]


def check(path=None, janus=None, vals=None):
    """Read and validate routes.json without raising: the Config carries the errors and warnings, in plain words."""
    cfg = Config(Path(path or config_path()))
    err, warn = cfg.errors.append, cfg.warnings.append
    try:
        text = cfg.path.read_text(encoding="utf-8")
    except OSError as e:
        err("%s: cannot read it (%s). Copy examples/routes.example.json there and fill it in, or point HERMES_CONFIG "
            "at your file." % (short(cfg.path), e.strerror))
        return cfg
    try:
        doc = json.loads(fill(text, vals or realm(), as_json=True))
    except ValueError as e:
        err("%s: not valid JSON (%s). Look for a missing comma, quote or bracket near that line." % (short(cfg.path), e))
        return cfg
    if not isinstance(doc, dict):
        err("%s: the file must hold one JSON object, like examples/routes.example.json." % short(cfg.path))
        return cfg
    if doc.get("version") != 1:
        err("version: must be 1 (the only routes.json version this Hermes reads), not %r." % (doc.get("version"),))

    stages = doc.get("stages")
    declared = set()
    if not isinstance(stages, dict) or not stages:
        err('stages: must map at least one stage name to a folder, e.g. "home": "~/.local/share/hermes/stage".')
    else:
        for name, folder in stages.items():
            declared.add(name)
            if not isinstance(folder, str) or not folder.strip():
                err("stages.%s: must be a folder path." % name)
            elif not Path(os.path.expanduser(folder)).is_dir():
                err("stages.%s: %s is not a folder. Create it (mkdir -p %s) or point the stage at a folder that exists."
                    % (name, folder, folder))
            else:
                cfg.stages[name] = Path(os.path.expanduser(folder)).resolve()

    routes = doc.get("routes")
    if not isinstance(routes, dict) or not routes:
        err('routes: must map at least one route id to a route, e.g. "lesson": { "to": "<folder>", "stage": "home" }.')
        routes = {}
    for rid, raw in routes.items():
        _check_route(cfg, str(rid), raw, declared)

    taildrop = doc.get("taildrop", {})
    if not isinstance(taildrop, dict):
        err('taildrop: must be an object with "by_kind" and "default".')
    else:
        by_kind = taildrop.get("by_kind", {})
        if not isinstance(by_kind, dict):
            err('taildrop.by_kind: must map file endings to route ids, e.g. { ".torrent": "torrent" }.')
        else:
            for kind, rid in by_kind.items():
                if not kind.startswith("."):
                    err("taildrop.by_kind: %r must be a file ending that starts with a dot, such as .torrent." % kind)
                if rid not in routes:
                    err("taildrop.by_kind: %s names route %r, which is not defined under routes." % (kind, rid))
        if "default" in taildrop and taildrop["default"] not in routes:
            err("taildrop.default: route %r is not defined under routes." % (taildrop["default"],))
        cfg.taildrop = taildrop

    fetch = doc.get("fetch", {})
    if not isinstance(fetch, dict):
        err("fetch: must map fetch names to jobs, each with a \"route\".")
    else:
        for name, job in fetch.items():
            if not isinstance(job, dict) or job.get("route") not in routes:
                err('fetch.%s: "route" must name a route defined under routes.' % name)
        cfg.fetch = fetch

    _check_janus(cfg, Path(janus or janus_path()))
    return cfg


def _check_route(cfg, rid, raw, declared):
    err, warn = cfg.errors.append, cfg.warnings.append
    if not ROUTE_ID.match(rid):
        err("route %r: route ids are lowercase letters, digits and dashes, start with a letter and are 32 characters "
            "at most (e.g. torrent-movies)." % rid)
        return
    if not isinstance(raw, dict):
        err('route %s: must be an object with "to" and "stage".' % rid)
        return
    r = Route(rid, raw)
    for k in sorted(set(raw) - set(ROUTE_KEYS)):
        warn("route %s: unknown setting %r is ignored (the settings are %s)." % (rid, k, ", ".join(ROUTE_KEYS)))

    to = raw.get("to")
    if not isinstance(to, str) or not to.strip():
        err('route %s: "to" is missing. Give the destination folder, or taildrop:<device> for an outbound route.' % rid)
    elif to.startswith(TAILDROP):
        device = to[len(TAILDROP):].strip()
        if not device or any(c.isspace() or c in "/\\" for c in device):
            err('route %s: "to": %r must name a device, e.g. taildrop:{{PC}} (filled in from realm.json).' % (rid, to))
        else:
            r.device = device
    elif not Path(os.path.expanduser(to)).is_dir():
        err('route %s: "to": %s is not a folder. Create it (mkdir -p %s) or fix the path.' % (rid, to, to))
    else:
        r.to = Path(os.path.expanduser(to)).resolve()

    stage = raw.get("stage")
    if not isinstance(stage, str) or stage not in declared:
        err('route %s: "stage": %r names no stage; stages defined: %s.' % (rid, stage, ", ".join(sorted(declared)) or "none"))
    elif stage in cfg.stages:
        r.stage_path = cfg.stages[stage]
    if r.to is not None and r.stage_path is not None:
        if r.to == r.stage_path or r.stage_path in r.to.parents:
            err('route %s: "to" %s is inside stage %s (%s), where arrivals wait to be checked. Deliver to a folder '
                "outside every stage." % (rid, short(r.to), stage, short(r.stage_path)))
        else:
            r.same_drive = device_of(r.to) == device_of(r.stage_path)
            if not r.same_drive:
                warn("route %s: %s and stage %s (%s) are on different drives, so a passing file is copied instead of "
                     "renamed into place. Add a stage on the destination's drive and use it for this route."
                     % (rid, short(r.to), stage, short(r.stage_path)))

    if "conflict" in raw and raw["conflict"] not in CONFLICTS:
        err('route %s: "conflict" must be rename, skip or replace, not %r.' % (rid, raw["conflict"]))
    if "after" in raw and raw["after"] not in DOORBELLS:
        err('route %s: "after" must be a doorbell Hermes knows (%s), not %r. Hermes never runs a command named in '
            "config." % (rid, ", ".join(DOORBELLS), raw["after"]))
    for key in ("subpath", "enabled"):
        if key in raw and not isinstance(raw[key], bool):
            err('route %s: "%s" must be true or false, not %r.' % (rid, key, raw[key]))
    hours = raw.get("retry_hours")
    if "retry_hours" in raw and (isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0):
        err('route %s: "retry_hours" must be a number of hours above 0, not %r.' % (rid, hours))
    prefix = raw.get("prefix")
    if "prefix" in raw and (not isinstance(prefix, str) or any(c in prefix for c in "/\\\0")):
        err('route %s: "prefix" must be plain text with no slashes, not %r.' % (rid, prefix))
    cfg.routes[rid] = r


def _check_janus(cfg, path):
    """Warn about routes Janus's file doesn't mention: every arrival on them would be quarantined."""
    if not path.is_file():
        return
    try:
        jroutes = json.loads(path.read_text(encoding="utf-8"))["routes"]
        if not isinstance(jroutes, dict):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        cfg.warnings.append("%s: cannot read Janus's routes from it, so no route was checked against Janus's file."
                            % short(path))
        return
    missing = [rid for rid in cfg.routes if rid not in jroutes]
    if missing:
        one = len(missing) == 1
        cfg.warnings.append("Janus's %s doesn't mention route%s %s: every arrival on %s would be quarantined. Add %s to "
                            "Janus's file." % (short(path), "" if one else "s", ", ".join(missing),
                                               "it" if one else "them", "it" if one else "them"))


def load(path=None, janus=None):
    """Read and validate routes.json for the commands that carry; raises ConfigError when it has errors."""
    cfg = check(path, janus)
    if cfg.errors:
        raise ConfigError(cfg)
    return cfg
