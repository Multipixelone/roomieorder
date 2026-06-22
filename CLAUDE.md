# CLAUDE.md

Entry point for AI agents (and new developers) working in this repo. Read this
first, then **`AGENTS.md`** — it has the hard-won context that isn't obvious from
the code, especially:

- **§0 Troubleshooting cheat-sheet** — symptom → first command → what to read.
- **§1 Green CI does not mean the buy flow works** — no test drives a real
  browser; every DOM selector in `purchase.py` is a best-guess flagged
  `# TODO(<store>): verify against live DOM`. A passing `pytest`/`nix flake
  check` proves only the pure helpers and the FakePurchaser path.

## What this is

A self-hosted service that turns a Home Assistant button into an automatic
Costco/Amazon order. FastAPI **intake** (`src/roomieorder/main.py`) enqueues to a
SQLite queue (`store.py`); an async **worker** drains it and drives a real
Playwright checkout (`purchase.py`), logging each attempt to Google Sheets
(`sheets.py`) and notifying over Telegram (`notify.py`). Safety rails live in
`guards.py` (debounce, cooldown, price ceiling, daily spend cap) and `DRY_RUN`
(stop before the final Place Order) is on by default.

## Read-only triage (safe — no browser, no spend)

These are allow-listed in `.claude/settings.json`, so they run without a prompt:

```bash
roomieorder doctor        # config, Chrome, display, profiles, DB, 24h spend, catalog
roomieorder status        # dry_run / paused / pending
roomieorder failures      # recent trouble rows + newest screenshots/DOM/probe
roomieorder queue         # recent queue rows
roomieorder catalog       # the item catalog
```

`dump-dom` / `verify-selectors` / `trace-order` hit live store pages read-only
(need a logged-in profile + network) and are operator-run, not CI. `trace-order`
walks the whole flow to the review page but is always DRY_RUN — it never orders.

The `.claude/commands/` slash commands (`/diagnose`, `/triage-failure`,
`/verify-selectors`, `/trace-order`, `/bring-up`) chain these for you.

## Dev / test

```bash
nix develop            # primary dev shell (pytest, ruff, mypy, Chrome, Playwright)
pytest -q              # unit tests (mock browser; no live store)
ruff check . && mypy src
```

No Nix? A SessionStart hook (`.claude/hooks/session-start.sh`) bootstraps a
`.venv` with the same tools; use `.venv/bin/pytest` / `.venv/bin/roomieorder`.

## House rules

- **Never** flip `DRY_RUN=false` or place a real order without explicit operator
  go-ahead (see `/bring-up`).
- **Don't edit live DOM selectors/markers in `purchase.py` blind.** Confirm
  against a real page first via `verify-selectors` / `trace-order`, then propose
  the change — `Read` the `*_dom.html` it dumps to find the real selector.
- Defaults are tuned to be money-safe; keep new behavior opt-in.
