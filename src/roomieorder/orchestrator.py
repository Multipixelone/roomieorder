"""Costco-first ordering with an Amazon fallback.

Each catalog item can declare a ``costco`` source and/or an ``amazon`` source
(at least one). :class:`Orchestrator` tries them in order — Costco first, Amazon
second — and falls back to the next store when the current one can't fulfil the
order: sold out, not carried / not found, or over that store's price ceiling.

A successful fallback returns ``placed`` (with ``provider="amazon"``), so the
worker's pause/cooldown/spend logic — which keys off ``placed`` — is untouched.
Statuses that mean "a human must intervene" (``challenge``, ``blocked``,
``failed``, ``spend_capped``) are terminal and never trigger a fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Union

from roomieorder.catalog import AmazonSource, CatalogItem, CostcoSource
from roomieorder.config import Config
from roomieorder.guards import GuardResult, check_price_ceiling, check_spend_cap
from roomieorder.purchase import (
    AmazonPurchaser,
    BasePurchaser,
    CostcoPurchaser,
    FlowTracer,
    ProceedCheck,
    PurchaseResult,
    new_run_id,
)
from roomieorder.store import Store

# Either store's source shape. The chain pairs each with its own purchaser, so
# the buy/proceed-check sites accept the union and read the common fields.
Source = Union[CostcoSource, AmazonSource]

_logger = logging.getLogger(__name__)

# Outcomes from a non-final store that mean "try the next one". Everything else
# (placed, dry_run, challenge, blocked, failed, spend_capped) is terminal.
_FALLBACK_STATUSES = {"unavailable", "price_blocked"}


class Orchestrator:
    """Runs an item's buy across its declared stores, Costco before Amazon."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    def _providers(self, item: CatalogItem) -> list[tuple[str, Source, BasePurchaser[Any]]]:
        """The (name, source, purchaser) chain for ``item``, in fallback order.

        Costco first, Amazon second; only declared sources appear. Each store
        gets its own browser profile so their sessions/anti-bot state don't mix.
        """
        chain: list[tuple[str, Source, BasePurchaser[Any]]] = []
        if item.costco is not None:
            chain.append(
                (
                    "costco",
                    item.costco,
                    CostcoPurchaser(
                        self.config,
                        profile_dir=self.config.costco_profile_dir,
                        domain=self.config.costco_domain,
                    ),
                )
            )
        if item.amazon is not None:
            chain.append(
                (
                    "amazon",
                    item.amazon,
                    AmazonPurchaser(
                        self.config,
                        profile_dir=self.config.amazon_profile_dir,
                        domain=self.config.amazon_domain,
                    ),
                )
            )
        return chain

    def _proceed_check(self, item: CatalogItem, source: Source) -> ProceedCheck:
        """Per-store guard: the source's own price ceiling, then the global cap."""
        ceiling = source.price_ceiling

        def check(live_price: float) -> GuardResult:
            ceiling_result = check_price_ceiling(item.title, ceiling, live_price)
            if not ceiling_result.ok:
                return ceiling_result
            return check_spend_cap(self.store, self.config, live_price * item.qty)

        return check

    def buy(self, item_key: str, item: CatalogItem) -> PurchaseResult:
        """Try each declared store in turn; return the first terminal result.

        A ``unavailable`` / ``price_blocked`` from a non-final store falls
        through to the next; on the last store it's returned as-is. The returned
        result's ``message`` notes the fallback chain so the operator can see
        Costco was tried first.
        """
        chain = self._providers(item)
        last_miss: PurchaseResult | None = None

        for idx, (name, source, purchaser) in enumerate(chain):
            is_last = idx == len(chain) - 1
            # Opt-in full-flow trace on live orders (config.trace_orders, default
            # off). Each store-leg gets its own run_id so a Costco→Amazon fallback
            # keeps its two traces apart. Off → the no-op default keeps the buy
            # byte-for-byte unchanged.
            tracer = (
                FlowTracer(purchaser, item_key, run_id=new_run_id())
                if self.config.trace_orders
                else None
            )
            kwargs = {"tracer": tracer} if tracer is not None else {}
            result = purchaser.buy(
                item_key, item, source, self._proceed_check(item, source), **kwargs
            )
            result.provider = name

            if result.status in _FALLBACK_STATUSES and not is_last:
                _logger.info(
                    "%s %s for %s — falling back to the next store",
                    name,
                    result.status,
                    item_key,
                )
                last_miss = result
                continue

            if last_miss is not None:
                result.message = f"{last_miss.message}; fell back → {result.message}"
            return result

        # Unreachable: the catalog validator guarantees at least one source.
        return last_miss or PurchaseResult(
            status="failed", message=f"no source declared for {item_key}"
        )
