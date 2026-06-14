from __future__ import annotations

import pytest

from roomieorder.purchase import looks_like_challenge, parse_price


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
