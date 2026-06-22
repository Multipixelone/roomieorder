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

import roomieorder.cli as cli
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
    # New diagnostics: effective buy-flow timeouts and 24h spend vs the cap.
    assert "timeouts" in result.output and "step=20000ms" in result.output
    assert "spend" in result.output


def test_doctor_fails_on_bad_catalog(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOMIEORDER_CATALOG", str(env / "does-not-exist.json"))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_doctor_check_login_probes_each_profile(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe only runs for *present* profiles, so create both subdirs.
    (env / "profile" / "costco").mkdir(parents=True)
    (env / "profile" / "amazon").mkdir(parents=True)

    class _FakePurchaser:
        def __init__(self, logged_in: bool) -> None:
            self._logged_in = logged_in

        def verify_session(self) -> bool:
            return self._logged_in

    monkeypatch.setattr(cli, "_purchaser_for", lambda config, provider: _FakePurchaser(provider == "costco"))
    result = CliRunner().invoke(main, ["doctor", "--check-login"])
    assert result.exit_code == 0, result.output
    assert "login/costco" in result.output and "LOGGED-IN" in result.output
    assert "login/amazon" in result.output and "LOGGED-OUT" in result.output


def test_doctor_default_never_probes_login(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without --check-login, doctor must not launch a browser/probe at all.
    def _boom(config: object, provider: str) -> object:
        raise AssertionError("doctor probed login without --check-login")

    monkeypatch.setattr(cli, "_purchaser_for", _boom)
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "login/" not in result.output


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


# ─────────── prune-shots ───────────


def test_prune_shots_removes_old_files(env: Path) -> None:
    import os
    import time

    shots = env / "shots"
    shots.mkdir()
    old = shots / "20260101T000000Z_costco_paper_towels_review.png"
    old.write_bytes(b"x")
    when = time.time() - 40 * 86_400
    os.utime(old, (when, when))
    fresh = shots / "fresh.png"
    fresh.write_bytes(b"x")

    result = CliRunner().invoke(main, ["prune-shots", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "pruned 1 file(s) older than 30d" in result.output
    assert not old.exists()
    assert fresh.exists()


def test_prune_shots_disabled_with_zero(env: Path) -> None:
    result = CliRunner().invoke(main, ["prune-shots", "--days", "0"])
    assert result.exit_code == 0
    assert "retention disabled" in result.output


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


# ─────────── trace-order arg handling + dry-run contract (no browser) ───────────


def test_trace_order_unknown_item(env: Path) -> None:
    result = CliRunner().invoke(main, ["trace-order", "nope"])
    assert result.exit_code != 0
    assert "unknown item_key" in result.output


def test_trace_order_no_source_for_provider(env: Path) -> None:
    result = CliRunner().invoke(main, ["trace-order", "dish_soap", "--provider", "amazon"])
    assert result.exit_code != 0
    assert "no amazon source" in result.output


def test_trace_order_forces_dry_run_and_prints_steps(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from roomieorder.purchase import PurchaseResult, TraceStep

    # DRY_RUN off in the env must NOT reach the buy: trace-order hard-forces it.
    monkeypatch.setenv("DRY_RUN", "false")
    seen: dict[str, object] = {}

    class _FakePurchaser:
        config: object = None

        def _resolve_url(self, source: object) -> str:
            return "https://example.test/p"

        def buy(self, item_key, item, source, proceed_check, *, tracer):  # type: ignore[no-untyped-def]
            seen["dry_run"] = self.config.dry_run  # type: ignore[attr-defined]
            seen["tracer"] = tracer
            tracer.steps.append(
                TraceStep(
                    name="product_loaded",
                    idx=1,
                    url="https://example.test/p",
                    summary="[price]\n  sel  count=1\n",
                    probe=Path("/tmp/p_probe.txt"),
                )
            )
            tracer.steps.append(
                TraceStep(
                    name="checkout_landed",
                    idx=2,
                    url="https://example.test/checkout",
                    summary="[place-order]\n  sel  count=1\n[order-total]\n  sel  count=1\n",
                    probe=Path("/tmp/c_probe.txt"),
                )
            )
            return PurchaseResult(status="dry_run", unit_price=24.99, order_total=27.39)

    def _fake_purchaser_for(config: object, provider: str) -> object:
        p = _FakePurchaser()
        p.config = config
        return p

    monkeypatch.setattr(cli, "_purchaser_for", _fake_purchaser_for)
    result = CliRunner().invoke(main, ["trace-order", "paper_towels"])
    assert result.exit_code == 0, result.output
    assert seen["dry_run"] is True  # forced on despite DRY_RUN=false
    assert "01 product_loaded" in result.output
    assert "02 checkout_landed" in result.output
    # The checkout step is where place-order/order-total finally resolve.
    assert "place-order=ok" in result.output
    assert "order-total=ok" in result.output
    assert "status:      dry_run" in result.output


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
