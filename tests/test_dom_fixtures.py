"""Real-DOM regression net for the money-moving selector constants.

Unlike test_purchase.py — which drives hand-written `_FakePage` stubs that
return whatever the test sets up — these replay the **real Playwright locator
engine** against committed, sanitized snapshots of live Costco HTML (captured by
`dump-dom`, see tests/fixtures/dom/README.md). That makes them the one suite
that fails when a `purchase.py` selector constant drifts away from the real page
markup; a `_FakePage` test can't, because it never sees real markup.

Hermetic and offline: `page.set_content(html)` loads a static fixture, runs no
network, needs no login, and leaves `page.url == about:blank` — so URL-keyed
checks (`_is_signin`/`looks_like`) stay with the synthetic-URL unit tests in
test_purchase.py; here we assert only DOM-resolution. A real `Page` means no
`# mypy: disable-error-code="arg-type"` is needed.

All tests are `@pytest.mark.browser`: they need the headless browser the nix
shell ships and skip cleanly without it (see conftest.browser).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page

from roomieorder.cli import _group_hits, _read_price_from_summary
from roomieorder.config import Config
from roomieorder.purchase import CostcoPurchaser

pytestmark = pytest.mark.browser

# Known values asserted against the committed real DOM. See the per-fixture
# entry in tests/fixtures/dom/README.md for capture provenance.
_PAPER_TOWELS = "costco_product_paper_towels.html"
_PAPER_TOWELS_VISIBLE_PRICE = 27.39  # [data-testid='single-price-content'] sale price
_PAPER_TOWELS_JSONLD_PRICE = 32.99  # JSON-LD offers list price (metadata fallback)


def _costco(config: Config) -> CostcoPurchaser:
    return CostcoPurchaser(
        config, profile_dir=config.costco_profile_dir, domain=config.costco_domain
    )


def _group_resolves(page: Page, selectors: tuple[str, ...]) -> bool:
    """True when at least one selector in the group matches — the same
    "any candidate hit" rule `_read_price`/`_add_to_cart_disabled` walk."""
    return any(page.locator(sel).count() > 0 for sel in selectors)


# ─────────── product page (primary tier) ───────────


def test_read_price_resolves_visible_selector(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    """The full `_read_price` chain reads the visible sale price off real markup.

    This exercises `PRICE_SELECTORS[0]` (`[data-testid='single-price-content']`):
    mangle it and this assertion drops to the 32.99 metadata fallback and fails.
    """
    page.set_content(dom_fixture(_PAPER_TOWELS))
    assert _costco(config)._read_price(page) == _PAPER_TOWELS_VISIBLE_PRICE


def test_metadata_fallback_reads_jsonld(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    """`_read_price_from_metadata` reaches the JSON-LD `offers` price.

    Costco emits no `product:price:amount`/`og:price:amount` meta, so this rides
    the `_JSONLD_SELECTOR` path — the resilient fallback when the visible price
    block hasn't hydrated.
    """
    page.set_content(dom_fixture(_PAPER_TOWELS))
    assert _costco(config)._read_price_from_metadata(page) == _PAPER_TOWELS_JSONLD_PRICE


def test_product_selector_groups_resolve(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    """Each product-page selector group still matches the real PDP (count>0).

    These are the assertions that bite when a constant drifts: `price` and
    `add-to-cart` must hit; JSON-LD must be present. `price-meta` legitimately
    misses (Costco emits no price meta) and `place-order`/`order-total` aren't on
    a product page — asserting those stay misses keeps the fixture honest about
    what a PDP contains.
    """
    page.set_content(dom_fixture(_PAPER_TOWELS))

    assert _group_resolves(page, CostcoPurchaser.PRICE_SELECTORS)
    assert _group_resolves(page, CostcoPurchaser.ADD_TO_CART_SELECTORS)
    assert page.locator("script[type='application/ld+json']").count() > 0


def test_probe_hits_match_verify_selectors(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    """Reuse the exact `verify-selectors` plumbing so test and tool agree.

    `_group_hits(_probe_selectors(...))` is what the operator `verify-selectors`
    command reports; asserting on it here means the offline net and the live
    tool can't disagree by construction.
    """
    purchaser = _costco(config)
    page.set_content(dom_fixture(_PAPER_TOWELS))

    summary = purchaser._probe_selectors(page)
    hits = _group_hits(summary)

    assert hits.get("price") is True
    assert hits.get("add-to-cart") is True
    assert hits.get("json-ld") is True
    # Costco product pages carry no price meta and no checkout controls.
    assert hits.get("price-meta") is False
    assert hits.get("place-order") is False
    assert hits.get("order-total") is False
    # The resolved price line the operator sees is the visible sale price.
    assert _read_price_from_summary(summary) == str(_PAPER_TOWELS_VISIBLE_PRICE)


def test_in_stock_pdp_is_available(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    """An in-stock PDP returns no unavailability reason (drives the no-fallback path)."""
    page.set_content(dom_fixture(_PAPER_TOWELS))
    assert _costco(config)._check_availability(page, 200) is None


# ─────────── out-of-stock (skips until a delivery-unavailable PDP is captured) ───────────


def test_out_of_stock_pdp_reports_reason(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    page.set_content(dom_fixture("costco_product_out_of_stock.html"))
    reason = _costco(config)._check_availability(page, 200)
    assert reason is not None
    assert "out of stock" in reason


# ─────────── checkout review (stretch; skips until a scrubbed capture is committed) ───────────


def test_checkout_selector_groups_resolve(
    config: Config, page: Page, dom_fixture: Callable[[str], str]
) -> None:
    purchaser = _costco(config)
    page.set_content(dom_fixture("costco_checkout_review.html"))

    assert _group_resolves(page, CostcoPurchaser.PLACE_ORDER_SELECTORS)
    assert _group_resolves(page, CostcoPurchaser.PAYMENT_METHOD_SELECTORS)
    assert _group_resolves(page, CostcoPurchaser.ORDER_TOTAL_SELECTORS)
    assert purchaser._read_total(page) is not None
