# Hermes keys: one key per sending device, one thing it can do

Every device that sends files to {{SERVER}} has its own Hermes-only ssh key. On {{SERVER}} that key is tied to a forced command, so it can run `hermes receive` and nothing else: no shell, no port forwarding, no agent forwarding, no pty. A stolen key can only drop files at the door, where Janus checks them. The sender is identified twice, by its key (`--key <device>` on the line) and by `tailscale whois` on the connection's address, and never by a name it types.

## Setup
1. **On the sending device**, make a key for Hermes alone. Never reuse a key that can open a shell somewhere.
   ```
   ssh-keygen -t ed25519 -f hermes_{{PC}} -C hermes@{{PC}} -N ""
   ```
   On Windows the key goes in `{{HOME_PC}}\.ssh\`; `hermes.ps1 setup` (the Windows client, `docs/windows-client.md`) does this step, the alias in step 3, and prints the line for step 2.
2. **Copy the public half** (`hermes_{{PC}}.pub`, one line) to {{SERVER}}, then add it:
   ```
   hermes keys add {{PC}} hermes_{{PC}}.pub      # or:  cat hermes_{{PC}}.pub | ssh {{SSH}} 'hermes keys add {{PC}}'
   ```
   This writes one line to `~/.ssh/authorized_keys` and leaves every other line alone:
   ```
   restrict,from="100.64.0.0/10,fd7a:115c:a1e0::/48",command="{{HOME_SERVER}}/.local/bin/hermes receive --key {{PC}}" ssh-ed25519 AAAA… hermes:{{PC}}   (private-ok: Tailscale's fixed ranges)
   ```
   - `restrict` turns off everything but the command (OpenSSH 7.2 or newer).
   - `from=` limits the key to tailnet addresses: Tailscale's fixed ranges, not any device's.
   - `command=` is forced: whatever the client asks for lands in `SSH_ORIGINAL_COMMAND`, and `hermes receive` accepts only `ping` and `put`.
   - `hermes:{{PC}}` tags the line. `hermes keys` edits only tagged lines, and saves `authorized_keys.set-aside-YYYYMMDD-HHMM` before every change.
   The public key lines are also kept in the private realm repo (`hermes/authorized_keys.hermes`) so they can be restored.
3. **On the sending device**, an ssh alias that uses only this key:
   ```
   Host hermes-{{SERVER}}
       HostName {{SERVER}}
       User <your user on {{SERVER}}>
       IdentityFile ~/.ssh/hermes_{{PC}}
       IdentitiesOnly yes
   ```
4. **Try it:** `ssh hermes-{{SERVER}} ping` answers `hermes ok <version>`.

`hermes keys list` shows each device, its key type and fingerprint, and whether its line is still exactly what Hermes writes. `hermes keys remove {{PC}}` takes a device's line out (backup first). Adding a device that already has a line replaces it, which is how a key is rotated.

## Check that the key can do nothing else
Run these from the sending device after setup. Each one must be refused.

| Try | Expect |
|---|---|
| `ssh -o IdentitiesOnly=yes -i ~/.ssh/hermes_{{PC}} {{SERVER}}` (a shell; without `IdentitiesOnly` ssh would offer your normal key first and open a real shell) | no prompt; `hermes: no command. hermes receive runs only as an ssh forced command …`, exit 2 |
| `ssh hermes-{{SERVER}} bash` or `ssh hermes-{{SERVER}} 'cat ~/.ssh/authorized_keys'` | `hermes: unknown command …`, exit 2 |
| `ssh -L 8080:localhost:80 hermes-{{SERVER}} ping` (local forward) | ping still answers, but the forward fails: `channel …: open failed: administratively prohibited` |
| `ssh -R 8080:localhost:80 hermes-{{SERVER}} ping` (remote forward) | `Warning: remote port forwarding failed` |
| `ssh -A hermes-{{SERVER}} ping` then `ssh-add -l` on the far side | no agent (`restrict` turns agent forwarding off) |
| `ssh -tt hermes-{{SERVER}} ping` (a pty) | the pty is refused; `ping` still answers |
| the same key from a non-tailnet address | the connection is refused before any command runs (`from=`) |

If any of these gets through, the line in `authorized_keys` is not the one `hermes keys add` writes: `hermes keys list` says "options differ" for it. Remove and add the device again.

## What `hermes receive` does with a `put`
`put <route> <name-b64url> <bytes> <sha256>` with the file on stdin (every argument is a route id, base64url, digits or hex, so no shell on either side can mangle it). The file streams into `<stage>/incoming/<id>/data` and is hashed on the way; it stops when more bytes arrive than were declared or when the drive would drop below a 5 GB margin. Size and SHA-256 are checked against what the sender declared. Then `meta.json` is written (route, name, bytes, sha256, the key's device, and `node`/`user` from `tailscale whois`, which may be `null` when whois fails; Janus decides what that means), and the whole `<id>/` folder is renamed into `<stage>/in/` in one step, so the door never sees half a file. A short stream, a long one or a wrong hash sets the attempt aside in `<stage>/failed/<id>/` with `error.txt`; nothing is deleted. Exit 0 ok, 1 transfer failed, 2 refused, 3 the server's own problem (routes.json, disk). The reply on success is `ok <id>`.

Names are NFC-normalized and cleaned: no `/` or `\`, no control, invisible or direction-changing characters, no leading dot, no trailing space or dot, at most 200 characters. A transfer that sends nothing for 120 s is given up and set aside. An unexpected error on the server shows the sender one line, not a traceback; the traceback goes to `~/.local/share/hermes/log/receive-errors.log`. Files are written with mode 600, never with an exec bit. `receive` runs at nice 10.
