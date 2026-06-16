from __future__ import annotations

import pytest

from roomieorder.config import Config
from roomieorder.purchase import (
    _JSONLD_SELECTOR,
    _PLACE_ORDER_SELECTORS,
    _PRICE_META_SELECTORS,
    _PRICE_SELECTORS,
    _SIGNIN_SUBMIT_SELECTORS,
    CostcoPurchaser,
    _price_from_jsonld,
    looks_like_challenge,
    looks_like_signin,
    parse_price,
)

_ACCOUNT_NAV = "[automation-id='accountMenuButton']"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$24.99", 24.99),
        ("Price: $1,234.56", 1234.56),
        ("£9.50 each", 9.50),
        ("€11,99", 11.99),
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
    assert looks_like_challenge(text, url) is expected


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
    ],
)
def test_looks_like_signin(text: str, url: str, expected: bool) -> None:
    assert looks_like_signin(text, url) is expected


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
        self, reveal_on_wait: set[str] | None = None, role_text: bool = False
    ) -> None:
        self.present: set[str] = set()
        self._reveal = reveal_on_wait or set()
        self._role_text = role_text
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


def _purchaser(config: Config) -> CostcoPurchaser:
    return CostcoPurchaser(config)


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

    def locator(self, selector: str) -> _PriceLocator:
        return self._matches.get(selector, _PriceLocator([]))


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
        # Costco's account nav reads the login state.
        return "Hello, Finn" if self._page.logged_in else "Sign In / Register"

    def click(self, timeout: int | None = None) -> None:
        self._page.clicked.append(self._selector)
        # Submitting the prefilled logon form establishes the session.
        if self._selector in _SIGNIN_SUBMIT_SELECTORS:
            self._page.logged_in = True


class _LoginRole:
    """No role/text match — forces ensure_logged_in onto its selector backstops."""

    @property
    def first(self) -> "_LoginRole":
        return self

    def click(self, timeout: int | None = None) -> None:
        raise TimeoutError("no role match")


class _LoginPage:
    """Models the cached-credential sign-in: starts logged out and the account
    nav flips to a name once the prefilled logon form's submit is clicked."""

    def __init__(self, *, logged_in: bool, present: set[str] | None = None) -> None:
        self.logged_in = logged_in
        self.present = present or set()
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []

    def locator(self, selector: str) -> _LoginLocator:
        return _LoginLocator(self, selector)

    def get_by_role(self, role: str, name: object = None) -> _LoginRole:
        return _LoginRole()

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_urls.append(url)

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        pass


def test_ensure_logged_in_short_circuits_when_already_logged_in(config: Config) -> None:
    page = _LoginPage(logged_in=True, present={_ACCOUNT_NAV})
    assert _purchaser(config).ensure_logged_in(page) is True
    assert page.clicked == []
    assert page.goto_urls == []


def test_ensure_logged_in_clicks_cached_credential_submit(config: Config) -> None:
    # Logged out, but the prefilled logon form's submit is in the DOM.
    page = _LoginPage(logged_in=False, present={_ACCOUNT_NAV, _SIGNIN_SUBMIT_SELECTORS[0]})
    assert _purchaser(config).ensure_logged_in(page) is True
    # Reached the logon page (role link missed) and clicked the form's submit.
    assert any("logon" in u for u in page.goto_urls)
    assert _SIGNIN_SUBMIT_SELECTORS[0] in page.clicked


def test_ensure_logged_in_fails_when_submit_never_takes(config: Config) -> None:
    # Logged out and nothing to click — the caller must bail with manual login.
    page = _LoginPage(logged_in=False, present={_ACCOUNT_NAV})
    assert _purchaser(config).ensure_logged_in(page) is False
