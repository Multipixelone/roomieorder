# Build log — roomieorder

Running log of the build. Newest entries at the bottom of each phase.

## Decisions

- **Name**: `roomieorder` (the repo + README already settled on this; the PLAN's
  "Talon" working name is dropped).
- **Notifier**: OpenClaw only. Because `serve` is a long-running daemon (not a
  oneshot piped through `openclaw-send.sh` like commutecompass), the notifier
  shells out to the `openclaw` binary directly per message
  (`openclaw message send --channel … --target … --message …`).
- **Purchase module**: full Playwright buy flow written, but only the
  `DRY_RUN` / non-browser paths are exercised in this build. The real buy and
  the one-time manual Amazon login are left for the operator (see §8 of PLAN).
- **Config**: env vars + `catalog.json` (per the README), not a TOML file.
  Lighter than commutecompass; secrets stay in env / the browser profile.
- **Conventions mirror `commutecop`**: flake-utils + git-hooks.nix, hatchling
  `buildPythonApplication`, ruff/mypy(strict)/pytest, `src/` layout, Attic CI.

## Phase 0 — Scaffold

- flake.nix, pyproject.toml, nix/package.nix, .gitignore, .envrc
- src/roomieorder skeleton, config.py (env loader), catalog.py
- catalog.json + examples/
