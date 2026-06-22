# roomieorder — stability & design review

A standing punch-list of things that could be **more stable** or **better designed**,
to be iterated over and fixed later. Ordered roughly by risk. Each item notes the
location, the problem, and a suggested direction. Nothing here is a request to change
behaviour silently — money moves through this system, so each fix wants its own
verification.

**Status (2026-06-17):** the whole list below has been worked through — every item
now carries a verified **✅ Resolved** note citing the implementing code. The one
standing caveat is the unverified *confirmation* selector layer (🔵), which can only
be confirmed past a real Place Order that has deliberately never run.

Legend: 🔴 correctness / money-safety · 🟠 stability / reliability · 🟡 design / maintainability · 🔵 known & tracked

---

## 🔴 Correctness & money-safety

### 1. `parse_price` mis-parses grouped whole-dollar prices
`src/roomieorder/purchase.py` (`parse_price`)

The "treat the last `.`/`,` as the decimal point" heuristic assumes cents are always
present. They aren't always:

```
'$1,234' -> 1.234      # should be 1234.0
'$1,000' -> 1.0        # should be 1000.0
'$2,000.00' -> 2000.0  # ok (has cents)
```

This is the **dangerous direction**: an item that really costs `$1,000` reads as
`$1.00`, sails under every `price_ceiling`, and the spend cap (`live_price * qty`) is
computed against `1.0`. The price-ceiling guard — the main defence against a spike or a
hijacked listing — is silently defeated. Staples rarely hit four figures, but the
failure mode is "order anyway," not "fail safe."

**Fix direction:** disambiguate grouping vs decimal by separator *position* (a `,`/`.`
followed by exactly 3 digits and end-of-number is grouping, not a fraction), or parse
with locale awareness. Add the above cases to `tests/test_purchase.py`.

✅ **Resolved:** `parse_price` now splits on the last separator only when its trailing
run is 1, 2, or 4+ digits (a real fraction); a lone 3-digit tail is treated as a
thousands group and the number is integral (`$1,234`→`1234.0`, `$1,000`→`1000.0`,
`$2,000.00`→`2000.0`). Covered by the grouping cases in `tests/test_purchase.py`.

### 2. No idempotency around `_place_order` → confirmation scrape
`src/roomieorder/purchase.py` (`buy` / `_place_order` / `_scrape_confirmation`)

`_place_order` clicks Place Order, then `_scrape_confirmation` reads the order id/total.
If the click *succeeds* but the page then times out / the scrape throws, `buy` returns
`failed` (or the worker crashes) for an order that **was actually placed**. A manual
`resume` + re-tap then re-orders. There is no order-id de-dup and no "did we already
submit?" check.

**Fix direction:** before clicking, record an intent marker (row → `in_progress` with a
"submitting" note); after a failed scrape, treat the row as *needs-human-confirmation*
rather than `failed`, and never auto-retry a row that reached the submit step. Consider
scraping the confirmation defensively (retry the read) before giving up.

✅ **Resolved:** `buy` flips a `submitted` flag the instant Place Order is clicked; past
that point *every* failure/timeout/crash/scrape-miss routes to `_submitted_unconfirmed`
→ the pausing, non-fallback `needs_review` status rather than `failed`, so nothing
re-drives a possibly-placed order. `_scrape_confirmation` retries the body read before
giving up, and `MAX_ATTEMPTS=1` + `recover_stale` (see #5/#6) guarantee a row that
reached the worker is never auto-retried.

### 3. Costco order-id regex matches any 9–12 digit run on the page
`src/roomieorder/purchase.py` (`ORDER_ID_RE` + `_find_order_id`)

`ORDER_ID_RE = \b\d{9,12}\b` is searched against the **whole confirmation body text**.
Phone numbers, item numbers, ZIPs+4, and timestamps can all match first and be recorded
as the order id. (Amazon's `\d{3}-\d{7}-\d{7}` is far safer.)

**Fix direction:** anchor to a labelled element ("Order #", "Confirmation number") via a
selector, falling back to the regex only within that element's text.

✅ **Resolved (structurally):** `_find_order_id` now prefers the label-anchored
`ORDER_ID_LABEL_RE` (`(?:order|confirmation)…(\d{7,12})`, capture group 1) and only
falls back to the bare `ORDER_ID_RE` when no label is found. Covered by
`tests/test_purchase.py`. The *exact* Costco label/format is still a 🔵 live-DOM TODO
(reachable only past a real Place Order), but a stray digit run can no longer win over a
labelled one.

### 4. Intake endpoint is unauthenticated
`src/roomieorder/main.py` (`POST /reorder`)

Anyone who can reach the port can place real orders. Default bind is `127.0.0.1`
(`config.host`), which mitigates — but the whole point is for Home Assistant to call it,
and the moment `ROOMIEORDER_HOST` is widened to serve HA on the LAN, every device on the
network can trigger spending.

**Fix direction:** a shared-secret header/token checked in `/reorder` (and ideally
`/pause`/`resume` if those ever get HTTP handles). Keep it optional/off for the
loopback-only default so local dev isn't burdened.

✅ **Resolved:** `config.intake_token` (env `ROOMIEORDER_INTAKE_TOKEN`) is checked by
`_require_token` on `/reorder` and `/reload`, compared in constant time
(`hmac.compare_digest`) and sent as the `X-Roomieorder-Token` header. Empty by default,
so the loopback-only dev path is unburdened; set it the moment `host` is widened.

---

## 🟠 Stability & reliability

### 5. `in_progress` rows are orphaned on a hard restart
`src/roomieorder/store.py` (`claim_next_pending`) + `main.py` (`_process`)

`claim_next_pending` only ever selects `status='pending'`. The `_process` `try/except`
converts *exceptions* to `failed`, but if the **process itself** dies (SIGKILL, OOM,
power loss, systemd restart) between the claim and the `mark`, the row is stuck
`in_progress` forever: never re-claimed, never failed, never surfaced.

**Fix direction:** on `init_db`/startup, reset stale `in_progress` rows (back to
`pending` if under an attempts cap, else `failed` + pause). Pair with item #6.

✅ **Resolved:** `Store.recover_stale()` runs once at startup via
`Engine._recover_orphans`. A stale row at the attempts cap → `failed` (may have placed —
a human checks, never auto-retry); one under the cap → back to `pending`. When any are
recovered the worker is paused and the operator is notified. Idempotent; covered by
`tests/test_store.py`.

### 6. `attempts` is incremented but never enforced
`src/roomieorder/store.py`

Every claim does `attempts=attempts+1`, but nothing reads `attempts`. There's no retry
cap, so combined with #5 a row could be retried indefinitely once recovery is added.

**Fix direction:** define a max-attempts policy (likely 1 for a money-moving step — fail
to the operator rather than retry a possibly-placed order), and have the recovery in #5
respect it.

✅ **Resolved:** `MAX_ATTEMPTS = 1`. `claim_next_pending` guards `attempts < MAX_ATTEMPTS`
so an exhausted row is never re-claimed, and `recover_stale` fails (not retries) a row at
the cap. One attempt is the deliberate policy for a money-moving step.

### 7. One shared SQLite connection across the worker thread and uvicorn's threadpool
`src/roomieorder/store.py`

`check_same_thread=False` with a single `_conn` shared by the async intake threadpool and
the worker daemon. `commit` is connection-global, so one thread's `commit()` flushes the
other thread's half-finished write — fragile, and any future multi-statement transaction
is silently unsafe.

**Fix direction:** a connection per thread, a short-lived connection per operation, or a
`threading.Lock` wrapping each method.

✅ **Resolved:** every `Store` method runs under a `threading.RLock`, serialising whole
operations so each method's `commit()` is atomic relative to the other thread and any
future multi-statement transaction stays safe (WAL remains on).

### 8. `_place_order` text fallback can click the wrong element
`src/roomieorder/purchase.py` (`_place_order`)

The last-resort `page.get_by_text(/place (your )?order/i).first.click()` will happily
click a *heading* or *label* containing that text, not the button.

**Fix direction:** restrict the text fallback to clickable roles, or drop it in favour of
the role-named button + verified CSS ids once the live DOM is known.

✅ **Resolved 2026-06-17:** the last-resort fallback iterates *clickable roles*
(`button`/`link`) rather than a bare `get_by_text`, so it can no longer click a
heading/label — and the place-order button is live-verified: `#place-order-button-regular`
is the first `PLACE_ORDER_SELECTORS` entry (the old `[automation-id='placeOrderButton']`
guess was count=0).

### 9. `buy`'s catch-all swallows programmer errors as `failed`
`src/roomieorder/purchase.py` (`buy`)

The bare `except Exception` turned *every* error (AttributeError, TypeError, a bad
selector type) into a `failed` PurchaseResult, laundering real bugs into "store flakiness."

**Fix direction:** let a small allowlist of "this is a bug" exceptions propagate (or at
least log full tracebacks and distinguish them), so a code defect doesn't masquerade as a
sold-out item.

✅ **Resolved:** `_BUG_EXCEPTIONS` (`AttributeError`, `TypeError`, `NameError`,
`ImportError`, `NotImplementedError`) are caught separately, screenshotted, and
**re-raised** out of `buy` so the worker loop records them and pauses — they can no longer
hide as a `failed` result. Store flakiness still converts to a safe `failed`/`needs_review`.

### 10. Spend cap is checked against an estimate, not the real total
`src/roomieorder/guards.py` + `orchestrator.py`

The cap uses `live_price * qty` as the prospective total, but tax/shipping/fees aren't
known until the order total is scraped. An order can land over `daily_cap` once real
totals accrue.

**Fix direction:** add a margin to the prospective estimate, and/or re-check the cap
against `order_total` post-scrape and pause if the *recorded* trailing-24h spend exceeds
the cap.

✅ **Resolved:** `Engine._enforce_recorded_cap` re-checks the *recorded* trailing-24h
spend (`store.spend_since`) after each `placed` order and pauses + notifies before the
next buy when the real total has breached `daily_cap`. It can't unwind the order just
placed — it stops the next one. The pre-buy estimate guard stays as the first line.

---

## 🟡 Design & maintainability

### 11. `page`/`source`/`item` typed as `object` throughout `purchase.py`
`src/roomieorder/purchase.py`

mypy runs `strict`, yet nearly every Playwright/catalog interaction was `object` + a
`# type: ignore[attr-defined]`. Strict typing paid its cost (the ignores) without its
benefit (catching a wrong attribute/selector call). `getattr(source, "item_number")` with
no default also defeated the typed `CostcoSource`/`AmazonSource` already in hand.

**Fix direction:** type `page` against a Playwright `Page` (import under `TYPE_CHECKING`),
and pass the concrete `CostcoSource`/`AmazonSource` types into the purchasers instead of
`object`.

✅ **Resolved 2026-06-17:** `page` is typed `Page` (TYPE_CHECKING import); `BasePurchaser`
is `Generic[SourceT]` over `CostcoSource`/`AmazonSource`, with the concrete source threaded
to the per-store hooks; `item` is `CatalogItem`; the `getattr` round-trips are gone. The
`# type: ignore` count dropped 42 → 5 (only the genuinely-dynamic patchright/playwright
*module* handles remain). Typing immediately surfaced and fixed a latent bug —
`page.title(timeout=…)` (the method takes no timeout, so it was raising `TypeError` under
its best-effort `try/except`). mypy --strict + ruff clean; 118 tests pass.

### 12. Catalog is loaded once at startup; edits need a restart
`src/roomieorder/main.py` (`Engine.__init__` → `load_catalog`)

`self.catalog` is captured at boot. Adding an item or fixing a ceiling required a service
restart.

**Fix direction:** a `/reload` endpoint or an mtime check, or document the restart
requirement.

✅ **Resolved:** `POST /reload` (token-checked) calls `Engine.reload_catalog`, which
re-reads the file and swaps `self.catalog` **only on a clean parse** — a malformed edit
raises `CatalogError`, surfaced as a 400, leaving the running catalog untouched.

### 13. Test catalog duplicates `catalog.json`
`tests/conftest.py` (`_CATALOG`)

Two hand-maintained sample catalogs that could drift.

**Fix direction:** load one shared sample, or clearly label the fixture as an
intentionally-minimal two-source/one-source matrix (which it is — that's its value).

✅ **Resolved:** `conftest._CATALOG` is now explicitly documented as a purpose-built
two-source (Costco+Amazon) / one-source (Costco-only) matrix that exercises both
fallback paths — *not* a copy to keep in sync with `catalog.json` (which is itself only a
placeholder; the real catalog ships in `infra/nix-secrets`, so there's no single source
of truth to converge on). The minimal matrix is intentional.

### 14. `item_statuses` recomputes with N queries per poll
`src/roomieorder/main.py`

One `last_placed_at` query per catalog item on every `/items` poll.

**Fix direction:** a single grouped query if it ever matters.

✅ **Resolved:** `item_statuses` uses the single grouped `Store.last_placed_at_all()`
(`MAX(updated_at) … WHERE status='placed' GROUP BY item_key`), so the poll cost is flat in
the catalog size.

### 15. Dependency automation overlaps: Renovate + a flake-update workflow
`renovate.json` + `.github/workflows/flake-update.yml`

Two mechanisms that could both touch dependencies/lockfiles.

**Fix direction:** scope each to a clear lane and document it.

✅ **Resolved:** `renovate.json` documents and enforces the split — Renovate owns
`pyproject.toml` deps and GitHub Action digests; `flake-update.yml` owns `flake.lock`; the
Nix manager is disabled so Renovate never opens a `flake.lock` PR.

### 16. Minor: split `datetime` imports in `guards.py`
`src/roomieorder/guards.py`

`from datetime import timedelta, timezone` then `from datetime import datetime` on the
next line — fold into one.

✅ **Resolved:** a single `from datetime import datetime, timedelta, timezone`.

---

## 🔵 Known & tracked (here for completeness, not new findings)

- **The *confirmation* selector layer is still unverified against the live DOM.** The
  Costco PDP selectors were verified 2026-06-16/17 and the Costco *checkout* selectors
  live too — place-order (`#place-order-button-regular`), payment-method radio
  (`[automation-id='paymentReviewRadio']`), order-total (`[automation-id='orderTotalOutput']`),
  cart-remove (`[automation-id^='removeItemLink_']`). **Re-verified 2026-06-22** via
  `trace-order` (dry-run, dumps DOM+probe+screenshot at all 8 checkpoints) across two
  items (`disinfecting_wipes`, `cat_litter`) — the checkout selectors resolve and are
  item-independent. That run also corrected the cart-singleton line selector:
  `CART_LINE_SELECTORS` now leads with `.order-item` (count=1 on the v2 review page),
  since the old `[automation-id^='orderItemLine_']`/`lineItem_` guesses are count=0 there.
  **Still guesses:** the *confirmation* scrape selectors (`ORDER_ID_RE`/`ORDER_ID_LABEL_RE`,
  post-order total) — reachable only past a real Place Order; `trace-order` halts at
  `review_pre_place` by design, so it can't reach them (see `PROGRESS.md` and project
  memory). Items #2, #3 above hardened the parts that can be fixed *structurally*,
  independent of selector bring-up.
- **Some catalog entries resolve to Grocery-by-Instacart products the buy flow can't
  drive.** Found 2026-06-22: `trace-order paper_towels` failed at the PDP (`price=ok` but
  `add-to-cart=MISS`) because the catalog item now resolves to a *Grocery by Instacart*
  product whose PDP shows "Select Options" and has **no** `Button_addToCartDrawer_pdp` —
  a different fulfilment flow the standard `ADD_TO_CART_SELECTORS` correctly don't match.
  Fix at the catalog level: point such entries at a non-grocery Costco item number/url
  (the real catalog lives in `nix-secrets/roomieorder/catalog.json`), or add an explicit
  grocery/Select-Options path. Audit the other catalog items for the same fulfilment type.
- **Worker pause is global, not per-item** — one challenge halts all ordering. This is
  intentional fail-safe design, noted only so it isn't "fixed" by accident.

---

_Generated as a review pass; treat each item as a candidate, verify before changing
money-path behaviour._
