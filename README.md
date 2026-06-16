<h1 align="center">roomieorder</h1>
<div align="center">

[![Build](https://img.shields.io/github/actions/workflow/status/Multipixelone/roomieorder/ci.yml?style=for-the-badge&logo=github&label=build&color=a6e3a1&labelColor=313244&logoColor=cdd6f4)](https://github.com/Multipixelone/roomieorder/actions)
[![License](https://img.shields.io/github/license/Multipixelone/roomieorder?style=for-the-badge&logo=creativecommons&color=b4befe&labelColor=313244&logoColor=cdd6f4)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12+-fab387?style=for-the-badge&logo=python&labelColor=313244&logoColor=cdd6f4)
![Nix](https://img.shields.io/badge/nix-flakes-89b4fa?style=for-the-badge&logo=nixos&labelColor=313244&logoColor=cdd6f4)

</div>

A self-hosted [Python](https://www.python.org/) service that turns a Home Assistant dashboard button into an automatic order — logged to Google Sheets and notified via Telegram. No confirm step; roommates are trusted.

Each item can declare two sources: **Costco** is tried first, falling back to **Amazon** when Costco is sold out, not carried, or over its price ceiling.

> note: drives real Costco/Amazon checkout via Playwright. brittle by nature, against the stores' ToS, and requires a persistent logged-in browser profile per store. Costco fronts the site with Akamai bot detection, which is far more aggressive than most retailers' — the automation may be blocked outright.

## Architecture

Two decoupled halves joined by a SQLite queue:

```
HA dashboard button → script.order_<item> → POST /reorder {item_key}
   validate + guards + enqueue → queue row (status=pending)

worker loop drains queue → Playwright buys → scrape order # + total
   → append to Google Sheet → Telegram notify
```

Intake is always-on; execution needs a live graphical session. Requests sit in the queue if the desktop is asleep and drain on wake.

## Commands

- [`serve`](./src/roomieorder/main.py) — start the FastAPI intake service + worker loop
- [`init-db`](./src/roomieorder/store.py) — initialize the SQLite schema
- [`catalog show`](./src/roomieorder/cli.py) — print all items in the catalog
- [`queue`](./src/roomieorder/cli.py) — show pending/recent queue rows
- [`test-notify`](./src/roomieorder/notify.py) — emit a test message via the configured notifier
- [`login --provider costco|amazon`](./src/roomieorder/cli.py) — open a store's profile to sign in by hand (one profile per store)
- [`dry-run ITEM_KEY --provider costco|amazon`](./src/roomieorder/cli.py) — navigate one store to checkout and screenshot without placing the order
- [`dump-dom ITEM_KEY --provider costco|amazon`](./src/roomieorder/cli.py) — read-only DOM + selector probe for bring-up

## Configuration

`catalog.json` maps item keys to shared fields (title, quantity, cooldown) plus a `costco` block (item number + URL) and/or an `amazon` block (ASIN + URL), each with its own expected price and price ceiling. Costco is tried first; Amazon is the fallback. At least one source is required. See [`examples/catalog.json`](./examples/) and [`examples/env.example`](./examples/) for the full schema.

### Safety rails

- **`DRY_RUN` flag** — stops before the final click; default `true` until you've confirmed each item reaches the review page cleanly
- **Double-tap guard** — same item within 60 s is ignored
- **Per-item cooldown** — `cooldown_days` from the catalog
- **Price ceiling** — per-source; on Costco an over-ceiling price falls back to Amazon, on the last store it aborts and alerts
- **Out-of-stock fallback** — Costco sold out / not carried / not found falls back to the item's Amazon source
- **Daily spend cap** — global `$` ceiling per rolling 24 h; pauses worker and alerts on breach
- **Challenge detection** — CAPTCHA / OTP pages halt the worker and ping you with a screenshot rather than looping

## Home Assistant integration

Each staple item gets a button card on the HA dashboard that calls a `rest_command` pointing at `POST /reorder`:

```yaml
rest_command:
  roomieorder_reorder:
    url: "http://localhost:8723/reorder"
    method: POST
    content_type: "application/json"
    payload: '{"item_key": "{{ item_key }}"}'

script:
  order_paper_towels:
    sequence:
      - service: rest_command.roomieorder_reorder
        data: { item_key: "paper_towels" }
```

## Google Sheets logging

Each order attempt appends a row: `timestamp | item_key | title | provider | product_id | qty | unit_price | order_total | order_id | status | requester | notes`

`status` ∈ `placed | dry_run | skipped_cooldown | price_blocked | failed | challenge`.

Requires a Google Cloud service account JSON with editor access on the target sheet.

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.default
```
