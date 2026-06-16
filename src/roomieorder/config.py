"""Config schema and env loader.

roomieorder is configured by environment variables (see examples/env.example)
plus catalog.json. There is no TOML file: secrets live in the env / the
persistent browser profile, and the only structured data is the catalog, which
has its own loader in catalog.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class ConfigError(Exception):
    """Raised when a required env var is missing for the requested operation.

    Collects every missing name so the operator sees all of them at once
    rather than one failed restart per variable.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(f"missing required environment variables: {names}")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError([f"{name} (expected a number, got {raw!r})"]) from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError([f"{name} (expected an integer, got {raw!r})"]) from exc


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


class Config(BaseModel):
    """Resolved runtime configuration. Build via :func:`load_config`."""

    # Safety
    dry_run: bool = True
    daily_cap: float = Field(default=200.0, ge=0.0)
    debounce_seconds: int = Field(default=60, ge=0)

    # Intake service
    host: str = "127.0.0.1"
    port: int = Field(default=8723, ge=1, le=65535)

    # Paths
    catalog_path: Path = Path("catalog.json")
    db_path: Path = Path("data/state.sqlite")
    profile_dir: Path = Path("data/profile")
    shots_dir: Path = Path("data/shots")

    # Stores
    costco_domain: str = "costco.com"
    amazon_domain: str = "amazon.com"
    wayland: bool = False

    # Browser / anti-bot. Akamai fingerprints the *real* browser build, so the
    # buy flow must drive Google Chrome, not Playwright's bundled Chromium —
    # Chromium ships no proprietary H.264/AAC codecs and brands its Sec-CH-UA as
    # "Chromium", both of which Akamai reads as "not a real user". `chrome_path`
    # pins an exact binary (the NixOS deployment points it at the nixpkgs
    # google-chrome); when unset, `chrome_channel` asks Playwright to find a
    # system Chrome by channel. Empty channel + empty path falls back to the
    # bundled Chromium (the only build available on a bare dev checkout).
    chrome_path: str = ""
    chrome_channel: str = "chrome"

    # Google Sheets — logging is disabled when sheet_id is empty.
    google_service_account_json: str = ""
    sheet_id: str = ""
    sheet_tab: str = "Orders"

    # OpenClaw notifier
    openclaw_bin: str = "openclaw"
    openclaw_target: str = ""
    openclaw_channel: str = "telegram"

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.sheet_id and self.google_service_account_json)

    @property
    def notify_enabled(self) -> bool:
        return bool(self.openclaw_target)

    @property
    def costco_profile_dir(self) -> Path:
        """Browser profile dir for the Costco session (logged in by hand once).

        Each store gets its own profile so their cookies and anti-bot state stay
        isolated — Costco's Akamai and Amazon's checks key on different signals.
        """
        return self.profile_dir / "costco"

    @property
    def amazon_profile_dir(self) -> Path:
        """Browser profile dir for the Amazon session (the Costco fallback)."""
        return self.profile_dir / "amazon"

    def costco_product_url(self, item_number: str) -> str:
        """Fallback Costco product URL for an item number on the configured domain.

        Costco has no clean ``/dp/<id>`` form: a real product URL carries a
        slug (``.../kirkland-…product.<id>.html``). Prefer the source ``url``
        from the catalog, which has the slug; this slugless form is a last resort.
        """
        # TODO(costco): confirm the slugless .product.<id>.html form redirects.
        return f"https://www.{self.costco_domain}/.product.{item_number}.html"

    def amazon_product_url(self, asin: str) -> str:
        """Product URL for an Amazon ASIN on the configured domain."""
        return f"https://www.{self.amazon_domain}/dp/{asin}"


def load_config() -> Config:
    """Build a :class:`Config` from the process environment.

    Applies defaults for anything unset; raises :class:`ConfigError` only when
    a value is present but unparseable. Whether Sheets/notify are *required* is
    decided at use-site (see :meth:`Config.sheets_enabled` /
    :meth:`Config.notify_enabled`), not here, so the service can boot even
    before every integration is wired up.
    """
    return Config(
        dry_run=_env_bool("DRY_RUN", True),
        daily_cap=_env_float("ROOMIEORDER_DAILY_CAP", 200.0),
        debounce_seconds=_env_int("ROOMIEORDER_DEBOUNCE_SECONDS", 60),
        host=_env_str("ROOMIEORDER_HOST", "127.0.0.1"),
        port=_env_int("ROOMIEORDER_PORT", 8723),
        catalog_path=Path(_env_str("ROOMIEORDER_CATALOG", "catalog.json")),
        db_path=Path(_env_str("ROOMIEORDER_DB", "data/state.sqlite")),
        profile_dir=Path(_env_str("ROOMIEORDER_PROFILE_DIR", "data/profile")),
        shots_dir=Path(_env_str("ROOMIEORDER_SHOTS_DIR", "data/shots")),
        costco_domain=_env_str("ROOMIEORDER_COSTCO_DOMAIN", "costco.com"),
        amazon_domain=_env_str("ROOMIEORDER_AMAZON_DOMAIN", "amazon.com"),
        wayland=_env_bool("ROOMIEORDER_WAYLAND", False),
        chrome_path=_env_str("ROOMIEORDER_CHROME_PATH", ""),
        chrome_channel=_env_str("ROOMIEORDER_CHROME_CHANNEL", "chrome"),
        google_service_account_json=_env_str("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        sheet_id=_env_str("ROOMIEORDER_SHEET_ID", ""),
        sheet_tab=_env_str("ROOMIEORDER_SHEET_TAB", "Orders"),
        openclaw_bin=_env_str("OPENCLAW_BIN", "openclaw"),
        openclaw_target=_env_str("OPENCLAW_TARGET", ""),
        openclaw_channel=_env_str("OPENCLAW_CHANNEL", "telegram"),
    )
