"""The hermes command line. Each issue adds its command here (receive, send, door, keys, ...)."""
import argparse
import sys

import json

from hermes import config, door, keys, log, receive, version

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


def show_log(args):
    """hermes log [--json] [-n N]: the last outcomes, newest last."""
    for e in log.entries(limit=args.n):
        print(json.dumps(e, ensure_ascii=False) if args.json else log.line(e))
    return 0


def show_status(args):
    """hermes status [--json]: today's counts, what waits in the set-aside folders, and the alert."""
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print("hermes status: %s" % e, file=sys.stderr)
        return 1
    st = log.status(cfg)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
    else:
        print(log.summary(st))
        for folder in log.LISTED:
            for i in st[folder]:
                print("  %s  %s  %s  %s  %s%s" % (folder, i["id"], i["route"] or "?", i["name"] or "", log.size(i["bytes"]),
                                                  "  (" + " ".join(i["reasons"]) + ")" if i["reasons"] else ""))
    return 0


def ack(args):
    """hermes ack: the quarantined and held items have been seen; the alert clears until something new arrives."""
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print("hermes ack: %s" % e, file=sys.stderr)
        return 1
    n = log.ack(cfg)
    print("hermes ack: %d item%s seen; the alert is cleared" % (n, "" if n == 1 else "s"))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="hermes", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version="hermes " + version())
    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")
    p = sub.add_parser("check-config", help="read routes.json, say where every route goes, and list what is wrong")
    p.add_argument("--config", metavar="FILE", help="the routes file (default: $HERMES_CONFIG or " + config.CONFIG + ")")
    p.set_defaults(func=check_config)
    p = sub.add_parser("receive", help="the ssh forced command a sending device's key runs (ping, put)")
    p.add_argument("--key", metavar="DEVICE", required=True, help="the device this key belongs to (from the authorized_keys line)")
    p.set_defaults(func=lambda a: receive.receive(a.key))
    p = sub.add_parser("keys", help="the Hermes-only ssh keys in ~/.ssh/authorized_keys: add, list, remove")
    p.add_argument("action", choices=("add", "list", "remove"))
    p.add_argument("device", nargs="?", help="the device the key belongs to, e.g. {{PC}}")
    p.add_argument("pubkey", nargs="?", metavar="pubkey-file", help="its public key file (add; stdin when omitted)")
    p.add_argument("--file", metavar="AUTHORIZED_KEYS", help="another authorized_keys file (default: ~/.ssh/authorized_keys)")
    p.set_defaults(func=keys.main)
    p = sub.add_parser("door", help="check every waiting arrival with Janus and deliver, quarantine or hold it (hermes-door.service)")
    p.set_defaults(func=lambda a: door.run())
    p = sub.add_parser("retry", help="send a held arrival through the door again now")
    p.add_argument("id")
    p.set_defaults(func=lambda a: door.retry(a.id))
    p = sub.add_parser("release", help="deliver a quarantined arrival anyway; who did it and why is logged")
    p.add_argument("id")
    p.add_argument("--reason", required=True, metavar="WORD", help="one word, e.g. checked-by-hand")
    p.set_defaults(func=lambda a: door.release(a.id, a.reason))
    p = sub.add_parser("discard", help="move a set-aside arrival to a dated set-aside folder (never deleted)")
    p.add_argument("id")
    p.set_defaults(func=lambda a: door.discard(a.id))
    p = sub.add_parser("log", help="the delivery log, newest last")
    p.add_argument("--json", action="store_true", help="one JSON object per line, as in the log file")
    p.add_argument("-n", type=int, default=50, metavar="N", help="how many lines (default 50; 0 = all)")
    p.set_defaults(func=show_log)
    p = sub.add_parser("status", help="today's counts, what waits in quarantine, held, failed and skipped, and the alert")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=show_status)
    p = sub.add_parser("ack", help="mark the quarantined and held items as seen; clears the Argus alert")
    p.add_argument("--config", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=ack)
    return parser


def main(argv=None):
    """Run the command line; returns the exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)
