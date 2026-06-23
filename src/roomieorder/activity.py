"""Activity gate for the session-freshness probe.

The session probe (:meth:`main.Engine._session_check_tick`) launches a *headed*
Chrome window on the live desktop, so firing it on a blind timer steals focus
mid-game or mid-work. :func:`busy_gate` lets the worker hold the probe until the
operator is away: it returns a short human reason to defer, or ``None`` to fire
now. Three OR-combined signals, first tripped wins:

* **time window** — only probe inside a local-time window;
* **gamemode** — skip while a game runs under gamemode;
* **idle** — require N minutes of no input before probing.

Every external probe is best-effort: a missing binary, non-zero exit, or hung
command is caught and never crashes or wedges the worker loop. The window parse
is the one strict piece — a malformed value is operator error and raises
:class:`~roomieorder.config.ConfigError`, consistent with config.py treating a
present-but-unparseable value as a hard stop.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, time as dtime
from typing import Optional

from roomieorder.config import Config, ConfigError

_logger = logging.getLogger(__name__)

# Hard cap so a wedged idle/gamemode probe can't hang the worker between claims.
_PROBE_TIMEOUT_SECONDS = 5.0


def busy_gate(config: Config) -> Optional[str]:
    """Return a reason to defer the session probe, or ``None`` to fire now.

    OR-combined: the first tripped signal's reason is returned. Order is window
    → gamemode → idle (cheapest / most decisive first). Pure apart from the two
    subprocess probes, which are each wrapped so a broken probe never raises.
    """
    window = config.session_check_window.strip()
    if window and not _in_window(datetime.now(), window):
        return f"outside window {window}"

    if config.session_check_skip_gamemode and _gamemode_active(
        config.session_check_gamemode_cmd
    ):
        return "gamemode active"

    threshold = config.session_check_idle_minutes
    if threshold > 0:
        required = threshold * 60.0
        idle = _idle_seconds(config.session_check_idle_cmd)
        if idle is None:
            # Threshold set but we can't read idle time — refuse to prove the
            # operator is away rather than risk popping a window in their face.
            return f"idle unknown (need {required:.0f}s idle)"
        if idle < required:
            return f"user active (idle {idle:.0f}s < {required:.0f}s)"

    return None


def _in_window(now: datetime, window: str) -> bool:
    """True when ``now``'s local time falls in ``"HH:MM-HH:MM"`` (wrap allowed).

    A window whose end is at or before its start wraps past midnight, so
    ``"22:00-06:00"`` matches 23:00 and 02:00 but not 12:00. Raises
    :class:`ConfigError` on a malformed value.
    """
    start, end = _parse_window(window)
    current = now.time()
    if start <= end:
        return start <= current < end
    # Wrap past midnight: inside if at/after start OR before end.
    return current >= start or current < end


def _parse_window(window: str) -> tuple[dtime, dtime]:
    parts = window.split("-")
    if len(parts) != 2:
        raise ConfigError(
            [f"ROOMIEORDER_SESSION_CHECK_WINDOW (expected 'HH:MM-HH:MM', got {window!r})"]
        )
    try:
        return _parse_hhmm(parts[0]), _parse_hhmm(parts[1])
    except ValueError as exc:
        raise ConfigError(
            [f"ROOMIEORDER_SESSION_CHECK_WINDOW (expected 'HH:MM-HH:MM', got {window!r})"]
        ) from exc


def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.strip().split(":")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(value)
    return dtime(hour, minute)


def _gamemode_active(command: str) -> bool:
    """True when ``command``'s stdout reports gamemode active. Never raises.

    Default command is ``gamemoded -s``, which prints "gamemode is active" /
    "gamemode is inactive". A missing binary, non-zero exit, or timeout is read
    as "not gaming" — we never block the probe on a broken detector.
    """
    out = _run(command)
    return out is not None and "is active" in out.lower()


def _idle_seconds(command: str) -> Optional[float]:
    """Idle seconds from ``command``'s stdout, or ``None`` if unavailable.

    The command must print a single number of idle seconds (Wayland/Hyprland
    has no universal idle query, so the operator wires this). Unset, failed, or
    unparseable output all return ``None`` — busy_gate treats that as "can't
    prove away" and defers.
    """
    if not command.strip():
        return None
    out = _run(command)
    if out is None:
        return None
    try:
        return float(out.strip().split()[0])
    except (ValueError, IndexError):
        _logger.debug("idle command gave unparseable output: %r", out[:80])
        return None


def _run(command: str) -> Optional[str]:
    """Run ``command`` via the shell, returning stdout, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602 — operator-supplied probe, not external input
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("activity probe %r failed: %s", command, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout
