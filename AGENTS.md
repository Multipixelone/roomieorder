# AGENTS.md

Notes for AI agents working in this repo, distilled from prior sessions. These
are things that are **not obvious from the code or git history alone** — read
before touching the buy flow, the catalog, or login/bot-detection logic.

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

The assistant's own Bash shell has graphical-session access on host `link`
(`WAYLAND_DISPLAY`, `DISPLAY` set), so headed Playwright can be driven directly
from Bash against a logged-in profile dir when faster iteration is wanted — but
the operator-run `dump-dom` path keeps live Costco hits under operator control,
which is the default.

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

**Pending follow-up:** patchright is not packaged in nixpkgs, so the deployed
Nix build still runs vanilla Playwright — the real-Chrome fix is active, the
CDP-leak fix is not. Packaging patchright's PyPI-fetched patched driver in
`nix/package.nix` is the remaining work.

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
