# Hermes – design
Hermes is the delivery agent of an Elaia/Argus home setup ([argus-ui](https://github.com/Celeiritas/argus-ui)). It carries files and short messages between the devices of one tailnet, in both directions, and fetches from allowlisted sources. It never runs, unpacks or installs anything. Its companions: **Janus** checks every delivery at the door, **Zeus** deploys (only on the owner's approval), **Argus** watches every task.

Placeholders follow argus-ui's rule: `{{SERVER}}` (the always-on box that runs Elaia, Janus and Hermes's door), `{{PC}}` (a Windows desktop), `{{PHONE}}`, `{{SSH}}` (the ssh alias for the server), `{{HOME_PC}}`, `{{HOME_SERVER}}`. They are filled in on the server from `~/.config/leafy/realm.json`. Example paths such as `/srv/media` stand for your own.

## 0. Decisions
| Question | Choice | Why |
|---|---|---|
| How devices hand files to {{SERVER}} | **SSH door key.** Each sending device has its own Hermes-only key. On {{SERVER}} that key is tied to a forced command (`hermes receive`) | sshd already runs, so there's no new process and no new port. A stolen key can only drop files at the door. The sender is identified twice: by its key and by `tailscale whois`, never by a name it types |
| Phone Taildrops on {{SERVER}} | **`hermes-taildrop.timer` every 2 min**, plus a Collect now button | Tailscale has no "file received" hook. The check exits at once when nothing is waiting |
| Route rules | **Two files.** `~/.config/hermes/routes.json` says *where* a route goes. `~/.config/janus/routes.json` says *who may send what* | The carrier can't widen its own permissions (separation of duties) |
| Hub | **{{SERVER}}.** Every Hermes route passes {{SERVER}}'s door, both ways | Janus lives there. {{PC}} has no Janus, so anything sent *to* it is checked before it leaves {{SERVER}} |
| Fetching | A fetch is a delivery whose sender is an allowlisted URL, so it goes through the same door | One door, one log, one place to look |
| Deploys | Hermes carries deploy **requests** (messages), never code | Charter §5: Zeus pulls the code himself |
| Elaia page | Plugin id **`delivery`**, pill "Hermes · Delivery", URL `<base>/delivery` | Elaia already uses `/hermes` for Hermes's task page in Watch (persona keys are reserved plugin ids) |
| Releasing from quarantine | CLI on {{SERVER}} only in v1 | A one-click way past Janus would be the weakest door until Zeus's approval mechanism exists |

## 1. What Hermes is, and isn't
- **One job: carry.** Hermes moves files and short messages between devices, in both directions, and fetches things from allowlisted sources.
- **It never runs, unpacks or installs anything.** It never opens archives and never makes a file executable. It never decides whether a file is allowed (Janus does) and never deploys (Zeus does).
- **Nothing of Hermes runs while idle.** Its triggers are sshd (already running), one systemd path unit and a few timers. Everything Hermes runs is a one-shot that exits when its work is done.
- **Python 3 standard library only** on {{SERVER}}, installed as one file (a zipapp) at `~/.local/bin/hermes`. It also calls `git` and `tailscale`, which {{SERVER}} already has. **PowerShell 5 only** on {{PC}}, so no Python is needed there.

## 2. Routes
A **route** is a named path from senders to one destination. Hermes's file says where each route goes. Janus's file says which senders and which kinds of file each route allows.

| Route | From | To | Kinds Janus should allow (Janus decides) |
|---|---|---|---|
| `lesson` | {{PC}} (`/mentor` output), `hermes-decks` fetch | Mentor's inbox `~/.local/share/argus-ui/inbox/` | Mentor lesson JSON |
| `torrent` | {{PC}}, phones (Taildrop), the Delivery send box | qBittorrent `~/Downloads/torrents/watch/` (no category) | `.torrent`, `.magnet` |
| `torrent-movies`, `-shows`, `-audiobooks`, `-ebooks`, `-music`, `-games` | same | `watch/<type>/` (sets the qBittorrent category that `hermes-sort` reads) | `.torrent`, `.magnet` |
| `servermail` | {{PC}}, phones (Taildrop) | `~/Hermes-Mail/` | documents, images, audio, video, archives (carried, never unpacked) |
| `roms` | {{PC}} | `/srv/media/Emulation/_incoming/` | ROM and disc images, archives |
| `jellyfin-theme` | `hermes-jellyfin` fetch | the show's folder under `Media/Shows/` | mp3 |
| `pcmail` | {{SERVER}} (Dolphin right-click, the send box) | {{PC}}, by Taildrop, collected into `{{HOME_PC}}\Hermes-Mail\` | images, PDFs, text, documents |
| `phone` | {{SERVER}} | {{PHONE}}, by Taildrop | images, PDFs, text |
| `deploy-request` | `hermes-github` fetch | Zeus's queue `~/.local/share/zeus/requests/` | a deploy-request message (off until Zeus exists) |

**No route ever accepts executables or scripts** (charter §5). People can still send files to {{SERVER}} over plain `scp` as before, but that path is not Hermes.

### `~/.config/hermes/routes.json` (where things go)
The owner's real file lives in a private config repo (`hermes/routes.json`) and is copied into `~/.config/hermes/` when that repo is deployed. The public repo ships `examples/routes.example.json`, written with placeholders only. The loader expands `~` and argus-ui's `{{PLACEHOLDERS}}` from `~/.config/leafy/realm.json`, which is the same file Mentor uses, so the example runs as-is.

```json
{
  "version": 1,
  "stages": {
    "home":  "~/.local/share/hermes/stage",
    "media": "/srv/media/.hermes"
  },
  "routes": {
    "lesson":         { "to": "~/.local/share/argus-ui/inbox/", "stage": "home",  "conflict": "replace" },
    "servermail":       { "to": "~/Hermes-Mail/",             "stage": "home",  "conflict": "rename" },
    "torrent":        { "to": "~/Downloads/torrents/watch/",    "stage": "media", "conflict": "skip" },
    "torrent-movies": { "to": "~/Downloads/torrents/watch/movies/", "stage": "media", "conflict": "skip" },
    "roms":           { "to": "/srv/media/Emulation/_incoming/", "stage": "media", "conflict": "rename" },
    "jellyfin-theme": { "to": "/srv/media/Media/Shows/", "stage": "media", "subpath": true,
                        "conflict": "skip", "after": "jellyfin-refresh" },
    "pcmail":     { "to": "taildrop:{{PC}}",   "prefix": "hermes__", "stage": "home", "retry_hours": 24 },
    "phone":          { "to": "taildrop:{{PHONE}}", "stage": "home", "retry_hours": 24 },
    "deploy-request": { "to": "~/.local/share/zeus/requests/", "stage": "home", "enabled": false }
  },
  "taildrop": { "by_kind": { ".torrent": "torrent", ".magnet": "torrent" }, "default": "servermail" },
  "fetch": {
    "jellyfin-themes": { "job": "hermes-jellyfin", "route": "jellyfin-theme",
                         "allow": ["tvthemes.plexapp.com"], "jellyfin": "http://localhost:8096",
                         "key_file": "~/.config/leafy/jellyfin.key" },
    "decks":  { "job": "hermes-decks", "route": "lesson",
                "repo": "git@github-realm:you/realm.git", "paths": ["decks/*.json"] },
    "github": { "job": "hermes-github", "route": "deploy-request",
                "repos": ["Celeiritas/argus-ui", "Celeiritas/hermes", "you/realm"] }
  }
}
```
| Field | Meaning |
|---|---|
| `stages` | Where arrivals wait. One stage per drive, so a passing file is **renamed** into place rather than written a second time. That spares the SSD, and the rename is atomic. `check-config` warns if a route's stage and destination are on different drives (the file would be copied) |
| `to` | A folder, or `taildrop:<device>` for outbound routes. Symlinks such as `~/Downloads/torrents` are resolved first |
| `conflict` | When a file of the same name is already there: `rename` (add ` (2)`), `skip` (log it, set it aside), or `replace` (the older copy goes to `<stage>/replaced/`; nothing is deleted). Lessons use `replace` because Mentor re-imports a changed file and keeps the history |
| `subpath` | The sender gives a path *under* `to` (a show's folder). Anything that would climb out of `to` is refused |
| `after` | A doorbell rung after a delivery, from a fixed list: `jellyfin-refresh` (today's library refresh). It never runs a command from config |
| `prefix`, `retry_hours` | Outbound only. The name prefix Taildrop files get, and how long a send keeps retrying while the device is asleep |
| `enabled` | `false` keeps a route defined but closed. Its messages are listed on the Delivery page instead |

### What Hermes expects from Janus's file
Janus's design decides the format. Hermes needs it to answer "is this sender allowed on this route, and is this file an allowed kind and size?". For example:
```json
{ "routes": { "lesson": { "from": [{ "key": "{{PC}}", "node": "{{PC}}" }, { "fetch": "decks" }],
                          "kinds": ["mentor-lesson"], "max_bytes": 524288 } } }
```
`hermes check-config` warns about any Hermes route that Janus's file doesn't mention, because every arrival on it would be quarantined.

## 3. The door (how a delivery flows)
```
sender ─ssh, Hermes key─▶ hermes receive ─▶ <stage>/incoming/<id>/ ─one rename─▶ <stage>/in/<id>/
                                                                                  │ hermes-door.path (systemd)
                                                                                  ▼
                                                     hermes-door.service ─▶ janus check <id>
   pass ─▶ destination (rename; SELinux label of the folder; exec bits off) ─▶ log
   quarantine ─▶ <stage>/quarantine/<id>/ + verdict.json ─▶ log + alert (Argus glares)
   duplicate ─▶ <stage>/skipped/<id>/ ─▶ log
   Janus missing / failed / timed out ─▶ <stage>/held/<id>/ ─▶ log + alert (never delivered unchecked)
```
**1. Receive ({{PC}} → {{SERVER}}).** On {{PC}}, `hermes.ps1 send <route> <files>` runs `ssh -T hermes-{{SERVER}} put <route> <name-b64url> <bytes> <sha256>` once per file and streams the file on stdin.
- Every argument is a route id, base64url, digits or hex. PowerShell 5 can't mangle them (Windows PowerShell 5 strips inner quotes from native-command arguments).
- On {{SERVER}}, the key's `authorized_keys` line forces `hermes receive --key {{PC}}`:
  ```
  restrict,from="<tailnet ranges>",command="{{HOME_SERVER}}/.local/bin/hermes receive --key {{PC}}" ssh-ed25519 AAAA… hermes@{{PC}}
  ```
  `restrict` means no shell, no forwarding and no pty. `from=` limits the key to tailnet addresses.
  `<tailnet ranges>` = Tailscale's fixed ranges `100.64.0.0/10,fd7a:115c:a1e0::/48` (private-ok: Tailscale's documented ranges, no device of ours). `hermes keys add|list|remove` edits only lines tagged `hermes:<device>`, and saves a dated backup first. The public key lines are kept in `realm/hermes/authorized_keys.hermes` so they can be restored.
- `receive` accepts only `ping` and `put`. It streams the file into `<stage>/incoming/<id>/data` and hashes it as it goes. It stops if more bytes arrive than were declared, or if the drive would drop below a 5 GB margin. It checks the size and SHA-256 against the sender's, then writes `meta.json`. Finally it renames the whole `<id>/` folder into `in/` in one step, so the door never sees half a file.
- Names are cleaned: no `/`, `\` or NUL, no leading dot, at most 200 characters. `receive` lowers its own priority (nice 10), because it runs under sshd rather than as a systemd job with limits.

`meta.json`:
```json
{ "id": "20261020-141203-3fa9c1", "route": "lesson", "direction": "in",
  "name": "git-basics.json", "bytes": 18234, "sha256": "…",
  "sender": { "via": "ssh", "key": "{{PC}}", "node": "{{PC}}", "user": "<tailnet login>", "ip": "100.x.y.z" },
  "received": "2026-10-20T14:12:03-06:00", "hermes": "0.1.0" }
```
`node` and `user` come from `tailscale whois --json` on the address in `SSH_CONNECTION`. Janus checks that the key and the node name the same device.

**2. Door.** `hermes-door.path` watches every stage's `in/` (`DirectoryNotEmpty=`) and starts `hermes-door.service`, a one-shot. For each arrival:
1. Run `janus check --json <stage>/in/<id>` (contract below). The timeout grows with the file's size.
2. Act on the verdict:
   - **pass:** rename into the destination (copy + fsync + rename only when another drive can't be avoided), apply `conflict`, and turn off exec bits.
   - Then copy the destination folder's SELinux label onto the file. A moved file keeps its old label, and qBittorrent and Jellyfin, which run in containers, can't read it.
   - Then ring the route's `after` doorbell, if it has one.
   - **quarantine** and **duplicate** go to their folders as in the picture.
3. **Every arrival leaves `in/`.** Otherwise the path unit would fire again and again until systemd's trigger limit stops it.
4. Write one log line per outcome.

`hermes-door` exits non-zero **only when Hermes itself fails** (bad config, full disk). A quarantine is Janus doing his job, not a failed task.

**3. Outbound ({{SERVER}} → {{PC}} or a phone).**
- `hermes send pcmail <files>` (also the Dolphin right-click "Hermes: send to {{PC}}") stages a hardlink of each file, or a copy across drives, with `direction: out`. The original is never touched.
- The same door and the same Janus check run before `tailscale file cp` sends `hermes__<name>`.
- If the device is asleep, the item waits in `<stage>/outbox/<id>/`. `hermes-taildrop` retries it on every run for `retry_hours`, then marks it *undelivered* and raises an alert.
- Hermes removes its own staging link once Tailscale confirms the send. {{PC}} collects on demand, as today (`hermes.ps1 collect` moves `hermes__*` files from Downloads into `Hermes-Mail\` and strips the prefix).

**4. Phone Taildrops (→ {{SERVER}}).**
- `hermes-taildrop.timer` runs every 2 minutes (Collect now runs it at once).
- It runs `tailscale file get` without `--wait`, which returns at once when nothing is waiting (check this in the build). Files land in `<home stage>/incoming/` and go to the door with `sender.via = "taildrop"`.
- The route is picked by kind (`taildrop.by_kind`), otherwise `servermail`.
- As far as the CLI shows, Tailscale doesn't tell the receiver *which* device sent a Taildrop. It does only accept Taildrops from the owner's own devices, so Janus treats the sender as "one of the owner's devices" and should allow only a narrow set of kinds.
- Whatever collects Taildrops into `~/Hermes-Mail/` today gets found and retired. It is set aside, not deleted.

**5. Messages.** A message is a small arrival (4 KB at most) whose data is JSON:
| `type` | Made by | Delivered as |
|---|---|---|
| `magnet` | the send box, the Magnet Send To | a `<hash8>.magnet` file in the torrent route's watch folder |
| `note`, `link` | the send box | a `.txt` file on the route (e.g. `hermes__note-20261020-1412.txt` to {{PC}}) |
| `deploy-request` | `hermes-github` | `<repo>-<sha7>.json` in Zeus's queue |

`hermes message <route>` reads the text on stdin. That's how the Delivery page sends, so the send box goes through Janus like everything else.

### The Janus contract (Hermes's side; Janus's design confirms it)
```
janus check --json <arrival dir>     reads meta.json and data; never writes there
exit 0 = pass · 10 = quarantine · 11 = duplicate · anything else = Janus failed → Hermes holds
stdout: { "verdict": "pass", "reasons": [], "sha256": "…", "janus": "0.1.0",
          "checks": { "route": "ok", "kind": "ok", "shape": "ok", "integrity": "ok" } }
```
- `reasons` are plain sentences, because the Delivery page shows them as-is (e.g. "`.exe` files are never allowed on any route").
- **No Janus, no delivery.** Until Janus exists, arrivals wait in `held/`. The old portals stay in use until then (§9).

### Quarantine, held, skipped
| Folder (per stage) | What's in it | Leaves by |
|---|---|---|
| `quarantine/<id>/` | data (mode 0400), `meta.json`, `verdict.json` | `ssh {{SSH}} 'hermes release <id> --reason <word>'`: delivers anyway and logs who released it (ssh user and whois). Or `hermes discard <id>`, which moves it to `~/.local/share/hermes/.set-aside-<date>-<time>/` |
| `held/<id>/` | same, no verdict | retried on every door run; `hermes retry <id>` |
| `skipped/<id>/` | duplicates, and `conflict: skip` | Poseidon's cleanup later (preview, then `--go`) |
| `replaced/<id>/` | older copies replaced by a newer one | same |

Nothing in these folders is deleted automatically.

### Log
`~/.local/share/hermes/log/deliveries-YYYY-MM.jsonl`, one line per outcome:
```json
{ "t": "2026-10-20T14:12:04-06:00", "id": "20261020-141203-3fa9c1", "route": "lesson", "dir": "in",
  "from": "{{PC}}", "to": "~/.local/share/argus-ui/inbox/git-basics.json", "name": "git-basics.json",
  "bytes": 18234, "verdict": "pass", "outcome": "delivered", "reasons": [] }
```
Outcomes are `delivered`, `quarantined`, `skipped`, `held`, `sent`, `waiting`, `undelivered`, `released` and `discarded`. Each line is about 200 bytes. A new file starts every month, and Poseidon trims files older than six months (preview first).

## 4. Fetching
Every fetched item becomes an arrival with `sender.via = "fetch"` and `sender.fetch = <name>`, and goes through the door.

| Job (timer) | What it fetches | How |
|---|---|---|
| `hermes-jellyfin` (Sun 04:00, Persistent, as today) | TV `theme.mp3` for every show Jellyfin knows | Same logic as `hermes-jellyfin.py`, which this replaces; the job keeps its name. It asks Jellyfin's API for shows, then downloads only from hosts in `allow` and stages `<show>/theme.mp3` as a `subpath` arrival. After a delivery it rings `jellyfin-refresh`. Jellyfin's media mount stays read-only |
| `hermes-decks` (daily 05:00 + Fetch now) | lesson files from private `realm` | A bare mirror `~/.local/share/hermes/mirrors/realm.git`, fetched with a **read-only deploy key** `~/.config/hermes/keys/realm` (mode 600; ssh alias `github-realm`). Hermes reads only `decks/*.json` blobs at `main` with `git cat-file`: no checkout, no hooks, nothing runs. Only blobs that changed since the last run are delivered (route `lesson`). A run with no changes writes nothing |
| `hermes-github` (hourly + Check now) | "a new commit is on `main`" | `git ls-remote <repo> refs/heads/main` for each allowlisted repo, a few KB per run. Public repos need no key; private ones use their own read-only deploy key. A new commit becomes a `deploy-request` message (below), with details from the GitHub REST API. Anonymous use is limited to 60 calls an hour, and this stays well under it |

**Deploy-request message**, read by Zeus. It's only a hint: Zeus checks again that the commit is on `main` before pulling anything.
```json
{ "type": "deploy-request", "repo": "Celeiritas/argus-ui", "sha": "<40 hex>", "previous": "<40 hex>",
  "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success",
  "changelog": ["- Learn cards …"], "seen": "2026-10-20T15:00:00-06:00" }
```
Until Zeus exists, the `deploy-request` route is closed (`enabled: false`). The Delivery page shows "New on main: argus-ui 4.9.0" with the manual line, `ssh {{SSH}} 'cd ~/src/argus-ui && bash scripts/deploy.sh'`.

`hermes-sort` (every 5 min) and `hermes-tosort` (hourly) stay as they are. They're Hermes's last mile, filing finished downloads into the library. A later issue moves them into the repo with their paths in config.

## 5. What Argus shows
- **Task cards:** `hermes-door`, `hermes-taildrop`, `hermes-jellyfin`, `hermes-decks`, `hermes-github`, `hermes-sort` and `hermes-tosort`. Each has an entry in Argus's agent registry (`<job>.json`: code, log, writes_to) and the usual `argus-limits.conf` drop-in (CPU 25 %, memory 512 MB, low I/O weight, nice 10).
- **Glare:** a task card goes red only when Hermes itself fails. Quarantined, held and undelivered items raise an **alert file** at `~/.local/state/argus-ui/alerts/hermes.json`:
  ```json
  { "job": "hermes-door", "level": "alert", "since": "…",
    "message": "1 delivery quarantined: .exe files are never allowed", "link": "delivery" }
  ```
- Argus glares while that file exists and shows the message on the card, linked to the Delivery page. Pressing **Seen** on the Delivery page clears it.
- This needs a small argus-ui feature: alert files any agent can raise (filed on argus-ui).

## 6. Hermes · Delivery (Elaia plugin)
A `server` plugin (the plugin kind from the Mentor server-plugin work) shipped in `hermes/plugin/` and installed to `~/.config/argus-ui/plugins/delivery/`. It uses the theme's Hermes hue and medallion.

**On the page (laid out for {{PHONE}} first, 375 px):**
1. **Status line:** "4 delivered today · 1 quarantined · 1 waiting for {{PC}}". The same line feeds Elaia's Home page.
2. **Send box:** pick a route (Torrent: type; Note or Link to {{PC}}), paste a magnet link, link or note, then Send. The answer appears in place: "Handed to Hermes · Janus checking…", then the verdict. It's text only, because files from the phone go by Taildrop and from {{PC}} by Send To, and Elaia's POST cap is 4 KB.
3. **Recent deliveries:** time, route, from → to, name, size and a verdict chip (✓ delivered · ⚠ quarantined · ⏸ held · ↻ waiting · ✕ undelivered). Filters: All · Quarantined · Outbox.
4. **Quarantine detail:** the reason in plain words, sender, SHA-256, and the `hermes release` / `hermes discard` lines with copy buttons.
5. **Fetchers:** the last run of each, with Fetch now, Check now and Collect phone mail.
6. **New on main** (until Zeus exists): each new commit with its deploy line.

**API:** `GET api/delivery/recent?filter=…` and `GET api/delivery/status` read Hermes's log and status files directly; there are no subprocesses on reads. The POSTs pass Elaia's checks (same-origin, JSON only, 4 KB):
- `send` runs `hermes message`.
- `run` starts one of an allowlisted set of jobs: `hermes-decks`, `hermes-github`, `hermes-jellyfin`, `hermes-taildrop`.
- `seen` clears the alert.

The send box records the tailnet user from `tailscale serve`'s `Tailscale-User-Login` header when there is one. The device can't be known behind `tailscale serve`, so the sender shows as "{{SERVER}} · Elaia".

**Why it's built this way**
- The verdict appears right where you sent the file (visibility of system status; Nielsen, 1994).
- Quarantine reasons say what happened and what to do (heuristic 9: help users recognize, diagnose and recover from errors).
- Chips use an icon and a word, never color alone (WCAG 2.1, SC 1.4.1).
- There's no Release button in v1. People click through warnings they see often (Akhawe & Felt, 2013), so a bypass deserves friction until Zeus's approval flow exists.
- Elaia's UI rules from argus-ui `CLAUDE.md` apply: a keyboard path, no sideways scrolling at 375 px, and reduced motion.

## 7. Resource use on {{SERVER}}
| When | What runs | Cost |
|---|---|---|
| Idle | nothing of Hermes's (sshd and systemd already run) | 0 |
| A delivery | `hermes receive` for the transfer, `hermes-door` for about a second, Janus | about 15–25 MB, for seconds |
| Every 2 min | `hermes-taildrop` | under 0.5 s; writes nothing when nothing waits |
| Hourly / daily / weekly | `hermes-github` / `hermes-decks` / `hermes-jellyfin` | a few KB / a small git fetch / as today |
| Disk | staging sits on the destination's drive, so each file is written once; the log is about 200 B per delivery | — |

## 8. Public vs private
**`Celeiritas/hermes` (public, MIT)**: code, contracts and examples with placeholders, and nothing about the owner's devices.
```
hermes/            the package (stdlib): cli, receive, door, send, collect, message, fetch/, config, log, keys
clients/windows/   hermes.ps1 (send, collect, setup), install-sendto.ps1
plugin/            Elaia server plugin "delivery": plugin.json, plugin.py, page script
units/             systemd templates (hermes-door.path/.service, hermes-taildrop, hermes-jellyfin, hermes-decks, hermes-github)
examples/          routes.example.json (placeholders)
docs/              how it works, files.md (meta, log, message formats), janus-contract.md, windows-client.md
scripts/           build.sh (zipapp), install.sh, deploy.sh, check-private.sh, install-hooks.sh
tests/             run.sh (unittest), fixtures, a fake janus
CLAUDE.md, README.md, VERSION, CHANGELOG.md, LICENSE
```
**`you/realm` (private)**: `hermes/routes.json`, `hermes/authorized_keys.hermes` (public key lines only), `janus/routes.json` and `decks/*.json`. Its deploy copies the configs into `~/.config/hermes/` and `~/.config/janus/`. They are copied, not symlinked, so a `git pull` alone never changes what the door allows. Later, Zeus approves those deploys too.

**Never in any repo:** private keys (`hermes_<device>`, deploy keys), `jellyfin.key`, `secret.env`, logs, quarantine.

## 9. Build order
| Version | Issues | Built by | Needs |
|---|---|---|---|
| 0.1 "the door" | skeleton · routes.json · receive + keys · door + quarantine · {{PC}} client | Claude Code cloud (tests); the Windows client on {{PC}} | Janus check v0 (janus repo) for real deliveries; tests use a fake Janus |
| 0.2 "both ways" | outbound send + phone Taildrops + messages | cloud, then a {{PC}} session for the {{SERVER}} test | 0.1 |
| 0.3 "fetch" | fetch framework + Jellyfin themes · decks · GitHub check | cloud | 0.1, the private `realm` repo and its deploy key |
| 0.4 "page" | Delivery plugin | cloud | argus-ui `server` plugin kind; argus-ui alert files |
| 1.0 "cut-over" | install/deploy on {{SERVER}}, swap the Send To entries, set the old portals aside | {{PC}} session | everything above and Janus v0 live |
| later | `hermes-sort`/`hermes-tosort` into the repo; email | — | — |


## 10. Open questions
- **Janus:** confirms the contract (§3), the kind names, the policy file format, whether Janus re-hashes very large files or trusts Hermes's streaming hash, and the Taildrop sender rule.
- **Zeus:** consume the deploy-request message (§4) and, later, approve realm config deploys and quarantine releases from a button.
- **Later:** email through Hermes (planned), and a Linux sender for a Linux laptop.

## References
- Akhawe, D., & Felt, A. P. (2013). Alice in Warningland: A large-scale field study of browser security warning effectiveness. *USENIX Security '13*, 257–272.
- Nielsen, J. (1994). Enhancing the explanatory power of usability heuristics. *CHI '94*, 152–158.
- W3C (2018). *Web Content Accessibility Guidelines 2.1*, Success Criterion 1.4.1 Use of Color.
