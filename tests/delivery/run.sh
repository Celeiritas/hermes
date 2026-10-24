#!/bin/bash
# tests/delivery/run.sh <argus-ui checkout>: the Delivery page in a real browser, under Elaia, against a fake hermes.
# It copies plugin/ and delivery.test.js into that checkout's tests/, so give it a throwaway checkout (CI does: the
# delivery-page job in .github/workflows/tests.yml). Needs what argus-ui's tests/run.sh needs: python3, node, Playwright.
# Reads use this checkout's hermes package ($HERMES_LIB); writes go to tests/delivery/fake_hermes.py.
set -e
R=$(cd "$(dirname "$0")/../.." && pwd); A=$(cd "${1:?usage: bash tests/delivery/run.sh <argus-ui checkout>}" && pwd)
rm -rf "$A/tests/plugin-fixtures/delivery"; cp -r "$R/plugin" "$A/tests/plugin-fixtures/delivery"; rm -f "$A/tests/plugin-fixtures/delivery/README.md"
cp "$R/tests/delivery/delivery.test.js" "$A/tests/delivery.test.js"
chmod +x "$R/tests/delivery/fake_hermes.py"
export HERMES_LIB="$R" HERMES_BIN="$R/tests/delivery/fake_hermes.py" HERMES_SYSTEMCTL="$R/tests/delivery/fake_hermes.py"
export HERMES_CONFIG="~/.config/hermes/routes.json" JANUS_CONFIG="~/no-janus" LEAFY_REALM="~/no-realm"
bash "$A/tests/run.sh" delivery
