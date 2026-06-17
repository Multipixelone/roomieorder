# roomieorder — stability & design review

A standing punch-list of things that could be **more stable** or **better designed**,
to be iterated over and fixed later. Ordered roughly by risk. Each item notes the
location, the problem, and a suggested direction. Nothing here is a request to change
behaviour silently — money moves through this system, so each fix wants its own
verification.

Legend: 🔴 correctness / money-safety · 🟠 stability / reliability · 🟡 design / maintainability · 🔵 known & tracked

---

## 🔴 Correctness & money-safety

### 1. `parse_price` mis-parses grouped whole-dollar prices
`src/roomieorder/purchase.py:124` (`parse_price`)

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

### 2. No idempotency around `_place_order` → confirmation scrape
`src/roomieorder/purchase.py:505`–`545`

`_place_order` clicks Place Order, then `_scrape_confirmation` reads the order id/total.
If the click *succeeds* but the page then times out / the scrape throws, `buy` returns
`failed` (or the worker crashes) for an order that **was actually placed**. A manual
`resume` + re-tap then re-orders. There is no order-id de-dup and no "did we already
submit?" check.

**Fix direction:** before clicking, record an intent marker (row → `in_progress` with a
"submitting" note); after a failed scrape, treat the row as *needs-human-confirmation*
rather than `failed`, and never auto-retry a row that reached the submit step. Consider
scraping the confirmation defensively (retry the read) before giving up.

### 3. Costco order-id regex matches any 9–12 digit run on the page
`src/roomieorder/purchase.py:1107` + `_scrape_confirmation` at `:891`

`ORDER_ID_RE = \b\d{9,12}\b` is searched against the **whole confirmation body text**.
Phone numbers, item numbers, ZIPs+4, and timestamps can all match first and be recorded
as the order id. (Amazon's `\d{3}-\d{7}-\d{7}` is far safer.)

**Fix direction:** anchor to a labelled element ("Order #", "Confirmation number") via a
selector, falling back to the regex only within that element's text. Marked
`TODO(costco): verify against live DOM` already — fold this in during bring-up.

### 4. Intake endpoint is unauthenticated
`src/roomieorder/main.py:246` (`POST /reorder`)

Anyone who can reach the port can place real orders. Default bind is `127.0.0.1`
(`config.host`), which mitigates — but the whole point is for Home Assistant to call it,
and the moment `ROOMIEORDER_HOST` is widened to serve HA on the LAN, every device on the
network can trigger spending.

**Fix direction:** a shared-secret header/token checked in `/reorder` (and ideally
`/pause`/`resume` if those ever get HTTP handles). Keep it optional/off for the
loopback-only default so local dev isn't burdened.

---

## 🟠 Stability & reliability

### 5. `in_progress` rows are orphaned on a hard restart
`src/roomieorder/store.py:150` (`claim_next_pending`) + `main.py:131` (`_process`)

`claim_next_pending` only ever selects `status='pending'`. The `_process` `try/except`
converts *exceptions* to `failed`, but if the **process itself** dies (SIGKILL, OOM,
power loss, systemd restart) between the claim and the `mark`, the row is stuck
`in_progress` forever: never re-claimed, never failed, never surfaced. On a daemon that's
expected to survive desktop sleep/wake, this will happen.

**Fix direction:** on `init_db`/startup, reset stale `in_progress` rows (e.g. back to
`pending` if under an attempts cap, else `failed` + pause). Pair with item #6.

### 6. `attempts` is incremented but never enforced
`src/roomieorder/store.py:159`

Every claim does `attempts=attempts+1`, but nothing reads `attempts`. There's no retry
cap, so combined with #5 a row could be retried indefinitely once recovery is added.

**Fix direction:** define a max-attempts policy (likely 1 for a money-moving step — fail
to the operator rather than retry a possibly-placed order), and have the recovery in #5
respect it.

### 7. One shared SQLite connection across the worker thread and uvicorn's threadpool
`src/roomieorder/store.py:108`

`check_same_thread=False` with a single `_conn` shared by the async intake threadpool and
the worker daemon. Python's `sqlite3` serialises individual calls, but **commit is
connection-global**: there is no transaction isolation between the two threads, so one
thread's `commit()` flushes the other thread's half-finished write. Today every method is
effectively autocommit (one statement + immediate commit), so it mostly survives — but
it's fragile and any future multi-statement transaction will be silently unsafe.

**Fix direction:** a connection per thread (thread-local), or a short-lived connection
per operation, or a `threading.Lock` wrapping each method. WAL is already on, so
per-operation connections are cheap.

### 8. `_place_order` text fallback can click the wrong element
`src/roomieorder/purchase.py:844`

The last-resort `page.get_by_text(/place (your )?order/i).first.click()` will happily
click a *heading* or *label* containing that text, not the button. On a live checkout
that could click something benign — or could click into a confirm flow unexpectedly.

**Fix direction:** restrict the text fallback to clickable roles, or drop it in favour of
the role-named button + verified CSS ids once the live DOM is known.

### 9. `buy`'s catch-all swallows programmer errors as `failed`
`src/roomieorder/purchase.py:554`

The module docstring says "the only exceptions that escape are programmer errors" — but
the bare `except Exception` turns *every* error (AttributeError, TypeError, a bad
selector type, etc.) into a `failed` PurchaseResult with a one-line message. Real bugs get
laundered into "store flakiness" and only show up as a screenshot + truncated message.

**Fix direction:** let a small allowlist of "this is a bug" exceptions propagate (or at
least log full tracebacks at ERROR — `_logger.exception` is already called, good — and
distinguish them in the message/status), so a code defect doesn't masquerade as a sold-out
item.

### 10. Spend cap is checked against an estimate, not the real total
`src/roomieorder/guards.py:120` + `orchestrator.py:85`

The cap uses `live_price * qty` as the prospective total, but tax/shipping/fees aren't
known until the order total is scraped. An order can land over `daily_cap` once real
totals accrue. Low stakes given the cap is a coarse backstop, but worth noting.

**Fix direction:** add a margin to the prospective estimate, and/or re-check the cap
against `order_total` post-scrape and pause if the *recorded* trailing-24h spend exceeds
the cap.

---

## 🟡 Design & maintainability

### 11. `page`/`source`/`item` typed as `object` throughout `purchase.py`
`src/roomieorder/purchase.py` (pervasive) — e.g. `page: object`, `source: object`

mypy runs `strict`, yet nearly every Playwright/catalog interaction is `object` + a
`# type: ignore[attr-defined]`. Strict typing is paying its cost (the ignores) without its
benefit (catching a wrong attribute/selector call). `getattr(source, "item_number")` with
no default also defeats the typed `CostcoSource`/`AmazonSource` we already have in hand.

**Fix direction:** type `page` against a Playwright `Page` Protocol (or import under
`TYPE_CHECKING`), and pass the concrete `CostcoSource`/`AmazonSource` types into the
purchasers instead of `object`. Removes most `# type: ignore`s and makes selector/attr
typos compile errors.

### 12. Catalog is loaded once at startup; edits need a restart
`src/roomieorder/main.py:90` (`Engine.__init__` → `load_catalog`)

`self.catalog` is captured at boot. Adding an item or fixing a ceiling requires a service
restart. Fine for now, surprising later.

**Fix direction:** a `/reload` endpoint or an mtime check, or document the restart
requirement explicitly in the README.

### 13. Test catalog duplicates `catalog.json`
`tests/conftest.py:14` (`_CATALOG`) vs the repo's `catalog.json`

Two hand-maintained sample catalogs that can drift. (Also note: per memory, repo
`catalog.json` is a placeholder; the real ~25-item catalog lives in `infra/nix-secrets`.)

**Fix direction:** have the fixture load `examples/catalog.json` (or the repo
`catalog.json`) so there's one sample, or clearly label the fixture as an
intentionally-minimal two-source/one-source matrix (which it is — that's its value).

### 14. `item_statuses` recomputes with N queries per poll
`src/roomieorder/main.py:157`

One `last_placed_at` query per catalog item on every `/items` poll. Comment acknowledges
it's fine for a tiny catalog — true today. Flagged only so it's on the radar if the
catalog or poll rate grows.

**Fix direction:** a single grouped query (`MAX(updated_at) ... WHERE status='placed'
GROUP BY item_key`) if it ever matters.

### 15. Dependency automation overlaps: Renovate + a flake-update workflow
`renovate.json` + `.github/workflows/flake-update.yml`

Two mechanisms that can both touch dependencies/lockfiles. Worth confirming they don't
fight (e.g. both opening lockfile PRs).

**Fix direction:** scope each to a clear lane (Renovate for `pyproject` deps, the workflow
for `flake.lock`, or vice versa) and document it.

### 16. Minor: split `datetime` imports in `guards.py`
`src/roomieorder/guards.py:19`–`20`

`from datetime import timedelta, timezone` then `from datetime import datetime` on the
next line — fold into one.

---

## 🔵 Known & tracked (here for completeness, not new findings)

- **The entire `purchase.py` selector layer is unverified against the live DOM.** Most
  constants carry `TODO(<store>): verify against live DOM`; the Costco PDP selectors were
  verified 2026-06-16/17 but **checkout/place-order/confirmation selectors remain
  guesses**, and no live buy has ever run. This is the single largest stability risk and
  is already the project's central caveat (see `PROGRESS.md` and project memory). Items
  #2, #3, #8 above are the parts of this worth hardening *structurally*, independent of
  selector bring-up.
- **Worker pause is global, not per-item** — one challenge halts all ordering. This is
  intentional fail-safe design, noted only so it isn't "fixed" by accident.

---

_Generated as a review pass; treat each item as a candidate, verify before changing
money-path behaviour._
