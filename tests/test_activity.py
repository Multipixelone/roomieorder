"""Unit tests for the session-probe activity gate (roomieorder.activity).

No live store, no real display: the gamemode/idle probes are monkeypatched at
the subprocess boundary so the gate's logic is exercised hermetically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from roomieorder import activity
from roomieorder.config import Config, ConfigError


def _cfg(**overrides: Any) -> Config:
    """A Config with the gate fully disabled by default; override per test."""
    base: dict[str, Any] = {
        "session_check_window": "",
        "session_check_skip_gamemode": False,
        "session_check_idle_minutes": 0.0,
        "session_check_idle_cmd": "",
    }
    base.update(overrides)
    return Config(**base)


# ─────────── time window ───────────


@pytest.mark.parametrize(
    "window,hour,inside",
    [
        ("03:00-08:00", 5, True),
        ("03:00-08:00", 8, False),  # end is exclusive
        ("03:00-08:00", 3, True),  # start is inclusive
        ("03:00-08:00", 12, False),
        ("22:00-06:00", 23, True),  # wrap past midnight
        ("22:00-06:00", 2, True),
        ("22:00-06:00", 12, False),
        ("22:00-06:00", 22, True),
        ("22:00-06:00", 6, False),
    ],
)
def test_in_window(window: str, hour: int, inside: bool) -> None:
    now = datetime(2026, 6, 23, hour, 30)
    assert activity._in_window(now, window) is inside


def test_window_defers_outside(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "datetime", _FrozenNow(datetime(2026, 6, 23, 12, 0)))
    reason = activity.busy_gate(_cfg(session_check_window="03:00-08:00"))
    assert reason == "outside window 03:00-08:00"


def test_window_clear_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "datetime", _FrozenNow(datetime(2026, 6, 23, 4, 0)))
    assert activity.busy_gate(_cfg(session_check_window="03:00-08:00")) is None


@pytest.mark.parametrize("bad", ["nonsense", "03:00", "25:00-08:00", "03:60-08:00", "a-b"])
def test_malformed_window_raises(bad: str) -> None:
    with pytest.raises(ConfigError):
        activity._parse_window(bad)


# ─────────── gamemode ───────────


def test_gamemode_active_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_run", lambda cmd: "gamemode is active\n")
    assert activity.busy_gate(_cfg(session_check_skip_gamemode=True)) == "gamemode active"


def test_gamemode_inactive_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_run", lambda cmd: "gamemode is inactive\n")
    assert activity.busy_gate(_cfg(session_check_skip_gamemode=True)) is None


def test_gamemode_probe_failure_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    # Missing binary / non-zero exit → _run returns None → treated as not gaming.
    monkeypatch.setattr(activity, "_run", lambda cmd: None)
    assert activity.busy_gate(_cfg(session_check_skip_gamemode=True)) is None


def test_gamemode_not_probed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(cmd: str) -> str:
        raise AssertionError("gamemode probed while skip_gamemode is False")

    monkeypatch.setattr(activity, "_run", _boom)
    assert activity.busy_gate(_cfg(session_check_skip_gamemode=False)) is None


# ─────────── idle ───────────


def test_idle_active_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_run", lambda cmd: "12\n")
    reason = activity.busy_gate(
        _cfg(session_check_idle_minutes=10.0, session_check_idle_cmd="idlecmd")
    )
    assert reason == "user active (idle 12s < 600s)"


def test_idle_enough_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_run", lambda cmd: "900\n")
    assert (
        activity.busy_gate(
            _cfg(session_check_idle_minutes=10.0, session_check_idle_cmd="idlecmd")
        )
        is None
    )


def test_idle_unknown_defers_when_cmd_unset() -> None:
    # Threshold set but no command to read idle time → can't prove away → defer.
    reason = activity.busy_gate(
        _cfg(session_check_idle_minutes=10.0, session_check_idle_cmd="")
    )
    assert reason == "idle unknown (need 600s idle)"


def test_idle_unparseable_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_run", lambda cmd: "not-a-number\n")
    reason = activity.busy_gate(
        _cfg(session_check_idle_minutes=10.0, session_check_idle_cmd="idlecmd")
    )
    assert reason == "idle unknown (need 600s idle)"


# ─────────── precedence / all-clear ───────────


def test_window_wins_over_gamemode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "datetime", _FrozenNow(datetime(2026, 6, 23, 12, 0)))
    monkeypatch.setattr(activity, "_run", lambda cmd: "gamemode is active")
    reason = activity.busy_gate(
        _cfg(session_check_window="03:00-08:00", session_check_skip_gamemode=True)
    )
    assert reason == "outside window 03:00-08:00"


def test_all_clear_returns_none() -> None:
    assert activity.busy_gate(_cfg()) is None


class _FrozenNow:
    """Stand-in for the ``datetime`` module exposing ``now()`` at a fixed time."""

    def __init__(self, when: datetime) -> None:
        self._when = when

    def now(self, tz: object = None) -> datetime:
        return self._when
