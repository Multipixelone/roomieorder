---
description: Walk the operator through first-time store bring-up.
argument-hint: "[costco|amazon]"
---
Guide me through bring-up for a store, in order, pausing for me between steps
(see PLAN-ROOMIE.md). Use the provider from $ARGUMENTS (default costco):

1. `roomieorder login --provider <store>` — sign in by hand.
2. `roomieorder doctor` — confirm profile / display / chrome are green.
3. `roomieorder verify-selectors --provider <store>` — confirm the price and
   add-to-cart selectors match; fix any MISS off the dom dump first.
4. `roomieorder trace-order <item> --provider <store>` — walk the whole flow and
   confirm the cart/checkout/review selectors (incl. `place-order`/`order-total`/
   payment) resolve; fix any MISS off that step's dom dump before the first order.
5. `roomieorder dry-run <item> --provider <store>` — confirm it reaches the
   review page; `Read` the screenshot.
6. Only after a clean dry-run on a cheap item: flip `DRY_RUN=false` and place
   one real order, then `roomieorder queue` to confirm `placed`.

Never flip `DRY_RUN` or place a real order without my explicit go-ahead.
