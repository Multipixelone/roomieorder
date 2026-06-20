"""Shared fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from roomieorder.config import Config
from roomieorder.store import Store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Browser, Page

_DOM_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dom"

# Deliberately NOT the repo's catalog.json. This is a purpose-built fixture
# covering the two source-shapes the code must handle — a two-source item (Costco
# + Amazon fallback) and a Costco-only item (no fallback) — so the orchestrator's
# fallback chain is exercised both ways. The repo catalog.json is itself only a
# placeholder (the real ~25-item catalog ships in infra/nix-secrets), so there's
# no single source of truth to converge on; keep this minimal matrix intentional.
_CATALOG = {
    # Two sources: Costco first, Amazon fallback.
    "paper_towels": {
        "title": "Bounty Advanced 12 Family Rolls",
        "qty": 1,
        "cooldown_days": 10,
        "costco": {
            "item_number": "1640526",
            "url": "https://www.costco.com/bounty-advanced.product.1640526.html",
            "expected_price": 24.99,
            "price_ceiling": 32.00,
        },
        "amazon": {
            "asin": "B07YYYYYYY",
            "url": "https://www.amazon.com/dp/B07YYYYYYY",
            "expected_price": 23.99,
            "price_ceiling": 30.00,
        },
    },
    # Costco-only: no Amazon fallback declared.
    "dish_soap": {
        "title": "Dawn Ultra 2-Pack",
        "qty": 2,
        "cooldown_days": 0,
        "costco": {
            "item_number": "1308124",
            "expected_price": 11.99,
            "price_ceiling": 16.00,
        },
    },
}


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(_CATALOG))
    return p


@pytest.fixture
def config(tmp_path: Path, catalog_path: Path) -> Config:
    return Config(
        dry_run=True,
        daily_cap=100.0,
        debounce_seconds=60,
        catalog_path=catalog_path,
        db_path=tmp_path / "state.sqlite",
        profile_dir=tmp_path / "profile",
        shots_dir=tmp_path / "shots",
    )


@pytest.fixture
def store(config: Config) -> Iterator[Store]:
    s = Store(config.db_path)
    s.init_db()
    yield s
    s.close()


# ─────────── browser-backed DOM-fixture harness (see tests/test_dom_fixtures.py) ───────────


@pytest.fixture(scope="session")
def browser() -> Iterator["Browser"]:
    """A headless Chromium for the `@pytest.mark.browser` DOM-fixture tests.

    The nix dev/CI shell ships the browser (flake.nix → playwright-driver.browsers);
    a bare `pip install` checkout has none, so a launch failure `skip`s rather
    than errors — the pure-helper suite still runs anywhere. The `--no-sandbox`
    / `--disable-dev-shm-usage` flags are CI hygiene for a contentless test
    browser that never touches a store; they are unrelated to the runtime stealth
    launch in `purchase._launch_context`, which deliberately avoids such flags.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - bare checkout
        pytest.skip(f"playwright not installed: {exc}")

    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # noqa: BLE001 - no browser binary in this shell
            pytest.skip(f"no headless Playwright browser available: {exc}")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(browser: "Browser") -> Iterator["Page"]:
    """A fresh page per test, so DOM state never leaks between fixtures."""
    p = browser.new_page()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def dom_fixture() -> Callable[[str], str]:
    """Read a named committed snapshot from tests/fixtures/dom/ (or `skip`)."""

    def _load(name: str) -> str:
        path = _DOM_FIXTURE_DIR / name
        if not path.exists():
            pytest.skip(f"DOM fixture not captured: {name}")
        return path.read_text(encoding="utf-8")

    return _load
