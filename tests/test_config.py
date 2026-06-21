from __future__ import annotations

from pathlib import Path

import pytest

from roomieorder.config import Config, ConfigError, load_config


def test_defaults_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in list(__import__("os").environ):
        if var.startswith(("ROOMIEORDER_", "OPENCLAW_")) or var in {"DRY_RUN", "GOOGLE_SERVICE_ACCOUNT_JSON"}:
            monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.dry_run is True
    assert cfg.port == 8723
    assert cfg.costco_domain == "costco.com"
    assert cfg.amazon_domain == "amazon.com"
    assert cfg.costco_store_id == "10301"
    assert cfg.costco_catalog_id == "10701"
    assert cfg.auto_retry is False
    assert cfg.auto_retry_max == 1
    assert cfg.sheets_enabled is False
    assert cfg.notify_enabled is False


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ROOMIEORDER_PORT", "9000")
    monkeypatch.setenv("ROOMIEORDER_DAILY_CAP", "50.5")
    monkeypatch.setenv("OPENCLAW_TARGET", "-100200300")
    monkeypatch.setenv("ROOMIEORDER_COSTCO_STORE_ID", "847")
    monkeypatch.setenv("ROOMIEORDER_COSTCO_CATALOG_ID", "11005")
    monkeypatch.setenv("ROOMIEORDER_AUTO_RETRY", "true")
    monkeypatch.setenv("ROOMIEORDER_AUTO_RETRY_MAX", "3")
    cfg = load_config()
    assert cfg.dry_run is False
    assert cfg.port == 9000
    assert cfg.daily_cap == 50.5
    assert cfg.notify_enabled is True
    assert cfg.costco_store_id == "847"
    assert cfg.costco_catalog_id == "11005"
    assert cfg.auto_retry is True
    assert cfg.auto_retry_max == 3


def test_bad_number_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOMIEORDER_DAILY_CAP", "not-a-number")
    with pytest.raises(ConfigError):
        load_config()


def test_product_url() -> None:
    cfg = Config(costco_domain="costco.ca", amazon_domain="amazon.ca")
    assert cfg.costco_product_url("1640526") == "https://www.costco.ca/.product.1640526.html"
    assert cfg.amazon_product_url("B07YYYYYYY") == "https://www.amazon.ca/dp/B07YYYYYYY"


def test_per_provider_profile_dirs() -> None:
    cfg = Config(profile_dir=Path("data/profile"))
    assert cfg.costco_profile_dir == Path("data/profile/costco")
    assert cfg.amazon_profile_dir == Path("data/profile/amazon")


def test_sheets_enabled_needs_both() -> None:
    assert Config(sheet_id="x").sheets_enabled is False
    assert Config(google_service_account_json="k.json").sheets_enabled is False
    assert Config(sheet_id="x", google_service_account_json="k.json").sheets_enabled is True
