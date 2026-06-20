from __future__ import annotations

# mypy: disable-error-code="arg-type"
# Test stubs are intentionally duck-typed fakes that implement only the Page
# subset each test exercises; casting every call site would add noise.

import pytest

from roomieorder.config import Config
from roomieorder.purchase import (
    _JSONLD_SELECTOR,
    AmazonPurchaser,
    CostcoPurchaser,
    _price_from_jsonld,
    looks_like,
    parse_price,
)

# DOM constants now live as class attributes on each purchaser.
_PRICE_SELECTORS = CostcoPurchaser.PRICE_SELECTORS
_PRICE_META_SELECTORS = CostcoPurchaser.PRICE_META_SELECTORS
_PLACE_ORDER_SELECTORS = CostcoPurchaser.PLACE_ORDER_SELECTORS
_ADD_TO_CART_SELECTORS = CostcoPurchaser.ADD_TO_CART_SELECTORS


def _purchaser(config: Config) -> CostcoPurchaser:
    return CostcoPurchaser(
        config, profile_dir=config.costco_profile_dir, domain=config.costco_domain
    )


def _amazon(config: Config) -> AmazonPurchaser:
    return AmazonPurchaser(
        config, profile_dir=config.amazon_profile_dir, domain=config.amazon_domain
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$24.99", 24.99),
        # Costco's React PDP splits the price into whole/dot/decimal spans, so
        # inner_text comes back with whitespace inside the number.
        ("$ 27 . 39", 27.39),
        ("$ 27 . 39 $5.60 OFF was $ 32.99", 27.39),
        ("Price: $1,234.56", 1234.56),
        ("£9.50 each", 9.50),
        ("€11,99", 11.99),
        # Grouped whole-dollar prices: a 3-digit tail is a thousands group, not
        # cents. Reading these as decimals (1.234 / 1.0) defeats the price
        # ceiling, so they must parse as the real four-figure amount.
        ("$1,234", 1234.0),
        ("$1,000", 1000.0),
        ("$2,000.00", 2000.0),
        ("€1.000", 1000.0),
        ("free shipping", None),
        ("", None),
    ],
)
def test_parse_price(text: str, expected: float | None) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Bare Product with a single offer.
        ('{"@type":"Product","offers":{"@type":"Offer","price":"24.99"}}', 24.99),
        # Numeric (not string) price.
        ('{"offers":{"price":24.99}}', 24.99),
        # @graph wrapper with the Product node buried among others.
        (
            '{"@graph":[{"@type":"BreadcrumbList"},'
            '{"@type":"Product","offers":{"price":"19.49"}}]}',
            19.49,
        ),
        # AggregateOffer price range → take the floor.
        ('{"offers":{"@type":"AggregateOffer","lowPrice":"11.99","highPrice":"15"}}', 11.99),
        # List of offers.
        ('{"offers":[{"price":"7.50"},{"price":"9.99"}]}', 7.50),
        # No offer price anywhere.
        ('{"@type":"Product","name":"Bath Tissue"}', None),
        # Not valid JSON.
        ("not json at all", None),
    ],
)
def test_price_from_jsonld(raw: str, expected: float | None) -> None:
    assert _price_from_jsonld(raw) == expected


@pytest.mark.parametrize(
    "text,url,expected",
    [
        ("Access Denied", "", True),
        ("normal product page", "https://www.costco.com/x.product.123.html", False),
        ("", "https://www.costco.com/_sec/verify", True),
        ("Pardon Our Interruption", "", True),
        ("Please verify you are human", "", True),
    ],
)
def test_looks_like_challenge(text: str, url: str, expected: bool) -> None:
    assert looks_like(text, url, CostcoPurchaser.CHALLENGE_MARKERS) is expected


@pytest.mark.parametrize(
    "text,url,expected",
    [
        # The header "Sign In / Register" link is on every logged-out page, so on
        # its own it must NOT read as a wall (that was the product-page misfire).
        ("Sign In / Register", "", False),
        ("Sign in or register to continue", "", True),
        ("", "https://signin.costco.com/?return=x", True),
        ("bounced to logon", "https://www.costco.com/logon", True),
        ("Checkout — Place Order", "", False),
        ("normal product page", "https://www.costco.com/x.product.123.html", False),
        # The logged-in header renders "Account" (not "Sign In"); on the checkout
        # page that must NOT read as a wall, or every member checkout false-fails
        # as a sign-in redirect. Locks the URL/cookie-based detection against a
        # future edit that naively adds "account"/"sign in" as a marker.
        ("Welcome back — Account", "https://www.costco.com/CheckoutView", False),
        ("My Account Orders & Returns", "", False),
    ],
)
def test_looks_like_signin(text: str, url: str, expected: bool) -> None:
    assert looks_like(text, url, CostcoPurchaser.SIGNIN_MARKERS) is expected


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self._selector in self._page.present else 0

    def click(self, timeout: int | None = None) -> None:
        self._page.clicked.append(self._selector)


class _RoleLocator:
    """Stand-in for a ``get_by_role`` result. Clicks if ``hit`` is True,
    otherwise raises like Playwright does when nothing matches."""

    def __init__(self, page: "_FakePage", hit: bool) -> None:
        self._page = page
        self._hit = hit

    @property
    def first(self) -> "_RoleLocator":
        return self

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        if not self._hit:
            raise TimeoutError("never visible")

    def click(self, timeout: int | None = None) -> None:
        if not self._hit:
            raise TimeoutError("no role match")
        self._page.clicked.append("role:place your order")


class _FakePage:
    """Minimal stand-in for a Playwright page that models the checkout race:
    the place-order button is absent until ``wait_for_selector`` is awaited,
    mimicking Costco's JS rendering the body after navigation."""

    def __init__(
        self,
        reveal_on_wait: set[str] | None = None,
        role_text: bool = False,
        url: str = "",
    ) -> None:
        self.present: set[str] = set()
        self._reveal = reveal_on_wait or set()
        self._role_text = role_text
        self.url = url
        self.clicked: list[str] = []
        self.waited: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def get_by_role(self, role: str, name: object = None) -> _RoleLocator:
        return _RoleLocator(self, self._role_text)

    def get_by_text(self, text: object) -> _RoleLocator:
        return _RoleLocator(self, self._role_text)

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.waited.append(selector)
        self.present |= self._reveal


def test_click_first_misses_unrendered_button(config: Config) -> None:
    # Blank body: none of the place-order selectors are in the DOM yet.
    page = _FakePage()
    assert _purchaser(config)._click_first(page, _PLACE_ORDER_SELECTORS) is False


def test_wait_for_any_lets_the_click_land(config: Config) -> None:
    # The first selector appears only once the page is given time to render.
    page = _FakePage(reveal_on_wait={_PLACE_ORDER_SELECTORS[0]})
    purchaser = _purchaser(config)

    assert purchaser._click_first(page, _PLACE_ORDER_SELECTORS) is False
    assert purchaser._wait_for_any(page, _PLACE_ORDER_SELECTORS) is True
    assert purchaser._click_first(page, _PLACE_ORDER_SELECTORS) is True
    assert page.clicked == [_PLACE_ORDER_SELECTORS[0]]


def test_place_order_falls_back_to_button_text(config: Config) -> None:
    # None of the CSS ids match (a checkout variant), but the button text does.
    page = _FakePage(role_text=True)
    assert _purchaser(config)._place_order(page) is True
    assert page.clicked == ["role:place your order"]


def test_place_order_fails_when_nothing_matches(config: Config) -> None:
    # No id and no text match — the worker should report the miss, not click.
    page = _FakePage(role_text=False)
    assert _purchaser(config)._place_order(page) is False
    assert page.clicked == []


def test_wait_for_any_returns_false_on_timeout(config: Config) -> None:
    class _NeverPage(_FakePage):
        def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
            raise TimeoutError("no selector")

    assert _purchaser(config)._wait_for_any(_NeverPage(), _PLACE_ORDER_SELECTORS) is False


def test_on_checkout_waits_out_the_landing_race(config: Config) -> None:
    # The review body paints a beat late: the place-order button is absent on the
    # first read (and the URL isn't a checkout URL yet) but appears once the page
    # is given time, mimicking #15's no_buy_button false negative.
    page = _FakePage(reveal_on_wait={_PLACE_ORDER_SELECTORS[0]})
    purchaser = _purchaser(config)

    assert purchaser._checkout_landed(page) is False
    assert purchaser._on_checkout(page) is True
    assert _PLACE_ORDER_SELECTORS[0] in page.present


def test_on_checkout_lands_on_url_without_a_button(config: Config) -> None:
    # The checkout URL is the drift-immune signal: even if every PLACE_ORDER id
    # has drifted (none present), a "checkout" URL still counts as landed.
    page = _FakePage(url="https://www.costco.com/CheckoutView")
    assert _purchaser(config)._on_checkout(page) is True


def test_on_checkout_returns_false_when_never_lands(config: Config) -> None:
    # No button ever paints and the URL never becomes a checkout URL → the
    # bounded wait expires and the miss is reported (the real "couldn't reach
    # checkout" case, distinct from the race).
    class _NeverPage(_FakePage):
        def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
            raise TimeoutError("no selector")

    assert _purchaser(config)._on_checkout(_NeverPage(url="https://www.costco.com/cart")) is False


class _PriceLocator:
    """Stand-in for a price/meta/JSON-LD locator. ``texts`` is the list of
    matches; ``attr`` is the meta ``content`` value. An empty match models a
    selector that isn't in the DOM (count 0)."""

    def __init__(self, texts: list[str], attr: str | None = None) -> None:
        self._texts = texts
        self._attr = attr

    @property
    def first(self) -> "_PriceLocator":
        return self

    def nth(self, i: int) -> "_PriceLocator":
        return _PriceLocator([self._texts[i]])

    def count(self) -> int:
        return len(self._texts)

    def inner_text(self, timeout: int | None = None) -> str:
        return self._texts[0]

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self._attr


class _PricePage:
    """Models a product page for _read_price: a map of selector → locator lets
    a test put the price in the visible element, a meta tag, or a JSON-LD block
    and confirm the fallback order."""

    def __init__(self, matches: dict[str, _PriceLocator]) -> None:
        self._matches = matches
        self.url = "https://www.costco.com/p/-/x/4000206004"

    def locator(self, selector: str) -> _PriceLocator:
        return self._matches.get(selector, _PriceLocator([]))

    def title(self, timeout: int | None = None) -> str:
        return "Kirkland Signature Bath Tissue"


def test_read_price_prefers_visible_selector(config: Config) -> None:
    page = _PricePage(
        {
            _PRICE_SELECTORS[0]: _PriceLocator(["$24.99"]),
            _PRICE_META_SELECTORS[0]: _PriceLocator(["x"], attr="99.99"),
        }
    )
    assert _purchaser(config)._read_price(page) == 24.99


def test_read_price_falls_back_to_meta_tag(config: Config) -> None:
    # No visible price element; the price is only in an OpenGraph meta tag.
    page = _PricePage({_PRICE_META_SELECTORS[1]: _PriceLocator(["x"], attr="$18.49")})
    assert _purchaser(config)._read_price(page) == 18.49


def test_read_price_falls_back_to_jsonld(config: Config) -> None:
    # Neither the visible element nor a meta tag; only the JSON-LD block.
    blob = '{"@type":"Product","offers":{"price":"32.99"}}'
    page = _PricePage({_JSONLD_SELECTOR: _PriceLocator([blob])})
    assert _purchaser(config)._read_price(page) == 32.99


def test_read_price_returns_none_when_no_source(config: Config) -> None:
    assert _purchaser(config)._read_price(_PricePage({})) is None


def test_probe_selectors_reports_matches_and_misses(config: Config) -> None:
    page = _PricePage(
        {
            _PRICE_SELECTORS[0]: _PriceLocator(["$24.99"]),
            _JSONLD_SELECTOR: _PriceLocator(['{"offers":{"price":"24.99"}}']),
        }
    )
    report = _purchaser(config)._probe_selectors(page)
    # The matching selector shows its count + sample; a miss shows count=0.
    assert f"{_PRICE_SELECTORS[0]}  count=1  sample='$24.99'" in report
    assert f"{_PRICE_SELECTORS[1]}  count=0" in report
    # Resolved price and the JSON-LD offer price both surface in the probe.
    assert "read_price:  24.99" in report
    assert "offer_price=24.99" in report
    assert "title: Kirkland Signature Bath Tissue" in report


class _FakeContext:
    """Stand-in for the BrowserContext cookie jar. WebSphere Commerce stamps the
    signed-in member's id into WC_AUTHENTICATION_<id>; a guest uses -1002."""

    def __init__(self, page: "_LoginPage") -> None:
        self._page = page

    def cookies(self) -> list[dict[str, str]]:
        uid = "2436747244" if self._page.logged_in else "-1002"
        return [{"name": f"WC_AUTHENTICATION_{uid}", "value": "x"}]


class _LoginLocator:
    def __init__(self, page: "_LoginPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_LoginLocator":
        return self

    def count(self) -> int:
        return 1 if self._selector in self._page.present else 0

    def inner_text(self, timeout: int | None = None) -> str:
        # The DOM backstop: the member "Account" button vs the guest sign-in link.
        return "Account" if self._page.logged_in else "Sign In or Register"


class _LoginPage:
    """Models Costco's silent SSO re-auth: a fresh launch starts as a guest
    (WC_AUTHENTICATION_-1002), and hitting the logon form upgrades the WC cookie
    to the member's id — unless the saved SSO cookie has also expired
    (``reauth_succeeds=False``), in which case it stays a guest."""

    def __init__(
        self, *, logged_in: bool, present: set[str] | None = None, reauth_succeeds: bool = True
    ) -> None:
        self.logged_in = logged_in
        self.present = present or set()
        self.reauth_succeeds = reauth_succeeds
        self.goto_urls: list[str] = []
        self.context = _FakeContext(self)

    def locator(self, selector: str) -> _LoginLocator:
        return _LoginLocator(self, selector)

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_urls.append(url)
        if "LogonForm" in url and self.reauth_succeeds:
            self.logged_in = True

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        pass


def test_ensure_logged_in_short_circuits_when_already_logged_in(config: Config) -> None:
    page = _LoginPage(logged_in=True)
    assert _purchaser(config).ensure_logged_in(page) is True
    # Already a member session — no logon navigation needed.
    assert page.goto_urls == []


def test_ensure_logged_in_silent_sso_reauth(config: Config) -> None:
    # Fresh launch lands as a guest, but the saved SSO cookie re-auths silently.
    page = _LoginPage(logged_in=False, reauth_succeeds=True)
    assert _purchaser(config).ensure_logged_in(page) is True
    # Reached the logon flow, which bounced back as the member.
    assert any("LogonForm" in u for u in page.goto_urls)


def test_ensure_logged_in_fails_when_sso_expired(config: Config) -> None:
    # Guest, and the SSO cookie has also expired so the logon form can't re-auth
    # on its own — the caller must bail to manual `roomieorder login`.
    page = _LoginPage(logged_in=False, reauth_succeeds=False)
    assert _purchaser(config).ensure_logged_in(page) is False
    assert any("LogonForm" in u for u in page.goto_urls)


# ─────────── availability (drives the Amazon fallback) ───────────


class _AvailLocator:
    def __init__(self, *, count: int, text: str = "", disabled: bool = False) -> None:
        self._count = count
        self._text = text
        self._disabled = disabled

    @property
    def first(self) -> "_AvailLocator":
        return self

    def count(self) -> int:
        return self._count

    def inner_text(self, timeout: int | None = None) -> str:
        return self._text

    def is_disabled(self, timeout: int | None = None) -> bool:
        return self._disabled


class _AvailPage:
    """Models a product page for _check_availability: a body text blob plus an
    optional add-to-cart locator (present + possibly disabled)."""

    def __init__(self, *, body: str = "", atc: _AvailLocator | None = None) -> None:
        self._body = body
        self._atc = atc
        self.url = "https://www.costco.com/x.product.123.html"

    def locator(self, selector: str) -> _AvailLocator:
        if selector == "body":
            return _AvailLocator(count=1, text=self._body)
        if self._atc is not None and selector in _ADD_TO_CART_SELECTORS:
            return self._atc
        return _AvailLocator(count=0)


def test_availability_flags_http_404(config: Config) -> None:
    reason = _purchaser(config)._check_availability(_AvailPage(), 404)
    assert reason is not None and "404" in reason


def test_availability_flags_delivery_out_of_stock_marker(config: Config) -> None:
    page = _AvailPage(body="Item 123 is out of stock or unavailable to order online")
    reason = _purchaser(config)._check_availability(page, 200)
    assert reason == "is out of stock"


def test_availability_ignores_warehouse_pickup_out_of_stock(config: Config) -> None:
    # Costco's "How To Get It" widget shows per-warehouse pick-up stock
    # ("<warehouse> Out of Stock" / "Low Stock") even when 2-Day Delivery is
    # available, so a warehouse-only sold-out body must NOT flag the item — it's
    # deliverable. With an enabled add-to-cart present, availability passes.
    page = _AvailPage(
        body="How To Get It  11201 Out of Stock  Brooklyn Low Stock  2-Day Delivery $24.99",
        atc=_AvailLocator(count=1, disabled=False),
    )
    assert _purchaser(config)._check_availability(page, 200) is None


def test_availability_flags_not_found_marker(config: Config) -> None:
    page = _AvailPage(body="Sorry, we can't find the page you requested")
    reason = _purchaser(config)._check_availability(page, 200)
    assert reason == "not found"


def test_availability_flags_disabled_add_to_cart(config: Config) -> None:
    page = _AvailPage(atc=_AvailLocator(count=1, disabled=True))
    reason = _purchaser(config)._check_availability(page, 200)
    assert reason is not None and "out of stock" in reason


def test_availability_passes_in_stock_page(config: Config) -> None:
    page = _AvailPage(body="In Stock", atc=_AvailLocator(count=1, disabled=False))
    assert _purchaser(config)._check_availability(page, 200) is None


# ─────────── cart reset (start every buy from an empty cart) ───────────


class _CartLocator:
    def __init__(self, page: "_CartPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_CartLocator":
        return self

    def _is_remove(self) -> bool:
        return self._selector in CostcoPurchaser.REMOVE_ITEM_SELECTORS

    def count(self) -> int:
        # A remove control exists while any line remains; the confirm modal is
        # never present in this fake (removal is direct).
        return 1 if (self._is_remove() and self._page.lines > 0) else 0

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        raise TimeoutError("confirm modal never visible")

    def click(self, timeout: int | None = None) -> None:
        if self._is_remove() and self._page.lines > 0:
            self._page.lines -= 1
            self._page.removes += 1


class _CartPage:
    """Models the Costco cart for _reset_cart: ``lines`` line items, each remove
    click drains one. Tracks how many removes fired so the test can assert the
    cart actually emptied rather than spun."""

    def __init__(self, lines: int) -> None:
        self.lines = lines
        self.removes = 0
        self.url = "https://www.costco.com/CheckoutCartDisplayView"

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = url

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        pass

    def locator(self, selector: str) -> _CartLocator:
        return _CartLocator(self, selector)


def test_reset_cart_drains_every_line(config: Config) -> None:
    page = _CartPage(lines=3)
    _purchaser(config)._reset_cart(page)
    assert page.lines == 0
    assert page.removes == 3


def test_reset_cart_noop_on_empty_cart(config: Config) -> None:
    page = _CartPage(lines=0)
    _purchaser(config)._reset_cart(page)
    assert page.removes == 0


def test_reset_cart_is_bounded(config: Config) -> None:
    # A cart that never drains (e.g. remove silently fails) must stop at the cap,
    # not loop forever.
    class _StuckCart(_CartPage):
        def locator(self, selector: str) -> _CartLocator:
            loc = _CartLocator(self, selector)
            self.lines = 5  # remove "fails": the line count never falls
            return loc

    page = _StuckCart(lines=5)
    _purchaser(config)._reset_cart(page)
    assert page.removes <= CostcoPurchaser._MAX_CART_LINES + 1


def test_reset_cart_is_base_noop_for_amazon(config: Config) -> None:
    # Amazon's Buy-Now path doesn't use the shared cart; the base hook does
    # nothing and must never raise.
    _amazon(config)._reset_cart(object())


# ─────────── Amazon checkout (the fallback flow) ───────────


def test_amazon_start_checkout_clicks_buy_now(config: Config) -> None:
    page = _FakePage()
    page.present = {AmazonPurchaser.BUY_NOW_SELECTORS[0]}
    assert _amazon(config)._start_checkout(page) is True
    assert page.clicked == [AmazonPurchaser.BUY_NOW_SELECTORS[0]]


# ─────────── order-id extraction ───────────


def test_order_id_prefers_labelled_number(config: Config) -> None:
    # A phone number (10 digits) appears before the order id; the bare regex
    # would grab it, but the label anchor must pick the real order number.
    body = "Need help? Call 8007742678.\nOrder # 99887766 placed.\nTotal $24.99"
    assert _purchaser(config)._find_order_id(body) == "99887766"


def test_order_id_handles_label_variants(config: Config) -> None:
    p = _purchaser(config)
    assert p._find_order_id("Order Number: 123456789") == "123456789"
    assert p._find_order_id("Confirmation #123456789") == "123456789"


def test_order_id_falls_back_to_bare_regex(config: Config) -> None:
    # No label on the page → fall back to the bare digit-run regex.
    assert _purchaser(config)._find_order_id("Thanks! 123456789 is your number") == "123456789"


# ─────────── confirmation scrape / submitted-but-unconfirmed ───────────


class _ConfirmLocator:
    def __init__(self, *, text: str = "", count: int = 1) -> None:
        self._text = text
        self._count = count

    @property
    def first(self) -> "_ConfirmLocator":
        return self

    def count(self) -> int:
        return self._count

    def inner_text(self, timeout: int | None = None) -> str:
        return self._text


class _ConfirmPage:
    """Models an order-confirmation page: a body blob plus an optional grand-total
    element. Missing selectors report count=0 like a real locator miss."""

    def __init__(self, *, body: str = "", total_text: str | None = None) -> None:
        self._body = body
        self._total_text = total_text

    def locator(self, selector: str) -> _ConfirmLocator:
        if selector == "body":
            return _ConfirmLocator(text=self._body, count=1)
        if self._total_text is not None and selector in CostcoPurchaser.ORDER_TOTAL_SELECTORS:
            return _ConfirmLocator(text=self._total_text, count=1)
        return _ConfirmLocator(count=0)

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        pass


def test_scrape_confirmation_reads_labelled_id_and_total(config: Config) -> None:
    page = _ConfirmPage(body="Thanks! Order # 123456789 confirmed", total_text="$24.99")
    order_id, total = _purchaser(config)._scrape_confirmation(page)
    assert order_id == "123456789"
    assert total == 24.99


def test_scrape_confirmation_returns_none_when_blank(config: Config) -> None:
    order_id, total = _purchaser(config)._scrape_confirmation(_ConfirmPage(body="loading…"))
    assert order_id is None and total is None


def test_submitted_unconfirmed_flags_needs_review(config: Config) -> None:
    # Place Order clicked but nothing confirmable scraped → never `failed`.
    result = _purchaser(config)._submitted_unconfirmed(_ConfirmPage(), "paper_towels", "no total")
    assert result.status == "needs_review"
    assert "MAY have been placed" in result.message


def test_submitted_unconfirmed_carries_review_total(config: Config) -> None:
    # The review-page total is logged onto the needs_review row so the human
    # still sees the amount to split even when the confirmation can't be read.
    result = _purchaser(config)._submitted_unconfirmed(
        _ConfirmPage(), "paper_towels", "no total", order_total=27.21
    )
    assert result.status == "needs_review"
    assert result.order_total == 27.21


def test_read_total_reads_order_total_selector(config: Config) -> None:
    # The grand total sits in ORDER_TOTAL_SELECTORS on the review page too — the
    # same element used on the confirmation page — so one reader serves both.
    page = _ConfirmPage(total_text="$27.21")
    assert _purchaser(config)._read_total(page) == 27.21


def test_read_total_none_when_absent(config: Config) -> None:
    # Costco's v2 confirmation page has no total element at all → None.
    assert _purchaser(config)._read_total(_ConfirmPage(body="Order # 123")) is None


def test_amazon_resolves_dp_url_from_asin(config: Config) -> None:
    class _Src:
        url = ""
        asin = "B07YYYYYYY"

    url = _amazon(config)._resolve_url(_Src())
    assert url == "https://www.amazon.com/dp/B07YYYYYYY"
