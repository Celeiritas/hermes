# Changelog

## 0.1.0 – 2026-10-20
- **Repo skeleton.** `hermes/` package (stdlib only) with `hermes --version`; `scripts/build.sh` makes `dist/hermes`, one zipapp with a `python3` shebang; `bash tests/run.sh` runs the `unittest` suite, with a fake Janus in `tests/fixtures/` that keeps to the door contract (pass, quarantine, duplicate, failure, forced verdicts)
- `scripts/check-private.sh` and `scripts/install-hooks.sh`, copied unchanged from argus-ui, stop personal data reaching the public repo; tests plant a home path, a `.ts.net` name, IPv4/IPv6 addresses and an email and expect the check to fail. GitHub Actions run the tests and the generic privacy check on every pull request
- `CLAUDE.md` (carry, never run; placeholders only; `<area>/<topic>` branches; one issue → one PR; bump `VERSION` + `CHANGELOG.md`; check-private before every push), `README.md` with the door picture, `examples/routes.example.json`, `.gitignore` for `CLAUDE.local.md`, `node_modules/` and test output
