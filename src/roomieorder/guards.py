"""Safety rails (PLAN §5).

Two tiers:

* **Intake guards** run synchronously in ``/reorder`` before anything is
  enqueued — debounce, per-item cooldown, and worker-pause. They keep junk out
  of the queue and give the tapper instant feedback.
* **Execution guards** run in the worker once a live price is known — price
  ceiling and the rolling daily spend cap. These can only be checked against
  the real Costco page, so they live next to the buy.

All guards are pure functions of (store snapshot, config, catalog item); they
never mutate state, so they're trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from roomieorder.catalog import CatalogItem
from roomieorder.config import Config
from roomieorder.store import Status, Store


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a guard check.

    ``ok`` True means proceed. When False, ``status`` is the terminal status to
    record (and surface to Sheets) and ``reason`` is the human line for the
    notification. ``enqueue`` distinguishes "drop silently before the queue"
    (paused, debounce) from "record a skipped row" (cooldown) — the latter is
    worth logging to Sheets; the former is noise.
    """

    ok: bool
    status: Optional[Status] = None
    reason: str = ""
    enqueue: bool = False


_OK = GuardResult(ok=True)


def _now(reference: Optional[datetime] = None) -> datetime:
    return reference or datetime.now(timezone.utc)


def check_intake(
    store: Store,
    config: Config,
    item_key: str,
    item: CatalogItem,
    *,
    now: Optional[datetime] = None,
) -> GuardResult:
    """Decide whether a tap should enqueue a buy.

    Order matters: pause beats everything (we're halted for a reason), then
    debounce (cheap, catches fat-fingered double taps), then cooldown.
    """
    current = _now(now)

    if store.is_paused():
        reason = store.pause_reason() or "worker is paused"
        return GuardResult(ok=False, reason=f"⏸️ worker paused: {reason}", enqueue=False)

    last_req = store.last_request_at(item_key)
    if last_req is not None and config.debounce_seconds > 0:
        elapsed = (current - last_req).total_seconds()
        if elapsed < config.debounce_seconds:
            return GuardResult(
                ok=False,
                status="skipped_debounce",
                reason=f"double-tap ignored ({int(elapsed)}s < {config.debounce_seconds}s)",
                enqueue=False,
            )

    last_placed = store.last_placed_at(item_key)
    if last_placed is not None and item.cooldown_days > 0:
        unlock = last_placed + timedelta(days=item.cooldown_days)
        if current < unlock:
            days_ago = (current - last_placed).days
            return GuardResult(
                ok=False,
                status="skipped_cooldown",
                reason=(
                    f"⛔ skipped {item.title} — ordered {days_ago}d ago "
                    f"(cooldown {item.cooldown_days}d)"
                ),
                enqueue=True,
            )

    return _OK


def check_price_ceiling(title: str, price_ceiling: float, live_price: float) -> GuardResult:
    """Block the buy when the live price has blown past the source's ceiling.

    The ceiling is per-source (Costco and Amazon price the same staple
    differently), so the caller passes the active source's ``price_ceiling``.
    On the Costco leg a ``price_blocked`` result tells the orchestrator to fall
    back to Amazon; on the last provider it's terminal.
    """
    if live_price > price_ceiling:
        return GuardResult(
            ok=False,
            status="price_blocked",
            reason=(
                f"⛔ {title} is ${live_price:.2f}, over your "
                f"${price_ceiling:.2f} ceiling — not ordering"
            ),
        )
    return _OK


def check_spend_cap(
    store: Store, config: Config, prospective_total: float
) -> GuardResult:
    """Block (and signal a pause) when this order would breach the 24h cap."""
    spent = store.spend_since(24.0)
    if spent + prospective_total > config.daily_cap:
        return GuardResult(
            ok=False,
            status="spend_capped",
            reason=(
                f"⛔ daily spend cap hit: ${spent:.2f} spent + "
                f"${prospective_total:.2f} > ${config.daily_cap:.2f} cap"
            ),
        )
    return _OK
