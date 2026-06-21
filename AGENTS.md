# AGENTS.md

Notes for AI agents working in this repo, distilled from prior sessions. These
are things that are **not obvious from the code or git history alone** — read
before touching the buy flow, the catalog, or login/bot-detection logic.

## 0. Troubleshooting cheat-sheet

Start every "why did X break" investigation with the read-only diagnostics
below — they're safe (no browser, no spend) and tell you where to look. The
`.claude/commands/` slash commands (`/diagnose`, `/triage-failure`,
`/verify-selectors`, `/bring-up`) chain these for you.

| Symptom | First command | Then read |
| --- | --- | --- |
| "is anything wrong?" / cold start | `roomieorder doctor` | its own output (config, Chrome, display, profiles, DB, catalog) |
| "the order didn't place" | `roomieorder failures` | the newest `*.png` it lists, plus the row's `notes` |
| selector miss / store redesign | `roomieorder verify-selectors [item]` | the `*_dom.html` it points at, then read the live selector off it (§1) |
| logged out / sign-in wall | `roomieorder dump-dom <item>` | §2 — prefilled ≠ logged in; check the **logon URL**, not header text |
| CAPTCHA / OTP challenge | (worker auto-pauses) `roomieorder status` | §1, §3 — Akamai may be blocking; this is expected-until-verified |
| Sheets row never appeared | `roomieorder test-sheet` | the gspread error (`-v`); a no-op logger silently "succeeds" otherwise |
| Telegram silent | `roomieorder test-notify` | `OPENCLAW_*` env + the openclaw binary |
| stuck after a failure | `roomieorder failures` → fix → `roomieorder retry <id>` | refuses `needs_review`/`placed` (may have ordered) |

`doctor`/`failures`/`status`/`queue`/`catalog`/`dump-dom`/`verify-selectors`
are read-only and allow-listed in `.claude/settings.json`, so they run without a
permission prompt. `verify-selectors` (and `dump-dom`) hit live store pages
read-only and need a logged-in profile + network — they're operator-run, not
CI.

**Queue statuses** (`store.py`, also the Sheets `status` column): `pending` /
`in_progress` (transient); `placed` (done); `dry_run`; `skipped_cooldown` /
`skipped_debounce` (guard declined); `price_blocked` / `spend_capped` (money
guard); `unavailable` (sold out → triggers Amazon fallback, terminal on the
last store); `failed`; `needs_review` (Place Order clicked but confirmation
unread — **may have ordered, never auto-retried**); `challenge`.

`failed` / `challenge` / `needs_review` / `spend_capped` **pause the worker**
(`main.py:_PAUSE_STATUSES`); clear the cause, then `roomieorder resume`.

**Screenshot tags** (suffix on files in the shots dir, written on each failed
step) tell you which stage died: `product` / `no_price` / `unavailable` /
`guard_block` / `no_buy_button` / `no_place_order` / `signin_*` / `challenge_*`
/ `submitted_unconfirmed` / `confirmation` / `review` / `timeout` / `crash` /
`dump`. `verify-selectors` and `dump-dom` also write `*_dom.html` (rendered
page) and `*_probe.txt` (per-selector match counts) — `Read` those to find the
real selector instead of guessing.

## 1. Green CI does not mean the buy flow works

`nix flake check` / pytest passing only proves the pure helpers are correct
(`parse_price`, `looks_like_challenge`, `looks_like_signin`,
`ensure_logged_in`/`_place_order` exercised against a `_FakePage`) and that the
FastAPI intake endpoint works against a `FakePurchaser`. **No test drives a
real browser.** The entire Playwright purchase path in
`src/roomieorder/purchase.py` has never executed against live Costco.

Every DOM-dependent constant in that file is an explicit best-guess, commented
"verify against live DOM": `_CHALLENGE_MARKERS`, `_SIGNIN_MARKERS`,
`_SIGNIN_SUBMIT_SELECTORS`, `_PRICE_SELECTORS`, `_ADD_TO_CART_SELECTORS`,
`_PLACE_ORDER_SELECTORS`, `_ORDER_TOTAL_SELECTORS`, `_ORDER_ID_RE`, the
account-nav check in `is_logged_in`, the cart URL (`/CheckoutCartView`), and
the add-to-cart → cart → checkout → review step order. They must be confirmed
during operator bring-up (`roomieorder login`, `roomieorder dry-run <item>`),
not assumed correct because tests pass.

On top of that, Costco fronts both the storefront and `signin.costco.com`
with Akamai bot detection that may block the automated profile outright —
see `PLAN.md` §1, §6. This is a known possible outcome, not a bug to chase.
`DRY_RUN` defaults to `true` and stops before the final click.

**If asked to "fix the buy flow" or "why didn't my order place":** don't
promise the purchase works off a clean CI run. The real verification surface
is live DOM during bring-up, plus the failure screenshots written to the
shots dir. Treat a buy-flow failure as expected-until-verified.

**Verifying a selector instead of guessing — `roomieorder dump-dom <item>`.**
This is the read-only bring-up command (`CostcoPurchaser.dump_dom`): it opens
the product page reusing the logged-in profile + the same stealth launch as
`buy`, **stops before add-to-cart** (never orders), and writes three artifacts
to the shots dir — `…_dom.html` (rendered `page.content()`), `…_probe.txt`
(per-selector match counts + text samples, the resolved `read_price`, and any
JSON-LD offer price), and `…_dump.png`. To fix a selector miss: ask the
operator to run `roomieorder dump-dom <item>` (real deployment writes to
`~/.openclaw/media/roomieorder/`), then `Read` the dumped `*_dom.html` and read
the real selector off it. The price renders logged-out, so the price selector
can be verified without a session; cart/checkout/place-order selectors need a
logged-in profile. (`_PRICE_SELECTORS` already has a structured-data fallback —
`og:price`/`product:price:amount` meta tags, then JSON-LD `offers.price` — for
when the visible-price CSS guesses miss on the `/p/-/<slug>/<id>` storefront.)

The assistant's own Bash shell on host `link` can reach the graphical session,
so headed Playwright (`dump-dom`, `dry-run`, `login`) can be driven directly
from Bash against a logged-in profile dir when faster iteration is wanted — but
the operator-run `dump-dom` path keeps live Costco hits under operator control,
which is the default. **Catch: the Bash-tool shell does _not_ export
`WAYLAND_DISPLAY`/`DISPLAY` by default — they're empty.** The compositor sockets
exist (`/run/user/$(id -u)/wayland-1`, `/tmp/.X11-unix/X0`), but Chrome launches
with a hardcoded `--ozone-platform=wayland` and dies with `Failed to connect to
Wayland display` → patchright reports it as `TargetClosedError: ...browser has
been closed` — looks like a crash, is actually a missing display env. Prefix any
headed run with `WAYLAND_DISPLAY=wayland-1 DISPLAY=:0` (after the `direnv export`
eval). This only bites the agent shell; the systemd **user** service inherits
the real session env (`nix/module.nix` §4), and an operator's own terminal
already has both set — so **don't** hardcode them into the Nix wrapper, whose
values (`wayland-1`/`:0`) are session-specific and would override correct
runtime values.

**What CI now does prove (real-DOM regression net).** The verified
**product-page** selectors carry an offline regression net in
`tests/test_dom_fixtures.py` (`@pytest.mark.browser`): it replays the real
Playwright locator engine — via the headless browser the nix shell ships —
against committed, sanitized `dump-dom` snapshots of live Costco HTML
(`tests/fixtures/dom/`), so a `PRICE_SELECTORS`/`ADD_TO_CART_SELECTORS` edit that
no longer matches the real page now fails CI instead of slipping through green.
A captured, PII-scrubbed **checkout** review page would extend the net to
`PLACE_ORDER_SELECTORS`/`PAYMENT_METHOD_SELECTORS`/`ORDER_TOTAL_SELECTORS` (the
test exists and skips until one is committed). Two caveats remain: the net only
drifts when a fixture is **re-captured** (it can't see a future Costco redesign —
that's the deferred live `verify-selectors` watchdog), and the **confirmation**
page stays unverifiable here (only reachable past a real Place Order) — the
standing 🔵.

## 2. Costco login: prefilled fields ≠ logged in

`roomieorder login` (manual hand-login, `CostcoPurchaser.login` in
`src/roomieorder/purchase.py`) caches email/password in the persistent
profile, so the fields prefill on return visits. But Costco does **not**
register the session as logged in until the operator actually clicks **Sign
In** — a prefilled-but-unclicked state looks logged in but isn't, and will
fail `is_logged_in` / hit the sign-in wall later.

`CostcoPurchaser.ensure_logged_in` already handles this: when `is_logged_in`
reads logged-out, it clicks the prefilled logon submit, then reloads the
product page. Any new login/re-login automation or operator instructions must
preserve that explicit click — don't short-circuit just because credentials
are cached.

Also: the "Sign In / Register" header link appears on **every** logged-out
page, so sign-in-wall detection (`looks_like_signin` / `_SIGNIN_MARKERS`)
keys off the **logon URL**, not header text — matching on text false-positives
a normal product page as a sign-in wall.

## 3. Akamai bot-detection hardening — don't undo this

The buy flow is deliberately hardened against Costco's Akamai bot detection
across `purchase.py`, `config.py`, and `nix/module.nix`:

- **Real Google Chrome, not bundled Chromium.** `_launch_context` passes
  `executable_path` (`ROOMIEORDER_CHROME_PATH`, set by the Nix module to
  `lib.getExe cfg.chromePackage`, default `pkgs.google-chrome`) or
  `channel="chrome"`. Chromium's missing proprietary codecs and "Chromium"
  Sec-CH-UA brand are the biggest tells.
- **`no_viewport=True`** — drops Playwright's emulated 1280×720 viewport,
  which mismatches a real window.
- **patchright auto-preferred.** `_playwright_api()` imports
  `patchright.sync_api` if installed (the `stealth` extra in `pyproject.toml`,
  `patchright>=1.40`), else falls back to stock Playwright. patchright closes
  the CDP `Runtime.enable` leak that `--disable-blink-features=AutomationControlled`
  doesn't reach.
- Deliberately **no** custom `user_agent`/headers and **no** stealth JS init
  scripts — those backfire on Akamai (inconsistent, more detectable than
  doing nothing).

**patchright ships in the deployed Nix build.** It is packaged from its PyPI
wheel in `nix/patchright.nix` and wired into `propagatedBuildInputs` via
`callPackage` in `nix/package.nix` ("two stealth layers, both active in this
build"); `nix/module.nix` sets `PLAYWRIGHT_NODEJS_PATH` so its bundled Node
driver runs on a Nix node. So **both** stealth layers are live in production —
real Chrome *and* the CDP-leak fix — not just on a `pip install -e .[stealth]`
dev checkout. (`pyproject.toml`'s `stealth` extra is the optional pip path; Nix
wires patchright separately through `callPackage`, so the extra stays optional.)

**Don't:** revert to bundled Chromium, or add stealth JS shims/custom headers
to "improve" evasion — both are known to backfire here.

## 4. This repo is the app, not the deployment

`catalog.json` in this repo holds only 3 placeholder staples (`paper_towels`,
`toilet_paper`, `dish_soap`) with **unverified** Costco item numbers/URLs. It
exists for examples/tests, not as the live catalog.

The actual running instance lives outside this repo:

- Per `DASHBOARD.md`, a live Home Assistant "Reorder" dashboard (storage
  mode, dashboard `main-home`) already carries **~25 real items**.
- Per `PLAN-ROOMIE.md`, deployment is wired through the user's `infra` flake:
  the service runs as a systemd **user** service on host **`link`**
  (Hyprland/Wayland — headed Chromium needs the graphical session); Home
  Assistant on host **`iot`** POSTs cross-host to `link` at
  `192.168.6.6:8723`; secrets (Sheets key, OpenClaw target, optionally the
  catalog) come from **agenix** via `nix-secrets`.
- HA buttons/scripts/status-sensors are generated from whichever
  `catalog.json` the deployment points at, via `lib.haButtons`
  (`nix/ha-buttons.nix`) — so a catalog is the single source of truth only
  for its own deployment, not universally.

**If asked to change "the items" or catalog for the live system:** edit the
deployment's catalog in `infra`/`nix-secrets`, not this repo's
`catalog.json`. To inspect the live dashboard, see `DASHBOARD.md`.
