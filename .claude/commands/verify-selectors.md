---
description: Probe live pages for stale buy-flow selectors and propose fixes.
argument-hint: "[item_key] [--provider costco|amazon]"
---
Run `roomieorder verify-selectors $ARGUMENTS`.

For every item that reports MISS, `Read` the `*_dom.html` artifact it points at
and find the real selector on the live page (per AGENTS.md §1). Propose the
corrected selector(s) for `purchase.py`.

Do NOT edit `purchase.py` unless I explicitly ask — the buy flow is
additive-only and can only be validated against live DOM during bring-up.
