"""Playwright buy flow against a persistent, already-logged-in Chromium profile.

This is the brittle half (PLAN §1, §3.4). Everything here is written to fail
*loudly and safely*: resilient role/text selectors over brittle CSS, a hard
timeout per step, a screenshot on every failure, and explicit challenge
detection that halts rather than looping into a CAPTCHA.

The operator logs into Costco by hand once into ``profile_dir`` (see
:meth:`CostcoPurchaser.login`, exposed as ``roomieorder login``); the profile
then remembers the email+password. Costco doesn't treat a remembered profile as
authenticated until Sign In is clicked, so each buy re-establishes the session
with :meth:`CostcoPurchaser.ensure_logged_in` (one click of the prefilled form).
Nothing here stores a Costco credential — the login lives entirely in the
browser profile.

Run order inside :meth:`CostcoPurchaser.buy`:

1. goto the product page (item.url, with slug; falls back to product_url())
2. detect challenge (Costco fronts the site with Akamai); if logged out,
   sign in with the profile's cached credentials and reload (ensure_logged_in)
3. read live price → ``proceed_check(price)`` (price ceiling + spend cap)
4. add to cart → go to cart → checkout (Costco has no one-click Buy Now)
5. on the review page: detect challenge
6. DRY_RUN → screenshot + stop; else click "Place Order"
7. scrape order number + total

⚠️ Every selector, challenge marker, order-number regex, and the checkout step
order below is a best-guess against Costco's live DOM, which nobody here can
see, and Akamai's bot detection is far more aggressive than Amazon's. Each
DOM-dependent constant is flagged ``# TODO(costco): verify against live DOM``
and MUST be confirmed during bring-up (`roomieorder login` / `dry-run`).
"""

from __future__ import annotations

import json
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

# Per-step navigation/click timeout. A step that stalls past this is a redesign
# or a challenge, not slowness.
_STEP_TIMEOUT_MS = 20_000

# Markers that mean Costco/Akamai wants a human: bot wall, CAPTCHA, OTP.
# Matched case-insensitively against page text + URL.
# TODO(costco): verify against live DOM — Akamai's block page wording/paths.
_CHALLENGE_MARKERS = (
    "access denied",
    "pardon our interruption",
    "verify you are human",
    "are you a human",
    "/_sec/",
    "akamai",
    "reference #",
    "recaptcha",
    "enter the characters",
    "verify your identity",
)

# A logged-out session gets bounced to Costco's dedicated logon page when it
# tries to check out. That's not a challenge — it's an auth bounce. Detect it by
# the *logon URL/host*: the "Sign In / Register" link text lives in the header
# of every logged-out page, so body text matching it false-positives on a
# perfectly normal product page. Live auth state is decided by `is_logged_in`
# (account nav), not this — this only catches a mid-flow bounce to the wall.
# "sign in or register" (the logon page's own heading, with "or", not the
# header link's "/") is kept as a body backstop for when the URL isn't telling.
# TODO(costco): verify against live DOM — sign-in host/path and CTA wording.
_SIGNIN_MARKERS = (
    "/logon",
    "signin.costco.com",
    "sign in or register",
)
# The remembered-credential logon form's submit control. The email+password are
# cached in the profile, so submitting re-establishes the session without
# roomieorder ever holding a credential (PLAN §1). Role/text is tried first by
# `ensure_logged_in`; these ids/attrs are the backstop.
# TODO(costco): verify against live DOM — logon submit control.
_SIGNIN_SUBMIT_SELECTORS = (
    "[automation-id='signInButton']",
    "input#sign-in-btn",
    "button#sign-in-btn",
    "button[type='submit']",
)

# Ordered, redundant locators. Each is tried in turn; the first that resolves
# wins. Role/text first (survives CSS churn), id/name as backstops.
# TODO(costco): verify against live DOM — Costco product-price selectors.
_PRICE_SELECTORS = (
    "[automation-id='productPriceOutput']",
    ".product-price-amount",
    ".product-price .value",
    "span.value",
)
# Structured-data price sources, tried after the visible selectors miss. The
# `/p/-/<slug>/<id>` storefront is a server-rendered Next.js app that emits the
# product's price as standard page metadata, present in the initial HTML and
# unambiguously the *product's* price (not a recommendations-carousel
# neighbour's). These survive the CSS/automation-id churn that breaks
# _PRICE_SELECTORS, so they're the durable fallback when the visible price
# element can't be located. `<meta>` tags carry the value in their `content`
# attribute; JSON-LD blocks carry it under `offers.price` (see _price_from_jsonld).
_PRICE_META_SELECTORS = (
    "meta[property='product:price:amount']",
    "meta[property='og:price:amount']",
    "meta[itemprop='price']",
)
_JSONLD_SELECTOR = "script[type='application/ld+json']"
# Costco has no one-click Buy Now — only Add to Cart (see _start_checkout).
# TODO(costco): verify against live DOM — add-to-cart control.
_ADD_TO_CART_SELECTORS = (
    "[automation-id='addToCartButton']",
    "input[value='Add to Cart']",
    "button#add-to-cart-btn",
)
# TODO(costco): verify against live DOM — final place-order button.
_PLACE_ORDER_SELECTORS = (
    "[automation-id='placeOrderButton']",
    "input[value='Place Order']",
    "button#place-order",
)
# TODO(costco): verify against live DOM — order-confirmation grand-total.
_ORDER_TOTAL_SELECTORS = (
    "[automation-id='orderTotalOutput']",
    ".order-total .value",
    ".grand-total .value",
)

# Costco web order numbers — best guess at the format (purely digits, ~10).
# TODO(costco): verify against live DOM — confirm order-number format.
_ORDER_ID_RE = re.compile(r"\b\d{9,12}\b")
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
    dropping every other separator as grouping. Costco shows cents, so the
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


def _extract_offer_price(offers: object) -> Optional[float]:
    """First parseable ``price``/``lowPrice`` in a schema.org ``offers`` value.

    ``offers`` is either a single Offer/AggregateOffer object or a list of them;
    handle both. ``lowPrice`` covers AggregateOffer (a price range) so a ranged
    listing still yields its floor."""
    for offer in offers if isinstance(offers, list) else [offers]:
        if not isinstance(offer, dict):
            continue
        for key in ("price", "lowPrice"):
            if key in offer:
                price = parse_price(str(offer[key]))
                if price is not None:
                    return price
    return None


def _price_from_jsonld(raw: str) -> Optional[float]:
    """Pull an offer price out of a schema.org JSON-LD blob, or None.

    Costco's ``/p/`` pages embed product data as ``application/ld+json``. The
    price lives under an ``offers`` key, but the surrounding shape varies (a bare
    Product, a ``@graph`` list of nodes, nested arrays), so walk the whole parsed
    structure for the first ``offers`` rather than hard-coding one layout."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    def walk(node: object) -> Optional[float]:
        if isinstance(node, dict):
            if "offers" in node:
                price = _extract_offer_price(node["offers"])
                if price is not None:
                    return price
            for value in node.values():
                price = walk(value)
                if price is not None:
                    return price
        elif isinstance(node, list):
            for item in node:
                price = walk(item)
                if price is not None:
                    return price
        return None

    return walk(data)


def looks_like_challenge(text: str, url: str = "") -> bool:
    haystack = f"{text}\n{url}".lower()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


def looks_like_signin(text: str, url: str = "") -> bool:
    haystack = f"{text}\n{url}".lower()
    return any(marker in haystack for marker in _SIGNIN_MARKERS)


class CostcoPurchaser:
    """Drives one purchase per :meth:`buy` call, launching a fresh persistent
    context each time so no stale checkout state leaks between orders."""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.shots_dir.mkdir(parents=True, exist_ok=True)

    def _launch_args(self) -> list[str]:
        # The worker runs unattended from a systemd service, so its headed
        # Chromium window is never presented/foregrounded — it opens occluded
        # (which is also why no window appears for an HA-triggered buy). For a
        # backgrounded window Chromium throttles requestAnimationFrame and
        # background timers to a crawl, so Costco's JS never hydrates the
        # checkout body: the page stays a bare header bar, the Place Order
        # button never enters the DOM, and the buy fails on a blank page. These
        # flags make a headed-but-occluded window keep rendering at full speed,
        # so the checkout hydrates the same as it does for an
        # interactive `roomieorder dry-run` (visible window, no throttling).
        args: list[str] = [
            "--disable-backgrounding-occluded-windows",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            # Hide the Playwright automation fingerprint. Costco fronts both the
            # storefront and signin.costco.com with Akamai bot detection, which
            # reads navigator.webdriver / the --enable-automation switch and
            # silently kills the sign-in window mid-flow (it "buffers then
            # closes", leaving the profile logged out). Dropping the switch
            # (ignore_default_args, below) plus this flag makes the headed
            # window present as an ordinary Chrome so the hand login completes.
            "--disable-blink-features=AutomationControlled",
        ]
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
        the only exceptions that escape are programmer errors, not Costco
        flakiness — those become a ``failed`` result with a screenshot."""
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        url = item.url or self.config.product_url(item.item_number)

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                headless=False,
                args=self._launch_args(),
                ignore_default_args=["--enable-automation"],
            )
            context.set_default_timeout(_STEP_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            # Mark this tab active so Chromium un-throttles its renderer even
            # when the OS window is occluded (see _launch_args). Belt-and-braces
            # with the launch flags; best-effort, never fatal.
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001 — purely an optimisation
                pass
            try:
                page.goto(url, wait_until="domcontentloaded")

                if self._is_challenge(page):
                    return self._challenge(page, item_key, "product")
                # A logged-out profile renders the product page fine (the header
                # just shows "Sign In / Register"), so don't read it as a wall —
                # sign in with the cached credentials, then reload so the price
                # and checkout run against the authenticated session.
                if not self.ensure_logged_in(page):
                    return self._signin_required(page, item_key, "product")
                page.goto(url, wait_until="domcontentloaded")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "product")

                # ── price + guards ──
                # Same JS-hydration race as checkout: the price block can paint
                # after domcontentloaded, so wait for it before reading or a
                # live product reads as "no price".
                self._wait_for_any(page, _PRICE_SELECTORS)
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
                        message="couldn't drive add-to-cart → cart → checkout",
                        screenshot=shot,
                    )

                page.wait_for_load_state("domcontentloaded")
                if self._is_signin(page):
                    return self._signin_required(page, item_key, "checkout")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "checkout")

                # ── DRY_RUN stops here ──
                if self.config.dry_run:
                    self._settle(page)
                    shot = self._screenshot(page, item_key, "review")
                    return PurchaseResult(
                        status="dry_run",
                        unit_price=price,
                        message=f"[DRY] would order {item_key} at ${price:.2f}",
                        screenshot=shot,
                    )

                # ── place the order ──
                # Costco renders the checkout body via JS *after*
                # domcontentloaded, so the button isn't in the DOM the instant
                # we arrive. Settle first (the same wait the dry-run review shot
                # relies on — without it the body is a blank header), then let
                # _place_order wait on the button itself. (Don't pre-wait on
                # _PLACE_ORDER_SELECTORS here: the CSS ids may drift between
                # checkout variants, so the wait could burn the whole step
                # timeout and the checkout session blanks out before we click.)
                self._settle(page)
                if not self._place_order(page):
                    # A slow render, a sign-in wall, or a challenge can all land
                    # us here with no button. Re-check the latter two so the
                    # operator gets the right next step, not a misleading
                    # "couldn't find Place Your Order". Settle again so the
                    # diagnostic shot shows the real page, not a blank header.
                    if self._is_signin(page):
                        return self._signin_required(page, item_key, "checkout")
                    if self._is_challenge(page):
                        return self._challenge(page, item_key, "checkout")
                    self._settle(page)
                    shot = self._screenshot(page, item_key, "no_place_order")
                    return PurchaseResult(
                        status="failed",
                        unit_price=price,
                        message=(
                            "reached checkout but couldn't find Place Order "
                            f"({self._page_debug(page)})"
                        ),
                        screenshot=shot,
                    )

                page.wait_for_load_state("domcontentloaded")
                if self._is_signin(page):
                    return self._signin_required(page, item_key, "confirm")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "confirm")

                self._settle(page)
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

    def login(self, wait_for_operator: Callable[[object], None]) -> None:
        """Open the persistent profile headed so the operator can sign into
        Costco by hand. Cookies persist in ``profile_dir``; roomieorder never
        stores a Costco credential of its own (PLAN §1).

        ``wait_for_operator(page)`` is invoked once the Costco home page has
        loaded and must *block* until the human is done — the context (and the
        saved session with it) is torn down as soon as it returns.
        """
        from playwright.sync_api import sync_playwright

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                headless=False,
                args=self._launch_args(),
                ignore_default_args=["--enable-automation"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    f"https://www.{self.config.costco_domain}",
                    wait_until="domcontentloaded",
                )
                wait_for_operator(page)
            finally:
                context.close()

    # ─────────── page helpers ───────────

    def is_logged_in(self, page: object) -> bool:
        """Best-effort sign-in check: Costco's account nav reads
        'Sign In / Register' when logged out and 'Hello, <name>' otherwise.
        Returns False if the nav can't be read, so a True is trustworthy but a
        False may be a miss.
        TODO(costco): verify against live DOM — account-nav selector + wording.
        """
        for sel in ("[automation-id='accountMenuButton']", "#header-user", ".sign-in-link"):
            try:
                loc = page.locator(sel).first  # type: ignore[attr-defined]
                if loc.count() == 0:
                    continue
                text = loc.inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
            text = text.lower()
            return "sign in" not in text and "register" not in text
        return False

    def ensure_logged_in(self, page: object) -> bool:
        """Drive Costco's cached-credential sign-in if the profile is logged out.

        Costco remembers the email *and* password in the persistent profile, but
        does not treat a remembered profile as authenticated until the Sign In
        button is clicked — a session that "has the cookies" still starts logged
        out (see the costco-login-click-required note). So when `is_logged_in`
        reads logged-out, open the logon page via the header "Sign In / Register"
        link, let the form prefill, and click its Sign In submit. Stores no
        credential — the click only submits what the browser already filled.

        Returns True if logged in (already, or after the click), False if the
        click sequence didn't take (caller bails with the manual-login message).
        TODO(costco): verify against live DOM — sign-in link + logon submit."""
        if self.is_logged_in(page):
            return True
        # Reach the logon form: the header link first, the logon URL as backstop.
        if not self._click_by_role(page, ("link", "button"), "sign in"):
            try:
                page.goto(  # type: ignore[attr-defined]
                    f"https://www.{self.config.costco_domain}/logon",
                    wait_until="domcontentloaded",
                )
            except Exception:  # noqa: BLE001 — no way to reach the form; give up
                return False
        self._settle(page)
        # Submit the prefilled form. Prefer the form's own submit control over a
        # role/text match so we don't re-click the header "Sign In" link.
        if not self._click_first(page, _SIGNIN_SUBMIT_SELECTORS):
            self._click_by_role(page, ("button",), "sign in")
        self._settle(page)
        return self.is_logged_in(page)

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
        # The visible price element couldn't be located (every _PRICE_SELECTORS
        # is an unverified guess against a DOM nobody here can see). Fall back to
        # the page's structured data, which is far less brittle.
        return self._read_price_from_metadata(page)

    def _read_price_from_metadata(self, page: object) -> Optional[float]:
        """Read the product price from page metadata when the visible price
        element can't be located: OpenGraph/schema.org ``<meta>`` tags first,
        then JSON-LD ``offers``. Both are server-rendered into the initial HTML,
        so they're present even before the price block hydrates."""
        for sel in _PRICE_META_SELECTORS:
            try:
                loc = page.locator(sel).first  # type: ignore[attr-defined]
                if loc.count() == 0:
                    continue
                content = loc.get_attribute("content", timeout=2_000)
            except Exception:  # noqa: BLE001 — meta miss; try the next source
                continue
            price = parse_price(content or "")
            if price is not None:
                return price
        try:
            blocks = page.locator(_JSONLD_SELECTOR)  # type: ignore[attr-defined]
            count = blocks.count()
        except Exception:  # noqa: BLE001 — no JSON-LD to read
            return None
        for i in range(count):
            try:
                raw = blocks.nth(i).inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 — unreadable block; try the next
                continue
            price = _price_from_jsonld(raw)
            if price is not None:
                return price
        return None

    def _start_checkout(self, page: object) -> bool:
        """Add to cart → go to cart → checkout → (delivery/address) review.

        Costco has no one-click Buy Now: the flow is add-to-cart, then the cart,
        then a Checkout CTA, then possibly a delivery-method / address
        confirmation before the place-order button. Role/text first for
        resilience, CSS ids as a backstop.
        TODO(costco): verify against live DOM — every step below.
        """
        # ── add to cart ──
        # Costco often shows an "added to cart" flyout/interstitial after this.
        if not self._click_by_role(page, ("button",), "add to cart") and not self._click_first(
            page, _ADD_TO_CART_SELECTORS
        ):
            return False
        page.wait_for_load_state("domcontentloaded")  # type: ignore[attr-defined]
        self._settle(page)

        # ── go to cart ──
        # Prefer the flyout's Checkout CTA if present; otherwise navigate to the
        # cart page directly and check out from there.
        if not self._click_by_role(page, ("button", "link"), "checkout"):
            # TODO(costco): verify against live DOM — cart URL.
            page.goto(  # type: ignore[attr-defined]
                f"https://www.{self.config.costco_domain}/CheckoutCartView",
                wait_until="domcontentloaded",
            )
            self._settle(page)
            if not self._click_by_role(page, ("button", "link"), "checkout"):
                return False

        # ── delivery / address confirmation → review ──
        # A delivery-method / address step may sit before the place-order
        # button. Settle, then click a "continue to review/payment" CTA if one
        # is present; if not, we're already on the review page.
        page.wait_for_load_state("domcontentloaded")  # type: ignore[attr-defined]
        self._settle(page)
        # TODO(costco): verify against live DOM — does delivery need a click?
        self._click_by_role(page, ("button", "link"), "continue")
        return True

    def _click_by_role(self, page: object, roles: tuple[str, ...], name: str) -> bool:
        """Click the first role/accessible-name match across ``roles``.

        Costco labels the same control as a button or a link across variants, so
        try each role with a case-insensitive name regex. Best-effort: returns
        False if nothing matches (the caller decides how to fail)."""
        pattern = re.compile(re.escape(name), re.I)
        for role in roles:
            try:
                loc = page.get_by_role(role, name=pattern).first  # type: ignore[attr-defined]
                loc.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001 — try the next role
                continue
        return False

    def _place_order(self, page: object) -> bool:
        """Click Place Order, waiting on the button's *accessible name*.

        CSS ids can drift between Costco's checkout variants, so keying off the
        ids alone could read as "couldn't find Place Order" even when the button
        is right there. The visible text is the most stable handle, so wait on
        the role-named button first and click it promptly — before the checkout
        session goes stale — then fall back to the ids and a looser text match.
        TODO(costco): verify against live DOM — final-button accessible name."""
        name_re = re.compile(r"place (your )?order", re.I)
        btn = page.get_by_role("button", name=name_re)  # type: ignore[attr-defined]
        try:
            btn.first.wait_for(state="visible", timeout=_STEP_TIMEOUT_MS)
            btn.first.click(timeout=5_000)
            return True
        except Exception:  # noqa: BLE001 — fall through to the id/text fallbacks
            pass
        if self._click_first(page, _PLACE_ORDER_SELECTORS):
            return True
        try:
            page.get_by_text(name_re).first.click(timeout=5_000)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001 — no match anywhere; caller fails
            return False

    def _page_debug(self, page: object) -> str:
        """A short 'url · title' tag for failure messages, so the operator can
        tell what page the worker actually reached without a screenshot."""
        try:
            url = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            url = "?"
        try:
            title = page.title(timeout=2_000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            title = "?"
        return f"{url} · {title}".strip(" ·")

    def _wait_for_any(
        self, page: object, selectors: tuple[str, ...], timeout: int = _STEP_TIMEOUT_MS
    ) -> bool:
        """Block until any of ``selectors`` is visible, then return True.

        ``_click_first`` decides via an instantaneous ``count()`` snapshot, so a
        control that Costco renders with JS *after* navigation reads as absent
        and the click is skipped. This gives that JS time to paint. Returns
        False on timeout (the caller decides how to fail) rather than raising."""
        try:
            page.wait_for_selector(", ".join(selectors), timeout=timeout)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001 — caller handles the miss
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

    def _settle(self, page: object) -> None:
        """Let a freshly-navigated page paint before we shoot it.

        ``domcontentloaded`` fires before Costco's JS renders the checkout body,
        so without this the screenshot is just the header bar over a blank white
        page. Both waits are bounded and best-effort — Costco's checkout rarely
        goes fully ``networkidle``, so we cap it and shoot whatever we have."""
        for state in ("load", "networkidle"):
            try:
                page.wait_for_load_state(state, timeout=8_000)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — bounded wait; shoot what painted
                pass

    def _is_challenge(self, page: object) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3_000)  # type: ignore[attr-defined]
            url = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        return looks_like_challenge(text, url)

    def _is_signin(self, page: object) -> bool:
        try:
            url = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        if "/logon" in url.lower() or "signin.costco.com" in url.lower():
            return True
        try:
            text = page.locator("body").inner_text(timeout=3_000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        return looks_like_signin(text, url)

    def _challenge(self, page: object, item_key: str, where: str) -> PurchaseResult:
        shot = self._screenshot(page, item_key, f"challenge_{where}")
        return PurchaseResult(
            status="challenge",
            message=f"⚠️ Costco challenge on the {where} page — worker paused, clear it manually",
            screenshot=shot,
        )

    def _signin_required(self, page: object, item_key: str, where: str) -> PurchaseResult:
        """The profile got bounced to the sign-in wall on the ``where`` page —
        it's logged out. Pause with a clear next step rather than mislabeling the
        login form as a review screenshot."""
        shot = self._screenshot(page, item_key, f"signin_{where}")
        return PurchaseResult(
            status="challenge",
            message=(
                f"⚠️ Costco is logged out (hit the sign-in wall on the {where} page) — "
                "run `roomieorder login`, then retry"
            ),
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
