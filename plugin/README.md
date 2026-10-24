# Hermes · Delivery
The Elaia server plugin `delivery` (docs/design.md §6, issue #11): `plugin.json`, `plugin.py` (reads Hermes's files through
Hermes's own code, writes only by running `hermes message`, `hermes ack` or `systemctl --user start` for an allowlisted
job), `page.js` and `page.css`. It installs to `~/.config/argus-ui/plugins/delivery/` (the install script is issue #12;
until then copy the folder there by hand and restart `argus-ui`). Needs Elaia 4.10 or later; reactions need 4.20.

Tests: `tests/test_delivery_plugin.py` (the Python side) and `tests/delivery/` (the page in a real browser under Elaia,
against a fake hermes; CI job `delivery-page`).
