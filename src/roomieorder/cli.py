"""Command-line entry point.

Subcommands:

* ``serve``       — run the FastAPI intake service + worker loop (the daemon).
* ``init-db``     — create the SQLite schema (idempotent).
* ``catalog``     — print the catalog.
* ``queue``       — show recent queue rows.
* ``test-notify`` — emit a test message via the configured notifier.
* ``login``        — open the profile headed to sign into Amazon by hand.
* ``dry-run KEY`` — drive one item to its review page and screenshot, no order.
* ``resume`` / ``pause`` / ``status`` — manage the worker-pause flag.
"""

from __future__ import annotations

import logging

import click

from roomieorder.catalog import load_catalog
from roomieorder.config import load_config
from roomieorder.guards import check_price_ceiling, check_spend_cap
from roomieorder.notify import build_notifier
from roomieorder.store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """roomieorder — HA button → Amazon order → Google Sheets."""
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
        click.echo(
            f"{key:16} {item.title}\n"
            f"{'':16} asin={item.asin} qty={item.qty} "
            f"expected=${item.expected_price:.2f} ceiling=${item.price_ceiling:.2f} "
            f"cooldown={item.cooldown_days}d"
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


@main.command()
def login() -> None:
    """Open the Amazon profile in a real browser so you can sign in by hand.

    roomieorder never stores an Amazon credential — the session lives in the
    persistent Chromium profile (``profile_dir``). Run this once; later
    ``dry-run`` / live orders reuse the saved cookies. Disable 2FA on the
    account first: the automated buy flow halts on any login challenge.
    """
    from roomieorder.purchase import AmazonPurchaser

    config = load_config()
    purchaser = AmazonPurchaser(config)

    def wait_for_operator(page: object) -> None:
        click.echo(f"opened {config.amazon_domain} on profile {config.profile_dir}")
        click.pause(info="log in, then press any key here to save the session and close…")
        if purchaser.is_logged_in(page):
            click.echo("✓ signed in — session saved to the profile")
        else:
            click.echo("⚠️  still looks signed out — re-run `roomieorder login` if needed")

    purchaser.login(wait_for_operator)


@main.command(name="dry-run")
@click.argument("item_key")
def dry_run(item_key: str) -> None:
    """Navigate ITEM_KEY to its review page and screenshot — never orders.

    Forces DRY_RUN regardless of the env flag, so this is always safe to run
    while filling out the §8 checklist.
    """
    from roomieorder.purchase import AmazonPurchaser

    config = load_config()
    config = config.model_copy(update={"dry_run": True})
    items = load_catalog(config.catalog_path)
    item = items.get(item_key)
    if item is None:
        raise click.ClickException(f"unknown item_key: {item_key} (have: {', '.join(items)})")

    store = Store(config.db_path)
    store.init_db()
    purchaser = AmazonPurchaser(config)

    def proceed_check(live_price: float):  # type: ignore[no-untyped-def]
        ceiling = check_price_ceiling(item, live_price)
        if not ceiling.ok:
            return ceiling
        return check_spend_cap(store, config, live_price * item.qty)

    click.echo(f"dry-run {item_key} → {item.url or config.product_url(item.asin)}")
    result = purchaser.buy(item_key, item, proceed_check)
    click.echo(f"status:     {result.status}")
    click.echo(f"unit_price: {result.unit_price}")
    click.echo(f"message:    {result.message}")
    if result.screenshot:
        click.echo(f"screenshot: {result.screenshot}")
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
