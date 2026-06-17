from __future__ import annotations

from datetime import timedelta

from roomieorder.store import Store, _iso, _utcnow


def test_enqueue_claim_mark(store: Store) -> None:
    rid = store.enqueue("paper_towels", "alice")
    assert store.pending_count() == 1

    row = store.claim_next_pending()
    assert row is not None
    assert row.id == rid
    assert row.status == "in_progress"
    assert row.attempts == 1
    assert store.pending_count() == 0

    store.mark(rid, "placed", unit_price=24.99, order_total=24.99, order_id="123-4567890-1234567")
    placed = store.list_queue()[0]
    assert placed.status == "placed"
    assert placed.order_total == 24.99
    assert placed.order_id == "123-4567890-1234567"


def test_claim_empty_returns_none(store: Store) -> None:
    assert store.claim_next_pending() is None


def test_claim_respects_attempts_cap(store: Store) -> None:
    # A row that's already been claimed up to MAX_ATTEMPTS (1) and bounced back
    # to pending must not be re-claimed — it's left for recovery to fail.
    rid = store.enqueue("paper_towels")
    first = store.claim_next_pending()
    assert first is not None and first.attempts == 1
    store.mark(rid, "pending")  # simulate a requeue
    assert store.claim_next_pending() is None


def test_fifo_order(store: Store) -> None:
    a = store.enqueue("paper_towels")
    b = store.enqueue("dish_soap")
    first = store.claim_next_pending()
    second = store.claim_next_pending()
    assert first is not None and second is not None
    assert first.id == a
    assert second.id == b


def test_recover_stale_fails_orphans(store: Store) -> None:
    # A row claimed but never marked (process died mid-buy) is stuck in_progress.
    rid = store.enqueue("paper_towels")
    claimed = store.claim_next_pending()
    assert claimed is not None and claimed.status == "in_progress"

    recovered = store.recover_stale()
    assert [r.id for r in recovered] == [rid]
    assert recovered[0].status == "failed"
    assert "review" in recovered[0].notes.lower()
    # Idempotent: nothing left in_progress on a second pass.
    assert store.recover_stale() == []


def test_recover_stale_noop_without_orphans(store: Store) -> None:
    store.enqueue("paper_towels")  # pending, never claimed
    assert store.recover_stale() == []
    assert store.pending_count() == 1


def test_spend_since_window(store: Store) -> None:
    rid = store.enqueue("paper_towels")
    store.mark(rid, "placed", order_total=30.0)
    assert store.spend_since(24.0) == 30.0

    # An order marked outside the window doesn't count. Backdate updated_at.
    old = store.enqueue("dish_soap")
    store.mark(old, "placed", order_total=99.0)
    store._conn.execute(
        "UPDATE queue SET updated_at=? WHERE id=?",
        (_iso(_utcnow() - timedelta(hours=25)), old),
    )
    store._conn.commit()
    assert store.spend_since(24.0) == 30.0


def test_last_placed_at(store: Store) -> None:
    assert store.last_placed_at("paper_towels") is None
    rid = store.enqueue("paper_towels")
    store.mark(rid, "placed", order_total=10.0)
    assert store.last_placed_at("paper_towels") is not None
    # A non-placed row doesn't set the cooldown clock.
    assert store.last_placed_at("dish_soap") is None


def test_last_placed_at_all_groups_by_item(store: Store) -> None:
    assert store.last_placed_at_all() == {}
    a = store.enqueue("paper_towels")
    store.mark(a, "placed", order_total=10.0)
    b = store.enqueue("dish_soap")
    store.mark(b, "failed")  # non-placed → absent from the map
    grouped = store.last_placed_at_all()
    assert set(grouped) == {"paper_towels"}
    assert grouped["paper_towels"] == store.last_placed_at("paper_towels")


def test_pause_roundtrip(store: Store) -> None:
    assert store.is_paused() is False
    store.set_paused(True, "captcha")
    assert store.is_paused() is True
    assert store.pause_reason() == "captcha"
    store.set_paused(False)
    assert store.is_paused() is False
