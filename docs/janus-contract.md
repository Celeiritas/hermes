# The Janus contract, Hermes's side

Janus checks every delivery at {{SERVER}}'s door. Hermes never decides whether a file is allowed; it asks Janus and acts on the answer. Janus's own design confirms this contract; until a real `janus` exists, arrivals wait in `held/` and the tests use `tests/fixtures/janus`.

## The call
```
janus check --json <stage>/in/<id>
```
- The folder holds `data` (the file, mode 0600) and `meta.json` (`docs/files.md`). Janus reads both and **never writes** there.
- Hermes runs it with a timeout of 30 s plus one second per 20 MB of the file (`$HERMES_JANUS_TIMEOUT` overrides it, for tests). The binary is `janus` on `PATH` (`$HERMES_JANUS` overrides).
- Hermes checks the arrival's `name` and `subpath` with its own rules before calling Janus, and quarantines a bad one itself (`"janus": null` in the verdict), so Janus never has to judge a malformed name.

## The answer
| Exit | Verdict | Hermes does |
|---|---|---|
| 0 | `pass` | delivers: the route's `to`, the `conflict` rule, exec bits off, the folder's SELinux label, the route's doorbell |
| 10 | `quarantine` | `<stage>/quarantine/<id>/` with `data` at mode 0400, `meta.json`, `verdict.json`; the Argus alert |
| 11 | `duplicate` | `<stage>/skipped/<id>/` |
| anything else, no binary, a timeout, or stdout that is not a verdict matching the exit code | — | **held**: `<stage>/held/<id>/` with `held.txt`; retried on every door run and by `hermes retry <id>`; the Argus alert. Never delivered unchecked |

stdout is one JSON object:
```json
{ "verdict": "pass", "reasons": [], "sha256": "…", "janus": "0.1.0",
  "checks": { "route": "ok", "kind": "ok", "shape": "ok", "integrity": "ok" } }
```
- `verdict` must match the exit code; `reasons` must be a list. Anything else is "no verdict" and the arrival is held.
- `reasons` are plain sentences; the Delivery page shows them as they are (e.g. `.exe files are never allowed on any route.`), and the first one is the alert's message.
- `sha256` is what Janus hashed (Hermes compares nothing with it in v0.1; it is recorded).
- `checks` is for the page and the log; Hermes doesn't act on single checks.

Hermes saves the answer as `verdict.json` inside the arrival **before** moving anything, so a door run that dies halfway never asks Janus twice about the same arrival, and never delivers a file twice.

## What Janus needs from Hermes
`meta.json` (`docs/files.md`): `route`, `name`, `bytes`, `sha256`, and `sender` with `via` (`ssh`, `taildrop`, `fetch`), `key` (the device whose key was used), `node` and `user` from `tailscale whois` (`null` when whois failed: Janus decides what that means), `ip`. Janus checks that `key` and `node` name the same device, that the sender may use the route, and that the file is an allowed kind and size for it. Who may send what is Janus's `~/.config/janus/routes.json`; Hermes only warns (`hermes check-config`) about routes that file doesn't mention, since every arrival on them would be quarantined.

## Releasing
`hermes release <id> --reason <word>` on {{SERVER}} delivers a quarantined (or held) arrival anyway. The verdict is kept, with `released` (who, when, why) added, and the log line's outcome is `released` with `released_by`. The name and subpath are checked again; a release never bypasses that. There is no button for it in v1.
