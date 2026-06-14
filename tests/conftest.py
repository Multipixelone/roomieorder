"""Shared fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from roomieorder.config import Config
from roomieorder.store import Store

_CATALOG = {
    "paper_towels": {
        "title": "Bounty Quick-Size 12 Family Rolls",
        "asin": "B07ABCDEFG",
        "url": "https://www.amazon.com/dp/B07ABCDEFG",
        "qty": 1,
        "expected_price": 24.99,
        "price_ceiling": 32.00,
        "cooldown_days": 10,
    },
    "dish_soap": {
        "title": "Dawn Ultra 3-Pack",
        "asin": "B07HIJKLMN",
        "qty": 2,
        "expected_price": 11.99,
        "price_ceiling": 16.00,
        "cooldown_days": 0,
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
