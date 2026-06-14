from __future__ import annotations

from pathlib import Path

import pytest

from roomieorder.config import Config
from roomieorder.notify import NullNotifier, OpenClawNotifier, build_notifier


def test_build_notifier_null_without_target() -> None:
    assert isinstance(build_notifier(Config()), NullNotifier)


def test_build_notifier_openclaw_with_target() -> None:
    n = build_notifier(Config(openclaw_target="-123"))
    assert isinstance(n, OpenClawNotifier)


def test_null_notifier_always_ok() -> None:
    assert NullNotifier().send("hi") is True


def test_openclaw_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr("roomieorder.notify.subprocess.run", fake_run)
    n = OpenClawNotifier("openclaw", "-555", "telegram")
    assert n.send("hello", photo=Path("/tmp/shot.png")) is True

    cmd = calls[0]
    assert cmd[:3] == ["openclaw", "message", "send"]
    assert "--target" in cmd and "-555" in cmd
    assert "--message" in cmd and "hello" in cmd
    assert "--photo" in cmd and "/tmp/shot.png" in cmd


def test_openclaw_nonzero_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr("roomieorder.notify.subprocess.run", lambda *a, **k: _Result())
    assert OpenClawNotifier("openclaw", "-1").send("x") is False


def test_openclaw_missing_binary_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_fnf(*a, **k):  # type: ignore[no-untyped-def]
        raise FileNotFoundError

    monkeypatch.setattr("roomieorder.notify.subprocess.run", raise_fnf)
    assert OpenClawNotifier("nope", "-1").send("x") is False
