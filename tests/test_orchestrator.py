from __future__ import annotations

from typing import Any

import pytest

from roomieorder.catalog import AmazonSource, CatalogItem, load_catalog
from roomieorder.config import Config
from roomieorder.orchestrator import Orchestrator
from roomieorder.purchase import PurchaseResult
from roomieorder.store import Status, Store


def _make_fake(
    provider_name: str,
    *,
    status: Status = "placed",
    price: float | None = None,
    use_guard: bool = False,
) -> type[Any]:
    """Build a browser-free stand-in purchaser that returns a canned result.

    ``use_guard=True`` runs the real per-source ``proceed_check`` against
    ``price`` so the price-ceiling fallback can be exercised."""

    class _Fake:
        def __init__(self, config: Config, *, profile_dir: object, domain: str) -> None:
            pass

        def buy(self, item_key, item, source, proceed_check):  # type: ignore[no-untyped-def]
            if use_guard:
                feed = price if price is not None else source.expected_price
                decision = proceed_check(feed)
                if not decision.ok:
                    return PurchaseResult(
                        status=decision.status or "failed",
                        unit_price=feed,
                        message=decision.reason,
                    )
            return PurchaseResult(status=status, message=f"{status} from {provider_name}")

    return _Fake


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    costco: object | None = None,
    amazon: object | None = None,
) -> None:
    if costco is not None:
        monkeypatch.setattr("roomieorder.orchestrator.CostcoPurchaser", costco)
    if amazon is not None:
        monkeypatch.setattr("roomieorder.orchestrator.AmazonPurchaser", amazon)


def _item(config: Config, key: str = "paper_towels") -> CatalogItem:
    return load_catalog(config.catalog_path)[key]


def test_costco_placed_no_fallback(config: Config, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # Amazon fake would report failure if reached — proves it isn't.
    _patch(monkeypatch, costco=_make_fake("costco", status="placed"),
           amazon=_make_fake("amazon", status="failed"))
    result = Orchestrator(config, store).buy("paper_towels", _item(config))
    assert result.status == "placed"
    assert result.provider == "costco"
    assert "fell back" not in result.message


def test_costco_unavailable_falls_back_to_amazon(
    config: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, costco=_make_fake("costco", status="unavailable"),
           amazon=_make_fake("amazon", status="placed"))
    result = Orchestrator(config, store).buy("paper_towels", _item(config))
    assert result.status == "placed"
    assert result.provider == "amazon"
    assert "fell back" in result.message


def test_costco_over_ceiling_falls_back_to_amazon(
    config: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Costco price 99 blows past its 32.00 ceiling → price_blocked → fall back.
    _patch(monkeypatch, costco=_make_fake("costco", price=99.0, use_guard=True),
           amazon=_make_fake("amazon", status="placed"))
    result = Orchestrator(config, store).buy("paper_towels", _item(config))
    assert result.status == "placed"
    assert result.provider == "amazon"


def test_both_unavailable_is_terminal(
    config: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, costco=_make_fake("costco", status="unavailable"),
           amazon=_make_fake("amazon", status="unavailable"))
    result = Orchestrator(config, store).buy("paper_towels", _item(config))
    assert result.status == "unavailable"
    assert result.provider == "amazon"  # the last store tried
    assert "fell back" in result.message


def test_amazon_only_item_skips_costco(
    config: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Costco fake raises if constructed — proves it's never in the chain.
    def _boom(*a: object, **k: object) -> None:  # noqa: ANN002, ANN003
        raise AssertionError("Costco should not be tried for an Amazon-only item")

    _patch(monkeypatch, costco=_boom, amazon=_make_fake("amazon", status="placed"))
    item = CatalogItem(
        title="t", amazon=AmazonSource(asin="B07YYYYYYY", expected_price=5, price_ceiling=10)
    )
    result = Orchestrator(config, store).buy("amazon_only", item)
    assert result.status == "placed"
    assert result.provider == "amazon"


def test_costco_challenge_does_not_fall_back(
    config: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A challenge needs a human — it must NOT silently fall back to Amazon.
    _patch(monkeypatch, costco=_make_fake("costco", status="challenge"),
           amazon=_make_fake("amazon", status="placed"))
    result = Orchestrator(config, store).buy("paper_towels", _item(config))
    assert result.status == "challenge"
    assert result.provider == "costco"
    assert "fell back" not in result.message
