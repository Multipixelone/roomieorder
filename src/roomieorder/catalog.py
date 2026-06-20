"""The item catalog — the "kind I like" for each staple, keyed by item_key.

Populated once by the operator (see catalog.json). The HA dashboard sends only
an item_key; everything store-specific (the Costco item number, the Amazon
ASIN, ceilings, cooldown) is resolved here, so the dashboard never carries a
price or a product id.

Each item can declare two sources — a ``costco`` block and an ``amazon`` block.
The buy flow always tries Costco first and falls back to Amazon when Costco
can't fulfil it (sold out, not carried, or over its ceiling); at least one of
the two must be present. Pricing (``expected_price`` / ``price_ceiling``) lives
on each source, since the same staple is usually priced differently per store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


class CatalogError(Exception):
    """Raised when the catalog file is missing, malformed, or invalid."""


def _ceiling_above_expected(v: float, info: ValidationInfo) -> float:
    expected = info.data.get("expected_price")
    if expected is not None and v < expected:
        raise ValueError(f"price_ceiling ({v}) must be >= expected_price ({expected})")
    return v


class CostcoSource(BaseModel):
    """Costco purchasing details for an item."""

    item_number: str
    url: str = ""
    expected_price: float = Field(ge=0.0)
    # Abort + fall back to Amazon if the live Costco price exceeds this — guards
    # against a spike or a hijacked listing. Must be >= expected_price.
    price_ceiling: float = Field(ge=0.0)

    @field_validator("item_number")
    @classmethod
    def _validate_item_number(cls, v: str) -> str:
        v = v.strip()
        # Costco item numbers are numeric and variable length (the digits in a
        # product URL's `.product.<id>.html`, or the shelf "item #").
        if not v or not v.isdigit():
            raise ValueError(f"item_number must be numeric, got {v!r}")
        return v

    _ceiling = field_validator("price_ceiling")(_ceiling_above_expected)


class AmazonSource(BaseModel):
    """Amazon purchasing details for an item — the Costco fallback."""

    asin: str
    url: str = ""
    expected_price: float = Field(ge=0.0)
    # Abort if the live Amazon price exceeds this. On the fallback path Amazon is
    # the last resort, so an over-ceiling here is terminal. Must be >= expected.
    price_ceiling: float = Field(ge=0.0)

    @field_validator("asin")
    @classmethod
    def _validate_asin(cls, v: str) -> str:
        v = v.strip().upper()
        # Amazon ASINs are 10-character alphanumeric ids (the `/dp/<asin>` tail).
        if len(v) != 10 or not v.isalnum():
            raise ValueError(f"asin must be 10 alphanumeric characters, got {v!r}")
        return v

    _ceiling = field_validator("price_ceiling")(_ceiling_above_expected)


class CatalogItem(BaseModel):
    title: str
    qty: int = Field(default=1, ge=1, le=10)
    # Presentation-only, consumed by the Nix `lib.haButtons` generator to build
    # the Home Assistant dashboard button. Ignored by the buy flow.
    button: str = ""  # short label for the HA button (falls back to title)
    icon: str = ""  # mdi icon for the HA button (falls back to a default)
    category: str = ""  # groups/sorts items on the generated HA dashboard
    # Personal owner of this item. When set, the order is still placed for real,
    # but the Sheets `status` column reads "ordered for <owner>" instead of
    # "placed" so the shared log distinguishes one roommate's personal buy from a
    # shared-household order. Purely a display label — the internal queue status
    # stays `placed`, so cooldown/spend/pause logic is unaffected.
    owner: str = ""
    # Block re-order inside this many days of the last placed order.
    cooldown_days: int = Field(default=0, ge=0, le=365)
    # The two sources. Costco is tried first; Amazon is the fallback. At least
    # one must be present (validated below).
    costco: Optional[CostcoSource] = None
    amazon: Optional[AmazonSource] = None

    @model_validator(mode="after")
    def _at_least_one_source(self) -> "CatalogItem":
        if self.costco is None and self.amazon is None:
            raise ValueError("item must declare at least one of 'costco' or 'amazon'")
        return self


Catalog = dict[str, CatalogItem]


def load_catalog(path: Path) -> Catalog:
    """Load and validate the catalog JSON into ``{item_key: CatalogItem}``.

    Raises :class:`CatalogError` (never a bare ValidationError) so callers —
    the intake endpoint and the CLI — get one consistent failure type.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise CatalogError("catalog root must be a JSON object of item_key → item")

    catalog: Catalog = {}
    for key, value in raw.items():
        try:
            catalog[key] = CatalogItem.model_validate(value)
        except Exception as exc:  # pydantic ValidationError or TypeError
            raise CatalogError(f"item {key!r} is invalid: {exc}") from exc
    return catalog
