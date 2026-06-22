from __future__ import annotations

from roomieorder.sheets import COLUMNS, row_to_values


def test_ref_is_the_last_column() -> None:
    # `ref` (the queue row id) is appended after the original schema, so existing
    # rows/columns keep their positions and only a new trailing column is added.
    assert COLUMNS[-1] == "ref"


def test_row_to_values_projects_ref_in_order() -> None:
    row = {
        "timestamp": "2026-06-22T00:00:00+00:00",
        "item_key": "paper_towels",
        "title": "Paper Towels",
        "provider": "costco",
        "product_id": "123",
        "qty": 1,
        "unit_price": 19.99,
        "order_total": 21.50,
        "order_id": "A1",
        "status": "placed",
        "requester": "finn",
        "notes": "ok",
        "ref": 47,
    }
    values = row_to_values(row)
    assert len(values) == len(COLUMNS)
    assert values[COLUMNS.index("ref")] == 47


def test_row_to_values_defaults_missing_ref_to_blank() -> None:
    # A caller that predates the column (or a None id) still projects cleanly.
    assert row_to_values({"item_key": "x"})[COLUMNS.index("ref")] == ""
    assert row_to_values({"item_key": "x", "ref": None})[COLUMNS.index("ref")] == ""
