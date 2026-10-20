"""The hermes command line. Each issue adds its command here (receive, send, door, keys, ...)."""
import argparse
import sys

from hermes import config, version

DESCRIPTION = "Hermes carries files and short messages between the devices of one tailnet. It never runs, unpacks or installs anything."


def check_config(args):
    """hermes check-config: one sentence per route, then the warnings and errors; exit 1 on errors."""
    cfg = config.check(args.config)
    for line in cfg.explain():
        print(line)
    for w in cfg.warnings:
        print("warning: " + w, file=sys.stderr)
    for e in cfg.errors:
        print("error: " + e, file=sys.stderr)
    n, w = len(cfg.routes), len(cfg.warnings)
    if cfg.errors:
        print("%s: %d error%s. Fix them; Hermes delivers nothing until routes.json is right."
              % (config.short(cfg.path), len(cfg.errors), "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        return 1
    print("%s: %d route%s ok%s" % (config.short(cfg.path), n, "" if n == 1 else "s",
                                  ", %d warning%s" % (w, "" if w == 1 else "s") if w else ""))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="hermes", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version="hermes " + version())
    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")
    p = sub.add_parser("check-config", help="read routes.json, say where every route goes, and list what is wrong")
    p.add_argument("--config", metavar="FILE", help="the routes file (default: $HERMES_CONFIG or " + config.CONFIG + ")")
    p.set_defaults(func=check_config)
    return parser


def main(argv=None):
    """Run the command line; returns the exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)
