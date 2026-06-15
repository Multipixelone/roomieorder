from __future__ import annotations

import pytest

from roomieorder.config import Config
from roomieorder.purchase import (
    _PLACE_ORDER_SELECTORS,
    AmazonPurchaser,
    looks_like_challenge,
    looks_like_signin,
    parse_price,
)


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
    "text,url,expected",
    [
        ("Enter the characters you see below", "", True),
        ("normal product page", "https://www.amazon.com/dp/B07X", False),
        ("", "https://www.amazon.com/ap/cvf/request", True),
        ("Verify it's you to continue", "", True),
        ("Solve this puzzle", "", True),
    ],
)
def test_looks_like_challenge(text: str, url: str, expected: bool) -> None:
    assert looks_like_challenge(text, url) is expected


@pytest.mark.parametrize(
    "text,url,expected",
    [
        ("Sign in or create account", "", True),
        ("Enter mobile number or email", "", True),
        ("", "https://www.amazon.com/ap/signin?openid", True),
        ("Secure checkout — Place your order", "", False),
        ("normal product page", "https://www.amazon.com/dp/B07X", False),
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

    def click(self, timeout: int | None = None) -> None:
        if not self._hit:
            raise TimeoutError("no role match")
        self._page.clicked.append("role:place your order")


class _FakePage:
    """Minimal stand-in for a Playwright page that models the checkout race:
    the place-order button is absent until ``wait_for_selector`` is awaited,
    mimicking Amazon's JS rendering the body after navigation."""

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

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.waited.append(selector)
        self.present |= self._reveal


def _purchaser(config: Config) -> AmazonPurchaser:
    return AmazonPurchaser(config)


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
