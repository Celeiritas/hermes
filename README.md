# Hermes

Hermes is the delivery agent of an Elaia/Argus home setup ([argus-ui](https://github.com/Celeiritas/argus-ui)). It carries files and short messages between the devices of one tailnet, in both directions, and fetches from allowlisted sources. **It never runs, unpacks or installs anything.** Its companions: **Janus** checks every delivery at the door, **Zeus** deploys (only on the owner's approval), **Argus** watches every task.

On the server Hermes is one file, a Python 3 zipapp with no dependencies (`bash scripts/build.sh` → `dist/hermes`). On a Windows PC it is one PowerShell 5 script (`clients/windows/hermes.ps1`: send, collect, setup, plus the right-click Send To entries; `docs/windows-client.md`). Nothing of Hermes runs while idle: sshd, one systemd path unit and a few timers start it, and every job exits when its work is done.

## The door
Every delivery, in either direction, passes the server's door, where Janus checks it before it goes anywhere:
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
A **route** names where things go (`~/.config/hermes/routes.json`, see `examples/routes.example.json`). Who may send what on a route is Janus's file, not Hermes's, so the carrier can never widen its own permissions. No route ever accepts executables or scripts. `hermes check-config` says in one sentence per route where its files go, and lists what is wrong with the file and how to fix it. Each sending device has its own Hermes-only ssh key that can run `hermes receive` and nothing else (`hermes keys`, `docs/keys.md`).

## Repository
`docs/design.md` is the design: decisions, routes, the door, fetching, the Delivery page and the build order. `hermes/` is the package, `clients/windows/` the PC client, `plugin/` the Elaia page, `units/` the systemd units, `scripts/` build, install and the privacy check, `tests/` the tests (`bash tests/run.sh`, with a fake Janus in `tests/fixtures/`).

Public text uses placeholders (`{{SERVER}}`, `{{PC}}`, `{{PHONE}}`, `{{SSH}}`, `{{HOME_PC}}`, `{{HOME_SERVER}}`) and never a real device, user, path or host; `scripts/check-private.sh` enforces that before every push. MIT licence.
