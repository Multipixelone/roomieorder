from __future__ import annotations

import pytest

from roomieorder import heartbeat


class _OkResp:
    def raise_for_status(self) -> None:
        return None


def test_ping_empty_url_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(heartbeat.httpx, "get", lambda *a, **k: calls.append("hit"))
    assert heartbeat.ping("") is False
    assert calls == []  # no HTTP at all when disabled


def test_ping_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_get(url: str, timeout: float | None = None) -> _OkResp:
        seen["url"] = url
        seen["timeout"] = timeout
        return _OkResp()

    monkeypatch.setattr(heartbeat.httpx, "get", fake_get)
    assert heartbeat.ping("https://hc.example.com/ping/abc") is True
    assert seen["url"] == "https://hc.example.com/ping/abc"
    assert seen["timeout"] == heartbeat._PING_TIMEOUT_SECONDS


def test_ping_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float | None = None) -> _OkResp:
        raise RuntimeError("network down")

    monkeypatch.setattr(heartbeat.httpx, "get", boom)
    # Never raises — a failed ping is logged and reported as False.
    assert heartbeat.ping("https://hc.example.com/ping/abc") is False
