# The Janus contract, Hermes's side

Janus checks every delivery at {{SERVER}}'s door. Hermes never decides whether a file is allowed; it asks Janus and acts on the answer. Janus is its own project and its design confirms this contract; on a machine with no `janus` on `PATH`, arrivals wait in `held/`. The tests use `tests/fixtures/janus`, a stand-in that keeps to this contract.

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
{ "verdict": "pass", "janus": "0.2.2", "sha256": "…", "kind": "archive",
  "checks": { "route": "ok", "kind": "ok", "shape": "ok", "integrity": "ok" },
  "reasons": [], "notes": ["archive contents unchecked (.7z)"], "ms": 212 }
```
- `verdict` must match the exit code; `reasons` must be a list. Anything else is "no verdict" and the arrival is held. These two are the only fields that can hold an arrival.
- `reasons` are plain sentences; the Delivery page shows them as they are (e.g. `.exe files are never allowed on any route.`). Janus runs every check even after one has failed, so one verdict may give **several** reasons. Hermes keeps all of them, in Janus's order: in `verdict.json`, in the log line, in `hermes log` and `hermes status`, and on the page (one line each). The alert's message is the first one, followed by "(and 2 more reasons)" when there are more.
- `notes` are remarks that change nothing about the verdict (plain sentences, too). Today Janus writes them for an archive it cannot look inside (`archive contents unchecked (.7z)`), for archives inside an archive (`nested archives: 3 (not opened)`) and for a tree (`tree: 3 files (image and document)`). A delivery that passes with a note is delivered like any other; the note goes into the log line, `hermes log` shows it as `⚠ note: …`, and the Delivery page shows a ⚠ chip with the words "Note: …" next to the item, delivered or set aside. Notes never raise the alert.
- `kind` is the kind Janus detected (`image`, `archive`, `document`, `tree` …), or `null` when it named none. It goes into the log line and onto the page.
- `sha256` is what Janus hashed (Hermes compares nothing with it in v0.1; it is recorded).
- `checks` and `ms` (how long the check took) are kept in `verdict.json`; Hermes doesn't act on them.
- **An older Janus is fine.** A verdict without `notes` or `kind` is read as no notes and no kind, and so is a `verdict.json` or a log line saved before Hermes 0.4.1. A `notes` that is not a list of sentences, or a `kind` that is not a word, is left out of the log line and the page; it never holds a delivery (`verdict.json` still has it as Janus sent it).

Hermes saves the answer as `verdict.json` inside the arrival, exactly as Janus gave it, **before** moving anything, so a door run that dies halfway never asks Janus twice about the same arrival, and never delivers a file twice.

## What Janus needs from Hermes
`meta.json` (`docs/files.md`): `route`, `name`, `bytes`, `sha256`, and `sender` with `via` (`ssh`, `taildrop`, `fetch`), `key` (the device whose key was used), `node` and `user` from `tailscale whois` (`null` when whois failed: Janus decides what that means), `ip`. Janus checks that `key` and `node` name the same device, that the sender may use the route, and that the file is an allowed kind and size for it. Who may send what is Janus's `~/.config/janus/routes.json`; Hermes only warns (`hermes check-config`) about routes that file doesn't mention, since every arrival on them would be quarantined.

## Releasing
`hermes release <id> --reason <word>` on {{SERVER}} delivers a quarantined (or held) arrival anyway. The verdict is kept, with `released` (who, when, why) added, and the log line's outcome is `released` with `released_by`. The name and subpath are checked again; a release never bypasses that. There is no button for it in v1.
