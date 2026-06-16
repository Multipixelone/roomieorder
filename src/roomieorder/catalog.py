"""The item catalog — the "kind I like" for each staple, keyed by item_key.

Populated once by the operator (see catalog.json). The HA dashboard sends only
an item_key; everything Costco-specific (item_number, ceiling, cooldown) is
resolved here, so the dashboard never carries a price or a product id.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationInfo, field_validator


class CatalogError(Exception):
    """Raised when the catalog file is missing, malformed, or invalid."""


class CatalogItem(BaseModel):
    title: str
    item_number: str
    url: str = ""
    qty: int = Field(default=1, ge=1, le=10)
    expected_price: float = Field(ge=0.0)
    # Presentation-only, consumed by the Nix `lib.haButtons` generator to build
    # the Home Assistant dashboard button. Ignored by the buy flow.
    button: str = ""  # short label for the HA button (falls back to title)
    icon: str = ""  # mdi icon for the HA button (falls back to a default)
    category: str = ""  # groups/sorts items on the generated HA dashboard
    # Abort + alert if the live price exceeds this — guards against a spike or
    # a hijacked listing. Must be >= expected_price (validated below).
    price_ceiling: float = Field(ge=0.0)
    # Block re-order inside this many days of the last placed order.
    cooldown_days: int = Field(default=0, ge=0, le=365)

    @field_validator("item_number")
    @classmethod
    def _validate_item_number(cls, v: str) -> str:
        v = v.strip()
        # Costco item numbers are numeric and variable length (the digits in a
        # product URL's `.product.<id>.html`, or the shelf "item #").
        if not v or not v.isdigit():
            raise ValueError(f"item_number must be numeric, got {v!r}")
        return v

    @field_validator("price_ceiling")
    @classmethod
    def _ceiling_above_expected(cls, v: float, info: ValidationInfo) -> float:
        expected = info.data.get("expected_price")
        if expected is not None and v < expected:
            raise ValueError(
                f"price_ceiling ({v}) must be >= expected_price ({expected})"
            )
        return v


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
