"""Playwright buy flow against a persistent, already-logged-in Chromium profile.

This is the brittle half (PLAN §1, §3.4). Everything here is written to fail
*loudly and safely*: resilient role/text selectors over brittle CSS, a hard
timeout per step, a screenshot on every failure, and explicit challenge
detection that halts rather than looping into a CAPTCHA.

Two stores are supported, one purchaser class each, sharing a :class:`BasePurchaser`:

* :class:`CostcoPurchaser` — tried first.
* :class:`AmazonPurchaser` — the fallback when Costco is sold out, not carried,
  or over its ceiling (see :mod:`roomieorder.orchestrator`).

The operator logs into each store by hand once into its own ``profile_dir`` (see
:meth:`BasePurchaser.login`, exposed as ``roomieorder login --provider …``); the
profile then remembers the session. Nothing here stores a credential — the login
lives entirely in the browser profile.

Run order inside :meth:`BasePurchaser.buy`:

1. goto the product page (source.url, falls back to the store's product_url())
2. detect challenge (Costco fronts the site with Akamai); ensure logged in
3. detect unavailability (404 / sold out / not carried) → ``unavailable`` so the
   orchestrator can fall back to the other store
4. read live price → ``proceed_check(price)`` (price ceiling + spend cap)
5. reach the review page (store-specific :meth:`_start_checkout`)
6. DRY_RUN → screenshot + stop; else click Place Order
7. scrape order number + total

⚠️ Every selector, marker, and order-number regex below is a best-guess against a
live DOM nobody here can see. Each DOM-dependent constant is flagged
``# TODO(<store>): verify against live DOM`` and MUST be confirmed during bring-up
(`roomieorder login` / `dry-run` / `dump-dom`).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generic, Literal, Optional, TypeVar

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

from roomieorder.catalog import AmazonSource, CatalogItem, CostcoSource
from roomieorder.config import Config
from roomieorder.guards import GuardResult
from roomieorder.store import Status

# Each purchaser drives exactly one store's source shape; bind it so the buy
# skeleton on the base can pass the concrete CostcoSource/AmazonSource through to
# the subclass hooks (_resolve_url/_source_label) without a getattr round-trip.
SourceT = TypeVar("SourceT", CostcoSource, AmazonSource)

_logger = logging.getLogger(__name__)


def _playwright_api() -> object:
    """Return the Playwright sync API module, preferring patchright.

    Akamai's strongest Playwright tell isn't ``navigator.webdriver`` (the
    ``--disable-blink-features=AutomationControlled`` flag clears that) — it's
    the Chrome DevTools Protocol leak: stock Playwright keeps ``Runtime.enable``
    on, so a page can plant a getter on an Error's ``stack`` and watch it fire
    when CDP serialises the object, unmasking the automation. ``patchright`` is
    an API-identical drop-in that runs scripts in isolated execution contexts
    and disables the Console API to close that leak (plus the command-flag and
    binding-global tells). Prefer it when installed; fall back to stock
    Playwright so a bare checkout without the ``stealth`` extra still runs.
    """
    import importlib

    for name in ("patchright.sync_api", "playwright.sync_api"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("neither patchright nor playwright is installed")


# Per-step navigation/click timeout. A step that stalls past this is a redesign
# or a challenge, not slowness.
_STEP_TIMEOUT_MS = 20_000

# Exception types that mean "this is our bug", not "the store flaked". These
# propagate out of `buy` instead of being laundered into a `failed` result, so a
# code defect surfaces (and pauses the worker via the loop's handler) rather than
# masquerading as a sold-out item with a one-line screenshot.
_BUG_EXCEPTIONS = (
    AttributeError,
    TypeError,
    NameError,
    ImportError,
    NotImplementedError,
)

# Accessible roles the click helpers target — a subset of Playwright's AriaRole.
# Stores label the same control as a button or a link across checkout variants.
_ClickRole = Literal["button", "link"]

_JSONLD_SELECTOR = "script[type='application/ld+json']"

# First number-ish run in a blob: digits with optional grouping/decimal
# separators, e.g. "24.99", "1,234.56", "11,99".
_PRICE_RE = re.compile(r"[0-9][0-9.,]*[0-9]|[0-9]")


@dataclass
class PurchaseResult:
    status: Status
    unit_price: Optional[float] = None
    order_total: Optional[float] = None
    order_id: Optional[str] = None
    # Which store produced this result ("costco"/"amazon"). Stamped by the
    # orchestrator; the purchaser leaves it blank.
    provider: str = ""
    message: str = ""
    screenshot: Optional[Path] = None


@dataclass
class DumpResult:
    """Artifacts from a read-only :meth:`BasePurchaser.dump_dom` bring-up run.

    ``summary`` is the same probe text written to ``probe``, surfaced so the CLI
    can print it without re-reading the file."""

    logged_in: bool = False
    challenge: bool = False
    blocked: bool = False
    html: Optional[Path] = None
    probe: Optional[Path] = None
    screenshot: Optional[Path] = None
    summary: str = ""


# proceed_check(live_price) -> GuardResult. Lets the worker run price-ceiling
# and spend-cap guards (which need the store) without pulling the store into
# this module.
ProceedCheck = Callable[[float], GuardResult]


def parse_price(text: str) -> Optional[float]:
    """Pull the first currency value out of a price blob, or None.

    Handles both US grouping (``$1,234.56``) and European decimal-comma
    (``€11,99``) by treating the *last* ``.``/``,`` as the decimal point — but
    only when it actually looks like a fraction. A trailing separator followed by
    exactly three digits (``$1,234``, ``$1,000``) is a *thousands group*, not
    cents: reading it as a decimal turns ``$1,000`` into ``1.0`` and sails the
    item under every price ceiling, which is the dangerous direction. So we only
    split on the last separator when its trailing run is 1, 2, or 4+ digits (real
    fractions); a lone 3-digit tail is grouping and the whole number is integral.

    Costco's React PDP splits the price across separate ``<span>``s — whole,
    dot, decimal — so the element's ``inner_text`` comes back as ``"$ 27 . 39"``
    with whitespace *inside* the number. Collapse whitespace sitting between two
    number characters first, so the value parses as ``27.39`` rather than ``27``.
    """
    text = re.sub(r"(?<=[\d.,])\s+(?=[\d.,])", "", text or "")
    m = _PRICE_RE.search(text)
    if not m:
        return None
    num = m.group(0)
    last_sep = max(num.rfind("."), num.rfind(","))
    tail = num[last_sep + 1 :] if last_sep != -1 else ""
    # A 3-digit tail is a thousands group, not cents → no decimal split.
    if last_sep == -1 or len(tail) == 3:
        whole = re.sub(r"[.,]", "", num)
        frac = ""
    else:
        whole = re.sub(r"[.,]", "", num[:last_sep])
        frac = tail
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

    Storefronts embed product data as ``application/ld+json``. The price lives
    under an ``offers`` key, but the surrounding shape varies (a bare Product, a
    ``@graph`` list of nodes, nested arrays), so walk the whole parsed structure
    for the first ``offers`` rather than hard-coding one layout."""
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


def looks_like(text: str, url: str, markers: tuple[str, ...]) -> bool:
    """True if any marker appears (case-insensitively) in the page text or URL."""
    haystack = f"{text}\n{url}".lower()
    return any(marker in haystack for marker in markers)


class BasePurchaser(Generic[SourceT]):
    """Drives one purchase per :meth:`buy` call, launching a fresh persistent
    context each time so no stale checkout state leaks between orders.

    Provider specifics (selectors, markers, checkout step order, sign-in nav)
    live in the subclass class-attributes and overrides below; the shared launch
    stealth, the ``buy`` skeleton, price reading, and the click/wait/challenge
    helpers all live here.
    """

    # ─────────── provider identity (override) ───────────
    PROVIDER = ""  # "costco" / "amazon"
    STORE_NAME = ""  # "Costco" / "Amazon"

    # ─────────── DOM constants (override) ───────────
    PRICE_SELECTORS: tuple[str, ...] = ()
    PRICE_META_SELECTORS: tuple[str, ...] = ()
    ADD_TO_CART_SELECTORS: tuple[str, ...] = ()
    BUY_NOW_SELECTORS: tuple[str, ...] = ()
    PLACE_ORDER_SELECTORS: tuple[str, ...] = ()
    ORDER_TOTAL_SELECTORS: tuple[str, ...] = ()
    SIGNIN_SUBMIT_SELECTORS: tuple[str, ...] = ()
    ACCOUNT_NAV_SELECTORS: tuple[str, ...] = ()
    # Akamai-style *hard block* markers (a 403 deny page with nothing to solve).
    # Checked before CHALLENGE_MARKERS; kept disjoint from it. Empty by default,
    # so a store whose wall is a solvable captcha stays `challenge`.
    BLOCK_MARKERS: tuple[str, ...] = ()
    CHALLENGE_MARKERS: tuple[str, ...] = ()
    SIGNIN_MARKERS: tuple[str, ...] = ()
    # Sold-out / not-carried / not-found markers that drive the Amazon fallback.
    OUT_OF_STOCK_MARKERS: tuple[str, ...] = ()
    NOT_FOUND_MARKERS: tuple[str, ...] = ()
    # Positive order-confirmation signals, checked alongside the order id / total.
    # A match means the store rendered a success page, so a placed order is no
    # longer misread as `needs_review` when the id/total selectors miss (the
    # confirmation can show a success banner with no scrapeable number). Both are
    # lowercased substring tests; empty by default.
    CONFIRMATION_MARKERS: tuple[str, ...] = ()       # success-banner body text
    CONFIRMATION_URL_MARKERS: tuple[str, ...] = ()   # thank-you URL fragments
    ORDER_ID_RE = re.compile(r"\b\d{9,12}\b")
    # Label-anchored order-id capture, tried *before* the bare ORDER_ID_RE so a
    # phone number / item number / ZIP+4 elsewhere on the confirmation page can't
    # be mistaken for the order id (capture group 1 is the id). None when the
    # store's bare ORDER_ID_RE is already specific enough (e.g. Amazon's dashed
    # format) to stand on its own.
    ORDER_ID_LABEL_RE: Optional["re.Pattern[str]"] = None

    # Bounded window for the checkout view to finish landing before we call it a
    # miss — deliberately NOT the full step timeout. The review body can paint a
    # beat after _settle returns (e.g. Costco's CheckoutCartView → 302 redirect
    # chain plus late React hydration), so a single instantaneous read mistook an
    # arrived checkout for "no Place Order" (the no_buy_button false negative).
    _LANDING_TIMEOUT_MS = 8_000

    def __init__(self, config: Config, *, profile_dir: Path, domain: str) -> None:
        self.config = config
        self.profile_dir = profile_dir
        self.domain = domain
        config.shots_dir.mkdir(parents=True, exist_ok=True)

    # ─────────── provider hooks (override) ───────────

    def _resolve_url(self, source: SourceT) -> str:
        """The product URL for ``source`` — its own ``url`` or a store fallback."""
        raise NotImplementedError

    def _source_label(self, source: SourceT) -> str:
        """A short id for log/probe messages, e.g. ``item #1640526``."""
        raise NotImplementedError

    def _start_checkout(self, page: "Page") -> bool:
        """Reach the place-order review page from the product page."""
        raise NotImplementedError

    def _reset_cart(self, page: "Page") -> None:
        """Empty the shared cart before adding this order's item. Base: no-op.

        Costco overrides this (its cart is server-side, shared across every run);
        Amazon's Buy-Now path doesn't touch the shared cart, so the default is to
        do nothing."""
        return None

    def is_logged_in(self, page: "Page") -> bool:
        """Best-effort sign-in check via the store's account nav.

        Both stores' nav reads a 'Sign In'/'Register' affordance when logged out
        and a greeting otherwise. Returns False if the nav can't be read, so a
        True is trustworthy but a False may be a miss."""
        for sel in self.ACCOUNT_NAV_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                text = loc.inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
            text = text.lower()
            return "sign in" not in text and "register" not in text
        return False

    def ensure_logged_in(self, page: "Page") -> bool:
        """Make sure the session is authenticated. Base: just report the state.

        Stores that re-establish a session from cached credentials (Costco)
        override this with a click flow; otherwise we rely on the persistent
        profile already holding a live session and bail to the manual-login
        message when it doesn't."""
        return self.is_logged_in(page)

    # ─────────── launch / paths ───────────

    def _launch_args(self) -> list[str]:
        # The worker runs unattended from a systemd service, so its headed
        # Chromium window is never presented/foregrounded — it opens occluded
        # (which is also why no window appears for an HA-triggered buy). For a
        # backgrounded window Chromium throttles requestAnimationFrame and
        # background timers to a crawl, so the store's JS never hydrates the
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

    def _launch_context(self, pw: object) -> "BrowserContext":
        """Launch the persistent context with the anti-bot configuration.

        Single source of truth for ``buy``, ``dump_dom`` and ``login`` so they
        present an identical browser to the store. The stealth-relevant choices:

        * **Real Google Chrome, not bundled Chromium** — ``executable_path``
          (a pinned binary, e.g. the NixOS google-chrome) wins; else
          ``channel`` ("chrome") finds a system install; else we fall back to
          Playwright's Chromium. Chrome carries the proprietary codecs and the
          ``"Google Chrome"`` Sec-CH-UA brand a real visitor has and Chromium
          lacks — Akamai keys on exactly that gap.
        * **``no_viewport=True``** — without it Playwright pins an emulated
          1280×720 viewport that doesn't match the real OS window, a mismatch
          bot detectors flag; this lets the content size track the window.
        * **No custom ``user_agent`` / headers** — deliberately omitted. An
          injected UA that disagrees with the real build's Client Hints is a
          worse tell than the honest default, so we never set one.
        """
        kwargs: dict[str, object] = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "args": self._launch_args(),
            "ignore_default_args": ["--enable-automation"],
            "no_viewport": True,
        }
        if self.config.chrome_path:
            kwargs["executable_path"] = self.config.chrome_path
        elif self.config.chrome_channel:
            kwargs["channel"] = self.config.chrome_channel
        return pw.chromium.launch_persistent_context(**kwargs)  # type: ignore[attr-defined,no-any-return]

    def _shot_path(self, item_key: str, tag: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.config.shots_dir / f"{stamp}_{self.PROVIDER}_{item_key}_{tag}.png"

    # ─────────── buy ───────────

    def buy(
        self,
        item_key: str,
        item: CatalogItem,
        source: SourceT,
        proceed_check: ProceedCheck,
    ) -> PurchaseResult:
        """Execute (or dry-run) the buy of ``source`` for ``item``.

        Always returns a PurchaseResult; the only exceptions that escape are
        programmer errors, not store flakiness — those become a ``failed`` result
        with a screenshot. A ``unavailable`` result (sold out / not carried /
        not found) signals the orchestrator to try the other store.
        """
        api = _playwright_api()
        PWTimeout = api.TimeoutError  # type: ignore[attr-defined]

        url = self._resolve_url(source)
        title = item.title

        with api.sync_playwright() as pw:  # type: ignore[attr-defined]
            context = self._launch_context(pw)
            context.set_default_timeout(_STEP_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            # Mark this tab active so Chromium un-throttles its renderer even
            # when the OS window is occluded (see _launch_args). Belt-and-braces
            # with the launch flags; best-effort, never fatal.
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001 — purely an optimisation
                pass
            # Flips True the instant Place Order is clicked — the point of no
            # return. Past it, *no* failure path may report `failed` (which would
            # invite a re-order of an order that may have gone through); they all
            # route to `needs_review` for a human to confirm.
            submitted = False
            # The order total only appears on the *review* page — Costco's
            # CheckoutConfirmationView_v2 shows just an order number — so it's
            # read there (before Place Order) and carried down to the result.
            review_total: Optional[float] = None
            try:
                resp = page.goto(url, wait_until="domcontentloaded")
                http_status = resp.status if resp is not None else None

                if self._is_blocked(page):
                    return self._blocked(page, item_key, "product")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "product")
                # A 404 means the product isn't carried — bail to `unavailable`
                # (→ fall back) *before* the login step, since a not-found page
                # often lacks the account nav and would otherwise misfire as a
                # sign-in wall.
                if http_status == 404:
                    shot = self._screenshot(page, item_key, "unavailable")
                    return PurchaseResult(
                        status="unavailable",
                        message=f"{title} not found (404) at {self.STORE_NAME}",
                        screenshot=shot,
                    )
                # A logged-out profile renders the product page fine (the header
                # just shows a sign-in link), so don't read it as a wall — sign
                # in (Costco re-establishes from cached credentials), then reload
                # so the price and checkout run against the authenticated session.
                if not self.ensure_logged_in(page):
                    return self._signin_required(page, item_key, "product")
                # Start from an empty cart: a live Place Order checks out the
                # *entire* cart, and every buy/dry-run only ever *adds* a line
                # (never clears it), so a stale item left by a prior run would be
                # ordered alongside this one. Drain it now, before we re-load the
                # PDP and add our single item. No-op for stores without a shared
                # cart (base). Best-effort — it navigates away, so reload the PDP
                # after regardless.
                self._reset_cart(page)
                page.goto(url, wait_until="domcontentloaded")
                if self._is_blocked(page):
                    return self._blocked(page, item_key, "product")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "product")

                # ── availability (drives the fallback) ──
                # Check before the price read: a 404 / sold-out page may carry no
                # price, and we want `unavailable` (fall back), not `failed`.
                self._settle(page)
                reason = self._check_availability(page, http_status)
                if reason is not None:
                    shot = self._screenshot(page, item_key, "unavailable")
                    return PurchaseResult(
                        status="unavailable",
                        message=f"{title} {reason} at {self.STORE_NAME}",
                        screenshot=shot,
                    )

                # ── price + guards ──
                # Same JS-hydration race as checkout: the price block can paint
                # after domcontentloaded, so wait for it before reading or a
                # live product reads as "no price".
                self._wait_for_any(page, self.PRICE_SELECTORS)
                price = self._read_price(page)
                if price is None:
                    shot = self._screenshot(page, item_key, "no_price")
                    return PurchaseResult(
                        status="failed",
                        message=f"couldn't read a price for {title}",
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
                    # A failure here means we never confirmed the review page. An
                    # Akamai block or a sign-in bounce mid-drive lands here too, so
                    # classify those first (worker pauses) instead of mislabelling
                    # a block as "couldn't drive checkout" and falling back to the
                    # other store. The bare no_buy_button is the genuine
                    # cart-drive/selector miss only when it's neither.
                    if self._is_blocked(page):
                        return self._blocked(page, item_key, "checkout")
                    if self._is_challenge(page):
                        return self._challenge(page, item_key, "checkout")
                    if self._is_signin(page):
                        return self._signin_required(page, item_key, "checkout")
                    shot = self._screenshot(page, item_key, "no_buy_button")
                    return PurchaseResult(
                        status="failed",
                        unit_price=price,
                        message=(
                            "couldn't drive add-to-cart → cart → checkout "
                            f"({self._page_debug(page)})"
                        ),
                        screenshot=shot,
                    )

                page.wait_for_load_state("domcontentloaded")
                if self._is_blocked(page):
                    return self._blocked(page, item_key, "checkout")
                if self._is_signin(page):
                    return self._signin_required(page, item_key, "checkout")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "checkout")
                if not self._checkout_landed(page):
                    # _start_checkout reported success but we're no longer on the
                    # review page: a krypto-ticket expiry or a soft Akamai bounce
                    # can drop us back to the storefront. Don't read a total off
                    # the wrong page or screenshot a "review" that isn't one — the
                    # challenge check above already caught a hard block, so this is
                    # the silent-bounce case.
                    shot = self._screenshot(page, item_key, "left_checkout")
                    return PurchaseResult(
                        status="failed",
                        unit_price=price,
                        message=(
                            "reached checkout but bounced off the review page "
                            f"({self._page_debug(page)})"
                        ),
                        screenshot=shot,
                    )

                # ── read the order total off the review page ──
                # The confirmation page (Costco's CheckoutConfirmationView_v2)
                # shows only an order number — no total — so the review page is
                # the one place the grand total (item + shipping + tax) is on
                # screen. Read it here, before Place Order, so it lands in the
                # result (→ Google Sheet, for cost-splitting) even on a dry run,
                # and stands in when the confirmation scrape finds no total.
                self._settle(page)
                # The grand-total element hydrates after the checkout body, so a
                # bare read can race it to None (seen live). Give it the same
                # bounded window the landing check uses before reading.
                self._wait_for_any(page, self.ORDER_TOTAL_SELECTORS, timeout=self._LANDING_TIMEOUT_MS)
                review_total = self._read_total(page)

                # ── DRY_RUN stops here ──
                if self.config.dry_run:
                    shot = self._screenshot(page, item_key, "review")
                    msg = f"[DRY] would order {item_key} at ${price:.2f}"
                    if review_total is not None:
                        msg += f" (total ${review_total:.2f})"
                    return PurchaseResult(
                        status="dry_run",
                        unit_price=price,
                        order_total=review_total,
                        message=msg,
                        screenshot=shot,
                    )

                # ── place the order ──
                # The store renders the checkout body via JS *after*
                # domcontentloaded, so the button isn't in the DOM the instant
                # we arrive. Settle first (the same wait the dry-run review shot
                # relies on — without it the body is a blank header), then let
                # _place_order wait on the button itself. (Don't pre-wait on
                # PLACE_ORDER_SELECTORS here: the CSS ids may drift between
                # checkout variants, so the wait could burn the whole step
                # timeout and the checkout session blanks out before we click.)
                self._settle(page)
                if not self._place_order(page):
                    # A slow render, a sign-in wall, or a challenge can all land
                    # us here with no button. Re-check the latter two so the
                    # operator gets the right next step, not a misleading
                    # "couldn't find Place Order". Settle again so the
                    # diagnostic shot shows the real page, not a blank header.
                    if self._is_blocked(page):
                        return self._blocked(page, item_key, "checkout")
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
                # The click landed: from here a scrape miss / timeout / crash
                # must not read as `failed`.
                submitted = True

                page.wait_for_load_state("domcontentloaded")
                if self._is_blocked(page):
                    return self._blocked(page, item_key, "confirm")
                if self._is_signin(page):
                    return self._signin_required(page, item_key, "confirm")
                if self._is_challenge(page):
                    return self._challenge(page, item_key, "confirm")

                self._settle(page)
                order_id, total, confirmed = self._scrape_confirmation(page)
                if not confirmed:
                    # Submitted, but nothing confirmable scraped — no order id,
                    # no total, and no success banner / thank-you URL. Don't
                    # claim a clean `placed` we can't evidence. Flag for human
                    # review, carrying the review-page total so the row still
                    # logs a dollar amount to split.
                    return self._submitted_unconfirmed(
                        page,
                        item_key,
                        "no order number, total, or confirmation banner on the page",
                        order_total=review_total,
                    )
                # The confirmation page rarely carries a total (Costco's v2 view
                # shows only the order number), so fall back to the grand total
                # read off the review page before the click.
                order_total = total if total is not None else review_total
                self._screenshot(page, item_key, "confirmation")
                return PurchaseResult(
                    status="placed",
                    unit_price=price,
                    order_total=order_total,
                    order_id=order_id,
                    message=(
                        f"ordered {title} — ${(order_total or price):.2f}"
                        + (f" — #{order_id}" if order_id else "")
                    ),
                )

            except PWTimeout as exc:
                detail = f"timed out: {exc}".split("\n")[0]
                if submitted:
                    return self._submitted_unconfirmed(
                        page, item_key, detail, order_total=review_total
                    )
                shot = self._screenshot(page, item_key, "timeout")
                return PurchaseResult(status="failed", message=detail, screenshot=shot)
            except _BUG_EXCEPTIONS:
                # A programmer error (bad attr/type/name, missing override, …).
                # Screenshot for context, then re-raise so it can't hide as
                # "store flakiness" — the worker loop records it and pauses.
                _logger.exception("buy flow hit a programmer error for %s", item_key)
                self._screenshot(page, item_key, "crash")
                raise
            except Exception as exc:  # noqa: BLE001 — convert any flake to a safe result
                _logger.exception("buy flow crashed for %s", item_key)
                detail = f"buy flow error: {exc}".split("\n")[0]
                if submitted:
                    return self._submitted_unconfirmed(
                        page, item_key, detail, order_total=review_total
                    )
                shot = self._screenshot(page, item_key, "crash")
                return PurchaseResult(status="failed", message=detail, screenshot=shot)
            finally:
                context.close()

    # ─────────── availability ───────────

    def _check_availability(self, page: "Page", http_status: Optional[int]) -> Optional[str]:
        """Return a human reason when the product can't be ordered here, else None.

        Drives the Amazon fallback: a 404, a not-found page, a sold-out marker,
        or a disabled add-to-cart button all mean "try the other store". Pure
        read-only and best-effort — a miss returns None and the buy proceeds.
        """
        if http_status == 404:
            return "not found (404)"
        try:
            body = page.locator("body").inner_text(timeout=3_000)
        except Exception:  # noqa: BLE001 — can't read the body; assume available
            body = ""
        if looks_like(body, "", self.NOT_FOUND_MARKERS):
            return "not found"
        if looks_like(body, "", self.OUT_OF_STOCK_MARKERS):
            return "is out of stock"
        if self._add_to_cart_disabled(page):
            return "is out of stock (add-to-cart unavailable)"
        return None

    def _add_to_cart_disabled(self, page: "Page") -> bool:
        """True only when an add-to-cart control is present *and* disabled.

        An absent button isn't treated as out-of-stock here (the selectors are
        unverified guesses, so "not found" would false-positive on every page);
        a present-but-disabled one is the reliable sold-out tell."""
        for sel in self.ADD_TO_CART_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                return bool(loc.is_disabled(timeout=2_000))
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        return False

    # ─────────── dump-dom (bring-up) ───────────

    def dump_dom(self, item_key: str, item: CatalogItem, source: SourceT) -> DumpResult:
        """Open the product page read-only and dump the rendered DOM.

        A bring-up aid for confirming the ``# TODO: verify against live DOM``
        selectors against the real page instead of guessing. It stops at the
        product page — it never adds to cart or places an order — reusing buy()'s
        logged-in profile and stealth launch. Writes the rendered HTML, a probe
        of every candidate selector group, and a screenshot to ``shots_dir``.
        Best-effort throughout: a challenge or a logged-out profile still dumps
        whatever painted, flagged in the result.
        """
        api = _playwright_api()
        url = self._resolve_url(source)
        result = DumpResult()

        with api.sync_playwright() as pw:  # type: ignore[attr-defined]
            context = self._launch_context(pw)
            context.set_default_timeout(_STEP_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001 — purely an optimisation
                pass
            try:
                page.goto(url, wait_until="domcontentloaded")
                result.blocked = self._is_blocked(page)
                result.challenge = self._is_challenge(page)
                # Sign in if we can so the probe also sees any logged-in-only
                # controls, but never bail on it — dumping a logged-out page is
                # still useful for the price selectors (price renders logged out).
                if not result.blocked and not result.challenge and not self.is_logged_in(page):
                    self.ensure_logged_in(page)
                    page.goto(url, wait_until="domcontentloaded")
                    result.blocked = self._is_blocked(page)
                    result.challenge = self._is_challenge(page)
                self._settle(page)
                result.logged_in = self.is_logged_in(page)
                result.summary = self._probe_selectors(page)
                result.html = self._write_text(item_key, "dom", "html", self._page_html(page))
                result.probe = self._write_text(item_key, "probe", "txt", result.summary)
                result.screenshot = self._screenshot(page, item_key, "dump")
            finally:
                context.close()
        return result

    def _page_html(self, page: "Page") -> str:
        try:
            return page.content()
        except Exception:  # noqa: BLE001 — dump whatever we can
            return ""

    def _write_text(self, item_key: str, tag: str, ext: str, content: str) -> Optional[Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.config.shots_dir / f"{stamp}_{self.PROVIDER}_{item_key}_{tag}.{ext}"
        try:
            path.write_text(content, encoding="utf-8")
            return path
        except Exception as exc:  # noqa: BLE001
            _logger.warning("write %s failed: %s", tag, exc)
            return None

    def _probe_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return (
            ("price", self.PRICE_SELECTORS),
            ("price-meta", self.PRICE_META_SELECTORS),
            ("add-to-cart", self.ADD_TO_CART_SELECTORS),
            ("buy-now", self.BUY_NOW_SELECTORS),
            ("place-order", self.PLACE_ORDER_SELECTORS),
            ("order-total", self.ORDER_TOTAL_SELECTORS),
            ("signin-submit", self.SIGNIN_SUBMIT_SELECTORS),
        )

    def _probe_selectors(self, page: "Page") -> str:
        """Human-readable report of which candidate selectors resolve on ``page``.

        For each selector: its match count and a short text/``content`` sample,
        so a glance tells you which guess is live and what the right one is. Pure
        read-only — it only ever reads counts and text."""
        lines: list[str] = []
        try:
            lines.append(f"url:   {page.url}")
        except Exception:  # noqa: BLE001
            pass
        try:
            lines.append(f"title: {page.title()}")
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"logged_in:   {self.is_logged_in(page)}")
        lines.append(f"read_price:  {self._read_price(page)}")
        lines.append("")
        for label, selectors in self._probe_groups():
            lines.append(f"[{label}]")
            lines.extend(self._probe_one(page, sel) for sel in selectors)
            lines.append("")
        lines.append("[json-ld]")
        try:
            blocks = page.locator(_JSONLD_SELECTOR)
            count = blocks.count()
        except Exception:  # noqa: BLE001
            count = 0
        lines.append(f"  {_JSONLD_SELECTOR}  count={count}")
        for i in range(count):
            try:
                raw = blocks.nth(i).inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001
                continue
            lines.append(f"    [{i}] offer_price={_price_from_jsonld(raw)}  ({len(raw)} chars)")
        return "\n".join(lines)

    def _probe_one(self, page: "Page", selector: str) -> str:
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception as exc:  # noqa: BLE001
            return f"  {selector}  ERROR {exc}"
        if count == 0:
            return f"  {selector}  count=0"
        try:
            first = loc.first
            if selector.startswith("meta"):
                sample = first.get_attribute("content", timeout=2_000) or ""
            else:
                sample = first.inner_text(timeout=2_000)
        except Exception:  # noqa: BLE001
            sample = "<unreadable>"
        sample = " ".join(sample.split())[:80]
        return f"  {selector}  count={count}  sample={sample!r}"

    # ─────────── login ───────────

    def login(self, wait_for_operator: Callable[[object], None]) -> None:
        """Open the persistent profile headed so the operator can sign in by hand.

        Cookies persist in ``profile_dir``; roomieorder never stores a store
        credential of its own (PLAN §1).

        ``wait_for_operator(page)`` is invoked once the store home page has loaded
        and must *block* until the human is done — the context (and the saved
        session with it) is torn down as soon as it returns.
        """
        api = _playwright_api()

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with api.sync_playwright() as pw:  # type: ignore[attr-defined]
            context = self._launch_context(pw)
            # Apply any store-specific login tweak to every document in this
            # context *before* the first navigation — Amazon forces the
            # "Keep me signed in" (rememberMe) param so the auth cookies persist.
            # Scoped to login only: the buy/dump contexts never inject JS into the
            # store's (Akamai-fronted) pages.
            script = self._login_init_script()
            if script:
                context.add_init_script(script)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    f"https://www.{self.domain}",
                    wait_until="domcontentloaded",
                )
                wait_for_operator(page)
            finally:
                context.close()

    def _login_init_script(self) -> Optional[str]:
        """JS injected on every document during ``login`` only. Base: none.

        Stores that need to nudge the hand-login (e.g. force a persistent-session
        form param) override this; the buy/dump flows never inject."""
        return None

    def verify_session(self) -> bool:
        """Relaunch the saved profile from disk and report if it reloads signed in.

        ``login``'s own in-window ``is_logged_in`` check passes the instant the
        operator signs in, but that reads cookies still live in memory — it can't
        see whether they were *persisted*. Amazon issues its auth cookies
        (``at-main``/``x-main``) as **session** cookies unless "Keep me signed in"
        (the ``rememberMe`` form param ``login`` now forces) is set, and Chrome
        never flushes session cookies to the on-disk profile — so without it a
        session that looked signed-in live reloads signed-out and the worker hits
        the sign-in wall. This closes the loop honestly: a fresh persistent-context
        launch reads cookies from disk, so a logged-in reload here *is* the proof
        the persistent cookies were written — i.e. that ``rememberMe`` took and
        the next run (the worker) will be logged in.
        """
        api = _playwright_api()
        with api.sync_playwright() as pw:  # type: ignore[attr-defined]
            context = self._launch_context(pw)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    f"https://www.{self.domain}",
                    wait_until="domcontentloaded",
                )
                self._settle(page)
                return self.ensure_logged_in(page)
            except Exception:  # noqa: BLE001 — couldn't reload; treat as unverified
                return False
            finally:
                context.close()

    # ─────────── page helpers ───────────

    def _read_price(self, page: "Page") -> Optional[float]:
        for sel in self.PRICE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                text = loc.inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 — selector miss; try the next
                continue
            price = parse_price(text)
            if price is not None:
                return price
        # The visible price element couldn't be located (every PRICE_SELECTORS
        # is an unverified guess against a DOM nobody here can see). Fall back to
        # the page's structured data, which is far less brittle.
        return self._read_price_from_metadata(page)

    def _read_price_from_metadata(self, page: "Page") -> Optional[float]:
        """Read the product price from page metadata when the visible price
        element can't be located: OpenGraph/schema.org ``<meta>`` tags first,
        then JSON-LD ``offers``. Both are server-rendered into the initial HTML,
        so they're present even before the price block hydrates."""
        for sel in self.PRICE_META_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                content = loc.get_attribute("content", timeout=2_000)
            except Exception:  # noqa: BLE001 — meta miss; try the next source
                continue
            price = parse_price(content or "")
            if price is not None:
                return price
        try:
            blocks = page.locator(_JSONLD_SELECTOR)
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

    def _click_by_role(
        self, page: "Page", roles: tuple[_ClickRole, ...], name: str
    ) -> bool:
        """Click the first role/accessible-name match across ``roles``.

        Stores label the same control as a button or a link across variants, so
        try each role with a case-insensitive name regex. Best-effort: returns
        False if nothing matches (the caller decides how to fail)."""
        pattern = re.compile(re.escape(name), re.I)
        for role in roles:
            try:
                loc = page.get_by_role(role, name=pattern).first
                loc.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001 — try the next role
                continue
        return False

    def _place_order(self, page: "Page") -> bool:
        """Click Place Order, waiting on the button's *accessible name*.

        CSS ids can drift between a store's checkout variants, so keying off the
        ids alone could read as "couldn't find Place Order" even when the button
        is right there. The visible text is the most stable handle, so wait on
        the role-named button first and click it promptly — before the checkout
        session goes stale — then fall back to the ids and a clickable-role text
        match. The last resort stays on *clickable roles* (button/link) rather
        than a bare ``get_by_text``, which would happily click a heading or label
        that merely contains "Place Order" and isn't the submit control."""
        name_re = re.compile(r"place (your )?order", re.I)
        btn = page.get_by_role("button", name=name_re)
        try:
            btn.first.wait_for(state="visible", timeout=_STEP_TIMEOUT_MS)
            btn.first.click(timeout=5_000)
            return True
        except Exception:  # noqa: BLE001 — fall through to the id/role fallbacks
            pass
        if self._click_first(page, self.PLACE_ORDER_SELECTORS):
            return True
        roles: tuple[_ClickRole, ...] = ("button", "link")
        for role in roles:
            try:
                page.get_by_role(role, name=name_re).first.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001 — try the next clickable role
                continue
        return False

    def _page_debug(self, page: "Page") -> str:
        """A short 'url · title' tag for failure messages, so the operator can
        tell what page the worker actually reached without a screenshot."""
        try:
            url = page.url
        except Exception:  # noqa: BLE001
            url = "?"
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = "?"
        return f"{url} · {title}".strip(" ·")

    def _wait_for_any(
        self, page: "Page", selectors: tuple[str, ...], timeout: int = _STEP_TIMEOUT_MS
    ) -> bool:
        """Block until any of ``selectors`` is visible, then return True.

        ``_click_first`` decides via an instantaneous ``count()`` snapshot, so a
        control that the store renders with JS *after* navigation reads as absent
        and the click is skipped. This gives that JS time to paint. Returns
        False on timeout (the caller decides how to fail) rather than raising."""
        if not selectors:
            return False
        try:
            page.wait_for_selector(", ".join(selectors), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 — caller handles the miss
            return False

    def _checkout_landed(self, page: "Page") -> bool:
        """One instantaneous read of the review-page landing signals.

        The Place Order button is the definitive signal — it exists only on the
        review page, never on the cart. The URL is the drift-immune backstop, but
        it must distinguish the review page from the cart: verified live on Costco
        2026-06-17, the review URL is `…/SinglePageCheckoutView` while the cart is
        `…/CheckoutCartView` → `…/CheckoutCartDisplayView`, and *both* carry
        "checkout". A bare "checkout in url" match therefore read the cart — and
        any post-checkout bounce back through it — as landed (the homepage-as-review
        false positive). Require "checkout" AND not "cart"."""
        for sel in self.PLACE_ORDER_SELECTORS:
            try:
                if page.locator(sel).first.count() > 0:
                    return True
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001 — no URL and no button → not landed
            return False
        return "checkout" in url and "cart" not in url

    def _click_first(self, page: "Page", selectors: tuple[str, ...]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=5_000)
                return True
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        return False

    def _submitted_unconfirmed(
        self,
        page: "Page",
        item_key: str,
        detail: str,
        order_total: Optional[float] = None,
    ) -> PurchaseResult:
        """Place Order was clicked but we couldn't confirm the result.

        The order *may* have gone through, so this never reports ``failed`` (which
        the worker could re-drive into a double order). It returns ``needs_review``
        — a pausing, non-fallback status — so a human checks the store account
        before anything re-orders the item. ``order_total`` (the review-page grand
        total, when known) is carried onto the row so the human still sees the
        amount to split even though the confirmation couldn't be scraped."""
        shot = self._screenshot(page, item_key, "submitted_unconfirmed")
        return PurchaseResult(
            status="needs_review",
            order_total=order_total,
            message=(
                f"⚠️ {self.STORE_NAME}: Place Order was clicked but the confirmation "
                f"couldn't be read — the order MAY have been placed. Check the "
                f"{self.STORE_NAME} account before re-ordering ({detail})"
            ),
            screenshot=shot,
        )

    def _read_total(self, page: "Page") -> Optional[float]:
        """First parseable amount from ``ORDER_TOTAL_SELECTORS``, or None.

        Shared by the review-page read (before Place Order) and the confirmation
        scrape — the same grand-total element id is used on Costco's
        SinglePageCheckoutView, so one reader serves both call sites."""
        for sel in self.ORDER_TOTAL_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                total = parse_price(loc.inner_text(timeout=2_000))
                if total is not None:
                    return total
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        return None

    def _scrape_confirmation(
        self, page: "Page"
    ) -> tuple[Optional[str], Optional[float], bool]:
        # Defensive read: the confirmation body can paint a beat after the order
        # POST returns, so a single read can miss it. Retry a few times before
        # giving up — a missed scrape here is what makes a placed order look
        # unconfirmed (see _submitted_unconfirmed).
        #
        # Returns (order_id, total, confirmed). `confirmed` is True when *any*
        # positive signal is seen — a success banner / thank-you URL, an order
        # id, or a total — so a real order whose number/total can't be scraped
        # still reads as `placed` instead of `needs_review`.
        body = ""
        confirmed = False
        for attempt in range(3):
            try:
                body = page.locator("body").inner_text(timeout=5_000)
            except Exception:  # noqa: BLE001
                body = ""
            confirmed = self._looks_confirmed(page, body)
            if confirmed or self._find_order_id(body) is not None:
                break
            if attempt < 2:
                self._settle(page)
        order_id = self._find_order_id(body)
        total = self._read_total(page)
        return order_id, total, (confirmed or order_id is not None or total is not None)

    def _looks_confirmed(self, page: "Page", body: str) -> bool:
        """True when the page shows a recognized order-confirmation signal.

        A positive banner/URL match is authoritative — the store only renders
        these on a successful order — so it confirms a placed order even when the
        order id and total can't be scraped (Amazon's confirmation surfaced
        'Order placed, thanks!' with no scrapeable number, the
        false-needs_review case this guards against)."""
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            url = ""
        if any(m in url for m in self.CONFIRMATION_URL_MARKERS):
            return True
        text = body.lower()
        return any(m in text for m in self.CONFIRMATION_MARKERS)

    def _find_order_id(self, body: str) -> Optional[str]:
        """Extract the order id from the confirmation body text.

        Prefer the label-anchored ``ORDER_ID_LABEL_RE`` (capture group 1) so a
        bare digit run elsewhere — a phone number, item number, ZIP+4, a
        timestamp — can't be mistaken for the order id. Fall back to the store's
        ``ORDER_ID_RE`` only when no label match is found (or none is defined)."""
        if self.ORDER_ID_LABEL_RE is not None:
            m = self.ORDER_ID_LABEL_RE.search(body)
            if m:
                return m.group(1)
        m = self.ORDER_ID_RE.search(body)
        return m.group(0) if m else None

    def _settle(self, page: "Page") -> None:
        """Let a freshly-navigated page paint before we shoot it.

        ``domcontentloaded`` fires before the store's JS renders the checkout
        body, so without this the screenshot is just the header bar over a blank
        white page. Both waits are bounded and best-effort — the checkout rarely
        goes fully ``networkidle``, so we cap it and shoot whatever we have."""
        states: tuple[Literal["load", "networkidle"], ...] = ("load", "networkidle")
        for state in states:
            try:
                page.wait_for_load_state(state, timeout=8_000)
            except Exception:  # noqa: BLE001 — bounded wait; shoot what painted
                pass

    def _is_blocked(self, page: "Page") -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3_000)
            url = page.url
        except Exception:  # noqa: BLE001
            return False
        return looks_like(text, url, self.BLOCK_MARKERS)

    def _is_challenge(self, page: "Page") -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3_000)
            url = page.url
        except Exception:  # noqa: BLE001
            return False
        return looks_like(text, url, self.CHALLENGE_MARKERS)

    def _is_signin(self, page: "Page") -> bool:
        try:
            url = page.url
        except Exception:  # noqa: BLE001
            return False
        try:
            text = page.locator("body").inner_text(timeout=3_000)
        except Exception:  # noqa: BLE001
            text = ""
        return looks_like(text, url, self.SIGNIN_MARKERS)

    def _blocked(self, page: "Page", item_key: str, where: str) -> PurchaseResult:
        """An Akamai hard block (Access Denied) on the ``where`` page — a
        fingerprint/IP ban, not a solvable captcha. Pause with a "nothing to
        click" next step so the operator waits it out / rotates rather than
        hunting for a challenge to clear (the `challenge` message)."""
        shot = self._screenshot(page, item_key, f"blocked_{where}")
        return PurchaseResult(
            status="blocked",
            message=(
                f"⛔ {self.STORE_NAME} blocked the {where} page (Akamai) — worker "
                "paused; nothing to click, wait it out / rotate fingerprint, then retry"
            ),
            screenshot=shot,
        )

    def _challenge(self, page: "Page", item_key: str, where: str) -> PurchaseResult:
        shot = self._screenshot(page, item_key, f"challenge_{where}")
        return PurchaseResult(
            status="challenge",
            message=(
                f"⚠️ {self.STORE_NAME} challenge on the {where} page — "
                "worker paused, clear it manually"
            ),
            screenshot=shot,
        )

    def _signin_required(self, page: "Page", item_key: str, where: str) -> PurchaseResult:
        """The profile got bounced to the sign-in wall on the ``where`` page —
        it's logged out. Pause with a clear next step rather than mislabeling the
        login form as a review screenshot."""
        shot = self._screenshot(page, item_key, f"signin_{where}")
        return PurchaseResult(
            status="challenge",
            message=(
                f"⚠️ {self.STORE_NAME} is logged out (hit the sign-in wall on the "
                f"{where} page) — run `roomieorder login --provider {self.PROVIDER}`, then retry"
            ),
            screenshot=shot,
        )

    def _screenshot(self, page: "Page", item_key: str, tag: str) -> Optional[Path]:
        path = self._shot_path(item_key, tag)
        try:
            page.screenshot(path=str(path), full_page=False)
            return path
        except Exception as exc:  # noqa: BLE001
            _logger.warning("screenshot failed (%s): %s", tag, exc)
            return None


class CostcoPurchaser(BasePurchaser[CostcoSource]):
    """Costco — the first store tried for every item."""

    PROVIDER = "costco"
    STORE_NAME = "Costco"

    # Verified against live DOM 2026-06-16 (paper_towels dump-dom): Costco's PDP is
    # a MUI/React app keyed on `data-testid`. `single-price-content` (the sale
    # price) is read first; the broader `[data-testid='price']` container also
    # holds the promo strikethrough so its text would mis-parse. Legacy guesses
    # are kept as backstops.
    PRICE_SELECTORS = (
        "[data-testid='single-price-content']",
        "[data-testid='price']",
        "[automation-id='productPriceOutput']",
        ".product-price-amount",
        ".product-price .value",
        "span.value",
    )
    # Structured-data price sources, tried after the visible selectors miss; the
    # server-rendered Next.js storefront emits these in the initial HTML.
    PRICE_META_SELECTORS = (
        "meta[property='product:price:amount']",
        "meta[property='og:price:amount']",
        "meta[itemprop='price']",
    )
    # Verified 2026-06-16: the PDP add-to-cart button is
    # `[data-testid='Button_addToCartDrawer_pdp']`. Its accessible name is the
    # product title, NOT "Add to Cart", so the role/text match misses and this
    # CSS fallback is what actually clicks it. Legacy guesses kept as backstops.
    ADD_TO_CART_SELECTORS = (
        "[data-testid='Button_addToCartDrawer_pdp']",
        "[automation-id='addToCartButton']",
        "input[value='Add to Cart']",
        "button#add-to-cart-btn",
    )
    # Verified against the live SinglePageCheckoutView 2026-06-17: the final
    # control is a `<div id="place-order-button-regular">` (not the old
    # `automation-id='placeOrderButton'` guess, which is count=0). Legacy guesses
    # kept as backstops in case the checkout variant differs.
    PLACE_ORDER_SELECTORS = (
        "#place-order-button-regular",
        "[automation-id='placeOrderButton']",
        "input[value='Place Order']",
        "button#place-order",
    )
    # Saved-card payment radios on SinglePageCheckoutView. Verified live
    # 2026-06-17: the default card's selector is a role=radio `<div>` with
    # `automation-id='paymentReviewRadio'` (`#radio-credit-card-review-ada-handler`),
    # carrying `aria-checked` and proxying the hidden `<input
    # #radio-credit-card-review>` (tabindex=-1). It is the "select my saved card"
    # control and is NOT reliably pre-selected on load — Place Order stays inert
    # until a payment method is chosen. The sibling `paymentRadio`
    # (`#radio-credit-card-ada-handler`, "Credit or Debit Card") is the
    # *enter-a-new-card* option, so it is deliberately NOT a candidate here.
    PAYMENT_METHOD_SELECTORS = (
        "[automation-id='paymentReviewRadio']",
        "#radio-credit-card-review-ada-handler",
    )
    # Cart line-item remove control. Verified live 2026-06-17 (CheckoutCartDisplayView):
    # the cart is the *legacy* WebSphere app (automation-id, not the PDP's
    # data-testid). Lines are 1-indexed (`removeItemLink_1`, `_2`, …) and
    # re-index after each removal, so _reset_cart always clicks the first match.
    REMOVE_ITEM_SELECTORS = (
        "[automation-id^='removeItemLink_']",
        "a[automation-id*='removeItem']",
    )
    # Primary button of Costco's generic confirm modal (reused site-wide; its
    # markup sits hidden in the cart DOM until triggered). Cart removal looks to
    # be a direct AJAX "Remove" with an undo toast — no confirm — but _reset_cart
    # dismisses this if a remove ever does pop it, gated on visibility so the
    # always-present hidden markup costs nothing. TODO(costco): confirm live
    # whether remove pops a modal at all.
    CART_CONFIRM_SELECTORS = ("[automation-id='confirmationButton']",)
    # Verified against live DOM 2026-06-17 — order-confirmation grand-total.
    ORDER_TOTAL_SELECTORS = (
        "[automation-id='orderTotalOutput']",
        ".order-total .value",
        ".grand-total .value",
    )
    # Legacy logon-form submit control. No longer used by the buy flow: Costco's
    # re-auth is a silent SSO redirect (see ensure_logged_in), not a typed form,
    # so there's no submit button to click. Kept only for the dump-dom probe.
    SIGNIN_SUBMIT_SELECTORS = (
        "[automation-id='signInButton']",
        "input#sign-in-btn",
        "button#sign-in-btn",
        "button[type='submit']",
    )
    # Verified against live DOM 2026-06-17: the header renders an "Account"
    # member-links button ONLY for an authenticated session (count=0 for a
    # guest), so its presence is a logged-in tell. The base text check reads
    # "Account" (no "sign in"/"register") → True when present, False when the
    # selector is absent. Used as the DOM backstop to the cookie check below.
    ACCOUNT_NAV_SELECTORS = (
        "[data-testid='icon-links-member-links-desktop-account']",
        "[data-testid='search-strip-member-links-desktop-account']",
    )
    # Akamai *hard block* — a 403 "Access Denied" deny page (fingerprint/IP ban).
    # Nothing to solve in the browser, so it pauses as `blocked` (not `challenge`)
    # with a "wait it out / rotate" message. Kept disjoint from CHALLENGE_MARKERS
    # and checked first. TODO(costco): verify against live DOM.
    BLOCK_MARKERS = (
        "access denied",
        "reference #",
        "akamai",
    )
    # Costco/Akamai *interactive* bot wall — a captcha/verification a human can
    # solve, so it pauses as `challenge`. TODO(costco): verify against live DOM.
    CHALLENGE_MARKERS = (
        "pardon our interruption",
        "verify you are human",
        "are you a human",
        "/_sec/",
        "recaptcha",
        "enter the characters",
        "verify your identity",
    )
    # A logged-out session bounces to Costco's logon page at checkout. Detect by
    # the logon URL/host; "sign in or register" (the page's own heading, with
    # "or") is a body backstop. The header "Sign In / Register" link is on every
    # page, so it deliberately isn't a marker. TODO(costco): verify against live DOM.
    SIGNIN_MARKERS = (
        "/logon",
        "signin.costco.com",
        "sign in or register",
    )
    # Verified live 2026-06-17 (toilet_paper dump-dom): the PDP renders a
    # per-warehouse pick-up availability widget ("How To Get It") that shows
    # "<warehouse> Out of Stock" / "Low Stock" even when 2-Day Delivery is in
    # stock, so a bare "out of stock" / "sold out" body scan false-positives on
    # it and falls a deliverable item back to Amazon. Match only the
    # delivery/online-order wording, which the warehouse widget never uses: the
    # widget's own sold-out copy is "…unavailable for pick-up at nearby
    # warehouses", whereas an item that can't be shipped reads "…unavailable to
    # order online". The disabled/absent add-to-cart button
    # (_add_to_cart_disabled) is the structural backstop for the unmessaged case.
    OUT_OF_STOCK_MARKERS = (
        "unavailable to order online",
        "out of stock or unavailable to order",
        "this item is currently unavailable",
    )
    # TODO(costco): verify against live DOM — not-found page wording.
    NOT_FOUND_MARKERS = (
        "we can't find the page",
        "page not found",
        "the page you requested cannot be found",
    )
    # Costco web order numbers — best guess (purely digits, ~10). Because a bare
    # digit run on the confirmation page is ambiguous (phone numbers, item
    # numbers, ZIP+4 all match), prefer the label-anchored capture below and only
    # fall back to this when no "Order #"/"Confirmation number" label is found.
    # TODO(costco): verify against live DOM — confirm order-number format + label.
    ORDER_ID_RE = re.compile(r"\b\d{9,12}\b")
    # "Order #12345678", "Order Number: 12345678", "Confirmation # 12345678", …
    ORDER_ID_LABEL_RE = re.compile(
        r"(?:order|confirmation)\s*(?:number|no\.?|#)?\s*[:#]?\s*(\d{7,12})",
        re.I,
    )
    # Backup confirmation signals for CheckoutConfirmationView_v2 (already
    # detected via the order number, so these only matter if the number scrape
    # misses). TODO(costco): verify banner wording + URL against live DOM.
    CONFIRMATION_MARKERS = ("thank you for your order", "order confirmation")
    CONFIRMATION_URL_MARKERS = ("checkoutconfirmation", "orderconfirmation")

    def _resolve_url(self, source: CostcoSource) -> str:
        return source.url or self.config.costco_product_url(source.item_number)

    def _source_label(self, source: CostcoSource) -> str:
        return f"item #{source.item_number}"

    # WebSphere Commerce stamps the signed-in member's numeric user id into the
    # WC_AUTHENTICATION_<id> session cookie; a guest session uses a negative
    # sentinel id (seen live as -1002). So an authenticated session is exactly a
    # WC_AUTHENTICATION cookie whose id is a positive integer — a signal immune to
    # the header's React hydration timing, since the cookie is set on the
    # navigation response before the nav paints. Verified live 2026-06-17: guest
    # WC_AUTHENTICATION_-1002 vs member WC_AUTHENTICATION_2436747244.
    _WC_AUTH_COOKIE_RE = re.compile(r"^WC_AUTHENTICATION_\d+$")

    def is_logged_in(self, page: "Page") -> bool:
        """True when the WC session cookie carries a real (positive) member id.

        Costco's session cookie is the reliable signal: the header keeps both the
        signed-in and signed-out menus in the DOM (toggled by CSS) and hydrates
        the member "Account" button late, so reading nav text misfires. The
        cookie is correct the instant the page loads. Falls back to the
        account-button DOM check (ACCOUNT_NAV_SELECTORS) if the jar can't be read.
        """
        try:
            cookies = page.context.cookies()
        except Exception:  # noqa: BLE001 — fall back to the DOM signal
            cookies = []
        for cookie in cookies:
            if self._WC_AUTH_COOKIE_RE.match(cookie.get("name", "")):
                return True
        return super().is_logged_in(page)

    def ensure_logged_in(self, page: "Page") -> bool:
        """Re-establish Costco's member session, silently, from the saved profile.

        Costco drops its WC_AUTHENTICATION *session* cookie on every fresh browser
        launch, so each run starts as a guest even though the profile still holds
        a live identity-provider (Azure AD B2C) SSO cookie. Hitting the logon form
        while that SSO cookie is valid bounces straight back to the storefront
        with a ``krypto`` ticket that upgrades the guest WC session to the
        member's — no credential form is shown and nothing is typed. When the SSO
        cookie has *also* expired the logon form actually appears and this stays
        logged out, so the caller bails to the manual `roomieorder login` path.

        Returns True if logged in (already, or after the silent re-auth)."""
        if self.is_logged_in(page):
            return True
        try:
            page.goto(
                f"https://www.{self.domain}/LogonForm?langId=-1&storeId=10301&catalogId=10701",
                wait_until="domcontentloaded",
            )
        except Exception:  # noqa: BLE001 — couldn't reach the logon flow; give up
            return False
        self._settle(page)
        return self.is_logged_in(page)

    def _start_checkout(self, page: "Page") -> bool:
        """Add to cart → go to cart → checkout → select payment → review.

        Costco has no one-click Buy Now: the flow is add-to-cart, then the cart,
        then a Checkout CTA, then a delivery/address confirmation, and finally —
        verified live 2026-06-17 — an explicit payment-method selection before
        Place Order activates. Role/text first for resilience, CSS ids as a
        backstop. Success is judged by *landing* on SinglePageCheckoutView, not
        by the click's return: the Checkout CTA navigates, and Playwright can
        report the click as a miss when the context tears down mid-navigation
        even though it took.
        """
        # ── add to cart ──
        if not self._click_by_role(page, ("button",), "add to cart") and not self._click_first(
            page, self.ADD_TO_CART_SELECTORS
        ):
            return False
        page.wait_for_load_state("domcontentloaded")
        self._settle(page)

        # ── go to cart → checkout ──
        # Prefer the flyout's Checkout CTA; if that doesn't land us on the
        # checkout view, go to the cart page directly and check out from there.
        self._click_by_role(page, ("button", "link"), "checkout")
        self._settle(page)
        if not self._on_checkout(page):
            # Verified live 2026-06-17: /CheckoutCartView 302s (with a krypto
            # ticket) to /CheckoutCartDisplayView, the real cart page.
            page.goto(
                f"https://www.{self.domain}/CheckoutCartView",
                wait_until="domcontentloaded",
            )
            self._settle(page)
            self._click_by_role(page, ("button", "link"), "checkout")
            self._settle(page)
            if not self._on_checkout(page):
                return False

        # ── delivery / address confirmation → review ──
        # TODO(costco): verify against live DOM — does delivery need a click?
        self._click_by_role(page, ("button", "link"), "continue")
        self._settle(page)

        # ── select the saved default payment method ──
        # Place Order is inert until a payment method is chosen, and the saved
        # card isn't reliably pre-selected, so click its radio here.
        self._select_payment_method(page)
        return True

    def _on_checkout(self, page: "Page") -> bool:
        """True once we've landed on the place-order review page.

        Patient by design: the review view can paint a beat after _settle, so an
        instantaneous read raced the land and reported a miss on a page that
        arrived a moment later. Check once, then give it a bounded window to
        paint before re-checking. Short timeout, not the step timeout: the
        PLACE_ORDER ids may drift, and the URL re-check is the drift-immune signal
        that still resolves once navigation settles. Pure detection — clicks
        nothing."""
        if self._checkout_landed(page):
            return True
        self._wait_for_any(page, self.PLACE_ORDER_SELECTORS, timeout=self._LANDING_TIMEOUT_MS)
        return self._checkout_landed(page)

    def _select_payment_method(self, page: "Page") -> bool:
        """Select the saved default card so Place Order isn't inert.

        Clicks the saved-card radio (``paymentReviewRadio``) unless it already
        reads ``aria-checked='true'`` — re-clicking a committed radio is a
        no-op, but skipping it keeps the flow idempotent and avoids fighting a
        selection the page already made. Best-effort: returns True once a radio
        is selected/clicked, False if no candidate resolves (the caller still
        proceeds to the review screenshot so the miss is visible)."""
        for sel in self.PAYMENT_METHOD_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                if (loc.get_attribute("aria-checked", timeout=2_000) or "").lower() == "true":
                    return True
                loc.click(timeout=5_000)
                self._settle(page)
                return True
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        return False

    # Hard cap on remove-clicks so a cart that won't drain (selector drift, a
    # confirm modal we don't dismiss) can't spin the step timeout — far above any
    # real cart's line count.
    _MAX_CART_LINES = 30

    def _reset_cart(self, page: "Page") -> None:
        """Remove every line from the Costco cart so checkout holds only our item.

        Costco's cart is server-side and shared across every launch of the saved
        profile, and the buy flow only ever *adds* a line, so without this a
        stale item from a prior dry-run/failed run rides along into a live Place
        Order (which checks out the whole cart). Navigate to the cart and click
        the first remove control until none remain; lines re-index after each
        removal, so the first match is always valid. Best-effort: any failure
        leaves the cart untouched and the buy proceeds — this only ever removes,
        never blocks the order.
        """
        try:
            page.goto(
                f"https://www.{self.domain}/CheckoutCartView",
                wait_until="domcontentloaded",
            )
        except Exception:  # noqa: BLE001 — couldn't reach the cart; skip the reset
            return
        self._settle(page)
        for _ in range(self._MAX_CART_LINES):
            if not self._click_first(page, self.REMOVE_ITEM_SELECTORS):
                return  # cart already empty / no remove control → done
            self._confirm_if_visible(page, self.CART_CONFIRM_SELECTORS)
            self._settle(page)

    def _confirm_if_visible(self, page: "Page", selectors: tuple[str, ...]) -> None:
        """Click a confirm-modal button, but only if it actually renders visible.

        Costco's confirm-modal markup lives hidden in the DOM whether or not a
        modal is open, so a plain ``count()``/click would block on a never-shown
        element. Gate on a short visibility wait: present-and-shown → click,
        present-but-hidden (the common case) → time out fast and move on."""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=1_500)
                loc.click(timeout=3_000)
                return
            except Exception:  # noqa: BLE001 — not shown / not clickable; skip
                continue


class AmazonPurchaser(BasePurchaser[AmazonSource]):
    """Amazon — the fallback when Costco can't fulfil an item.

    Restored from the pre-costco-switch buy flow (commit 9057046^). The DOM
    moves fast and these selectors are multi-year-old guesses, so every one is
    flagged ``TODO(amazon)`` and must be confirmed via
    `roomieorder dump-dom --provider amazon <item>` before any live order.
    """

    PROVIDER = "amazon"
    STORE_NAME = "Amazon"

    # Price block — verified against the live PDP dump (2026-06-21): the modern
    # corePrice container and the generic a-price span both carry the amount. The
    # legacy priceblock_* ids stay as fallbacks for older PDP variants.
    PRICE_SELECTORS = (
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#corePrice_feature_div span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "span.a-price span.a-offscreen",
    )
    # The modern PDP emits no product/price <meta> tags (none in the live dump);
    # kept only as a best-effort fallback for pages that still render them.
    PRICE_META_SELECTORS = (
        "meta[property='product:price:amount']",
        "meta[property='og:price:amount']",
        "meta[itemprop='price']",
    )
    # Add to Cart verified against the live PDP dump (2026-06-21). The Buy Now
    # button is injected by the turbo-checkout widget after load, so it isn't in
    # the static DOM — Amazon's own turboState declares its initiate selector as
    # [id^=buy-now-button], so lead with that and keep the exact / legacy ids as
    # fallbacks.
    BUY_NOW_SELECTORS = (
        "[id^='buy-now-button']",
        "#buy-now-button",
        "input[name='submit.buy-now']",
    )
    ADD_TO_CART_SELECTORS = (
        "#add-to-cart-button",
        "input[name='submit.add-to-cart']",
    )
    # TODO(amazon): verify against live DOM — final place-order button.
    PLACE_ORDER_SELECTORS = (
        "#placeYourOrder",
        "input[name='placeYourOrder1']",
        "#submitOrderButtonId input",
        "#bottomSubmitOrderButtonId input",
    )
    # TODO(amazon): verify against live DOM — order-confirmation grand-total.
    ORDER_TOTAL_SELECTORS = (
        "#subtotals-marketplace-table .grand-total-price",
        "td.grand-total-price",
        "#od-subtotals .a-color-price",
    )
    # TODO(amazon): verify against live DOM — account nav (reads "Hello, sign in"
    # when logged out).
    ACCOUNT_NAV_SELECTORS = (
        "#nav-link-accountList-nav-line-1",
        "#nav-link-accountList",
    )
    # Markers that mean Amazon wants a human: CAPTCHA, OTP, "verify it's you".
    # TODO(amazon): verify against live DOM.
    CHALLENGE_MARKERS = (
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
    # A logged-out profile gets bounced to the sign-in wall at checkout.
    # TODO(amazon): verify against live DOM.
    SIGNIN_MARKERS = (
        "/ap/signin",
        "sign in or create account",
        "enter mobile number or email",
    )
    # TODO(amazon): verify against live DOM — currently-unavailable wording.
    OUT_OF_STOCK_MARKERS = (
        "currently unavailable",
        "out of stock",
        "we don't know when or if this item will be back in stock",
        "temporarily out of stock",
    )
    # TODO(amazon): verify against live DOM — not-found / dogs-of-amazon page.
    NOT_FOUND_MARKERS = (
        "page not found",
        "we couldn't find that page",
        "looking for something",
        "sorry! we couldn't find that page",
    )
    # Amazon order numbers look like 123-4567890-1234567.
    ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
    # Order-confirmation success signals — the thank-you page renders a banner
    # and lands on a /thankyou URL even when the dashed order number isn't in the
    # scraped body text (the false-needs_review case). TODO(amazon): verify the
    # banner wording against live DOM; "Order placed, thanks!" is observed.
    CONFIRMATION_MARKERS = (
        "order placed, thank",      # "Order placed, thanks!" (observed live)
        "thank you, your order",
        "your order has been placed",
        "placed your order",
    )
    CONFIRMATION_URL_MARKERS = (
        "/gp/buy/thankyou",
        "thankyou",
    )

    def _resolve_url(self, source: AmazonSource) -> str:
        return source.url or self.config.amazon_product_url(source.asin)

    def _source_label(self, source: AmazonSource) -> str:
        return f"ASIN {source.asin}"

    # ─────────── session persistence ───────────
    # Amazon issues its auth cookies (at-main/x-main/sess-at-main) as *session*
    # cookies unless "Keep me signed in" is set, and Chrome never flushes session
    # cookies to the on-disk profile — so a hand-login that looks signed-in
    # reloads signed-out and the worker hits the sign-in wall. That checkbox is
    # just UI for the server-side ``rememberMe=true`` param on POST /ap/signin, and
    # Amazon A/B-tests it away (it isn't rendered on every flow). So instead of
    # depending on the box, force the param during login: whenever a sign-in form
    # appears we tick an existing rememberMe control or inject a hidden
    # rememberMe=true input, so the operator's submit carries it. Amazon then
    # issues *persistent* at-main/x-main cookies, which Chrome stores in the
    # profile the normal way — no cookie juggling, rolling lifetime of months.

    def _remember_me_js(self) -> str:
        """Core ``ensureRememberMe()`` — tick/inject the rememberMe form param.

        Returned separately from the init-script bootstrap so the offline
        browser-fixture net can ``page.evaluate`` it against a static sign-in
        form and assert the resulting DOM."""
        return """
        function ensureRememberMe() {
          var box = document.querySelector('input[name="rememberMe"]');
          if (box) {
            if (!box.checked) {
              box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }
            console.debug('[roomieorder] rememberMe: checked existing box');
            return;
          }
          var form = document.querySelector(
            'form[name="signIn"], form[action*="/ap/signin"]'
          );
          if (form && !form.querySelector('input[name="rememberMe"]')) {
            var hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = 'rememberMe';
            hidden.value = 'true';
            form.appendChild(hidden);
            console.debug('[roomieorder] rememberMe: injected hidden input');
          }
        }
        """

    def _login_init_script(self) -> Optional[str]:
        # Run ensureRememberMe now, on DOMContentLoaded, and from a
        # MutationObserver — the operator may reach the password form via a late
        # or SPA-rendered step, so a one-shot run can miss it.
        return (
            self._remember_me_js()
            + """
        ensureRememberMe();
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', ensureRememberMe);
        }
        new MutationObserver(ensureRememberMe).observe(
          document.documentElement, { childList: true, subtree: true }
        );
        """
        )

    def _start_checkout(self, page: "Page") -> bool:
        """Click Buy Now; fall back to Add to Cart → Proceed to checkout.
        TODO(amazon): verify against live DOM — every step below.
        """
        if self._click_first(page, self.BUY_NOW_SELECTORS):
            return True
        if not self._click_first(page, self.ADD_TO_CART_SELECTORS):
            return False
        # Cart interstitial → checkout.
        page.wait_for_load_state("domcontentloaded")
        for sel in ("#sc-buy-box-ptc-button", "input[name='proceedToRetailCheckout']"):
            if self._click_first(page, (sel,)):
                return True
        # Some flows expose a role-named link instead.
        try:
            page.get_by_role(
                "link", name=re.compile("proceed to checkout", re.I)
            ).first.click(timeout=5_000)
            return True
        except Exception:  # noqa: BLE001
            return False
