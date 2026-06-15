# roomieorder — Household Auto-Buy Plan

> **Status: built.** This was the design doc (working name *Talon*); the project
> shipped as **`roomieorder`**. The app is implemented, tested, and packaged —
> see [`PROGRESS.md`](./PROGRESS.md) for the build log and
> [`PLAN-ROOMIE.md`](./PLAN-ROOMIE.md) for deploying it into the `infra` flake.
> Where this doc and the code disagree, the code wins; the notes below are
> reconciled to match what was actually built.
>
> Two deliberate changes from the original design:
> - **Notifications are OpenClaw-only** (the worker shells out to
>   `openclaw message send`), not direct Telegram — the daemon can't pipe
>   through `openclaw-send.sh` like a oneshot.
> - **Config is env vars + `catalog.json`**, not a TOML file. Secrets live in
>   the env / the browser profile.

A button on the Home Assistant dashboard → an item gets ordered from Amazon automatically → the purchase and price land in a Google Sheet. No confirm step; roommates are trusted.

---

## 1. The honest framing (read once, then never again)

Amazon has no consumer purchase API, so "order it" means a real browser driving real checkout. Talon uses **Playwright in headed mode against a persistent, already-logged-in Chromium profile**. This works well but is brittle and against Amazon's ToS:

- Selectors drift when Amazon redesigns checkout.
- Amazon can still throw an email-OTP or CAPTCHA challenge even with 2FA off, especially from a new automation pattern.
- Headed mode means **the desktop session must be awake and unlocked** for a purchase to execute.
- Worst case is an account flag. You're buying that risk knowingly.

Everything below is designed to fail *safely and loudly* rather than silently mis-spend.

---

## 2. Architecture

Two decoupled halves, joined by a SQLite queue:

```
HA dashboard button
   → script.order_<item>
   → REST POST /reorder {item_key}        ┐
                                          │  INTAKE (FastAPI, always up)
   validate + apply guards + enqueue      │
   → row in queue table (status=pending)  ┘

   ───────────────────────────────────────────────────

   worker loop drains queue               ┐
   → Playwright buys the item             │  EXECUTION (needs display)
   → scrape order # + total               │
   → append to Google Sheet               │
   → OpenClaw notify (success/fail)       ┘
```

**Why the queue:** intake is instant and always-on; execution needs the graphical session. If the desktop is asleep, requests sit in the queue and drain on wake. It also gives free retries on Amazon flakiness. Intake never blocks on the browser.

---

## 3. Components

### 3.1 HA dashboard — one button per staple
Each button calls a `rest_command` with a fixed `item_key`. No text box, no fuzzy matching.

```yaml
rest_command:
  roomieorder_reorder:
    url: "http://192.168.6.6:8723/reorder"   # link's LAN addr; HA runs on iot
    method: POST
    content_type: "application/json"
    payload: '{"item_key": "{{ item_key }}"}'

script:
  order_paper_towels:
    sequence:
      - service: rest_command.roomieorder_reorder
        data: { item_key: "paper_towels" }
```

Dashboard: a grid of `button` cards, one per script. Everything logs as
`household` (no per-roommate attribution — decided out of scope). Full example
in [`examples/home-assistant.yaml`](./examples/home-assistant.yaml); the
cross-host Nix wiring is in [`PLAN-ROOMIE.md`](./PLAN-ROOMIE.md) §3.

### 3.2 Item catalog — `catalog.json`
The "kind I like" lives here. You populate the ASINs once.

```json
{
  "paper_towels": {
    "title": "Bounty Quick-Size 12 Family Rolls",
    "asin": "B07XXXXXXX",
    "url": "https://www.amazon.com/dp/B07XXXXXXX",
    "qty": 1,
    "expected_price": 24.99,
    "price_ceiling": 32.00,
    "cooldown_days": 10
  },
  "toilet_paper": { "...": "..." },
  "dish_soap":    { "...": "..." },
  "protein_shakes": { "...": "..." }
}
```

- `price_ceiling`: if the scraped price exceeds this, **abort and alert** instead of buying (guards against a price spike or a hijacked listing).
- `cooldown_days`: block re-order inside this window.

### 3.3 Intake service — FastAPI
- `POST /reorder {item_key}` → look up catalog → run guards (§5) → if clear, insert queue row `status=pending` → return 200. Guard rejections return 200 too but notify "skipped, ordered 3d ago."
- `GET /health`, `GET /queue` for debugging.
- Runs as a systemd **user** service so it stays up across the session.

### 3.4 Purchase module — Playwright
- `chromium.launch_persistent_context(user_data_dir=<profile>, headless=False)`.
- **You log into Amazon once, by hand, into that profile.** With no 2FA the session persists for a long time.
- Buy flow per item:
  1. Goto `https://www.amazon.com/dp/<asin>`.
  2. Read the live price; if `> price_ceiling`, abort + alert.
  3. Click **Buy Now** (fall back to add-to-cart → checkout if Buy Now isn't present).
  4. On the single-page checkout, leave default address + payment.
  5. **If `DRY_RUN`: screenshot the review page, log "would order," stop here.**
  6. Else click **Place your order**.
  7. Scrape order number + order total off the confirmation page.
- Resilient selectors (role/text based, not brittle CSS), screenshot on every failure, hard timeout per step.
- **Challenge detection:** if a CAPTCHA / "verify it's you" / OTP page appears, do *not* loop — screenshot, mark the queue row `status=challenge`, pause the worker, and Telegram you to intervene manually.

### 3.5 Logging — Google Sheets (gspread + service account)
1. Create a Google Cloud service account, download its JSON key.
2. Share the target sheet with the service account's email (editor).
3. Append a row per attempt.

Columns: `timestamp | item_key | title | asin | qty | unit_price | order_total | order_id | status | requester | notes`

`status` ∈ `placed | dry_run | skipped_cooldown | price_blocked | failed | challenge`.

### 3.6 Notifications — OpenClaw
The worker shells out to `openclaw message send --channel … --target … --message …`
once per message (`notify.py` → `OpenClawNotifier`), attaching the screenshot
via `--photo` when there is one. Best-effort: a delivery failure is logged but
never fails an order. With no `OPENCLAW_TARGET` set it degrades to a
`NullNotifier` that just logs.

- ✅ placed: `Ordered Bounty 12-pack — $24.99 — #123-4567890.`
- 🧪 dry run: `[DRY] would order paper_towels at $24.99.`
- ⛔ price blocked / cooldown skip: short line, no action needed.
- ⚠️ failed / challenge: includes the screenshot; the worker is **paused** until you `roomieorder resume`.

---

## 4. NixOS deployment (the part with real gotchas)

### Playwright on NixOS
Playwright's own downloaded browsers **will not run** on NixOS (dynamic-linking mismatch). Use the nixpkgs-provided ones and pin them with the Python lib:

```nix
environment = {
  PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
  PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
};
```

The python `playwright` version must match the browser build shipped by `playwright-driver`. Pin both in the flake; this is the single most likely thing to break on a `nixpkgs` bump.

### Headed + display
Run as a **user** service in the graphical session, not a system service (a system service can't reach `$WAYLAND_DISPLAY`/`$DISPLAY`):

```nix
systemd.user.services.talon = {
  wantedBy = [ "graphical-session.target" ];
  partOf   = [ "graphical-session.target" ];
  serviceConfig.ExecStart = "${talon}/bin/talon serve";
  # inherits the session env; Wayland may need --ozone-platform=wayland in launch args
};
```

On Wayland, pass `--ozone-platform=wayland` (or let XWayland handle it) in the Playwright launch args.

### Secrets (sops-nix or agenix)
- Google service-account JSON.
- Telegram bot token + chat ID.
- Amazon login is *not* stored as a credential — it lives in the persistent browser profile after your one-time manual login.

### Layout (as built — mirrors commutecop)
```
roomieorder/
  flake.nix              # flake-utils + git-hooks.nix; pins playwright browsers
  pyproject.toml         # hatchling; ruff/mypy(strict)/pytest
  nix/
    package.nix          # buildPythonApplication
    module.nix           # systemd USER service (graphical-session.target)
  src/roomieorder/
    __main__.py
    config.py            # env loader (no TOML)
    catalog.py           # catalog.json loader + validation
    store.py             # SQLite queue + guard bookkeeping
    main.py              # FastAPI intake (/reorder /health /queue) + worker loop
    purchase.py          # Playwright buy flow (DRY_RUN, challenge detection)
    sheets.py            # gspread append (best-effort)
    notify.py            # OpenClawNotifier (subprocess)
    guards.py            # debounce, cooldown, price ceiling, spend cap
    cli.py               # serve/init-db/catalog/queue/test-notify/dry-run/pause/resume/status
  catalog.json
  tests/                 # 45 cases; non-browser logic + intake endpoint
  examples/              # env.example, catalog.json, home-assistant.yaml
  data/                  # (gitignored, runtime)
    profile/             # persistent Chromium profile
    state.sqlite         # queue + guard bookkeeping
    shots/               # failure screenshots
```

The systemd unit is a **user** service (`systemd.user.services.roomieorder`)
bound to `graphical-session.target`, not a system service — headed Chromium
needs the session's `$WAYLAND_DISPLAY`/`$DISPLAY`. State lives under
`%S/roomieorder` (`~/.local/state/roomieorder`).

---

## 5. Safety rails

- **`DRY_RUN` global flag** — stops before the final click. Default ON until you've watched it reach a review page for every item.
- **Double-tap guard** — same `item_key` within 60s is ignored (debounce in `state.sqlite`).
- **Per-item cooldown** — `cooldown_days` from the catalog.
- **Price ceiling** — abort + alert if live price > `price_ceiling`.
- **Daily spend cap** — global $ ceiling across all orders per rolling 24h; exceeding it pauses the worker and alerts.
- **Worker pause on challenge/failure** — never retry blindly into a CAPTCHA; halt and ping you.

---

## 6. Known limitations (decide later, not blockers)

- **No requester attribution** from a shared dashboard — everything logs as `household` unless you make per-roommate buttons.
- **Desktop must be awake** to execute; queued requests drain on wake, but there's latency if you're out.
- **Selector drift** — checkout redesigns will break the buy flow; the screenshots-on-failure make this fast to diagnose.
- **Occasional Amazon challenge** even without 2FA; handled by the pause-and-alert path, but it means it's never 100% unattended.

---

## 7. Build phases

Code phases 0–6 are **done** (✅ = implemented + tested in this repo). Phases
needing the operator's hands / account / a live display are ⏳ — they're carried
out during deploy (see [`PLAN-ROOMIE.md`](./PLAN-ROOMIE.md) §4).

0. ✅ **Scaffold** — repo, flake, `catalog.json` schema, `config.py`.
1. ✅ **Intake loop** — FastAPI `/reorder` `/health` `/queue` + HA button + worker.
2. ✅ **Sheets** — gspread append (best-effort; no-op until configured).
3. ⏳ **Playwright on NixOS** — browsers pinned + module wired; the one-time
   manual Amazon login into the persistent profile happens on deploy.
4. ✅ **Buy flow in `DRY_RUN`** — full flow written; stops + screenshots at the
   review page. (Exercised live per-item during bring-up.)
5. ⏳ **One real buy** — flip `DRY_RUN` off for one cheap item (deploy step).
6. ✅ **Guards + alerts** — cooldown, debounce, price ceiling, spend cap, challenge pause.
7. ⏳ **Fill the catalog** — replace the placeholder ASINs with real ones + add each dashboard button.

---

## 8. One-time manual setup checklist

- [ ] Create Google service account, share the Sheet with it.
- [ ] Create Telegram bot (or reuse OpenClaw's), note token + chat ID.
- [ ] Populate `catalog.json` with real ASINs + price ceilings.
- [ ] Launch Talon's Chromium profile once, log into Amazon, confirm default address + 1-tap payment are set.
- [ ] Verify checkout reaches the review page in `DRY_RUN` for each item before going live.
