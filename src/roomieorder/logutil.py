"""Correlation logging — tag a buy's log lines so they grep together.

A single order produces log lines across the worker loop (``main.py``) and the
Playwright buy flow (``purchase.py``), plus screenshots and a Sheet row. Without
a shared token, tracing one buy through a busy journal is manual. This wraps a
stdlib logger in a :class:`logging.LoggerAdapter` that prefixes every record
with a short ``key=value`` correlation token — the same ``provider``/``item``
that already names the screenshot files (``{ts}_{provider}_{item}_{tag}.png``),
so logs ↔ shots ↔ Sheet rows line up under one grep.
"""

from __future__ import annotations

import logging
from typing import Any, MutableMapping


class _CorrelatedLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A LoggerAdapter that prefixes each message with its correlation token."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        corr = self.extra.get("corr") if self.extra else ""
        return (f"[{corr}] {msg}" if corr else msg), kwargs


def correlated(logger: logging.Logger, **fields: object) -> _CorrelatedLogger:
    """Wrap ``logger`` so every line is prefixed with a ``key=value`` token.

    Empty/None field values are dropped, so ``correlated(log, provider="costco",
    item="paper_towels")`` prefixes ``[provider=costco item=paper_towels]`` and
    ``correlated(log, row=7, item="dish_soap")`` prefixes ``[row=7 item=dish_soap]``.
    The adapter forwards every logging method (info/warning/exception/…) to the
    wrapped logger unchanged apart from the prefix.
    """
    corr = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    return _CorrelatedLogger(logger, {"corr": corr})
