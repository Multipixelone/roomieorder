---
description: Dump DOM + selector probe + screenshot at every checkpoint of the buy flow.
argument-hint: "<item_key> [--provider costco|amazon]"
---
Run `roomieorder trace-order $ARGUMENTS`.

This always forces DRY_RUN — it walks the real buy flow to the review page and
NEVER places an order. At each checkpoint (product → cart → cart view → delivery
→ payment → review) it writes a rendered `*_dom.html`, a selector `*_probe.txt`,
and a screenshot to the shots dir, and prints a per-step PASS/MISS digest.

Unlike `dump-dom`/`verify-selectors` (which stop at the product page), this
reaches the checkout/review surface where the `place-order`, `order-total`, and
payment selectors finally render. For any group still MISS at a checkout step,
`Read` that step's `*_dom.html` and find the real selector on the live page (per
AGENTS.md §1), then propose the corrected selector(s) for `purchase.py`.

Do NOT edit `purchase.py` unless I explicitly ask — the buy flow is
additive-only and can only be validated against live DOM during bring-up.
