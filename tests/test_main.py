from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from roomieorder.catalog import CatalogItem
from roomieorder.config import Config
from roomieorder.guards import GuardResult
from roomieorder.purchase import PurchaseResult
from roomieorder.store import Status


class FakePurchaser:
    """Stand-in for CostcoPurchaser — never launches a browser.

    Honours the proceed_check callback (so guard wiring is exercised) and
    returns whatever ``result_status`` the test asked for.
    """

    result_status: Status = "dry_run"

    def __init__(self, config: Config) -> None:
        self.config = config

    def buy(self, item_key: str, item: CatalogItem, proceed_check):  # type: ignore[no-untyped-def]
        decision: GuardResult = proceed_check(item.expected_price)
        if not decision.ok:
            status: Status = decision.status if decision.status is not None else "failed"
            return PurchaseResult(
                status=status,
                unit_price=item.expected_price,
                message=decision.reason,
            )
        return PurchaseResult(
            status=self.result_status,
            unit_price=item.expected_price,
            message=f"[fake] {self.result_status} {item_key}",
        )


@pytest.fixture
def client(config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Fast worker tick + no real browser.
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.CostcoPurchaser", FakePurchaser)
    from roomieorder.main import create_app

    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _wait_for_status(client: TestClient, item_key: str, status: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = client.get("/queue").json()
        if any(r["item_key"] == item_key and r["status"] == status for r in rows):
            return True
        time.sleep(0.05)
    return False


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True
    assert "paper_towels" in body["items"]


def test_reorder_unknown_item(client: TestClient) -> None:
    r = client.post("/reorder", json={"item_key": "ghost"})
    assert r.status_code == 404


def test_reorder_accepted_and_processed(client: TestClient) -> None:
    r = client.post("/reorder", json={"item_key": "paper_towels", "requester": "bob"})
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert _wait_for_status(client, "paper_towels", "dry_run")


def test_reorder_debounced(client: TestClient) -> None:
    first = client.post("/reorder", json={"item_key": "dish_soap"})
    assert first.json()["accepted"] is True
    second = client.post("/reorder", json={"item_key": "dish_soap"})
    body = second.json()
    assert body["accepted"] is False
    assert body["status"] == "skipped_debounce"


def test_items_reports_cooldown(client: TestClient) -> None:
    engine = client.app.state.engine  # type: ignore[attr-defined]

    # Nothing ordered yet → nothing on cooldown.
    before = client.get("/items").json()
    assert before["paper_towels"]["on_cooldown"] is False
    assert before["paper_towels"]["last_placed_at"] is None
    assert before["paper_towels"]["cooldown_days"] == 10

    # A *placed* order arms paper_towels' 10-day cooldown.
    rid = engine.store.enqueue("paper_towels")
    engine.store.mark(rid, "placed", order_total=24.99)
    after = client.get("/items").json()
    assert after["paper_towels"]["on_cooldown"] is True
    assert after["paper_towels"]["last_placed_at"] is not None
    assert after["paper_towels"]["cooldown_until"] is not None

    # dish_soap has cooldown_days=0 → never grays, even after a placed order.
    rid2 = engine.store.enqueue("dish_soap")
    engine.store.mark(rid2, "placed", order_total=11.99)
    assert client.get("/items").json()["dish_soap"]["on_cooldown"] is False


def test_worker_pauses_on_challenge(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.CostcoPurchaser", FakePurchaser)
    FakePurchaser.result_status = "challenge"
    try:
        from roomieorder.main import create_app

        app = create_app(config)
        with TestClient(app) as c:
            c.post("/reorder", json={"item_key": "paper_towels"})
            deadline = time.time() + 5.0
            while time.time() < deadline and not c.get("/health").json()["paused"]:
                time.sleep(0.05)
            assert c.get("/health").json()["paused"] is True
    finally:
        FakePurchaser.result_status = "dry_run"
