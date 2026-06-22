"""Heartbeat — a dead-man's-switch ping for external liveness monitoring.

systemd ``Restart=on-failure`` only catches a full process exit, so a worker
thread that *wedges* (a hung browser, a deadlock) leaves the process up and the
queue silently undrained. To catch that, the worker pings a configurable URL on
a timer (see :meth:`main.Engine._heartbeat_tick`); the URL is the only coupling,
so it works with any push-style monitor — hosted Healthchecks.io or a
self-hosted open-source Healthchecks instance, Uptime Kuma push, etc. — which
alerts when the pings stop. Best-effort and side-effect-free: a ping failure is
logged and ignored, never raised, so monitoring can never take the worker down.
"""

from __future__ import annotations

import logging

import httpx

_logger = logging.getLogger(__name__)

# Short cap: a wedged/slow monitor must not stall the worker loop waiting on it.
_PING_TIMEOUT_SECONDS = 10.0


def ping(url: str) -> bool:
    """GET ``url`` as a liveness ping; return True on a 2xx/3xx, else False.

    A no-op returning False when ``url`` is empty (heartbeat disabled). Never
    raises — a network error / timeout / bad status is logged and swallowed, so
    the caller can fire-and-forget."""
    if not url:
        return False
    try:
        resp = httpx.get(url, timeout=_PING_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — monitoring must never crash the worker
        _logger.warning("heartbeat ping failed: %s", exc)
        return False
