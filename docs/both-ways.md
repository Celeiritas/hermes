# Both ways: sending from {{SERVER}}, the phones' Taildrops, messages

Design §3 steps 3–5. Everything below goes through the same door and the same Janus check as a file put by {{PC}}; only the way in and the way out differ.

## {{SERVER}} → {{PC}} or a phone: `hermes send`
```
hermes send pcmail ~/Documents/report.pdf ~/Pictures/plan.png     # or Dolphin: right-click → "Hermes: send to {{PC}}"
report.pdf: sent to {{PC}}
plan.png: waiting for {{PC}} (asleep?); hermes collect keeps trying for 24 hours
```
1. Each file is staged as a **hardlink** in the route's stage (`<stage>/in/<id>/data`, a copy when the file is on another drive), with `direction: out` and `source` in `meta.json`. The original is never touched: not moved, not renamed, not re-moded (a quarantine does not chmod an outbound `data`, because it is the original).
2. The door runs for those arrivals at once, so `hermes send` can say what happened. Janus checks them as any arrival.
3. On a pass, the link is renamed to the name the device will see, the route's `prefix` plus the name (`hermes__report.pdf`), and sent with `tailscale file cp <file> <device>:`. The timeout is 60 s plus one per MB.
4. Sent: the log says `sent`, Hermes removes its own link, and `meta.json` + `verdict.json` go to `delivered/`. The original is still exactly where it was.
5. Not sent (the device is asleep, `file cp` fails or hangs): the folder goes to `<stage>/outbox/<id>/`, the log says `waiting` once, and `tries.txt` records each try. `hermes collect` tries again on every run. After the route's `retry_hours` (24 when unset) the item is `undelivered`: `<stage>/undelivered/<id>/`, a log line, and the Argus alert until `hermes ack`. `hermes release <id> --reason <word>` gives it another try later; `hermes discard <id>` sets it aside.
6. On {{PC}}, `hermes.ps1 collect` moves `hermes__*` from Downloads into `Hermes-Mail\` and strips the prefix (the Windows client issue).

The Dolphin menu is `clients/dolphin/hermes-send-to-pc.desktop` (install line in its header).

## Phones → {{SERVER}}: `hermes collect`
`units/hermes-taildrop.timer` runs `hermes collect` every 2 minutes (Tailscale has no "file received" hook); the Delivery page's **Collect now** runs it at once.
1. The outbox is retried (above).
2. `tailscale file get --conflict=rename <home stage>/incoming/taildrop/` runs **without `--wait`**, so it returns at once when nothing is waiting. The pickup folder is made once and stays, so a quiet run writes nothing.
3. Every file picked up becomes an arrival in the stage of its route (moved, not copied, when the stage is on the same drive): the route is `taildrop.by_kind` by the file's ending (`.torrent`, `.magnet` → `torrent`), else `taildrop.default` (`servermail`); a kind with neither goes to `failed/` with the reason. `sender` is `{ "via": "taildrop" }` with `key`, `node` and `user` null: as far as the CLI shows, Tailscale doesn't tell the receiver which device sent a Taildrop, only that it was one of the owner's. Janus treats such a sender as "one of the owner's devices" and should allow only a narrow set of kinds.
4. The door runs for the new arrivals. A `.torrent` lands in the torrent watch folder, a photo in `~/Hermes-Mail/`, an `.exe` in quarantine.

`hermes collect` exits non-zero only for Hermes's own problems (tailscale missing, `file get` failing, a kind with no route).

### Two things to check on {{SERVER}} itself
- **`tailscale file get` without `--wait` returns at once.** On the installed Tailscale: `time tailscale file get --conflict=rename /tmp/hermes-check` with nothing waiting must come back in well under a second and leave the folder empty. The fake in `tests/fixtures/tailscale` refuses `--wait`, so the code can never grow to depend on it. Record the version here when checked: `tailscale version` → _(not yet recorded; checked with the fake only)_.
- **A quiet run is fast and writes nothing.** `time hermes collect` with nothing waiting: the test with the fake measures the in-process run under 0.5 s and asserts that no file under HOME changed. The number on {{SERVER}} (with the real `tailscale file get`) goes here: _(not yet measured)_.

### What collected Taildrops before Hermes
`bash scripts/retire-taildrop-collector.sh` lists every user unit, timer or crontab line that runs `tailscale file get` or writes to `Hermes-Mail`; `--go` disables the units and moves their files to `<name>.set-aside-<date>`, and comments the crontab lines out with a dated note. Nothing is deleted, and the scripts those units ran stay where they are.

## Messages: `hermes message`
```
echo 'magnet:?xt=urn:btih:…' | hermes message torrent-movies     # → <hash8>.magnet in the movies watch folder
echo 'https://example.com/article' | hermes message pcmail       # → hermes__link-20261020-141203.txt on {{PC}}
hermes message pcmail <<< 'buy milk'                             # → hermes__note-20261020-141203.txt
```
The text comes on stdin, 4 KB at most, UTF-8. A magnet link is named by the first 8 characters of its info-hash; a single URL becomes `link-<time>.txt`; anything else `note-<time>.txt`. The arrival's `data` is the text as delivered and `meta.json` says `"message": { "type": … }`. The Delivery page's send box runs this with `--user <tailnet login>` from `tailscale serve`'s header, so the sender is recorded. Messages go through the door like files, so Janus sees them too.

## The door, both ways
- An arrival's `direction` must fit its route: `in` on a route with a folder, `out` on a `taildrop:` route; a mismatch is quarantined by Hermes.
- One door per stage at a time: `hermes door`, `hermes send`, `hermes message` and `hermes collect` take `<stage>/.door.lock`, so the path unit firing while a send runs just waits its turn.
- `hermes status` lists `outbox` and `undelivered` next to `quarantine`, `held`, `failed` and `skipped`; undelivered items raise the alert like quarantined and held ones.
