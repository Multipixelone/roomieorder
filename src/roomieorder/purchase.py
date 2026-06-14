"""Playwright buy flow against a persistent, already-logged-in Chromium profile.

This is the brittle half (PLAN §1, §3.4). Everything here is written to fail
*loudly and safely*: resilient role/text selectors over brittle CSS, a hard
timeout per step, a screenshot on every failure, and explicit challenge
detection that halts rather than looping into a CAPTCHA.

The operator logs into Amazon by hand once into ``profile_dir``; with no 2FA
the session persists. Nothing here stores an Amazon credential — the login
lives entirely in the browser profile.

Run order inside :meth:`AmazonPurchaser.buy`:

1. goto /dp/<asin>
2. detect challenge
3. read live price → ``proceed_check(price)`` (price ceiling + spend cap)
4. Buy Now (fallback: add-to-cart → proceed to checkout)
5. on the review page: detect challenge
6. DRY_RUN → screenshot + stop; else click "Place your order"
7. scrape order number + total
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from roomieorder.catalog import CatalogItem
from roomieorder.config import Config
from roomieorder.guards import GuardResult
from roomieorder.store import Status

_logger = logging.getLogger(__name__)

# Per-step navigation/click timeout. Amazon is usually quick; a step that
# stalls past this is a redesign or a challenge, not slowness.
_STEP_TIMEOUT_MS = 20_000

# Markers that mean Amazon wants a human: CAPTCHA, OTP, "verify it's you".
# Matched case-insensitively against page text + URL.
_CHALLENGE_MARKERS = (
    "enter the characters",
    "type the characters",
    "type the letters",
    "solve this puzzle",
    "verify it's you",
    "verify your identity",
    "authentication required",
    "two-step verification",
    "enter the otp",
    "/ap/cvf/",
    "validatecaptcha",
    "/errors/validatecaptcha",
)

# Ordered, redundant locators. Each is tried in turn; the first that resolves
# wins. Role/text first (survives CSS churn), id/name as backstops.
_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "#corePrice_feature_div span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "span.a-price span.a-offscreen",
)
_BUY_NOW_SELECTORS = (
    "#buy-now-button",
    "input[name='submit.buy-now']",
    "#submit\\.buy-now",
)
_ADD_TO_CART_SELECTORS = (
    "#add-to-cart-button",
    "input[name='submit.add-to-cart']",
)
_PLACE_ORDER_SELECTORS = (
    "#placeYourOrder",
    "input[name='placeYourOrder1']",
    "#submitOrderButtonId input",
    "#bottomSubmitOrderButtonId input",
)
_ORDER_TOTAL_SELECTORS = (
    "#subtotals-marketplace-table .grand-total-price",
    "td.grand-total-price",
    "#od-subtotals .a-color-price",
)

# Order numbers look like 123-4567890-1234567.
_ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
# First number-ish run in a blob: digits with optional grouping/decimal
# separators, e.g. "24.99", "1,234.56", "11,99".
_PRICE_RE = re.compile(r"[0-9][0-9.,]*[0-9]|[0-9]")


@dataclass
class PurchaseResult:
    status: Status
    unit_price: Optional[float] = None
    order_total: Optional[float] = None
    order_id: Optional[str] = None
    message: str = ""
    screenshot: Optional[Path] = None


# proceed_check(live_price) -> GuardResult. Lets the worker run price-ceiling
# and spend-cap guards (which need the store) without pulling the store into
# this module.
ProceedCheck = Callable[[float], GuardResult]


def parse_price(text: str) -> Optional[float]:
    """Pull the first currency value out of a price blob, or None.

    Handles both US grouping (``$1,234.56``) and European decimal-comma
    (``€11,99``) by treating the *last* ``.``/``,`` as the decimal point and
    dropping every other separator as grouping. Amazon shows cents, so the
    trailing group is the fraction.
    """
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    num = m.group(0)
    last_sep = max(num.rfind("."), num.rfind(","))
    if last_sep == -1:
        whole = num
        frac = ""
    else:
        whole = re.sub(r"[.,]", "", num[:last_sep])
        frac = num[last_sep + 1 :]
    try:
        return float(f"{whole}.{frac}") if frac else float(whole)
    except ValueError:
        return None


def looks_like_challenge(text: str, url: str = "") -> bool:
    haystack = f"{text}\n{url}".lower()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


class AmazonPurchaser:
    """Drives one purchase per :meth:`buy` call, launching a fresh persistent
    context each time so no stale checkout state leaks between orders."""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.shots_dir.mkdir(parents=True, exist_ok=True)

    def _launch_args(self) -> list[str]:
        args: list[str] = []
        if self.config.wayland:
            # XWayland usually handles headed Chromium, but force native
            # Wayland when asked (PLAN §4 "Headed + display").
            args.append("--ozone-platform=wayland")
        return args

    def _shot_path(self, item_key: str, tag: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.config.shots_dir / f"{stamp}_{item_key}_{tag}.png"

    def buy(
        self,
        item_key: str,
        item: CatalogItem,
        proceed_check: ProceedCheck,
    ) -> PurchaseResult:
        """Execute (or dry-run) the buy. Always returns a PurchaseResult;
        the only exceptions that escape are programmer errors, not Amazon
        flakiness — those become a ``failed`` result with a screenshot."""
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        url = item.url or self.config.product_url(item.asin)

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                headless=False,
                args=self._launch_args(),
            )
            context.set_default_timeout(_STEP_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")

                if self._is_challenge(page):
                    return self._challenge(page, item_key, "product")

                # ── price + guards ──
                price = self._read_price(page)
                if price is None:
                    shot = self._screenshot(page, item_key, "no_price")
                    return PurchaseResult(
                        status="failed",
                        message=f"couldn't read a price for {item.title}",
                        screenshot=shot,
                    )

                decision = proceed_check(price)
                if not decision.ok:
                    shot = self._screenshot(page, item_key, "guard_block")
                    return PurchaseResult(
                        status=decision.status or "failed",
                        unit_price=price,
                        message=decision.reason,
                        screenshot=shot,
                    )

                # ── reach the review page ──
                if not self._start_checkout(page):
                    shot = self._screenshot(page, item_key, "no_buy_button")
                    return PurchaseResult(
                        status="failed",
                        unit_price=price,
                        message="couldn't find Buy Now / Add to Cart",
                        screenshot=shot,
                    )

                page.wait_for_load_state("domcontentloaded")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "checkout")

                # ── DRY_RUN stops here ──
                if self.config.dry_run:
                    shot = self._screenshot(page, item_key, "review")
                    return PurchaseResult(
                        status="dry_run",
                        unit_price=price,
                        message=f"[DRY] would order {item_key} at ${price:.2f}",
                        screenshot=shot,
                    )

                # ── place the order ──
                if not self._click_first(page, _PLACE_ORDER_SELECTORS):
                    shot = self._screenshot(page, item_key, "no_place_order")
                    return PurchaseResult(
                        status="failed",
                        unit_price=price,
                        message="reached checkout but couldn't find Place Your Order",
                        screenshot=shot,
                    )

                page.wait_for_load_state("domcontentloaded")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "confirm")

                order_id, total = self._scrape_confirmation(page)
                self._screenshot(page, item_key, "confirmation")
                return PurchaseResult(
                    status="placed",
                    unit_price=price,
                    order_total=total,
                    order_id=order_id,
                    message=(
                        f"ordered {item.title} — ${(total or price):.2f}"
                        + (f" — #{order_id}" if order_id else "")
                    ),
                )

            except PWTimeout as exc:
                shot = self._screenshot(page, item_key, "timeout")
                return PurchaseResult(
                    status="failed",
                    message=f"timed out: {exc}".split("\n")[0],
                    screenshot=shot,
                )
            except Exception as exc:  # noqa: BLE001 — convert any flake to a safe result
                _logger.exception("buy flow crashed for %s", item_key)
                shot = self._screenshot(page, item_key, "crash")
                return PurchaseResult(
                    status="failed",
                    message=f"buy flow error: {exc}".split("\n")[0],
                    screenshot=shot,
                )
            finally:
                context.close()

    # ─────────── page helpers ───────────

    def _read_price(self, page: object) -> Optional[float]:
        for sel in _PRICE_SELECTORS:
            try:
                loc = page.locator(sel).first  # type: ignore[attr-defined]
                if loc.count() == 0:
                    continue
                text = loc.inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 — selector miss; try the next
                continue
            price = parse_price(text)
            if price is not None:
                return price
        return None

    def _start_checkout(self, page: object) -> bool:
        """Click Buy Now; fall back to Add to Cart → Proceed to checkout."""
        if self._click_first(page, _BUY_NOW_SELECTORS):
            return True
        if not self._click_first(page, _ADD_TO_CART_SELECTORS):
            return False
        # Cart interstitial → checkout.
        page.wait_for_load_state("domcontentloaded")  # type: ignore[attr-defined]
        for sel in ("#sc-buy-box-ptc-button", "input[name='proceedToRetailCheckout']"):
            if self._click_first(page, (sel,)):
                return True
        # Some flows expose a role-named link instead.
        try:
            page.get_by_role("link", name=re.compile("proceed to checkout", re.I)).first.click(  # type: ignore[attr-defined]
                timeout=5_000
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def _click_first(self, page: object, selectors: tuple[str, ...]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first  # type: ignore[attr-defined]
                if loc.count() == 0:
                    continue
                loc.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        return False

    def _scrape_confirmation(self, page: object) -> tuple[Optional[str], Optional[float]]:
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5_000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        order_id = None
        m = _ORDER_ID_RE.search(body)
        if m:
            order_id = m.group(0)
        total = None
        for sel in _ORDER_TOTAL_SELECTORS:
            try:
                loc = page.locator(sel).first  # type: ignore[attr-defined]
                if loc.count() == 0:
                    continue
                total = parse_price(loc.inner_text(timeout=2_000))
                if total is not None:
                    break
            except Exception:  # noqa: BLE001
                continue
        return order_id, total

    def _is_challenge(self, page: object) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3_000)  # type: ignore[attr-defined]
            url = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        return looks_like_challenge(text, url)

    def _challenge(self, page: object, item_key: str, where: str) -> PurchaseResult:
        shot = self._screenshot(page, item_key, f"challenge_{where}")
        return PurchaseResult(
            status="challenge",
            message=f"⚠️ Amazon challenge on the {where} page — worker paused, clear it manually",
            screenshot=shot,
        )

    def _screenshot(self, page: object, item_key: str, tag: str) -> Optional[Path]:
        path = self._shot_path(item_key, tag)
        try:
            page.screenshot(path=str(path), full_page=False)  # type: ignore[attr-defined]
            return path
        except Exception as exc:  # noqa: BLE001
            _logger.warning("screenshot failed (%s): %s", tag, exc)
            return None
