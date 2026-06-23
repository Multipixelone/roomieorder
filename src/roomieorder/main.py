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

import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from roomieorder import activity, heartbeat
from roomieorder.catalog import Catalog, CatalogError, CatalogItem, load_catalog
from roomieorder.config import Config, load_config
from roomieorder.guards import check_intake
from roomieorder.logutil import correlated
from roomieorder.notify import Notifier, build_notifier
from roomieorder.orchestrator import Orchestrator
from roomieorder.purchase import PurchaseResult, build_purchaser
from roomieorder.retention import prune_shots
from roomieorder.sheets import SheetsClient, build_sheets
from roomieorder.store import QueueRow, Store

_logger = logging.getLogger(__name__)

# How often the worker wakes to check for pending rows.
_WORKER_POLL_SECONDS = 5.0

# Outcomes that halt the worker until the operator clears them (PLAN §5).
# `needs_review` means an order may have been placed but couldn't be confirmed —
# halt so a human checks before anything re-orders the item.
_PAUSE_STATUSES = {"challenge", "blocked", "failed", "spend_capped", "needs_review"}


def _require_token(config: Config, provided: Optional[str]) -> None:
    """Reject the request when an intake token is configured and doesn't match.

    No-op when ``config.intake_token`` is empty (the loopback-only default), so
    local dev isn't burdened. Compared in constant time to avoid leaking the
    secret through response timing."""
    expected = config.intake_token
    if not expected:
        return
    if provided is None or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing intake token")


def _product_id(item: CatalogItem, provider: str) -> str:
    """The store-specific id of the source that handled the order, for Sheets.

    Costco item number or Amazon ASIN, or '' when the provider is unknown (e.g.
    an unavailable-everywhere result that never settled on a store)."""
    if provider == "costco" and item.costco is not None:
        return item.costco.item_number
    if provider == "amazon" and item.amazon is not None:
        return item.amazon.asin
    return ""


def _sheet_status(item: CatalogItem, status: str) -> str:
    """The label written to the Sheets `status` column for one attempt.

    A *placed* order for a personally-owned item (catalog ``owner`` set) reads
    "ordered for <owner>" so the shared log separates a roommate's personal buy
    from a shared-household order. Only the display label changes — the internal
    queue status stays ``placed``, so cooldown/spend/pause logic is untouched. A
    non-placed outcome (skipped, blocked, failed…) keeps its raw status for the
    operator regardless of owner."""
    if status == "placed" and item.owner:
        return f"ordered for {item.owner}"
    return status


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
    # Presentation-only, mirrors the catalog field — lets a dashboard group/sort.
    category: str = ""
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
        self.orchestrator = Orchestrator(config, self.store)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Per-item count of consecutive opt-in auto-retries (ROOMIEORDER_AUTO_RETRY),
        # bounding how many times a money-safe pre-cart failure is re-driven before
        # the worker gives up and pauses. In-memory is fine: the worker is
        # single-threaded and these are transient PDP-load failures, so a restart
        # resetting the count is harmless (recover_stale covers in-progress rows).
        self._transient_attempts: dict[str, int] = {}
        # Monotonic timestamps of the last heartbeat ping / session-freshness
        # probe, so the worker loop can fire each on its own interval without a
        # dedicated timer thread. 0.0 = never fired (the first loop iteration
        # triggers both, subject to their config being enabled).
        self._last_heartbeat = 0.0
        self._last_session_check = 0.0
        self._recover_orphans()

    def _recover_orphans(self) -> None:
        """Fail rows stranded ``in_progress`` by a crash, then pause for review.

        A row left mid-buy may have placed an order, so recovery never
        auto-retries it (see :meth:`store.Store.recover_stale`). When any are
        found we pause the worker and tell the operator, who clears them and
        resumes once they've confirmed whether the order went through.
        """
        recovered = self.store.recover_stale()
        if not recovered:
            return
        keys = ", ".join(r.item_key for r in recovered)
        reason = (
            f"⚠️ {len(recovered)} order(s) were interrupted by a restart "
            f"({keys}) — they may have been placed; review, then resume"
        )
        self.store.set_paused(True, reason)
        self.notifier.send(reason)
        _logger.warning("worker paused: %s", reason)

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
        # Sweep stale shots once at startup so a long-idle service still reclaims
        # disk even before the next order; each order re-prunes via _process.
        self._prune_shots()
        while not self._stop.is_set():
            # Liveness/health ticks run before the pause/empty `continue`s below,
            # so a paused-or-idle but *alive* loop still pings the heartbeat (that
            # IS the signal a wedged worker can't send) and still probes session
            # freshness on schedule.
            self._heartbeat_tick()
            self._session_check_tick()
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
                correlated(_logger, row=row.id, item=row.item_key).exception(
                    "worker failed processing row"
                )
                self.store.mark(row.id, "failed", notes="worker crashed")
                self.store.set_paused(True, f"worker crashed on row {row.id}")
            finally:
                self._prune_shots()

    def _prune_shots(self) -> None:
        """Best-effort shots retention sweep — never disrupts the worker loop."""
        try:
            prune_shots(self.config.shots_dir, self.config.shots_retention_days)
        except Exception:  # noqa: BLE001 — disk hygiene must never crash the loop
            _logger.exception("shots prune failed")

    def _heartbeat_tick(self, *, now: Optional[float] = None) -> None:
        """Ping the heartbeat URL when one is configured and the interval elapsed.

        No-op when ``heartbeat_url`` is empty. The ping itself is best-effort
        (see :func:`heartbeat.ping`); the timestamp advances on every attempt so
        a flapping monitor can't make the loop ping every iteration."""
        if not self.config.heartbeat_url:
            return
        now = time.monotonic() if now is None else now
        if now - self._last_heartbeat < self.config.heartbeat_interval_seconds:
            return
        self._last_heartbeat = now
        heartbeat.ping(self.config.heartbeat_url)

    def _session_check_tick(self, *, now: Optional[float] = None) -> None:
        """Probe each store profile's login and notify if it reloads logged out.

        No-op when ``session_check_hours`` is 0 (disabled). Runs on the worker
        thread between claims, so it never overlaps a buy. Relaunches each present
        profile read-only via the buy flow's ``verify_session``; a logged-out
        result pings the operator *before* a real order hits the sign-in wall.
        Per-provider best-effort, and the timestamp advances even on error so a
        broken probe can't hammer the stores."""
        hours = self.config.session_check_hours
        if hours <= 0:
            return
        now = time.monotonic() if now is None else now
        if now - self._last_session_check < hours * 3600.0:
            return
        # The probe opens a headed Chrome window, so once it's due we hold it
        # until the operator is away (gamemode off, idle, inside the window).
        # Leave _last_session_check unadvanced so the gate is re-evaluated every
        # worker poll and the probe fires within seconds of them stepping away.
        reason = activity.busy_gate(self.config)
        if reason is not None:
            _logger.debug("session probe deferred: %s", reason)
            return
        self._last_session_check = now
        profiles = {
            "costco": self.config.costco_profile_dir,
            "amazon": self.config.amazon_profile_dir,
        }
        for provider, profile_dir in profiles.items():
            if not profile_dir.exists():
                continue
            try:
                logged_in = build_purchaser(self.config, provider).verify_session()
            except Exception:  # noqa: BLE001 — a probe failure must not crash the loop
                _logger.exception("session probe failed for %s", provider)
                continue
            if not logged_in:
                store_name = provider.capitalize()
                self.notifier.send(
                    f"⚠️ {store_name} session looks logged out — run "
                    f"`roomieorder login --provider {provider}` before the next order"
                )
                _logger.warning("%s session probe: logged OUT", provider)

    # ─────────── per-row processing ───────────

    def _process(self, row: QueueRow) -> None:
        log = correlated(_logger, row=row.id, item=row.item_key)
        item = self.catalog.get(row.item_key)
        if item is None:
            self.store.mark(row.id, "failed", notes="item_key not in catalog")
            self.notifier.send(f"⚠️ unknown item_key {row.item_key!r} — skipped")
            return

        result = self.orchestrator.buy(row.item_key, item)
        self.store.mark(
            row.id,
            result.status,
            unit_price=result.unit_price,
            order_total=result.order_total,
            order_id=result.order_id,
            provider=result.provider,
            notes=result.message,
        )
        self._log_sheet(row, item, result)
        self.notifier.send(result.message, photo=result.screenshot)

        # A money-safe pre-cart failure (no cart interaction, no order placed) can
        # be re-driven rather than pausing for manual `retry` — opt-in and bounded.
        if result.status == "failed" and self._maybe_auto_retry(row, result):
            return
        # Any settled, non-auto-retried outcome clears the transient counter so
        # the next independent request for this item starts fresh.
        self._transient_attempts.pop(row.item_key, None)

        if result.status in _PAUSE_STATUSES:
            self.store.set_paused(True, result.message)
            log.warning("worker paused: %s", result.message)
        elif result.status == "placed":
            self._enforce_recorded_cap()

    def _maybe_auto_retry(self, row: QueueRow, result: PurchaseResult) -> bool:
        """Re-enqueue a money-safe transient failure instead of pausing.

        Only fires when ``ROOMIEORDER_AUTO_RETRY`` is on and the result is flagged
        ``retryable`` — set exclusively for failures strictly before any cart
        interaction (a PDP-load timeout, a one-off no_price), never once the cart
        was touched or an order submitted (see purchase.BasePurchaser.buy). Bounds
        re-drives per item to ``auto_retry_max`` so a persistently-failing item
        still ends up paused rather than looping. Returns True when it re-enqueued
        (caller skips the pause path)."""
        if not (self.config.auto_retry and result.retryable):
            return False
        count = self._transient_attempts.get(row.item_key, 0)
        if count >= self.config.auto_retry_max:
            return False
        self._transient_attempts[row.item_key] = count + 1
        new_id = self.store.enqueue(row.item_key, row.requester)
        correlated(_logger, row=row.id, item=row.item_key).info(
            "auto-retry: transient pre-cart failure (%d/%d) — re-enqueued as #%d",
            count + 1,
            self.config.auto_retry_max,
            new_id,
        )
        return True

    def _enforce_recorded_cap(self) -> None:
        """Backstop the spend cap against *recorded* totals after a placed order.

        The pre-buy guard checks ``live_price * qty``, but tax/shipping/fees only
        land once the order total is scraped, so a run of orders can creep over
        ``daily_cap`` in real money. Re-check the recorded trailing-24h spend and
        pause before the next buy when it's actually breached. Can't unwind the
        order just placed — it stops the *next* one."""
        spent = self.store.spend_since(24.0)
        if spent <= self.config.daily_cap:
            return
        reason = (
            f"⛔ recorded 24h spend ${spent:.2f} is over the ${self.config.daily_cap:.2f} "
            "cap once real totals landed — pausing before the next order"
        )
        self.store.set_paused(True, reason)
        self.notifier.send(reason)
        _logger.warning("worker paused: %s", reason)

    def reload_catalog(self) -> list[str]:
        """Re-read the catalog from disk so edits land without a service restart.

        The catalog is otherwise captured once at boot. Swaps ``self.catalog``
        only on a clean parse, so a malformed edit raises (surfaced as a 400 by
        the endpoint) and leaves the running catalog untouched. Returns the new
        item keys."""
        new_catalog = load_catalog(self.config.catalog_path)
        self.catalog = new_catalog
        _logger.info("catalog reloaded: %d items", len(new_catalog))
        return sorted(new_catalog)

    # ─────────── dashboard state ───────────

    def item_statuses(self, *, now: Optional[datetime] = None) -> dict[str, ItemStatus]:
        """Per-item cooldown snapshot for the HA dashboard (`GET /items`).

        Mirrors the cooldown arm of :func:`guards.check_intake` — keep the two
        in sync. One grouped query fetches every item's last-placed time, so the
        poll cost is flat in the catalog size. Only *placed* orders arm the
        cooldown, so nothing grays out while ``dry_run`` is on (PLAN §4).
        """
        current = now or datetime.now(timezone.utc)
        placed_at = self.store.last_placed_at_all()
        out: dict[str, ItemStatus] = {}
        for key in sorted(self.catalog):
            item = self.catalog[key]
            last_placed = placed_at.get(key)
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
                category=item.category,
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
                "provider": result.provider,
                "product_id": _product_id(item, result.provider),
                "qty": item.qty,
                "unit_price": result.unit_price,
                "order_total": result.order_total,
                "order_id": result.order_id,
                "status": _sheet_status(item, result.status),
                "requester": row.requester,
                "notes": result.message,
                "ref": row.id,
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
    def reorder(
        req: ReorderRequest,
        x_roomieorder_token: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        engine: Engine = app.state.engine
        _require_token(engine.config, x_roomieorder_token)
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

    @app.post("/reload")
    def reload(
        x_roomieorder_token: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        engine: Engine = app.state.engine
        _require_token(engine.config, x_roomieorder_token)
        try:
            items = engine.reload_catalog()
        except CatalogError as exc:
            # Bad edit — the running catalog is untouched; report and keep serving.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"reloaded": True, "items": items}

    return app
