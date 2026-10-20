#!/bin/bash
# scripts/build.sh [out] - build Hermes as one file: a Python zipapp (standard library only) with a python3 shebang.
#   bash scripts/build.sh              -> dist/hermes          bash scripts/build.sh /tmp/hermes   -> that path
# Install it as ~/.local/bin/hermes on the server (scripts/install.sh, a later issue). Needs only python3.
set -e
R=$(cd "$(dirname "$0")/.." && pwd); V=$(cat "$R/VERSION"); OUT=${1:-$R/dist/hermes}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/app" "$(dirname "$OUT")"
cp -r "$R/hermes" "$T/app/hermes"; find "$T/app" -name __pycache__ -type d -exec rm -rf {} +
echo "$V" > "$T/app/hermes/VERSION"               # hermes.version() reads it from inside the archive
cp "$R/hermes/__main__.py" "$T/app/__main__.py"   # the zipapp's entry point: sys.exit(main())
python3 -m zipapp "$T/app" -o "$OUT" -p "/usr/bin/env python3" -c
chmod +x "$OUT"
echo "built $OUT (hermes $V, $(du -h "$OUT" | cut -f1))"
