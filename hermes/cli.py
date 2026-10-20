"""The hermes command line. Each later issue adds its command here (receive, send, door, keys, ...)."""
import argparse

from hermes import version

DESCRIPTION = "Hermes carries files and short messages between the devices of one tailnet. It never runs, unpacks or installs anything."


def build_parser():
    parser = argparse.ArgumentParser(prog="hermes", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version="hermes " + version())
    parser.add_subparsers(dest="command", title="commands", metavar="<command>")   # commands land here, one issue at a time
    return parser


def main(argv=None):
    """Run the command line; returns the exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return 0
