"""python3 -m hermes ... (the zipapp built by scripts/build.sh has the same entry point at its root)."""
import sys

from hermes.cli import main

sys.exit(main())
