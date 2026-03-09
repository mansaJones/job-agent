"""CLI entry point — Typer-based command interface for the job agent.

Commands:
    scrape   — Run scraper(s) once
    status   — Show database stats
    init-db  — Initialize/reset the database
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from app.config import LOGS_DIR, load_settings
from app.database import Database
from app.logging_config import setup_logging
from app.scrapers.base import BaseScraper

app = typer.Typer(
    name="job-agent",
    help="Autonomous job search agent for Jetson Orin Nano.",
    no_args_is_help=True,
)
console = Console()


def _get_settings():  # type: ignore[no-untyped-def]
    """Load settings and init logging (used by every command)."""
    settings = load_settings()
    setup_logging(LOGS_DIR, settings.log_level)
    return settings


# ------------------------------------------------------------------
# scrape command
# ------------------------------------------------------------------

@app.command()
def scrape(
    board: Optional[str] = typer.Option(
        None, "--board", "-b",
        help="Specific board to scrape (e.g. 'indeed'). Omit for all enabled boards.",
    ),
    enrich: bool = typer.Option(
        True, "--enrich/--no-enrich",
        help="Fetch full job descriptions after listing scrape.",
    ),
) -> None:
    """Run job board scraper(s)."""
    settings = _get_settings()

    async def _run() -> None:
        async with Database(settings.db_path) as db:
            boards_to_run = {}

            if board:
                if board not in settings.boards.boards:
                    console.print(f"[red]Unknown board: {board}[/red]")
                    console.print(f"Available: {', '.join(settings.boards.boards.keys())}")
                    raise typer.Exit(1)
                boards_to_run = {board: settings.boards.boards[board]}
            else:
                boards_to_run = {
                    name: cfg
                    for name, cfg in settings.boards.boards.items()
                    if cfg.enabled
                }

            if not boards_to_run:
                console.print("[yellow]No enabled boards to scrape.[/yellow]")
                return

            for name, board_cfg in boards_to_run.items():
                console.print(f"\n[bold cyan]Scraping: {name}[/bold cyan]")
                try:
                    scraper = _load_scraper(board_cfg.module, board_cfg, settings.profile, db)
                    async with scraper:
                        stats = await scraper.run()
                    console.print(f"[green]  {stats.summary()}[/green]")
                except Exception as e:
                    logging.getLogger(__name__).error("Scraper %s failed: %s", name, e, exc_info=True)
                    console.print(f"[red]  Failed: {e}[/red]")

            # Print summary
            db_stats = await db.get_stats()
            console.print(f"\n[bold]Database totals:[/bold] {db_stats}")

    asyncio.run(_run())


def _load_scraper(module_path: str, board_cfg, profile, db) -> BaseScraper:  # type: ignore[no-untyped-def]
    """Dynamically load a scraper class from its module path."""
    module = importlib.import_module(module_path)

    # Convention: the scraper class is the one that ends with "Scraper"
    scraper_class = None
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, BaseScraper)
            and attr is not BaseScraper
        ):
            scraper_class = attr
            break

    if scraper_class is None:
        raise ImportError(f"No BaseScraper subclass found in {module_path}")

    return scraper_class(board_cfg, profile, db)


# ------------------------------------------------------------------
# status command
# ------------------------------------------------------------------

@app.command()
def status() -> None:
    """Show database statistics."""
    settings = _get_settings()

    async def _run() -> None:
        async with Database(settings.db_path) as db:
            stats = await db.get_stats()

            table = Table(title="Job Agent Status")
            table.add_column("Status", style="cyan")
            table.add_column("Count", justify="right", style="green")

            for status_name, count in sorted(stats.items()):
                table.add_row(status_name, str(count))

            console.print(table)

    asyncio.run(_run())


# ------------------------------------------------------------------
# init-db command
# ------------------------------------------------------------------

@app.command("init-db")
def init_db() -> None:
    """Initialize the database (creates tables if they don't exist)."""
    settings = _get_settings()

    async def _run() -> None:
        async with Database(settings.db_path) as db:
            console.print(f"[green]Database initialized at {settings.db_path}[/green]")

    asyncio.run(_run())


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
