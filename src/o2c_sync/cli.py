"""Command line entry point: `o2c sync`, `o2c status`, `o2c dlq list|replay`."""

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from o2c_sync.config import Settings
from o2c_sync.engine import ORDER_STREAM, STATUS_STREAM, SyncEngine
from o2c_sync.mapping import FieldMapper
from o2c_sync.state import RunStats, StateStore

app = typer.Typer(help="Salesforce <-> ERP order-to-cash sync.", no_args_is_help=True)
dlq_app = typer.Typer(help="Inspect and replay the dead-letter queue.", no_args_is_help=True)
app.add_typer(dlq_app, name="dlq")
console = Console()


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )


def _store(settings: Settings) -> StateStore:
    return StateStore(settings.database_url)


def _engine(settings: Settings) -> SyncEngine:
    from o2c_sync.connectors.odoo import OdooERP
    from o2c_sync.connectors.salesforce import SalesforceCRM

    limits = {"api_usage_ceiling": settings.sf_api_usage_ceiling,
              "max_retries": settings.max_retries}
    if settings.sf_cli_org:
        crm = SalesforceCRM.with_sf_cli(settings.sf_cli_org, **limits)
    elif settings.sf_username and settings.sf_password and settings.sf_security_token:
        crm = SalesforceCRM.with_password(
            settings.sf_username,
            settings.sf_password.get_secret_value(),
            settings.sf_security_token.get_secret_value(),
            domain=settings.sf_domain,
            **limits,
        )
    else:
        raise typer.BadParameter("Salesforce credentials missing; copy .env.example to .env")
    erp = OdooERP(settings.odoo_url, settings.odoo_db, settings.odoo_username,
                  settings.odoo_password.get_secret_value(), max_retries=settings.max_retries)
    return SyncEngine(crm, erp, FieldMapper.from_yaml(settings.mapping_path), _store(settings))


def _report(stats: RunStats) -> None:
    table = Table(title="Sync run", show_header=False)
    for key, value in stats.as_dict().items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)
    for err in stats.errors[:10]:
        console.print(f"[red]x[/red] {err}")


def _exit_code(stats: RunStats) -> int:
    # Distinct codes let a scheduler alert differently on "partial" vs "aborted".
    if stats.aborted:
        return 3
    return 2 if stats.failed else 0


@app.command()
def sync(
    dry_run: bool = typer.Option(False, help="Map and validate only; write nothing."),
    writeback: bool = typer.Option(True, help="Push ERP invoice status back to Salesforce."),
) -> None:
    """Run one incremental sync cycle."""
    stats = _engine(Settings()).run(dry_run=dry_run, writeback=writeback)
    _report(stats)
    raise typer.Exit(_exit_code(stats))


@app.command()
def status() -> None:
    """Show watermarks, recent runs and dead-letter backlog."""
    store = _store(Settings())
    for stream in (ORDER_STREAM, STATUS_STREAM):
        console.print(f"[bold]{stream}[/bold] watermark: {store.get_watermark(stream) or '-'}")
    table = Table("run", "started", "status", "created", "updated", "skipped", "failed")
    for run in store.recent_runs():
        s = run.stats or {}
        table.add_row(str(run.id), f"{run.started_at:%Y-%m-%d %H:%M}", run.status,
                      *(str(s.get(k, "")) for k in ("created", "updated", "skipped", "failed")))
    console.print(table)
    console.print(f"open dead letters: {len(store.open_dead_letters())}")


@dlq_app.command("list")
def dlq_list() -> None:
    """List unresolved dead letters."""
    table = Table("id", "source id", "stage", "attempts", "last seen", "error")
    for d in _store(Settings()).open_dead_letters():
        table.add_row(str(d.id), d.source_id, d.stage, str(d.attempts),
                      f"{d.last_seen_at:%Y-%m-%d %H:%M}", d.error[:80])
    console.print(table)


@dlq_app.command("replay")
def dlq_replay() -> None:
    """Retry all parked orders from their stored payloads."""
    stats = _engine(Settings()).replay_dead_letters()
    _report(stats)
    raise typer.Exit(_exit_code(stats))


if __name__ == "__main__":
    app()
