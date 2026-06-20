"""CLI tests for the offline diagnostic commands.

Covers the read-only commands that don't drive a browser: ``doctor``,
``failures``, ``retry``, and the pure summary-parsing helpers behind
``verify-selectors``. The live ``verify-selectors`` browser path is operator-run
(it hits real store pages), so only its catalog/argument handling — which fails
before any browser launch — is exercised here, mirroring how test_purchase.py
covers only the pure helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from roomieorder.cli import _group_hits, _read_price_from_summary, main
from roomieorder.store import Store

_CATALOG = {
    "paper_towels": {
        "title": "Bounty Advanced",
        "qty": 1,
        "costco": {"item_number": "1640526", "expected_price": 24.99, "price_ceiling": 32.0},
        "amazon": {"asin": "B07YYYYYYY", "expected_price": 23.99, "price_ceiling": 30.0},
    },
    "dish_soap": {
        "title": "Dawn Ultra",
        "qty": 1,
        "costco": {"item_number": "1308124", "expected_price": 11.99, "price_ceiling": 16.0},
    },
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's ``load_config`` at a tmp catalog/db/shots, return tmp_path."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(_CATALOG))
    monkeypatch.setenv("ROOMIEORDER_CATALOG", str(catalog))
    monkeypatch.setenv("ROOMIEORDER_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("ROOMIEORDER_SHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setenv("ROOMIEORDER_PROFILE_DIR", str(tmp_path / "profile"))
    return tmp_path


def _seed(tmp_path: Path, item_key: str, status: str, notes: str = "") -> int:
    store = Store(tmp_path / "state.sqlite")
    store.init_db()
    row_id = store.enqueue(item_key)
    store.mark(row_id, status, notes=notes)  # type: ignore[arg-type]
    store.close()
    return row_id


# ─────────── doctor ───────────


def test_doctor_runs_clean(env: Path) -> None:
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "catalog        2 items" in result.output
    assert "dry_run" in result.output


def test_doctor_fails_on_bad_catalog(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOMIEORDER_CATALOG", str(env / "does-not-exist.json"))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


# ─────────── failures ───────────


def test_failures_lists_trouble_rows(env: Path) -> None:
    _seed(env, "paper_towels", "failed", notes="no_buy_button")
    _seed(env, "dish_soap", "placed")  # not a trouble status
    result = CliRunner().invoke(main, ["failures"])
    assert result.exit_code == 0, result.output
    assert "failed" in result.output
    assert "paper_towels" in result.output
    assert "no_buy_button" in result.output
    assert "dish_soap" not in result.output.split("shots dir")[0]


def test_failures_empty(env: Path) -> None:
    result = CliRunner().invoke(main, ["failures"])
    assert result.exit_code == 0
    assert "(no recent failures)" in result.output


# ─────────── retry ───────────


def test_retry_reenqueues_failed_row(env: Path) -> None:
    row_id = _seed(env, "paper_towels", "failed")
    result = CliRunner().invoke(main, ["retry", str(row_id)])
    assert result.exit_code == 0, result.output
    assert "re-enqueued paper_towels" in result.output
    store = Store(env / "state.sqlite")
    assert store.pending_count() == 1
    store.close()


def test_retry_refuses_needs_review(env: Path) -> None:
    row_id = _seed(env, "paper_towels", "needs_review")
    result = CliRunner().invoke(main, ["retry", str(row_id)])
    assert result.exit_code != 0
    assert "refusing to retry" in result.output
    store = Store(env / "state.sqlite")
    assert store.pending_count() == 0  # nothing enqueued
    store.close()


def test_retry_unknown_row(env: Path) -> None:
    result = CliRunner().invoke(main, ["retry", "999"])
    assert result.exit_code != 0
    assert "no queue row #999" in result.output


# ─────────── verify-selectors arg handling (no browser) ───────────


def test_verify_selectors_unknown_item(env: Path) -> None:
    result = CliRunner().invoke(main, ["verify-selectors", "nope"])
    assert result.exit_code != 0
    assert "unknown item_key" in result.output


def test_verify_selectors_no_source_for_provider(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # dish_soap has no amazon source; paper_towels does — make a catalog with only
    # the costco-only item so the amazon target set is empty.
    catalog = env / "catalog.json"
    catalog.write_text(json.dumps({"dish_soap": _CATALOG["dish_soap"]}))
    result = CliRunner().invoke(main, ["verify-selectors", "--provider", "amazon"])
    assert result.exit_code != 0
    assert "no items declare a amazon source" in result.output


# ─────────── summary parsing helpers ───────────

_SAMPLE_SUMMARY = """\
url:   https://www.costco.com/p/-/123
title: Bounty
logged_in:   True
read_price:  24.99

[price]
  .product-price  count=0
  span[data-testid='price']  count=1  sample='$24.99'

[price-meta]
  meta[property='og:price:amount']  count=0

[add-to-cart]
  button#add-to-cart  count=0
"""


def test_group_hits_parses_counts() -> None:
    hits = _group_hits(_SAMPLE_SUMMARY)
    assert hits["price"] is True
    assert hits["price-meta"] is False
    assert hits["add-to-cart"] is False


def test_read_price_from_summary() -> None:
    assert _read_price_from_summary(_SAMPLE_SUMMARY) == "24.99"
    assert _read_price_from_summary("read_price:  None") is None
    assert _read_price_from_summary("no price line here") is None
