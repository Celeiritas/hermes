# Hermes's files

Everything Hermes writes, where, and in what shape. Paths are on {{SERVER}}; `<stage>` is a stage folder from `routes.json` (one per drive, e.g. `~/.local/share/hermes/stage`).

## The stage folders
| `<stage>/…` | What waits there | Leaves by |
|---|---|---|
| `incoming/<id>/` | a transfer in progress (`hermes receive`) | one rename into `in/` when it is complete, or into `failed/` |
| `in/<id>/` | a complete arrival waiting for the door | the door: every arrival leaves `in/` on every run |
| `delivered/<id>/` | `meta.json` + `verdict.json` of a delivered arrival (the file itself is at its destination) | Poseidon's cleanup later |
| `quarantine/<id>/` | `data` (mode 0400), `meta.json`, `verdict.json` | `hermes release <id> --reason <word>` or `hermes discard <id>` |
| `held/<id>/` | `data`, `meta.json`, `held.txt` (one line per try: why Janus gave no verdict) | the next door run, `hermes retry <id>`, or `hermes discard <id>` |
| `skipped/<id>/` | duplicates (Janus) and `conflict: skip` arrivals, with `verdict.json` | `hermes discard`, Poseidon |
| `replaced/<id>/<name>` | the older copy a `conflict: replace` delivery set aside | Poseidon |
| `failed/<id>/` | a transfer that went wrong in `receive` (`data` so far, `error.txt`), junk found in `in/`, or a Taildrop of a kind no route takes | `hermes discard`, Poseidon |
| `outbox/<id>/` | a passed outbound item whose device is asleep: `<prefix><name>` (Hermes's hardlink), `meta.json`, `verdict.json`, `tries.txt` | `hermes collect`, every run, until it is sent (→ `delivered/`) or `retry_hours` are up |
| `undelivered/<id>/` | an outbound item whose device stayed asleep for `retry_hours` | `hermes release` (another try), `hermes discard` |
| `incoming/taildrop/` | where `tailscale file get` puts the phones' files for a moment; empty between runs | `hermes collect`, at once |

`hermes discard <id>` moves a folder to `~/.local/share/hermes/.set-aside-YYYYMMDD-HHMMSS/<id>/`. **Nothing is ever deleted**: only renames into these folders or the dated set-aside.

## `meta.json` (written by `hermes receive`)
```json
{ "id": "20261020-141203-3fa9c1", "route": "lesson", "direction": "in",
  "name": "git-basics.json", "bytes": 18234, "sha256": "…",
  "sender": { "via": "ssh", "key": "{{PC}}", "node": "{{PC}}", "user": "<tailnet login>", "ip": "100.x.y.z" },
  "received": "2026-10-20T14:12:03-06:00", "hermes": "0.1.4" }
```
- `id` is `YYYYMMDD-HHMMSS-<6 hex>`, the arrival's folder name everywhere.
- `sender.node` and `sender.user` are `null` when `tailscale whois` failed, and `sender.whois_error` says why.
- `subpath` (optional, subpath routes only): a relative folder under the route's `to`, e.g. `"Some Show"`. Fetch jobs set it.
- Outbound items (`hermes send`, `hermes message` on a `taildrop:` route) have `"direction": "out"`, `"sender": { "via": "local", "user": "<who ran it>" }` and, for a file, `"source"`: the original's path. The phones' files have `"sender": { "via": "taildrop", "key": null, "node": null, "user": null }` (Tailscale doesn't say which of the owner's devices sent a Taildrop). Fetch jobs will add `"via": "fetch"` with `"fetch": "<job>"`.
- Messages carry `"message": { "type": "note" | "link" | "magnet" }`; their `data` is the text as it will be delivered (a magnet URI in `<hash8>.magnet`, the note or link in a `.txt`), so Janus checks exactly what lands.

## `verdict.json` (Janus's answer, saved by the door)
```json
{ "verdict": "quarantine", "reasons": [".exe files are never allowed on any route."], "sha256": "…", "janus": "0.1.0",
  "checks": { "route": "ok", "kind": "fail", "shape": "ok", "integrity": "ok" } }
```
`verdict` is `pass`, `quarantine` or `duplicate` (`docs/janus-contract.md`). When Hermes itself quarantines (a name it refuses, a route that no longer exists), `janus` is `null` and `hermes` is Hermes's version. A release adds `"released": { "at": "…", "user": "<ssh user>", "reason": "<word>", "node": …, "login": …, "ip": … }`.

## The log: `~/.local/share/hermes/log/deliveries-YYYY-MM.jsonl`
One line per outcome, about 200 bytes; a new file every month.
```json
{ "t": "2026-10-20T14:12:04-06:00", "id": "20261020-141203-3fa9c1", "route": "lesson", "dir": "in",
  "from": "{{PC}}", "to": "~/.local/share/argus-ui/inbox/git-basics.json", "name": "git-basics.json",
  "bytes": 18234, "verdict": "pass", "outcome": "delivered", "reasons": [] }
```
- `outcome`: `delivered`, `quarantined`, `skipped`, `held`, `failed`, `released` (with `released_by`), `discarded`; outbound: `sent`, `waiting` (once, when the item enters the outbox), `undelivered`.
- `from` is the sender's key device, fetch job or `via` (`local`, `taildrop`). `to` is the final path (`~` for the home folder), `taildrop:<device>` for outbound items (then `name` is the name the device sees, prefix included), or where a discard went.
- `hermes log [-n N] [--json]` prints the last N lines (default 50) as `time  route  from → to  name  size  outcome  (reasons)`, or as they are in the file.
- Errors Hermes hit itself go to `receive-errors.log` and `door-errors.log` next to it, with tracebacks; a sender only ever sees one line.

## `hermes status --json`
```json
{ "hermes": "0.1.4", "at": "…", "today": { "delivered": 4, "quarantined": 1 },
  "quarantine": [ { "id": "…", "stage": "home", "folder": "quarantine", "route": "lesson", "name": "setup.exe", "bytes": 1234,
                    "from": "{{PC}}", "since": "2026-10-20T14:12:03-06:00", "reasons": [".exe files are never allowed on any route."], "path": "…" } ],
  "held": [], "undelivered": [], "outbox": [], "failed": [], "skipped": [],
  "alert": { "job": "hermes-door", "level": "alert", "since": "…", "message": "1 delivery quarantined: …", "link": "delivery" } }
```
Without `--json`, the status line the Delivery page and Elaia's Home page show ("4 delivered today · 1 quarantined today · 1 quarantined"), then one line per waiting item.

## The alert file: `~/.local/state/argus-ui/alerts/hermes.json`
Written by the door and by `hermes collect` while `quarantine/`, `held/` or `undelivered/` hold items the owner hasn't seen; removed when there are none. Argus glares while it exists and shows `message` on the `hermes-door` card, linked to the Delivery page (design §5).
```json
{ "job": "hermes-door", "level": "alert", "since": "2026-10-20T14:12:03-06:00",
  "message": "1 delivery quarantined: .exe files are never allowed on any route.", "link": "delivery" }
```
`hermes ack` (the page's **Seen** button) marks everything waiting now as seen (`~/.local/state/hermes/seen.json`) and removes the file; the next new item brings it back.
