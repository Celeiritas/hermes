"""Hermes carries files and short messages between the devices of one tailnet, and fetches from allowlisted sources.
It never runs, unpacks or installs anything: Janus checks every delivery at the door, Zeus deploys, Argus watches.
Python 3 standard library only. See docs/design.md."""
from importlib import resources
from pathlib import Path


def version():
    """The version string: hermes/VERSION inside the built zipapp (scripts/build.sh puts it there), else the repo's VERSION."""
    try:
        return resources.files(__name__).joinpath("VERSION").read_text().strip()
    except (FileNotFoundError, OSError):
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
