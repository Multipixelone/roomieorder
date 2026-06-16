"""Shared fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from roomieorder.config import Config
from roomieorder.store import Store

_CATALOG = {
    # Two sources: Costco first, Amazon fallback.
    "paper_towels": {
        "title": "Bounty Advanced 12 Family Rolls",
        "qty": 1,
        "cooldown_days": 10,
        "costco": {
            "item_number": "1640526",
            "url": "https://www.costco.com/bounty-advanced.product.1640526.html",
            "expected_price": 24.99,
            "price_ceiling": 32.00,
        },
        "amazon": {
            "asin": "B07YYYYYYY",
            "url": "https://www.amazon.com/dp/B07YYYYYYY",
            "expected_price": 23.99,
            "price_ceiling": 30.00,
        },
    },
    # Costco-only: no Amazon fallback declared.
    "dish_soap": {
        "title": "Dawn Ultra 2-Pack",
        "qty": 2,
        "cooldown_days": 0,
        "costco": {
            "item_number": "1308124",
            "expected_price": 11.99,
            "price_ceiling": 16.00,
        },
    },
}


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(_CATALOG))
    return p


@pytest.fixture
def config(tmp_path: Path, catalog_path: Path) -> Config:
    return Config(
        dry_run=True,
        daily_cap=100.0,
        debounce_seconds=60,
        catalog_path=catalog_path,
        db_path=tmp_path / "state.sqlite",
        profile_dir=tmp_path / "profile",
        shots_dir=tmp_path / "shots",
    )


@pytest.fixture
def store(config: Config) -> Iterator[Store]:
    s = Store(config.db_path)
    s.init_db()
    yield s
    s.close()
