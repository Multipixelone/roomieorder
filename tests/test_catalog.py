from __future__ import annotations

import json
from pathlib import Path

import pytest

from roomieorder.catalog import CatalogError, load_catalog


def _costco(**over: object) -> dict[str, object]:
    base = {"item_number": "1640526", "expected_price": 1, "price_ceiling": 2}
    base.update(over)
    return base


def _amazon(**over: object) -> dict[str, object]:
    base = {"asin": "B07YYYYYYY", "expected_price": 1, "price_ceiling": 2}
    base.update(over)
    return base


def _write(tmp_path: Path, item: dict[str, object]) -> Path:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"x": item}))
    return p


def test_load_ok(catalog_path: Path) -> None:
    cat = load_catalog(catalog_path)
    assert set(cat) == {"paper_towels", "dish_soap"}
    assert cat["paper_towels"].costco is not None
    assert cat["paper_towels"].costco.item_number == "1640526"
    assert cat["paper_towels"].amazon is not None
    assert cat["paper_towels"].amazon.asin == "B07YYYYYYY"
    assert cat["dish_soap"].qty == 2
    # dish_soap is Costco-only — no fallback declared.
    assert cat["dish_soap"].amazon is None
    # category is presentation-only and defaults to "" when absent.
    assert cat["paper_towels"].category == ""


def test_category_roundtrips(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "category": "Kitchen", "costco": _costco()})
    assert load_catalog(p)["x"].category == "Kitchen"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope.json")


def test_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json")
    with pytest.raises(CatalogError, match="not valid JSON"):
        load_catalog(p)


def test_amazon_only_item_is_valid(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "amazon": _amazon()})
    item = load_catalog(p)["x"]
    assert item.costco is None
    assert item.amazon is not None


def test_item_with_no_source_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t"})
    with pytest.raises(CatalogError, match="at least one"):
        load_catalog(p)


def test_bad_item_number(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "costco": _costco(item_number="B07X")})
    with pytest.raises(CatalogError, match="item_number"):
        load_catalog(p)


def test_bad_asin(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "amazon": _amazon(asin="too-short")})
    with pytest.raises(CatalogError, match="asin"):
        load_catalog(p)


def test_ceiling_below_expected_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "costco": _costco(expected_price=30, price_ceiling=20)})
    with pytest.raises(CatalogError, match="price_ceiling"):
        load_catalog(p)


def test_amazon_ceiling_below_expected_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "t", "amazon": _amazon(expected_price=30, price_ceiling=20)})
    with pytest.raises(CatalogError, match="price_ceiling"):
        load_catalog(p)
