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
* ``resume`` / ``pause`` / ``status`` — manage the worker-pause flag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import click

from roomieorder.catalog import CatalogItem, load_catalog
from roomieorder.config import Config, load_config
from roomieorder.guards import check_price_ceiling, check_spend_cap
from roomieorder.notify import build_notifier
from roomieorder.sheets import build_sheets
from roomieorder.store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _purchaser_for(config: Config, provider: str) -> object:
    """Build the purchaser for ``provider`` with its own profile dir + domain."""
    from roomieorder.purchase import AmazonPurchaser, CostcoPurchaser

    if provider == "amazon":
        return AmazonPurchaser(
            config, profile_dir=config.amazon_profile_dir, domain=config.amazon_domain
        )
    return CostcoPurchaser(
        config, profile_dir=config.costco_profile_dir, domain=config.costco_domain
    )


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
        click.pause(info="log in, then press any key here to save the session and close…")
        if purchaser.is_logged_in(page):  # type: ignore[attr-defined]
            click.echo("✓ signed in — session saved to the profile")
        else:
            click.echo(
                f"⚠️  still looks signed out — re-run `roomieorder login --provider {provider}`"
            )

    purchaser.login(wait_for_operator)  # type: ignore[attr-defined]


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
    click.echo(f"challenge:  {result.challenge}")
    click.echo(f"html:       {result.html}")
    click.echo(f"probe:      {result.probe}")
    click.echo(f"screenshot: {result.screenshot}")
    click.echo("")
    click.echo(result.summary)


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
