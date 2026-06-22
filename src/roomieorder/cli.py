"""Command-line entry point.

Subcommands:

* ``serve``       — run the FastAPI intake service + worker loop (the daemon).
* ``init-db``     — create the SQLite schema (idempotent).
* ``catalog``     — print the catalog.
* ``queue``       — show recent queue rows.
* ``test-notify`` — emit a test message via the configured notifier.
* ``test-sheet``  — append a test row to the configured Google Sheet.
* ``login``        — open the profile headed to sign into Costco by hand.
* ``dry-run KEY`` — drive one item to its review page and screenshot, no order.
* ``dump-dom KEY`` — read-only DOM dump + selector probe for bring-up.
* ``trace-order KEY`` — DRY_RUN walk dumping DOM + probe + screenshot per step.
* ``verify-selectors`` — probe live pages for stale buy-flow selectors.
* ``doctor``      — one-shot, read-only health check of every subsystem
                    (``--check-login`` adds a per-store signed-in probe).
* ``prune-shots`` — delete old screenshots/DOM dumps from the shots dir.
* ``failures``    — list recent failed/blocked orders and their screenshots.
* ``retry ID``    — re-enqueue a failed row for another attempt.
* ``resume`` / ``pause`` / ``status`` — manage the worker-pause flag.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional

import click

from roomieorder.catalog import CatalogItem, load_catalog
from roomieorder.config import Config, load_config
from roomieorder.guards import check_price_ceiling, check_spend_cap
from roomieorder.notify import build_notifier
from roomieorder.retention import prune_shots
from roomieorder.sheets import build_sheets
from roomieorder.store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _purchaser_for(config: Config, provider: str) -> object:
    """Build the purchaser for ``provider`` with its own profile dir + domain."""
    from roomieorder.purchase import build_purchaser

    return build_purchaser(config, provider)


def _source_for(item: CatalogItem, provider: str) -> object:
    """The catalog source block for ``provider``, or raise if not declared."""
    source = item.amazon if provider == "amazon" else item.costco
    if source is None:
        raise click.ClickException(f"item has no {provider} source declared")
    return source


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """roomieorder — HA button → Costco order → Google Sheets."""
    _setup_logging(verbose)


@main.command()
def serve() -> None:
    """Run the intake service and worker loop."""
    import uvicorn

    from roomieorder.main import create_app

    config = load_config()
    app = create_app(config)
    click.echo(f"serving on http://{config.host}:{config.port} (dry_run={config.dry_run})")
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


@main.command(name="init-db")
def init_db() -> None:
    """Create the SQLite schema (idempotent)."""
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    store.close()
    click.echo(f"initialized {config.db_path}")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
def catalog(as_json: bool) -> None:
    """Print all items in the catalog."""
    config = load_config()
    items = load_catalog(config.catalog_path)
    if as_json:
        import json

        click.echo(json.dumps({k: v.model_dump() for k, v in items.items()}, indent=2))
        return
    for key, item in items.items():
        click.echo(f"{key:16} {item.title}  qty={item.qty} cooldown={item.cooldown_days}d")
        if item.costco is not None:
            c = item.costco
            click.echo(
                f"{'':16} costco  item#={c.item_number} "
                f"expected=${c.expected_price:.2f} ceiling=${c.price_ceiling:.2f}"
            )
        if item.amazon is not None:
            a = item.amazon
            click.echo(
                f"{'':16} amazon  asin={a.asin} "
                f"expected=${a.expected_price:.2f} ceiling=${a.price_ceiling:.2f}"
            )


@main.command()
@click.option("--limit", default=20, show_default=True)
def queue(limit: int) -> None:
    """Show recent queue rows."""
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    rows = store.list_queue(limit)
    if not rows:
        click.echo("(queue empty)")
        return
    for r in rows:
        total = f"${r.order_total:.2f}" if r.order_total is not None else "-"
        click.echo(
            f"#{r.id:<4} {r.status:<16} {r.item_key:<16} {total:<9} "
            f"{r.created_at.isoformat()}  {r.notes[:60]}"
        )
    store.close()


@main.command(name="test-notify")
@click.option("--message", default="roomieorder test notification 🦅", show_default=True)
def test_notify(message: str) -> None:
    """Emit a test message via the configured notifier."""
    config = load_config()
    notifier = build_notifier(config)
    ok = notifier.send(message)
    click.echo("sent" if ok else "FAILED — check OPENCLAW_* env and the openclaw binary")
    raise SystemExit(0 if ok else 1)


@main.command(name="test-sheet")
def test_sheet() -> None:
    """Append a test row to the configured Google Sheet.

    Verifies the Sheets integration end-to-end (auth, share, append) without
    placing an order. Refuses up front when Sheets is unconfigured, since
    ``build_sheets`` would otherwise return a no-op logger that silently
    "succeeds" — exactly the trap that makes a misconfigured sheet look fine.
    """
    config = load_config()
    if not config.sheets_enabled:
        click.echo(
            "Sheets not configured — set ROOMIEORDER_SHEET_ID and "
            "GOOGLE_SERVICE_ACCOUNT_JSON, then share the sheet with the "
            "service account's email."
        )
        raise SystemExit(1)
    sheets = build_sheets(config)
    ok = sheets.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "item_key": "test-sheet",
            "title": "roomieorder test row 🦅",
            "status": "test",
            "notes": "appended by `roomieorder test-sheet` — safe to delete",
        }
    )
    click.echo(
        f"appended a test row to sheet {config.sheet_id} (tab {config.sheet_tab!r})"
        if ok
        else "FAILED — run with -v to see the gspread error (auth, sharing, sheet id)"
    )
    raise SystemExit(0 if ok else 1)


_PROVIDER_OPT = click.option(
    "--provider",
    type=click.Choice(["costco", "amazon"]),
    default="costco",
    show_default=True,
    help="Which store to act on.",
)


@main.command()
@_PROVIDER_OPT
def login(provider: str) -> None:
    """Open the store profile in a real browser so you can sign in by hand.

    roomieorder never stores a store credential — the session lives in the
    per-store persistent Chromium profile. Run this once per store; later
    ``dry-run`` / live orders reuse the saved cookies. Disable 2FA on the
    account first: the automated buy flow halts on any login challenge.
    """
    config = load_config()
    purchaser = _purchaser_for(config, provider)

    def wait_for_operator(page: object) -> None:
        click.echo(f"opened {purchaser.domain} on profile {purchaser.profile_dir}")  # type: ignore[attr-defined]
        click.pause(
            info=(
                "log in normally — roomieorder keeps the session signed in for "
                "you — then press any key here once the page shows you signed in…"
            )
        )

    purchaser.login(wait_for_operator)  # type: ignore[attr-defined]

    # The in-window check can't tell a persisted session from one that only lives
    # in memory until the browser closes (Amazon's session-scoped auth cookies),
    # so reload the saved profile from disk and verify there — that is the state
    # the worker will actually launch into, and a logged-in reload is the proof
    # the persistent ("remember me") cookies were written.
    if purchaser.verify_session():  # type: ignore[attr-defined]
        click.echo("✓ signed in — session persisted (survives restarts and the worker)")
    else:
        click.echo(
            "⚠️  the saved profile reloads signed OUT — the session didn’t persist. "
            f"Re-run `roomieorder login --provider {provider}`; if it recurs, the "
            "sign-in form’s rememberMe field needs re-capturing."
        )


@main.command(name="dry-run")
@click.argument("item_key")
@_PROVIDER_OPT
def dry_run(item_key: str, provider: str) -> None:
    """Navigate ITEM_KEY to its review page and screenshot — never orders.

    Targets a single store (``--provider``) so each leg of the Costco→Amazon
    fallback can be brought up independently. Forces DRY_RUN regardless of the
    env flag, so this is always safe to run while filling out the §8 checklist.
    """
    config = load_config()
    config = config.model_copy(update={"dry_run": True})
    items = load_catalog(config.catalog_path)
    item = items.get(item_key)
    if item is None:
        raise click.ClickException(f"unknown item_key: {item_key} (have: {', '.join(items)})")
    source = _source_for(item, provider)

    store = Store(config.db_path)
    store.init_db()
    purchaser = _purchaser_for(config, provider)

    def proceed_check(live_price: float):  # type: ignore[no-untyped-def]
        ceiling = check_price_ceiling(item.title, source.price_ceiling, live_price)  # type: ignore[attr-defined]
        if not ceiling.ok:
            return ceiling
        return check_spend_cap(store, config, live_price * item.qty)

    click.echo(f"dry-run {item_key} ({provider}) → {purchaser._resolve_url(source)}")  # type: ignore[attr-defined]
    result = purchaser.buy(item_key, item, source, proceed_check)  # type: ignore[attr-defined]
    click.echo(f"status:      {result.status}")
    click.echo(f"unit_price:  {result.unit_price}")
    click.echo(f"order_total: {result.order_total}")
    click.echo(f"message:     {result.message}")
    if result.screenshot:
        click.echo(f"screenshot:  {result.screenshot}")
    store.close()


@main.command(name="dump-dom")
@click.argument("item_key")
@_PROVIDER_OPT
def dump_dom(item_key: str, provider: str) -> None:
    """Open ITEM_KEY's product page read-only and dump the rendered DOM.

    A bring-up aid for confirming the live-DOM selectors: navigates to the
    product page for ``--provider`` (reusing that store's logged-in profile) and
    writes the rendered HTML, a probe of every candidate selector, and a
    screenshot to the shots dir, then prints the probe. Never adds to cart or
    places an order.
    """
    config = load_config()
    items = load_catalog(config.catalog_path)
    item = items.get(item_key)
    if item is None:
        raise click.ClickException(f"unknown item_key: {item_key} (have: {', '.join(items)})")
    source = _source_for(item, provider)

    purchaser = _purchaser_for(config, provider)
    click.echo(f"dump-dom {item_key} ({provider}) → {purchaser._resolve_url(source)}")  # type: ignore[attr-defined]
    result = purchaser.dump_dom(item_key, item, source)  # type: ignore[attr-defined]
    click.echo(f"logged_in:  {result.logged_in}")
    click.echo(f"blocked:    {result.blocked}")
    click.echo(f"challenge:  {result.challenge}")
    click.echo(f"html:       {result.html}")
    click.echo(f"probe:      {result.probe}")
    click.echo(f"screenshot: {result.screenshot}")
    click.echo("")
    click.echo(result.summary)


# Selector groups worth a one-line PASS/MISS digest per checkpoint in the
# trace-order table — the buy-flow groups, skipping the noisier price-meta/signin.
_DIGEST_GROUPS = ("price", "add-to-cart", "buy-now", "place-order", "order-total")


@main.command(name="trace-order")
@click.argument("item_key")
@_PROVIDER_OPT
def trace_order(item_key: str, provider: str) -> None:
    """Walk ITEM_KEY through the whole buy flow, dumping every step — never orders.

    Forces DRY_RUN (like ``dry-run``) so it always halts at the review page
    *before* Place Order, then attaches a tracer that writes a rendered DOM, a
    selector probe, and a screenshot at each checkpoint — product page, cart,
    cart view, delivery, payment, and the review page. Unlike ``dump-dom`` (which
    stops at the product page), this reaches the checkout/review surface where the
    ``place-order``/``order-total``/payment selectors finally render, so they
    become discoverable. Hits live store pages, so it's operator-run, not CI.
    """
    from roomieorder.purchase import FlowTracer, new_run_id

    config = load_config()
    config = config.model_copy(update={"dry_run": True})
    items = load_catalog(config.catalog_path)
    item = items.get(item_key)
    if item is None:
        raise click.ClickException(f"unknown item_key: {item_key} (have: {', '.join(items)})")
    source = _source_for(item, provider)

    store = Store(config.db_path)
    store.init_db()
    purchaser = _purchaser_for(config, provider)

    def proceed_check(live_price: float):  # type: ignore[no-untyped-def]
        ceiling = check_price_ceiling(item.title, source.price_ceiling, live_price)  # type: ignore[attr-defined]
        if not ceiling.ok:
            return ceiling
        return check_spend_cap(store, config, live_price * item.qty)

    tracer = FlowTracer(purchaser, item_key, run_id=new_run_id())  # type: ignore[arg-type]
    click.echo(f"trace-order {item_key} ({provider}) → {purchaser._resolve_url(source)}")  # type: ignore[attr-defined]
    result = purchaser.buy(item_key, item, source, proceed_check, tracer=tracer)  # type: ignore[attr-defined]
    store.close()

    click.echo(f"status:      {result.status}")
    click.echo(f"unit_price:  {result.unit_price}")
    click.echo(f"order_total: {result.order_total}")
    click.echo(f"message:     {result.message}")
    click.echo("")
    click.echo(f"steps ({len(tracer.steps)}):")
    any_artifact = False
    for step in tracer.steps:
        hits = _group_hits(step.summary)
        digest = " ".join(
            f"{g}={'ok' if hits.get(g) else 'MISS'}"
            for g in _DIGEST_GROUPS
            if g in hits
        )
        click.echo(f"  {step.idx:02d} {step.name:18} {step.url}")
        click.echo(f"     {digest}")
        if step.probe:
            any_artifact = True
            click.echo(f"     probe: {step.probe}")
        if step.html:
            click.echo(f"     dom:   {step.html}")
        if step.screenshot:
            click.echo(f"     shot:  {step.screenshot}")
    if any_artifact:
        click.echo("")
        click.echo("For any group still MISS at a checkout step, Read that step's *_dom.html to find the live selector.")


# Statuses that mean an order didn't cleanly place — what `failures` surfaces.
_TROUBLE_STATUSES = (
    "failed",
    "needs_review",
    "challenge",
    "blocked",
    "spend_capped",
    "price_blocked",
    "unavailable",
)

# Selector groups checkable on the *product page* (where dump-dom stops). The
# place-order/order-total groups only render at checkout/confirmation, so
# verify-selectors can't reach them — see purchase.BasePurchaser._probe_groups.
_PRODUCT_PAGE_GROUPS = ("price", "price-meta", "add-to-cart", "buy-now")

_COUNT_RE = re.compile(r"count=(\d+)")
_READ_PRICE_RE = re.compile(r"^read_price:\s*(.+)$")


def _group_hits(summary: str) -> dict[str, bool]:
    """Parse a ``_probe_selectors`` summary into ``{group: any_selector_matched}``.

    A group ``[name]`` is a hit when at least one of its candidate selectors
    resolved to ``count>0`` on the live page; a miss when every guess was
    ``count=0``. Pure string parsing so it's unit-testable without a browser.
    """
    hits: dict[str, bool] = {}
    current: Optional[str] = None
    for raw in summary.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            hits.setdefault(current, False)
            continue
        if current is not None:
            match = _COUNT_RE.search(line)
            if match and int(match.group(1)) > 0:
                hits[current] = True
    return hits


def _read_price_from_summary(summary: str) -> Optional[str]:
    """Pull the resolved ``read_price`` line out of a probe summary, or None."""
    for raw in summary.splitlines():
        match = _READ_PRICE_RE.match(raw.strip())
        if match:
            value = match.group(1).strip()
            return None if value == "None" else value
    return None


@main.command(name="verify-selectors")
@click.argument("item_key", required=False)
@_PROVIDER_OPT
def verify_selectors(item_key: Optional[str], provider: str) -> None:
    """Probe live product pages and report which buy-flow selectors still match.

    Opens each item's product page read-only — the same footprint as
    ``dump-dom`` (reuses the logged-in profile, never adds to cart or orders) —
    and reports PASS/MISS per item for the selectors reachable on a product
    page: the price (visible CSS, meta tags, or JSON-LD) and add-to-cart. The
    place-order/order-total selectors only exist at checkout, so they're out of
    scope here. Hits live store pages, so this is operator-run and intentionally
    not part of CI. With no ITEM_KEY, checks every item that declares a
    ``--provider`` source.
    """
    config = load_config()
    items = load_catalog(config.catalog_path)

    if item_key is not None:
        if item_key not in items:
            raise click.ClickException(
                f"unknown item_key: {item_key} (have: {', '.join(items)})"
            )
        targets = {item_key: items[item_key]}
    else:
        targets = {
            key: item
            for key, item in items.items()
            if (item.amazon if provider == "amazon" else item.costco) is not None
        }
    if not targets:
        raise click.ClickException(f"no items declare a {provider} source")

    purchaser = _purchaser_for(config, provider)
    any_miss = False
    for key, item in targets.items():
        source = _source_for(item, provider)
        result = purchaser.dump_dom(key, item, source)  # type: ignore[attr-defined]
        hits = _group_hits(result.summary)
        price = _read_price_from_summary(result.summary)
        cart_group = "buy-now" if provider == "amazon" else "add-to-cart"
        cart_ok = hits.get(cart_group, False) or hits.get("add-to-cart", False)
        price_ok = price is not None or hits.get("price", False) or hits.get("price-meta", False)

        if result.blocked:
            verdict = "BLOCKED (Akamai — can't verify; wait it out / rotate)"
        elif result.challenge:
            verdict = "CHALLENGE (can't verify — clear it manually)"
        elif not result.logged_in and not price_ok:
            verdict = "LOGGED-OUT (sign in, then retry)"
        elif price_ok and cart_ok:
            verdict = "PASS"
        else:
            verdict = "MISS"
            any_miss = True

        click.echo(
            f"{key:18} price={price or '-':>8}  add-to-cart="
            f"{'ok' if cart_ok else 'MISS':<4}  logged_in={result.logged_in}  → {verdict}"
        )
        if result.probe:
            click.echo(f"{'':18} probe: {result.probe}")
        if result.html:
            click.echo(f"{'':18} dom:   {result.html}")

    if any_miss:
        click.echo("")
        click.echo("Some selectors missed — Read the dom/probe artifacts above to find the live ones.")
        raise SystemExit(1)


@main.command()
@click.option(
    "--check-login",
    is_flag=True,
    help="Also launch each store profile read-only and report whether it's still "
    "signed in (needs a graphical session; slower).",
)
def doctor(check_login: bool) -> None:
    """Print a one-shot, read-only health check of every subsystem.

    By default never launches a browser or touches a store, so it's safe and
    instant. Reports config/anti-bot, the graphical session the worker needs, the
    per-store profiles, the DB/queue, and the catalog. Exits non-zero when a hard
    check fails (a pinned Chrome that doesn't exist, an unopenable DB, an
    unparseable catalog), so it doubles as a smoke test.

    ``--check-login`` adds a read-only session probe: it relaunches each store's
    saved profile and reports LOGGED-IN / LOGGED-OUT (reusing the buy flow's
    ``verify_session``) so an expired session is caught here instead of at the
    next real order. It opens a browser and needs a graphical session.
    """
    config = load_config()
    hard_fail = False

    def line(state: str, label: str, detail: str) -> None:
        click.echo(f"{state:4} {label:14} {detail}")

    # ── config / safety ──
    line("ok", "dry_run", str(config.dry_run))
    line("ok", "daily_cap", f"${config.daily_cap:.2f}")
    line("ok", "timeouts", f"step={config.step_timeout_ms}ms  landing={config.landing_timeout_ms}ms")
    line("ok", "intake", f"{config.host}:{config.port}  token={'set' if config.intake_token else 'none'}")
    line(
        "ok" if config.sheets_enabled else "warn",
        "sheets",
        "configured" if config.sheets_enabled else "disabled (no sheet id / service account)",
    )
    line(
        "ok" if config.notify_enabled else "warn",
        "notify",
        "configured" if config.notify_enabled else "disabled (no OPENCLAW_TARGET)",
    )

    # ── browser / anti-bot (Akamai keys on a *real* Chrome — AGENTS.md §3) ──
    if config.chrome_path:
        ok = os.path.isfile(config.chrome_path) and os.access(config.chrome_path, os.X_OK)
        hard_fail = hard_fail or not ok
        line("ok" if ok else "FAIL", "chrome", f"pinned {config.chrome_path}" + ("" if ok else " — NOT EXECUTABLE"))
    elif config.chrome_channel:
        found = shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chrome")
        line(
            "ok" if found else "warn",
            "chrome",
            f"channel={config.chrome_channel} ({found})" if found else f"channel={config.chrome_channel} — no system Chrome on PATH",
        )
    else:
        line("warn", "chrome", "no path/channel — falls back to bundled Chromium (an Akamai tell)")

    # ── graphical session (the worker drives a headed browser) ──
    # Check the display that matches the configured mode: a Wayland-first box
    # legitimately has no X11 DISPLAY, so the old "no DISPLAY" warn was a false
    # alarm there. Cross-check ROOMIEORDER_WAYLAND against what's actually set.
    wl = os.environ.get("WAYLAND_DISPLAY")
    x11 = os.environ.get("DISPLAY")
    if config.wayland:
        if wl:
            line("ok", "display", f"wayland {wl}")
        else:
            extra = f" (X11 DISPLAY={x11} present)" if x11 else ""
            line("warn", "display", f"ROOMIEORDER_WAYLAND=true but WAYLAND_DISPLAY unset{extra}")
    else:
        if x11:
            line("ok", "display", f"x11 {x11}")
        elif wl:
            line("warn", "display", f"WAYLAND_DISPLAY={wl} present but ROOMIEORDER_WAYLAND is false — set it?")
        else:
            line("warn", "display", "no DISPLAY/WAYLAND_DISPLAY — the worker can't run a headed browser here")

    # ── per-store profiles (login can't be confirmed offline — AGENTS.md §2) ──
    for label, path in (("costco", config.costco_profile_dir), ("amazon", config.amazon_profile_dir)):
        if path.exists():
            stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            present = "present" if check_login else "present (login unverified — run dump-dom)"
            line("ok", f"profile/{label}", f"{present}, mtime {stamp}")
        else:
            line("warn", f"profile/{label}", f"missing {path} — run `roomieorder login --provider {label}`")
            continue
        if not check_login:
            continue
        # Read-only session probe: relaunch the saved profile and report whether
        # it reloads signed in. Best-effort — a launch failure (no display, no
        # Chrome) is a warn, not a hard fail, so the rest of doctor still reports.
        try:
            logged_in = _purchaser_for(config, label).verify_session()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the check
            line("warn", f"login/{label}", f"probe failed: {str(exc).splitlines()[0][:80]}")
            continue
        line(
            "ok" if logged_in else "warn",
            f"login/{label}",
            "LOGGED-IN" if logged_in else f"LOGGED-OUT — run `roomieorder login --provider {label}`",
        )

    # ── DB / queue ──
    try:
        store = Store(config.db_path)
        store.init_db()
        paused = store.is_paused()
        trouble = sum(1 for r in store.list_queue(200) if r.status in _TROUBLE_STATUSES)
        line(
            "warn" if paused else "ok",
            "worker",
            f"paused={paused} {store.pause_reason()}" if paused else "running (not paused)",
        )
        line("ok", "queue", f"pending={store.pending_count()}  recent_trouble={trouble}")
        # 24h spend vs the cap — the same window check_spend_cap guards on, so the
        # operator sees how much headroom is left before the next order is capped.
        spent = store.spend_since(24.0)
        near_cap = config.daily_cap > 0 and spent >= 0.9 * config.daily_cap
        line(
            "warn" if near_cap else "ok",
            "spend",
            f"${spent:.2f} / ${config.daily_cap:.2f} (24h)" + ("  — near cap" if near_cap else ""),
        )
        store.close()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the check
        hard_fail = True
        line("FAIL", "db", f"{config.db_path}: {exc}")

    # ── catalog ──
    try:
        items = load_catalog(config.catalog_path)
        no_source = [k for k, v in items.items() if v.costco is None and v.amazon is None]
        line("ok", "catalog", f"{len(items)} items" + (f"  (no source: {', '.join(no_source)})" if no_source else ""))
    except Exception as exc:  # noqa: BLE001
        hard_fail = True
        line("FAIL", "catalog", f"{config.catalog_path}: {exc}")

    if hard_fail:
        raise SystemExit(1)


@main.command(name="prune-shots")
@click.option(
    "--days",
    type=int,
    default=None,
    help="Delete shots older than this many days (default: ROOMIEORDER_SHOTS_RETENTION_DAYS).",
)
def prune_shots_cmd(days: Optional[int]) -> None:
    """Delete old screenshots / DOM dumps from the shots dir.

    The buy flow writes a PNG (and dump-dom an HTML + probe) on every attempt
    with no rotation, so the shots dir grows unbounded. The worker prunes
    automatically; this runs the same sweep by hand. ``--days`` overrides the
    configured retention window; 0 (or an unset window) disables pruning.
    """
    config = load_config()
    retention = days if days is not None else config.shots_retention_days
    if retention <= 0:
        click.echo(
            "retention disabled — pass --days N or set ROOMIEORDER_SHOTS_RETENTION_DAYS > 0"
        )
        return
    removed = prune_shots(config.shots_dir, retention)
    click.echo(f"pruned {removed} file(s) older than {retention}d from {config.shots_dir}")


@main.command()
@click.option("--limit", default=10, show_default=True, help="Max rows / screenshots to show.")
def failures(limit: int) -> None:
    """List recent failed/blocked orders and the newest screenshots to read.

    One place to start triage: the recent queue rows that didn't cleanly place
    (failed/needs_review/challenge/blocked), each with its notes, plus the
    newest artifacts in the shots dir (``*.png`` / ``*_dom.html`` /
    ``*_probe.txt``) by full path so they can be opened directly.
    """
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    rows = [r for r in store.list_queue(500) if r.status in _TROUBLE_STATUSES][:limit]
    if not rows:
        click.echo("(no recent failures)")
    for r in rows:
        click.echo(f"#{r.id:<4} {r.status:<13} {r.item_key:<16} {r.updated_at.isoformat()}")
        if r.notes:
            click.echo(f"      {r.notes[:100]}")
    store.close()

    click.echo("")
    shots = config.shots_dir
    click.echo(f"shots dir: {shots}")
    if not shots.exists():
        click.echo("  (does not exist yet)")
        return
    files = sorted(
        (p for p in shots.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    if not files:
        click.echo("  (empty)")
    for f in files:
        click.echo(f"  {f}")


@main.command()
@click.argument("row_id", type=int)
@click.option("--resume", "do_resume", is_flag=True, help="Also clear the worker-pause flag.")
def retry(row_id: int, do_resume: bool) -> None:
    """Re-enqueue a failed queue row for another attempt.

    Refuses rows whose order may already have been placed (``needs_review``,
    ``placed``): re-ordering those risks a double charge — confirm against the
    store account and handle by hand instead (see store.MAX_ATTEMPTS). On a safe
    status it enqueues a fresh ``pending`` row for the same item.
    """
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    row = next((r for r in store.list_queue(1000) if r.id == row_id), None)
    if row is None:
        store.close()
        raise click.ClickException(f"no queue row #{row_id}")
    if row.status in {"needs_review", "placed"}:
        store.close()
        raise click.ClickException(
            f"refusing to retry #{row_id} ({row.status}) — the order may already have been "
            "placed; confirm against the store account before re-ordering"
        )
    new_id = store.enqueue(row.item_key, row.requester)
    click.echo(f"re-enqueued {row.item_key} as #{new_id} (from #{row_id} {row.status})")
    if do_resume:
        store.set_paused(False)
        click.echo("worker resumed")
    store.close()


@main.command()
def resume() -> None:
    """Clear the worker-pause flag after handling a challenge/failure."""
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    store.set_paused(False)
    store.close()
    click.echo("worker resumed")


@main.command()
@click.option("--reason", default="paused via CLI", show_default=True)
def pause(reason: str) -> None:
    """Manually pause the worker."""
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    store.set_paused(True, reason)
    store.close()
    click.echo(f"worker paused: {reason}")


@main.command()
def status() -> None:
    """Show worker pause state and pending count."""
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    click.echo(f"dry_run: {config.dry_run}")
    click.echo(f"paused:  {store.is_paused()}  {store.pause_reason()}")
    click.echo(f"pending: {store.pending_count()}")
    store.close()


if __name__ == "__main__":
    main()
