from __future__ import annotations

from datetime import datetime, timedelta, timezone

from roomieorder.catalog import CatalogItem, load_catalog
from roomieorder.config import Config
from roomieorder.guards import (
    check_intake,
    check_price_ceiling,
    check_spend_cap,
)
from roomieorder.store import Store


def _item(config: Config, key: str = "paper_towels") -> CatalogItem:
    return load_catalog(config.catalog_path)[key]


def test_intake_allows_first_tap(store: Store, config: Config) -> None:
    res = check_intake(store, config, "paper_towels", _item(config))
    assert res.ok is True


def test_intake_debounce(store: Store, config: Config) -> None:
    store.enqueue("paper_towels")
    res = check_intake(store, config, "paper_towels", _item(config))
    assert res.ok is False
    assert res.status == "skipped_debounce"
    assert res.enqueue is False


def test_intake_cooldown(store: Store, config: Config) -> None:
    rid = store.enqueue("paper_towels")
    store.mark(rid, "placed", order_total=24.99)
    # Past debounce window but inside the 10-day cooldown.
    now = datetime.now(timezone.utc) + timedelta(seconds=120)
    res = check_intake(store, config, "paper_towels", _item(config), now=now)
    assert res.ok is False
    assert res.status == "skipped_cooldown"
    assert res.enqueue is True  # cooldown skips get a Sheets trail


def test_intake_cooldown_expired(store: Store, config: Config) -> None:
    rid = store.enqueue("paper_towels")
    store.mark(rid, "placed", order_total=24.99)
    now = datetime.now(timezone.utc) + timedelta(days=11)
    res = check_intake(store, config, "paper_towels", _item(config), now=now)
    assert res.ok is True


def test_intake_blocks_when_paused(store: Store, config: Config) -> None:
    store.set_paused(True, "captcha")
    res = check_intake(store, config, "paper_towels", _item(config))
    assert res.ok is False
    assert "paused" in res.reason


def test_price_ceiling() -> None:
    item = CatalogItem(title="t", asin="B07ABCDEFG", expected_price=24.99, price_ceiling=32.0)
    assert check_price_ceiling(item, 30.0).ok is True
    blocked = check_price_ceiling(item, 33.0)
    assert blocked.ok is False
    assert blocked.status == "price_blocked"


def test_spend_cap(store: Store, config: Config) -> None:
    # cap is 100. One $90 order already placed → a $20 order should trip it.
    rid = store.enqueue("paper_towels")
    store.mark(rid, "placed", order_total=90.0)
    assert check_spend_cap(store, config, 5.0).ok is True
    tripped = check_spend_cap(store, config, 20.0)
    assert tripped.ok is False
    assert tripped.status == "spend_capped"
