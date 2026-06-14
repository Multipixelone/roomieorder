from __future__ import annotations

import json
from pathlib import Path

import pytest

from roomieorder.catalog import CatalogError, load_catalog


def test_load_ok(catalog_path: Path) -> None:
    cat = load_catalog(catalog_path)
    assert set(cat) == {"paper_towels", "dish_soap"}
    assert cat["paper_towels"].asin == "B07ABCDEFG"
    assert cat["dish_soap"].qty == 2


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope.json")


def test_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json")
    with pytest.raises(CatalogError, match="not valid JSON"):
        load_catalog(p)


def test_bad_asin(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"x": {"title": "t", "asin": "short", "expected_price": 1, "price_ceiling": 2}}))
    with pytest.raises(CatalogError, match="asin"):
        load_catalog(p)


def test_ceiling_below_expected_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps({"x": {"title": "t", "asin": "B07ABCDEFG", "expected_price": 30, "price_ceiling": 20}})
    )
    with pytest.raises(CatalogError, match="price_ceiling"):
        load_catalog(p)
