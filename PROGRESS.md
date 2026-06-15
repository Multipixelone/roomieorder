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

## Phases 1–6 — App

All code written and verified (`nix develop` → pytest/ruff/mypy, `nix build`,
NixOS module eval). 45 tests pass.

- **store.py** — SQLite queue + guard bookkeeping. Single table is the source
  of truth; `claim_next_pending` uses `UPDATE…RETURNING` so a row can't be
  claimed twice. WAL mode. Spend/cooldown/debounce all derived from the queue.
- **notify.py** — `OpenClawNotifier` shells out to `openclaw message send` per
  message (the daemon can't pipe through `openclaw-send.sh` like a oneshot).
  Best-effort: a delivery failure never fails an order. `NullNotifier` when no
  target is set.
- **guards.py** — pure functions. Intake tier (pause → debounce → cooldown)
  runs in `/reorder`; execution tier (price ceiling → daily spend cap) runs in
  the worker once a live price is known.
- **sheets.py** — gspread append, lazy client, best-effort. `NullSheets` when
  unconfigured. Columns match PLAN §3.5.
- **purchase.py** — full Playwright buy flow against the persistent profile.
  Resilient role/text+id selectors, per-step timeout, challenge detection
  (CAPTCHA/OTP/"verify it's you") that returns `challenge` instead of looping,
  screenshot on every failure, DRY_RUN stops at the review page. Only the pure
  helpers (`parse_price`, `looks_like_challenge`) are unit-tested; the browser
  path needs a real display + Amazon login.
- **main.py** — FastAPI intake (`/reorder`, `/health`, `/queue`) + worker
  thread (sync Playwright can't share the asyncio loop). Worker pauses on
  challenge/failure/spend-cap (PLAN §5).
- **cli.py** — serve / init-db / catalog / queue / test-notify / dry-run /
  pause / resume / status.

## Tests, module, CI

- 45 pytest cases over config, catalog, store, guards, notify, purchase
  helpers, and the intake endpoint (FakePurchaser — no browser).
- **nix/module.nix** — systemd **user** service bound to
  `graphical-session.target` (headed Chromium needs `$DISPLAY`/`$WAYLAND_DISPLAY`,
  which a system service can't reach). Pins `PLAYWRIGHT_BROWSERS_PATH` to the
  nixpkgs browsers; secrets via `environmentFile`; state under `%S/roomieorder`.
  Verified by evaluating it inside a minimal NixOS system.
- CI (already present, mirrors commutecop): pytest/ruff/mypy + `nix flake
  check` + Attic push.
- **examples/home-assistant.yaml** — `rest_command` + per-staple scripts +
  button-grid card (PLAN §3.1).

## What's left for the operator (PLAN §8 — needs hands-on / secrets)

- [ ] Create a Google service account, share the Sheet with its email; set
  `GOOGLE_SERVICE_ACCOUNT_JSON` + `ROOMIEORDER_SHEET_ID`.
- [ ] Create/reuse an OpenClaw Telegram target; set `OPENCLAW_TARGET`.
- [ ] Populate `catalog.json` with real ASINs + price ceilings (current
  entries are placeholders with fake ASINs).
- [ ] Launch the Chromium profile once, log into Amazon, confirm default
  address + 1-tap payment.
- [ ] `roomieorder dry-run <item>` for each staple until it reaches the review
  page, *then* flip `DRY_RUN=false` for one cheap item.

## infra handoff

- **PLAN-ROOMIE.md** added — a step-by-step for wiring roomieorder into the
  `infra` flake: it targets host **`link`** (the Hyprland desktop that already
  runs openclaw + commutecompass), agenix env-file secret from `nix-secrets`,
  the openclaw wrapper reuse, cross-host HA buttons on `iot` (HA POSTs to
  link's LAN addr `192.168.6.6:8723`), and the one-time manual bring-up.
- Confirmed the module **accepts an agenix env-file secret** via
  `environmentFile = config.age.secrets."roomieorder".path` (verified by NixOS
  eval: `EnvironmentFile` renders, `EnvironmentFile` overrides `Environment=`
  so `OPENCLAW_TARGET` etc. stay out of /nix/store).
- Hardened the module: state paths are now **relative to `WorkingDirectory`**
  (`%S/roomieorder`) instead of embedding `%S` in `Environment=` values, which
  isn't reliably specifier-expanded.
- PLAN.md reconciled with the as-built code (name, OpenClaw-only notify,
  env+catalog config, real layout, phase status).
