# AGENTS.md

This repo's rules live in **CLAUDE.md** in this folder. Every agent (Claude, Codex/Astra, any other) follows it
exactly as written; this file only adds what every agent must do, whatever its tool.

## For every agent
- Read `CLAUDE.md` here first, then the owner's global instructions on the machine you run on.
- Claim the project in the Leafy hub before editing, and write a handoff when you stop (the hub's `/resume` and
  `/handoff` steps; another agent may hold the project, so check the claim first).
- Merges, deploys, repo settings, rule edits and deletions need the owner's yes in the chat, every time.
- Cut branches from `main`; never push to `main` directly.
- Never commit sound files, home paths, private network names or ports, credentials, paper PDFs, or reference
  game data.
- If this file and `CLAUDE.md` disagree, `CLAUDE.md` wins; say so in your handoff.
