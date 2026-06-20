from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roomieorder.catalog import CatalogItem
from roomieorder.config import Config
from roomieorder.purchase import PurchaseResult
from roomieorder.store import Status, Store


class FakeOrchestrator:
    """Stand-in for Orchestrator — never launches a browser.

    Returns whatever ``result_status`` the test asked for, with a costco
    provider, so the worker-loop/intake wiring is exercised without Playwright.
    The Costco→Amazon fallback itself is covered in test_orchestrator.py.
    """

    result_status: Status = "dry_run"

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    def buy(self, item_key: str, item: CatalogItem):  # type: ignore[no-untyped-def]
        price = item.costco.expected_price if item.costco else item.amazon.expected_price  # type: ignore[union-attr]
        # A placed order records a real order_total (what the cap backstop reads).
        order_total = price * item.qty if self.result_status == "placed" else None
        return PurchaseResult(
            status=self.result_status,
            unit_price=price,
            order_total=order_total,
            provider="costco",
            message=f"[fake] {self.result_status} {item_key}",
        )


@pytest.fixture
def client(config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Fast worker tick + no real browser.
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.Orchestrator", FakeOrchestrator)
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
    # This test drives the store directly (enqueue → mark placed) to arm the
    # cooldown. The live worker polls every 20ms and would claim the freshly
    # enqueued pending row, process it via FakeOrchestrator (dry_run), and mark
    # it — racing our mark(placed) and intermittently reverting the row's status
    # so last_placed_at_all() finds no placed order and on_cooldown reads False.
    # Quiesce the worker first so the only writer is the test.
    engine.stop_worker()

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


def test_reorder_requires_token_when_configured(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.Orchestrator", FakeOrchestrator)
    secured = config.model_copy(update={"intake_token": "s3cret"})
    from roomieorder.main import create_app

    with TestClient(create_app(secured)) as c:
        # No header → rejected; wrong header → rejected; right header → accepted.
        assert c.post("/reorder", json={"item_key": "paper_towels"}).status_code == 401
        bad = c.post(
            "/reorder", json={"item_key": "paper_towels"}, headers={"X-Roomieorder-Token": "nope"}
        )
        assert bad.status_code == 401
        ok = c.post(
            "/reorder",
            json={"item_key": "paper_towels"},
            headers={"X-Roomieorder-Token": "s3cret"},
        )
        assert ok.status_code == 200 and ok.json()["accepted"] is True


def test_reorder_open_when_no_token(client: TestClient) -> None:
    # Default config has no token → no header needed (loopback dev case).
    r = client.post("/reorder", json={"item_key": "paper_towels"})
    assert r.status_code == 200


def test_startup_recovers_orphaned_in_progress(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a crash: a row left in_progress in the DB before the app boots.
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.Orchestrator", FakeOrchestrator)
    pre = Store(config.db_path)
    pre.init_db()
    rid = pre.enqueue("paper_towels")
    pre.claim_next_pending()  # in_progress, never marked
    pre.close()

    from roomieorder.main import create_app

    app = create_app(config)
    with TestClient(app) as c:
        # Startup recovery fails the orphan and pauses for review.
        assert c.get("/health").json()["paused"] is True
        rows = c.get("/queue").json()
        orphan = next(r for r in rows if r["id"] == rid)
        assert orphan["status"] == "failed"


def test_reload_picks_up_catalog_edits(client: TestClient, catalog_path: Path) -> None:
    import json

    before = client.get("/health").json()["items"]
    assert "cocoa" not in before

    data = json.loads(catalog_path.read_text())
    data["cocoa"] = {
        "title": "Hot Cocoa",
        "qty": 1,
        "cooldown_days": 0,
        "costco": {"item_number": "9999999", "expected_price": 8.0, "price_ceiling": 12.0},
    }
    catalog_path.write_text(json.dumps(data))

    r = client.post("/reload")
    assert r.status_code == 200
    assert "cocoa" in r.json()["items"]
    assert "cocoa" in client.get("/health").json()["items"]


def test_reload_rejects_bad_catalog(client: TestClient, catalog_path: Path) -> None:
    catalog_path.write_text("{ not valid json")
    r = client.post("/reload")
    assert r.status_code == 400
    # The running catalog is untouched — the bad edit didn't take.
    assert "paper_towels" in client.get("/health").json()["items"]


def test_worker_pauses_when_recorded_spend_breaches_cap(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.Orchestrator", FakeOrchestrator)
    # paper_towels Costco price 24.99 → a $20 cap is breached by the real total.
    capped = config.model_copy(update={"dry_run": False, "daily_cap": 20.0})
    FakeOrchestrator.result_status = "placed"
    try:
        from roomieorder.main import create_app

        with TestClient(create_app(capped)) as c:
            c.post("/reorder", json={"item_key": "paper_towels"})
            deadline = time.time() + 5.0
            while time.time() < deadline and not c.get("/health").json()["paused"]:
                time.sleep(0.05)
            assert c.get("/health").json()["paused"] is True
            assert "cap" in c.get("/health").json()["pause_reason"]
    finally:
        FakeOrchestrator.result_status = "dry_run"


def test_worker_pauses_on_challenge(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("roomieorder.main._WORKER_POLL_SECONDS", 0.02)
    monkeypatch.setattr("roomieorder.main.Orchestrator", FakeOrchestrator)
    FakeOrchestrator.result_status = "challenge"
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
        FakeOrchestrator.result_status = "dry_run"
