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
  the one-time manual Costco login are left for the operator (see §8 of PLAN).
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
  path needs a real display + Costco login.
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

- [x] Create a Google service account, share the Sheet with its email; set
      `GOOGLE_SERVICE_ACCOUNT_JSON` + `ROOMIEORDER_SHEET_ID`.
- [x] Create/reuse an OpenClaw Telegram target; set `OPENCLAW_TARGET`.
- [ ] Populate `catalog.json` with real Costco item numbers + slug URLs +
      price ceilings (current entries are placeholders — verify each URL).
- [x] Launch the Chromium profile once, log into Costco, confirm default
      shipping address + saved payment. (2026-06-17: profile logged in;
      `dry-run disinfecting_wipes` shows the shipping address + saved Mastercard,
      now auto-selected by `_select_payment_method`.)
- [ ] `roomieorder dry-run <item>` for each staple until it reaches the review
      page, _then_ flip `DRY_RUN=false` for one cheap item. (2026-06-17:
      `disinfecting_wipes` reaches the review page cleanly; the other staples
      and the `DRY_RUN=false` go-live are still pending.)

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

## HA buttons generated from catalog.json (single source of truth)

No more maintaining two lists. The catalog now drives the Home Assistant config:

- **`nix/ha-buttons.nix`** → flake `lib.haButtons { catalogFile; endpoint; }`,
  a pure builtins-only function returning `restCommand`, `scripts` (list of
  `{id; alias; sequence;}` matching infra's `iotHass.nixScripts`), `scriptsAttrs`
  (upstream `config.script` shape), and a `dashboardCard` button grid.
- **`nix/ha-module.nix`** → `nixosModules.homeAssistant`, a turnkey
  `services.roomieorder.homeAssistant` for HA hosts on the upstream
  `config.script` path (optional generated "Reorder" dashboard).
- Catalog gained optional `button` (short label) + `icon` (mdi) fields, used
  only by the generator; the buy flow ignores them.
- Flake `checks.ha-buttons` guards the generator invariants (one script + one
  button per item, well-formed ids) in CI.
- `examples/home-assistant.yaml` + PLAN-ROOMIE §3 rewritten: the YAML is now a
  reference of generated output; the infra glue calls `lib.haButtons` and feeds
  `iotHass.nixScripts`.
- Verified: `nix flake check` green (package, ha-buttons check, all three
  nixosModules incl. homeAssistant), generator eval matches catalog, 45 tests +
  ruff + mypy still clean.

## Costco checkout bring-up — payment, cart reset, branch consolidation (2026-06-17)

Live-verified the Costco checkout against the real `disinfecting_wipes` item (real
nix-secrets catalog, real Google Chrome for Akamai). All work is dry-run only — no live
Place Order has run.

- **Payment-method selection** (`c759fb6`): the live SinglePageCheckoutView needs an
  explicit click on the saved card before Place Order activates, and the default card
  isn't reliably pre-selected. Added `_select_payment_method` (clicks the saved-card
  radio `[automation-id='paymentReviewRadio']` unless already `aria-checked=true`) +
  `PAYMENT_METHOD_SELECTORS`, called from `_start_checkout`. Deliberately avoids the
  sibling "enter-a-new-card" radio.
- **Checkout landing** (`c759fb6`): `_start_checkout` now judges success by *landing* on
  the review page (`_on_checkout`: "checkout" in URL, place-order button as backstop)
  instead of trusting the Checkout-CTA click's return — the CTA navigates and Playwright
  reports the click as a miss when the context tears down mid-navigation, which had made
  the dry-run bail `no_buy_button` despite reaching checkout.
- **Verified checkout selectors** (`c759fb6`): corrected `PLACE_ORDER_SELECTORS` to
  `#place-order-button-regular` (old `automation-id='placeOrderButton'` was count=0);
  confirmed `[automation-id='orderTotalOutput']` is correct. See IMPROVEMENTS.md #8 + the
  🔵 selector caveat (updated).
- **Cart reset** (`6f5094d`, cherry-picked from `fix/costco-cart-reset-and-delivery-stock`):
  `_reset_cart` drains the shared server-side cart before each buy so a stale line from a
  prior run can't ride into a live Place Order; Costco drains via `removeItemLink_*` on
  CheckoutCartDisplayView (bounded at 30, visibility-gated confirm dismissal). Also
  narrowed Costco `OUT_OF_STOCK_MARKERS` to delivery/online wording so the per-warehouse
  pick-up widget no longer false-positives a deliverable item to Amazon. Live-verified:
  dry-run subtotal dropped $87.96 → $21.99 (stale lines drained, exactly one item added).
- **Branch consolidation**: audited all branches `--no-merged main`. Every one except the
  cart-reset branch was already in main *by content* (squash-merged via punchlist PR #9);
  `costco-switch` is a true ancestor. Brought the one genuinely-missing branch in.
- Verified end-to-end: 118 tests pass, ruff + mypy clean; `dry-run disinfecting_wipes`
  reaches the review page with the saved Mastercard selected.

## IMPROVEMENTS punch-list closed out — typing pass + doc reconciliation (2026-06-17)

Audited every IMPROVEMENTS.md item against the code: #1–#10, #12, #14, #15, #16 were
already implemented by prior commits but the doc still listed them as open, and #13 was
resolved-by-documentation in `conftest.py`. The one genuinely-open code item was **#11**
(typing).

- **#11 typing** (two atomic commits): `page` is now the Playwright `Page` type
  (TYPE_CHECKING import), and `BasePurchaser` is `Generic[SourceT]` over
  `CostcoSource`/`AmazonSource` with `item: CatalogItem`, so the per-store hooks take the
  concrete source and the `getattr` round-trips are gone. `# type: ignore` count in
  `purchase.py` dropped **42 → 5** (only the dynamic patchright/playwright *module*
  handles keep theirs). Typing surfaced and fixed a latent bug: `page.title(timeout=…)` —
  `Page.title()` takes no timeout, so both call sites were raising `TypeError` (swallowed
  by their best-effort `try/except`, timeout never applied). Added a `_ClickRole` Literal
  alias + annotated `_settle`'s state tuple so the role/load-state args type-check.
- **Doc reconciliation**: `IMPROVEMENTS.md` now carries a verified **✅ Resolved** note on
  every item, each citing the implementing code; a status banner records the list is
  worked through, leaving only the 🔵 confirmation-selector caveat.
- Verified: `nix develop` → mypy --strict + ruff clean, 118 tests pass. No money-path
  behaviour changed — the typing pass is annotations + getattr→attribute access only.
