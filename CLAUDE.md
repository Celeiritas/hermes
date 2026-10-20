# Hermes – notes for Claude Code

Hermes carries files and short messages between the devices of one tailnet, and fetches from allowlisted sources. **It carries and never runs, unpacks or installs anything**: it never opens archives, never sets an exec bit, never decides whether a file is allowed (Janus does) and never deploys (Zeus does). `docs/design.md` is the spec; read the section an issue names before changing anything.

## Map
- `hermes/` – the package, Python 3 standard library only (it may call `git` and `tailscale`). `cli.py` is the command line; each issue adds its command (`receive`, `door`, `send`, `keys`, `fetch/` ...). `config.py` reads, validates and explains `routes.json` (`config.load()` raises `ConfigError`; `config.check()` returns errors and warnings for `hermes check-config`); placeholders come from `realm.json` with Mentor's public defaults. `hermes.version()` reads `VERSION`.
- `scripts/build.sh` → `dist/hermes`, one file: a zipapp with a `python3` shebang, installed as `~/.local/bin/hermes` on the server. `scripts/check-private.sh` scans for personal data (staged changes, a range, `--tree`, `--history`); `scripts/install-hooks.sh` runs it before every push. Both are copied unchanged from argus-ui.
- `clients/windows/` – `hermes.ps1`, PowerShell 5 only (no Python on the PC). `plugin/` – the Elaia server plugin `delivery`. `units/` – systemd templates. `examples/routes.example.json` – placeholders only. `docs/` – design and the file formats.
- `tests/` – stdlib `unittest`, run by `bash tests/run.sh`. `tests/fixtures/janus` is a fake Janus that keeps to the contract in design §3; door tests use it. There is no real Janus in this repo.
- `.github/workflows/` – `tests.yml` and `check-private.yml` (the generic privacy check) run on every pull request.

## Test before you say it works
`bash tests/run.sh` (needs only python3, bash and git). `bash scripts/build.sh && ./dist/hermes --version` must work on a clean Linux checkout with only Python 3. Add a test under `tests/` for every feature.

## Rules
- **Hermes carries, never runs.** Nothing from config or from a delivery is ever executed, unpacked, imported or made executable. Doorbells (`after`) come from a fixed list in code. A delivery that Janus has not passed is never delivered.
- **Placeholders, never real names.** Public text (code, docs, examples, tests, commit messages) says `{{SERVER}}`, `{{PC}}`, `{{PHONE}}`, `{{SSH}}`, `{{HOME_PC}}`, `{{HOME_SERVER}}`, never a real device, user, path, host or account. The owner's real config lives in the private realm repo. Your own notes go in `CLAUDE.local.md` (gitignored).
- **Run `bash scripts/check-private.sh` before every push** (once per clone: `bash scripts/install-hooks.sh` makes it automatic). Fix what it finds; `private-ok` on a line is only for a deliberate example of a pattern, such as Tailscale's fixed ranges.
- **Branches are `<area>/<topic>`** (`door/quarantine`, `docs/design`). One issue → one pull request, small commits with clear messages. The owner merges.
- **Bump `VERSION` and add a `CHANGELOG.md` entry** for every change that ships.
- Standard library only on the server; PowerShell 5 only on the PC. If npm is ever used for test tooling, commit `package-lock.json`.
- Never commit keys, `jellyfin.key`, `secret.env`, logs, quarantine contents or sound files.
