# Changelog

## 0.1.1 – 2026-10-20
- **routes.json loads, validates and explains itself.** `hermes/config.py` reads `~/.config/hermes/routes.json` (or `$HERMES_CONFIG`), expands `~` and `{{PLACEHOLDERS}}` from `~/.config/leafy/realm.json` (missing file → the public defaults Mentor uses), resolves symlinks and checks: route ids, `to` is a folder or `taildrop:<device>`, every stage is defined and exists, `conflict`, `after` (a fixed list of doorbells, never a command), no `to` inside a stage, and the taildrop and fetch sections name defined routes. Warnings for a route whose stage and `to` are on different drives (a pass would copy, not rename), for routes Janus's `~/.config/janus/routes.json` doesn't mention (every arrival would be quarantined) and for unknown settings
- **`hermes check-config`** prints one plain sentence per route ("lesson: files go to … (staged in home on the same drive; same name: the older copy is set aside).") and exits 1 on errors. Every error says what is wrong and how to fix it
- Tests: the example passes against a temporary HOME; one test per error and warning; the command

## 0.1.0 – 2026-10-20
- **Repo skeleton.** `hermes/` package (stdlib only) with `hermes --version`; `scripts/build.sh` makes `dist/hermes`, one zipapp with a `python3` shebang; `bash tests/run.sh` runs the `unittest` suite, with a fake Janus in `tests/fixtures/` that keeps to the door contract (pass, quarantine, duplicate, failure, forced verdicts)
- `scripts/check-private.sh` and `scripts/install-hooks.sh`, copied unchanged from argus-ui, stop personal data reaching the public repo; tests plant a home path, a `.ts.net` name, IPv4/IPv6 addresses and an email and expect the check to fail. GitHub Actions run the tests and the generic privacy check on every pull request
- `CLAUDE.md` (carry, never run; placeholders only; `<area>/<topic>` branches; one issue → one PR; bump `VERSION` + `CHANGELOG.md`; check-private before every push), `README.md` with the door picture, `examples/routes.example.json`, `.gitignore` for `CLAUDE.local.md`, `node_modules/` and test output
