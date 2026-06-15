"""FastAPI intake + background worker loop.

Two halves in one process, joined by the SQLite queue (PLAN §2):

* **Intake** — the FastAPI app. ``POST /reorder`` validates, runs the intake
  guards, and enqueues. It never touches the browser, so it answers instantly
  and stays up regardless of the graphical session.
* **Worker** — a daemon thread draining the queue. It uses *sync* Playwright,
  which cannot share the asyncio event loop, so it lives on its own thread.

If the desktop is asleep the browser can't run, but intake still enqueues;
the worker drains the backlog on wake.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from roomieorder.catalog import Catalog, load_catalog
from roomieorder.config import Config, load_config
from roomieorder.guards import check_intake, check_price_ceiling, check_spend_cap
from roomieorder.notify import Notifier, build_notifier
from roomieorder.purchase import AmazonPurchaser, PurchaseResult
from roomieorder.sheets import SheetsClient, build_sheets
from roomieorder.store import QueueRow, Store

_logger = logging.getLogger(__name__)

# How often the worker wakes to check for pending rows.
_WORKER_POLL_SECONDS = 5.0

# Outcomes that halt the worker until the operator clears them (PLAN §5).
_PAUSE_STATUSES = {"challenge", "failed", "spend_capped"}


class ReorderRequest(BaseModel):
    item_key: str
    requester: str = "household"


class ItemStatus(BaseModel):
    """Per-item state for the dashboard (`GET /items`).

    The HA buttons poll this to gray themselves out: while ``on_cooldown`` is
    true, the item was ordered inside its ``cooldown_days`` window, so a button
    can show when it was last ordered and refuse further taps instead of
    enqueuing a buy the intake guard would only reject (PLAN §5).
    """

    item_key: str
    title: str
    # Most recent *placed* order — the "last ordered" time the cooldown keys off.
    last_placed_at: Optional[datetime] = None
    cooldown_days: int = 0
    on_cooldown: bool = False
    cooldown_until: Optional[datetime] = None


class Engine:
    """Owns every long-lived collaborator and runs the worker thread.

    Built once at startup and stashed on ``app.state``; the worker thread and
    the request handlers both reach the store/notifier/etc. through it.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.catalog: Catalog = load_catalog(config.catalog_path)
        self.store = Store(config.db_path)
        self.store.init_db()
        self.notifier: Notifier = build_notifier(config)
        self.sheets: SheetsClient = build_sheets(config)
        self.purchaser = AmazonPurchaser(config)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─────────── worker lifecycle ───────────

    def start_worker(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker_loop, name="worker", daemon=True)
        self._thread.start()
        _logger.info("worker thread started (dry_run=%s)", self.config.dry_run)

    def stop_worker(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            if self.store.is_paused():
                self._stop.wait(_WORKER_POLL_SECONDS)
                continue
            row = self.store.claim_next_pending()
            if row is None:
                self._stop.wait(_WORKER_POLL_SECONDS)
                continue
            try:
                self._process(row)
            except Exception:  # noqa: BLE001 — a crash must not kill the loop
                _logger.exception("worker failed processing row %d", row.id)
                self.store.mark(row.id, "failed", notes="worker crashed")
                self.store.set_paused(True, f"worker crashed on row {row.id}")

    # ─────────── per-row processing ───────────

    def _process(self, row: QueueRow) -> None:
        item = self.catalog.get(row.item_key)
        if item is None:
            self.store.mark(row.id, "failed", notes="item_key not in catalog")
            self.notifier.send(f"⚠️ unknown item_key {row.item_key!r} — skipped")
            return

        def proceed_check(live_price: float):  # type: ignore[no-untyped-def]
            ceiling = check_price_ceiling(item, live_price)
            if not ceiling.ok:
                return ceiling
            return check_spend_cap(self.store, self.config, live_price * item.qty)

        result = self.purchaser.buy(row.item_key, item, proceed_check)
        self.store.mark(
            row.id,
            result.status,
            unit_price=result.unit_price,
            order_total=result.order_total,
            order_id=result.order_id,
            notes=result.message,
        )
        self._log_sheet(row, item, result)
        self.notifier.send(result.message, photo=result.screenshot)

        if result.status in _PAUSE_STATUSES:
            self.store.set_paused(True, result.message)
            _logger.warning("worker paused: %s", result.message)

    # ─────────── dashboard state ───────────

    def item_statuses(self, *, now: Optional[datetime] = None) -> dict[str, ItemStatus]:
        """Per-item cooldown snapshot for the HA dashboard (`GET /items`).

        Mirrors the cooldown arm of :func:`guards.check_intake` — keep the two
        in sync. Catalog is tiny, so a couple of indexed lookups per item is
        cheap enough to compute on every poll. Only *placed* orders arm the
        cooldown, so nothing grays out while ``dry_run`` is on (PLAN §4).
        """
        current = now or datetime.now(timezone.utc)
        out: dict[str, ItemStatus] = {}
        for key in sorted(self.catalog):
            item = self.catalog[key]
            last_placed = self.store.last_placed_at(key)
            cooldown_until: Optional[datetime] = None
            on_cooldown = False
            if last_placed is not None and item.cooldown_days > 0:
                unlock = last_placed + timedelta(days=item.cooldown_days)
                if current < unlock:
                    on_cooldown = True
                    cooldown_until = unlock
            out[key] = ItemStatus(
                item_key=key,
                title=item.title,
                last_placed_at=last_placed,
                cooldown_days=item.cooldown_days,
                on_cooldown=on_cooldown,
                cooldown_until=cooldown_until,
            )
        return out

    def _log_sheet(self, row: QueueRow, item, result: PurchaseResult) -> None:  # type: ignore[no-untyped-def]
        self.sheets.append(
            {
                "timestamp": row.updated_at.isoformat(),
                "item_key": row.item_key,
                "title": item.title,
                "asin": item.asin,
                "qty": item.qty,
                "unit_price": result.unit_price,
                "order_total": result.order_total,
                "order_id": result.order_id,
                "status": result.status,
                "requester": row.requester,
                "notes": result.message,
            }
        )


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = Engine(config)
        app.state.engine = engine
        engine.start_worker()
        try:
            yield
        finally:
            engine.stop_worker()

    app = FastAPI(title="roomieorder", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, object]:
        engine: Engine = app.state.engine
        return {
            "status": "ok",
            "dry_run": engine.config.dry_run,
            "paused": engine.store.is_paused(),
            "pause_reason": engine.store.pause_reason(),
            "pending": engine.store.pending_count(),
            "items": sorted(engine.catalog),
        }

    @app.get("/items")
    def items() -> dict[str, dict[str, object]]:
        # Keyed by item_key (same shape as catalog.json) so an HA `rest:` sensor
        # can pull one item with `value_json['<key>']` — see PLAN-ROOMIE.md §3.
        engine: Engine = app.state.engine
        return {k: v.model_dump(mode="json") for k, v in engine.item_statuses().items()}

    @app.get("/queue")
    def queue(limit: int = 20) -> list[dict[str, object]]:
        engine: Engine = app.state.engine
        return [r.model_dump(mode="json") for r in engine.store.list_queue(limit)]

    @app.post("/reorder")
    def reorder(req: ReorderRequest) -> dict[str, object]:
        engine: Engine = app.state.engine
        item = engine.catalog.get(req.item_key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"unknown item_key: {req.item_key}")

        decision = check_intake(engine.store, engine.config, req.item_key, item)
        if not decision.ok:
            # Guard rejections are a 200 with a skipped result (PLAN §3.3): the
            # tap was understood, we're just declining to act. Cooldown skips
            # get a queue row for the Sheets trail; debounce/pause don't.
            if decision.enqueue:
                row_id = engine.store.enqueue(req.item_key, req.requester)
                engine.store.mark(row_id, decision.status or "skipped_cooldown", notes=decision.reason)
            engine.notifier.send(decision.reason)
            return {"accepted": False, "reason": decision.reason, "status": decision.status}

        row_id = engine.store.enqueue(req.item_key, req.requester)
        return {"accepted": True, "row_id": row_id, "item_key": req.item_key}

    return app
