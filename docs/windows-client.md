# The Windows client: `hermes.ps1`

Design §3 step 1 (send) and step 3 (collect). One PowerShell 5 script on {{PC}}, no Python: `clients/windows/hermes.ps1`, plus `install-sendto.ps1` for the right-click entries. Needs Windows' own OpenSSH client (Settings > Apps > Optional features > OpenSSH Client; on Windows 10 and 11 it is usually there) and Tailscale.

## Setup, once
1. Clone the repo (or copy `clients/windows/`) somewhere that stays, e.g. `{{HOME_PC}}\src\hermes`.
2. In PowerShell:
   ```
   powershell -ExecutionPolicy Bypass -File {{HOME_PC}}\src\hermes\clients\windows\hermes.ps1 setup -Server {{SERVER}} -User <your user on {{SERVER}}> -Alias {{SSH}}
   ```
   This makes `{{HOME_PC}}\.ssh\hermes_{{PC}}` (ed25519, no passphrase, for Hermes only; kept if it exists), adds a `Host hermes-{{SERVER}}` block to `{{HOME_PC}}\.ssh\config` (HostName, Port when one was named, User, IdentityFile, `IdentitiesOnly yes`, `StrictHostKeyChecking accept-new`), saves `%APPDATA%\Hermes\config.json` (server, user, port, alias, device, mail folder) and makes `{{HOME_PC}}\Hermes-Mail\`. The device name defaults to the PC's computer name in lower case; `-Device` sets it; it must be the name the server knows the PC by (`tailscale whois` names the node, and Janus compares the two). `-Mail` for another collect folder.

   **The address and the port come from your ssh alias.** `sshd` on {{SERVER}} may listen on any port, and on the server's Tailscale address only, so setup never assumes 22. `-Alias {{SSH}}` names the ssh alias you already use for the server: setup copies its `HostName` and its `Port` into `Host hermes-{{SERVER}}` (the user is always `-User`, the key is always the Hermes one). Without `-Alias` it looks for a `Host` block in this order: one called like the server, then one whose `HostName` is the server, and only when there is neither, the one block whose `HostName` is the server plus a dot and a domain (`{{SERVER}}.corp.example`). If two or more blocks match that last way, setup picks none (one of them could be another machine's, with another port and address), lists them, and stops: name the right one with `-Alias`. Whenever a block is chosen, setup prints which one (its name, `HostName` and `Port`) so you can see it. `-Port <n>` wins over the alias. With no alias and no `-Port`, setup asks for the port. **If no port is known** (nobody gave `-Port`, the alias has no `Port` line, and the question gets no number), setup stops with exit code 1 and changes nothing, because writing no `Port` line would make ssh quietly use 22. If ssh's own default really is right, say so on purpose with `-Port 0` (or answer `0`): then there is no `Port` line. Setup reads plain `Host` blocks only: a port that reaches your alias through a pattern (`Host *.example`), a `Match` or an `Include` is not seen, so give `-Port` then. A `Host hermes-{{SERVER}}` block that is already there is never changed: setup says what it holds, and says so when the alias or `-Port` names another port.
3. It prints **one pasteable line** that installs the public key on the server with `hermes keys add`, through your own alias when there is one (otherwise `ssh -p <port> <user>@{{SERVER}}`, without `-p` when no port was named); it asks for your usual login there:
   ```
   Get-Content "{{HOME_PC}}\.ssh\hermes_{{PC}}.pub" | ssh {{SSH}} 'hermes keys add {{PC}}'
   ```
4. `hermes.ps1 ping` → `{{SERVER}} answers: hermes <version> is at the door.`
5. `install-sendto.ps1` makes the Send To entries (and `-Desktop` puts Magnet and Collect on the Desktop). `-Remove` sets them aside, dated, never deleting.

**The server side must be OpenSSH.** The door is an `authorized_keys` forced command, which only OpenSSH's `sshd` reads. A server that runs *Tailscale SSH* on port 22 answers ssh itself, ignores `authorized_keys`, and would run `ping` as a shell command; on such a server `sshd` has to listen on another port, and that port belongs in your `{{SSH}}` alias, where setup finds it (or `setup -Port <n>`). `hermes.ps1 ping` says so when the answer is not `hermes ok <version>`. On the server, `hermes check-config` warns when `sshd` is not listening on the server's Tailscale address at all, because no device could then reach the door.

## Commands
| Command | What it does |
|---|---|
| `hermes.ps1 send <route> <file>...` | each file to {{SERVER}}'s door on that route; one result line per file |
| `hermes.ps1 torrent <file>...` | asks the type (1 Movie … 6 Game, Enter = none), then sends on `torrent` or `torrent-<type>` |
| `hermes.ps1 magnet [<file>...]` | finds every magnet link on the clipboard, or inside `.txt .md .html .csv .docx` files; shows name + info-hash; asks which; sends each as `<hash8>.magnet` on the torrent route of the chosen type |
| `hermes.ps1 collect` | `hermes__*` files from Downloads (where the Tailscale app saves Taildrops), plus anything still waiting in `tailscale file get`, into the mail folder without the prefix (` (2)` when a name is taken); opens the folder; says how many arrived |
| `hermes.ps1 ping` | does the server answer the Hermes key? |
| `hermes.ps1 setup …` | as above; safe to run again (key and alias are kept) |

`-Window` is what the Send To entries pass: the script runs in its own window, pauses 3 s after success and waits for Enter after a failure, so a message is never lost. `-Server`, `-Device`, `-Mail` override the setup for one run. `-Alias`, `-Port` and `-User` matter to `setup` only: every send goes through `Host hermes-{{SERVER}}`, and ssh reads the address, the port and the user there. `-Type 1-6` and `-Pick all|1,3` answer the torrent and magnet questions ahead of time (for scripts and tests). Exit code 0 only when everything went through.

Send To entries (`install-sendto.ps1`): **Hermes · Mail** (`servermail`), **Hermes · Torrent** (asks the type), **Hermes · Magnet** (clipboard or files), **Hermes · Lesson** (`lesson`), **Hermes · ROMs** (`roms`).

## How a send works
Per file: `Get-FileHash -Algorithm SHA256`, the name UTF-8 → base64url (no padding), then
```
ssh -T -o BatchMode=yes -o ConnectTimeout=15 hermes-{{SERVER}} put <route> <name-b64url> <bytes> <sha256>
```
with the file streamed to ssh's standard input through `System.Diagnostics.Process` (`StandardInput.BaseStream`, 1 MB chunks). It is **never a PowerShell pipeline**: a pipeline turns bytes into strings and back and would corrupt the file. One more trap, found in the first end-to-end run: .NET Framework builds the child's `StandardInput` text writer from the console's input encoding and flushes it as soon as the process starts, so on a PC whose console is UTF-8 *with* a byte-order mark, three bytes (`EF BB BF`) went ahead of every file and the server refused each one ("more bytes arrived than declared"). The script switches the console input encoding to BOM-less UTF-8 around the process start, so the stream is exactly the file's bytes. Every argument is a route id, base64url, digits or hex (the script checks each token against the same rules as the server before composing the line), so Windows PowerShell 5's quote stripping has nothing to break. A progress bar shows for files over 8 MB.

The server streams the file into `<stage>/incoming/<id>/data`, checks size and SHA-256 and answers `ok <id>`. **Only that answer counts as delivered** to the door (Janus checks it next; the file is then delivered, quarantined or held, which `hermes status` on the server shows). Anything else is reported in one line and counted as not sent:

| ssh exit | Meaning | The line |
|---|---|---|
| 0, `ok <id>` | at the door | `name: at the door of {{SERVER}} on route servermail (0.3 MB, arrival 2026…)` |
| 255 | ssh itself failed | `could not reach {{SERVER}}. Is the server on? Is a VPN blocking Tailscale?`; or `refused the Hermes key` (run the `keys add` line); or the host-key line |
| 1 | the transfer failed (short, long, wrong hash, timed out) | the server's reason; the attempt is set aside on the server |
| 2 | refused (route, name, closed) | the server's reason, e.g. `the name starts with a dot` |
| 3 | the server's own problem | `tell the owner`, with the reason |

## Manual test checklist
CI can't run PowerShell against a real server, so these are done by hand on {{PC}} against {{SERVER}}; the protocol itself is tested on Linux with the same arguments, and `setup` is run for real in a temporary home folder (`tests/test_windows_client.py`). Write the date and the Hermes version next to a run.

| # | Test | How | Expect |
|---|---|---|---|
| 1 | all 256 byte values | a 256-byte file with every value 0–255: `[IO.File]::WriteAllBytes('all256.bin', [byte[]](0..255))`; `hermes.ps1 send servermail all256.bin` | `ok`; on the server `sha256sum <stage>/in/<id>/data` equals `Get-FileHash all256.bin` |
| 2 | 2 GB | a 2 GiB file of random bytes; send it | `ok`; the SHA-256 matches; the progress bar moved; nothing left in `<stage>/incoming/` |
| 3 | names | `Report (final) 'draft' [v2] é ü ß 日本語.txt` (spaces, parentheses, quotes, brackets, non-ASCII; `"` can't be in a Windows name) | `meta.json` `name` is exactly that, NFC |
| 4 | a refused name | `.hidden.txt` | exit 2, `refused by {{SERVER}}. the name starts with a dot …`; nothing in `in/` |
| 5 | server unreachable | `hermes.ps1 send servermail f.txt -Server nowhere-{{SERVER}}` (or the server off, or a VPN on) | one line: `could not reach … Is the server on? Is a VPN blocking Tailscale?`; with `-Window` the window stays open; the file is not counted as sent |
| 6 | key not installed | ping before `hermes keys add` (or after `hermes keys remove {{PC}}`) | `refused the Hermes key. Run the 'hermes keys add' line …` |
| 7 | empty file | a 0-byte file | `ok`, 0 bytes on the server |
| 8 | a folder, a missing file | send a folder; send `nope.txt` | `is a folder`, `not found`; the other files still go |
| 9 | Send To | right-click a file > Send To > Hermes · Mail | a window, the result line, closes after 3 s |
| 10 | Torrent | Send To > Hermes · Torrent on a `.torrent`, answer `1` | at the door on route `torrent-movies` |
| 11 | Magnet | copy a magnet link, run Hermes · Magnet (or `hermes.ps1 magnet -Pick all -Type 2`) | name + hash listed; `<hash8>.magnet` at the door on `torrent-shows` |
| 12 | collect | on the server `tailscale file cp f.txt {{PC}}:` as `hermes__f.txt`; `hermes.ps1 collect` | `f.txt` in `{{HOME_PC}}\Hermes-Mail\`, the folder opens, `1 file(s) arrived` |
| 13 | key does nothing else | the table in `docs/keys.md` | each refused |
| 14 | the port comes from the alias | on a PC whose `{{SSH}}` alias names the port of the server's OpenSSH: set the old `Host hermes-{{SERVER}}` block aside, `hermes.ps1 setup -Server {{SERVER}} -User <user> -Alias {{SSH}}`, `hermes.ps1 ping`, then item 9 | the new block has the alias's `HostName` and `Port`; `ping` answers; the Send To file is at the door |

Last run: 2026-10-20, hermes 0.2.1, {{PC}} → {{SERVER}} over Tailscale, against a user-level OpenSSH `sshd` on a test port bound to the server's Tailscale address (the server's port 22 is Tailscale SSH) with a test `routes.json`. Items 1–13 all as expected; the 2 GiB file took 945 s through a Tailscale relay and arrived with the same SHA-256. Item 14 came with 0.4.2 and has not been run by hand yet.
