# DOM regression fixtures

Sanitized snapshots of **real, live-verified** store HTML, captured by
`roomieorder dump-dom`. `tests/test_dom_fixtures.py` replays the actual
Playwright locator engine against them (`page.set_content(...)`) and asserts the
`purchase.py` selector constants still resolve and read the right values — the
one thing the 100+ `_FakePage` unit tests can't prove (they assert against
hand-written stubs, not real store markup).

These run offline and hermetically: no network, no login, `page.url` stays
`about:blank`. They drift only when a fixture is **re-captured**, so they catch
a *local* selector edit that no longer matches the page — not a future Costco
redesign (that's the deferred live `verify-selectors` watchdog). See AGENTS.md §1.

## Sanitization rule (apply before committing any capture)

A `dump-dom` `*_dom.html` is the full rendered page (~3.5 MB, ~1100 `<script>`
blocks, Next.js state, possibly session/geolocation/PII). Before it can be
committed it MUST be reduced with `/tmp/sanitize_dom.py`-style processing
(Chromium with JS disabled, so nothing executes):

1. **Strip every `<script>` except `type="application/ld+json"`.** Removes the
   Next.js `__NEXT_DATA__` state blob, any session token, geolocation, and ~99%
   of the bytes. JSON-LD `offers` is kept — it's a tested price fallback.
2. **Strip `<style>` and `<link>`** — weight with no selector value.
3. **For any logged-in capture, additionally scrub PII** the body itself
   renders: member name/greeting ("Hello, <name>"), shipping address, and
   saved-card last-4. (The product fixtures below are logged-out, so the body
   carries none — only template placeholders like `Hello, {firstName}!`.)

After sanitizing, grep the result for the operator's name/email, `cookie`,
`authorization`, `wc_authentication`, and 12–16-digit runs to confirm it's
clean. Keep the resulting file ≤ ~300 KB.

## Fixtures

### `costco_product_paper_towels.html`

- **Source:** `https://www.costco.com/p/-/bounty-advanced-paper-towels-2-ply-103-sheets-12-count/4000346087`
  (catalog item `paper_towels`, item `1640526`).
- **Captured:** 2026-06-16 via `roomieorder dump-dom paper_towels`, **logged
  out** (the price renders logged-out — AGENTS.md §1 — so no session, no PII).
- **Provenance:** `dump-dom` `*_dom.html`, then sanitized per the rule above
  (3.61 MB → 290 KB; 1103 → 2 `<script>`, both JSON-LD).
- **Page shape:** Costco's MUI/React PDP, in-stock. Visible price in
  `[data-testid='single-price-content']`; add-to-cart in
  `[data-testid='Button_addToCartDrawer_pdp']`; JSON-LD `offers` present;
  no `product:price:amount`/`og:price:amount` meta (Costco emits none).
- **Known values the tests assert** (replayed through the real locator engine):
  - `_read_price` → **27.39** (the visible sale price).
  - `_read_price_from_metadata` → **32.99** (JSON-LD `offers` list price; the
    visible-selector fallback path).
  - `_check_availability(page, 200)` → `None` (in stock).
  - selector-group hits: `price` ✓, `add-to-cart` ✓, JSON-LD ✓;
    `price-meta` ✗ (Costco emits no price meta — expected),
    `place-order`/`order-total` ✗ (not on a product page — expected).

## Not committed (and why)

- **`costco_product_out_of_stock.html`** — no delivery-unavailable PDP has been
  captured (every dumped item was in stock). The `_check_availability` test
  skips until one is dropped in.
- **`costco_checkout_review.html`** — `dump-dom` stops at the product page and
  never proceeds to checkout, so no SinglePageCheckoutView HTML exists to
  sanitize (only `*_review.png` screenshots do). The `PLACE_ORDER_SELECTORS` /
  `PAYMENT_METHOD_SELECTORS` / `ORDER_TOTAL_SELECTORS` test skips until a
  logged-in, PII-scrubbed review-page capture is committed here.
- **Confirmation page** — only reachable past a real Place Order; it remains the
  project's standing 🔵 caveat (AGENTS.md §1), out of scope for this harness.

The `test_dom_fixtures.py` tests for the two missing fixtures already exist and
`pytest.skip(...)` when the file is absent, so the net tightens automatically
the moment a capture is added.
