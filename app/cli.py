"""CLI entry point — Typer-based command interface for the job agent.

Commands:
    scrape    — Run scraper(s) once
    evaluate  — Score new jobs with the local LLM
    list      — Browse saved job listings
    status    — Show database stats
    init-db   — Initialize/reset the database
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
# enrich command
# ------------------------------------------------------------------

@app.command()
def enrich(
    limit: int = typer.Option(
        50, "--limit", "-n",
        help="Max number of jobs to enrich.",
    ),
) -> None:
    """Fetch full descriptions for jobs that are missing them."""
    settings = _get_settings()

    async def _run() -> None:
        async with Database(settings.db_path) as db:
            # Find jobs with missing or short descriptions
            cursor = await db.conn.execute(
                "SELECT * FROM jobs WHERE description IS NULL "
                "OR length(description) < 200 "
                "ORDER BY date_scraped DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            jobs = [__import__('app.database', fromlist=['JobRecord']).JobRecord(**dict(r)) for r in rows]

            if not jobs:
                console.print("[yellow]All jobs already have descriptions.[/yellow]")
                return

            console.print(f"Found [bold]{len(jobs)}[/bold] jobs needing enrichment")

            # Load the Indeed scraper for its enrich_job method
            indeed_cfg = settings.boards.boards.get("indeed")
            if not indeed_cfg:
                console.print("[red]No Indeed board config found[/red]")
                return

            scraper = _load_scraper(indeed_cfg.module, indeed_cfg, settings.profile, db)
            enriched = 0

            async with scraper:
                for job in jobs:
                    if job.id is None:
                        continue
                    console.print(f"  Enriching #{job.id}: {job.title}...", end="")

                    from app.scrapers.indeed import IndeedScraper
                    if isinstance(scraper, IndeedScraper):
                        enriched_job = await scraper.enrich_job(job)
                    else:
                        console.print(" [yellow]skip (not Indeed)[/yellow]")
                        continue

                    if enriched_job.description and len(enriched_job.description) > len(job.description or ""):
                        await db.conn.execute(
                            "UPDATE jobs SET description = ?, raw_html = ? WHERE id = ?",
                            (enriched_job.description, enriched_job.raw_html, job.id),
                        )
                        await db.conn.commit()
                        enriched += 1
                        desc_len = len(enriched_job.description)
                        console.print(f" [green]OK ({desc_len} chars)[/green]")
                    else:
                        console.print(f" [yellow]no description found[/yellow]")

                    await scraper.random_delay()

            console.print(f"\n[bold green]Enriched {enriched}/{len(jobs)} jobs[/bold green]")

    asyncio.run(_run())


# ------------------------------------------------------------------
# evaluate command
# ------------------------------------------------------------------

@app.command()
def evaluate(
    limit: int = typer.Option(
        100, "--limit", "-n",
        help="Max number of jobs to evaluate.",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Ollama model to use (default: from config or llama3.2:3b-instruct-q4_K_M).",
    ),
    job_id: Optional[int] = typer.Option(
        None, "--id",
        help="Evaluate a single job by ID (useful for testing).",
    ),
) -> None:
    """Score unevaluated jobs against your profile using the local LLM."""
    settings = _get_settings()

    async def _run() -> None:
        from app.evaluator.ollama_client import OllamaClient, OllamaError
        from app.evaluator.pipeline import EvaluationPipeline

        ollama_url = settings.secrets.ollama_base_url or "http://localhost:11434"
        model_name = model or settings.secrets.ollama_model or "llama3.2:3b-instruct-q4_K_M"

        async with Database(settings.db_path) as db:
            async with OllamaClient(base_url=ollama_url, model=model_name) as ollama:
                # Health check
                if not await ollama.is_healthy():
                    console.print(
                        f"[red]Cannot reach Ollama at {ollama_url}[/red]\n"
                        f"Make sure Ollama is running: [bold]ollama serve[/bold]"
                    )
                    raise typer.Exit(1)

                available = await ollama.list_models()
                if not await ollama.model_available():
                    console.print(f"[red]Model '{model_name}' not found.[/red]")
                    console.print(f"Available models: {', '.join(available) or 'none'}")
                    console.print(f"Pull it with: [bold]ollama pull {model_name}[/bold]")
                    raise typer.Exit(1)

                console.print(f"[bold cyan]Evaluating with model: {model_name}[/bold cyan]")

                pipeline = EvaluationPipeline(db, ollama, settings.profile)

                # Single job mode
                if job_id is not None:
                    job = await db.get_job(job_id)
                    if job is None:
                        console.print(f"[red]No job with ID {job_id}[/red]")
                        return

                    console.print(f"Evaluating: [bold]{job.title}[/bold] at {job.company}")
                    results = await pipeline.evaluate_job(job)

                    if not results:
                        console.print("[red]Evaluation failed — check logs[/red]")
                        return

                    for result in results:
                        _print_eval_result(job, result)
                    return

                # Batch mode
                new_count = await db.count_jobs(status="new")
                if new_count == 0:
                    console.print("[yellow]No new jobs to evaluate.[/yellow]")
                    return

                console.print(f"Found [bold]{new_count}[/bold] unevaluated jobs (processing up to {limit})")

                try:
                    stats = await pipeline.run(limit=limit)
                except OllamaError as e:
                    console.print(f"[red]Evaluation failed: {e}[/red]")
                    raise typer.Exit(1)

                console.print(f"\n[green]{stats.summary()}[/green]")

                # Show breakdown
                db_stats = await db.get_stats()
                console.print(f"\n[bold]Database totals:[/bold] {db_stats}")

    asyncio.run(_run())


def _print_eval_result(job, result) -> None:  # type: ignore[no-untyped-def]
    """Pretty-print a single evaluation result."""
    from app.evaluator.pipeline import EvalResult

    # Color the score
    score = result.score
    if score >= 0.7:
        score_style = "bold green"
    elif score >= 0.4:
        score_style = "bold yellow"
    else:
        score_style = "bold red"

    console.print(f"\n  [{score_style}]Score: {score:.2f}[/{score_style}]")
    console.print(f"  [dim]Reasoning:[/dim] {result.reasoning}")

    if result.highlights:
        console.print(f"  [green]Highlights:[/green] {', '.join(result.highlights)}")
    if result.red_flags:
        console.print(f"  [red]Red flags:[/red] {', '.join(result.red_flags)}")


# ------------------------------------------------------------------
# list command
# ------------------------------------------------------------------

@app.command("list")
def list_jobs(
    status_filter: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="Filter by status: new, evaluated, approved, rejected, applied. Omit for all.",
    ),
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Max number of jobs to show.",
    ),
    full: bool = typer.Option(
        False, "--full", "-f",
        help="Show full description for each job.",
    ),
    job_id: Optional[int] = typer.Option(
        None, "--id",
        help="Show detail for a single job by ID.",
    ),
) -> None:
    """Browse saved job listings."""
    settings = _get_settings()

    async def _run() -> None:
        async with Database(settings.db_path) as db:
            # Single job detail view
            if job_id is not None:
                job = await db.get_job(job_id)
                if job is None:
                    console.print(f"[red]No job with ID {job_id}[/red]")
                    return

                console.print(f"\n[bold cyan]#{job.id}[/bold cyan] [bold]{job.title}[/bold]")
                console.print(f"  [green]Company:[/green]  {job.company or 'N/A'}")
                console.print(f"  [green]Location:[/green] {job.location or 'N/A'}")

                if job.salary_min or job.salary_max:
                    sal_min = f"${job.salary_min:,.0f}" if job.salary_min else "?"
                    sal_max = f"${job.salary_max:,.0f}" if job.salary_max else "?"
                    console.print(f"  [green]Salary:[/green]   {sal_min} – {sal_max}")

                console.print(f"  [green]Status:[/green]   {job.status}")
                console.print(f"  [green]Lane:[/green]     {job.search_lane or 'N/A'}")
                console.print(f"  [green]Posted:[/green]   {job.date_posted or 'N/A'}")
                console.print(f"  [green]Scraped:[/green]  {job.date_scraped or 'N/A'}")
                console.print(f"  [green]URL:[/green]      {job.url}")

                if job.rejection_reason:
                    console.print(f"  [red]Rejected:[/red] {job.rejection_reason}")

                # Show evaluation if exists
                evaluation = await db.get_evaluation(job.id)  # type: ignore[arg-type]
                if evaluation:
                    s = evaluation.match_score or 0.0
                    if s >= 0.7:
                        score_style = "bold green"
                    elif s >= 0.4:
                        score_style = "bold yellow"
                    else:
                        score_style = "bold red"
                    console.print(f"  [{score_style}]Score:    {s:.2f}[/{score_style}]")
                    console.print(f"  [green]Model:[/green]    {evaluation.model_used}")
                    if evaluation.reasoning:
                        console.print(f"  [green]Reasoning:[/green] {evaluation.reasoning}")

                if job.description:
                    console.print(f"\n[bold]Description:[/bold]")
                    console.print(job.description[:2000])
                    if len(job.description) > 2000:
                        console.print("[dim]... (truncated)[/dim]")
                return

            # List view
            if status_filter:
                cursor = await db.conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY date_scraped DESC LIMIT ?",
                    (status_filter, limit),
                )
            else:
                cursor = await db.conn.execute(
                    "SELECT * FROM jobs ORDER BY date_scraped DESC LIMIT ?",
                    (limit,),
                )

            rows = await cursor.fetchall()

            if not rows:
                console.print("[yellow]No jobs found.[/yellow]")
                return

            table = Table(title=f"Job Listings ({len(rows)} shown)")
            table.add_column("ID", style="dim", justify="right", width=4)
            table.add_column("Title", style="bold", max_width=30)
            table.add_column("Company", max_width=18)
            table.add_column("Location", max_width=16)
            table.add_column("Salary", justify="right", max_width=14)
            table.add_column("Score", justify="right", width=6)
            table.add_column("Status", style="cyan", width=10)
            table.add_column("Reason", style="dim", max_width=40)

            for row in rows:
                row_dict = dict(row)

                # Format salary
                sal = ""
                if row_dict.get("salary_min") or row_dict.get("salary_max"):
                    s_min = f"${row_dict['salary_min']/1000:.0f}k" if row_dict.get("salary_min") else "?"
                    s_max = f"${row_dict['salary_max']/1000:.0f}k" if row_dict.get("salary_max") else "?"
                    sal = f"{s_min}–{s_max}"

                # Get eval score if exists
                score_str = ""
                eval_row = await db.conn.execute(
                    "SELECT match_score FROM evaluations WHERE job_id = ? "
                    "ORDER BY evaluated_at DESC LIMIT 1",
                    (row_dict["id"],),
                )
                eval_data = await eval_row.fetchone()
                if eval_data and eval_data[0] is not None:
                    s = eval_data[0]
                    if s >= 0.7:
                        score_str = f"[green]{s:.2f}[/green]"
                    elif s >= 0.4:
                        score_str = f"[yellow]{s:.2f}[/yellow]"
                    else:
                        score_str = f"[red]{s:.2f}[/red]"

                # Rejection reason (truncate for table view)
                reason = row_dict.get("rejection_reason", "") or ""
                if len(reason) > 40:
                    reason = reason[:37] + "..."

                table.add_row(
                    str(row_dict["id"]),
                    row_dict.get("title", ""),
                    row_dict.get("company", "") or "",
                    row_dict.get("location", "") or "",
                    sal,
                    score_str,
                    row_dict.get("status", ""),
                    reason,
                )

            console.print(table)

            if full:
                console.print("\n[dim]Tip: use --id <num> to see full details for a specific job.[/dim]")

    asyncio.run(_run())


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

            by_lane = stats.pop("by_lane", {})
            for status_name, count in sorted(stats.items()):
                table.add_row(status_name, str(count))

            console.print(table)

            lane_table = Table(title="Jobs by Search Lane")
            lane_table.add_column("Lane", style="cyan")
            lane_table.add_column("Count", justify="right", style="green")
            for lane_name, count in sorted(by_lane.items()):
                lane_table.add_row(lane_name, str(count))
            console.print(lane_table)

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
# dashboard command
# ------------------------------------------------------------------

@app.command()
def dashboard(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Bind address."),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)."),
) -> None:
    """Start the web dashboard (FastAPI + HTMX)."""
    _get_settings()  # validate config + init logging
    console.print(f"[bold cyan]Starting dashboard at http://{host}:{port}[/bold cyan]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    import uvicorn
    uvicorn.run(
        "app.dashboard.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ------------------------------------------------------------------
# notify command
# ------------------------------------------------------------------

@app.command()
def notify(
    test: bool = typer.Option(False, "--test", "-t", help="Send a test message to verify bot works."),
    digest: bool = typer.Option(False, "--digest", "-d", help="Send the daily digest now."),
) -> None:
    """Send Telegram notifications (test or daily digest)."""
    settings = _get_settings()

    if not test and not digest:
        console.print("[yellow]Specify --test or --digest[/yellow]")
        raise typer.Exit(1)

    async def _run() -> None:
        from app.notifier.telegram import TelegramNotifier

        notifier = TelegramNotifier.from_secrets(settings.secrets)
        if notifier is None:
            console.print(
                "[red]Telegram not configured.[/red]\n"
                "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to config/secrets.env"
            )
            raise typer.Exit(1)

        async with notifier:
            if test:
                console.print("Sending test message...")
                ok = await notifier.test_connection()
                if ok:
                    console.print("[green]Test message sent! Check Telegram.[/green]")
                else:
                    console.print("[red]Failed to send — check token and chat ID.[/red]")

            if digest:
                console.print("Sending daily digest...")
                async with Database(settings.db_path) as db:
                    ok = await notifier.send_daily_digest(
                        db, dashboard_url="http://192.168.5.58:8080"
                    )
                if ok:
                    console.print("[green]Digest sent![/green]")
                else:
                    console.print("[red]Failed to send digest.[/red]")

    asyncio.run(_run())


# ------------------------------------------------------------------
# polish command
# ------------------------------------------------------------------

@app.command()
def polish(
    job_id: int = typer.Option(
        ..., "--id", help="Job ID to generate a cover letter for.",
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume", "-r",
        help="Path to resume file (PDF/TXT). Default: auto-detect from resumes/.",
    ),
) -> None:
    """Generate a tailored cover letter for a job using Claude API."""
    settings = _get_settings()

    if not settings.secrets.anthropic_api_key:
        console.print(
            "[red]Anthropic API key not configured.[/red]\n"
            "Add ANTHROPIC_API_KEY to config/secrets.env"
        )
        raise typer.Exit(1)

    async def _run() -> None:
        from pathlib import Path as P
        from app.polisher.claude_client import ClaudeClient
        from app.polisher.pipeline import PolishPipeline

        resume_path = P(resume) if resume else None

        async with Database(settings.db_path) as db:
            # Verify job exists
            job = await db.get_job(job_id)
            if not job:
                console.print(f"[red]No job with ID {job_id}[/red]")
                return

            console.print(f"[bold cyan]Generating cover letter for:[/bold cyan]")
            console.print(f"  {job.title} @ {job.company or 'Unknown'}")

            async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
                pipeline = PolishPipeline(db, claude, settings, resume_path=resume_path)
                result = await pipeline.polish_job(job_id)

            if result is None:
                console.print("[red]Cover letter generation failed — check logs.[/red]")
                return

            console.print(f"\n[green]Cover letter generated![/green]")
            console.print(f"[dim]Model: {result.model_used} | "
                         f"Tokens: {result.input_tokens} in / {result.output_tokens} out | "
                         f"Cost: {result.cost_display}[/dim]\n")
            console.print("[bold]--- Cover Letter ---[/bold]\n")
            console.print(result.cover_letter)
            console.print(f"\n[dim]Saved to database — view at http://192.168.5.58:8080/jobs/{job_id}[/dim]")

    asyncio.run(_run())


# ------------------------------------------------------------------
# parse-linkedin command
# ------------------------------------------------------------------

@app.command("parse-linkedin")
def parse_linkedin(
    zip_path: Optional[str] = typer.Option(
        None, "--zip", help="LinkedIn export ZIP. Default: resumes/linkedin_export.zip",
    ),
) -> None:
    """Parse your LinkedIn data export into resumes/linkedin_data.json."""
    from pathlib import Path as P
    from app.resume_generator.linkedin_parser import (
        DEFAULT_DATA_PATH, DEFAULT_EXPORT_PATH, parse_linkedin_export,
    )

    settings = _get_settings()
    path = P(zip_path) if zip_path else DEFAULT_EXPORT_PATH
    if not path.exists():
        console.print(
            f"[red]{path} not found.[/red]\n"
            "Download your data from LinkedIn → Settings & Privacy → Data privacy → "
            "Get a copy of your data, then save the ZIP there."
        )
        raise typer.Exit(1)

    data = parse_linkedin_export(path, contact=settings.profile.contact)

    starts = [p.start_date for p in data.positions if p.start_date]
    ends = [p.end_date or "present" for p in data.positions]
    date_range = f"{min(starts)} → {max(ends)}" if starts else "unknown"

    table = Table(title=f"LinkedIn Export — {data.full_name}")
    table.add_column("Item", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Positions", str(len(data.positions)))
    table.add_row("Skills", str(len(data.skills)))
    table.add_row("Education", str(len(data.education)))
    table.add_row("Certifications", str(len(data.certifications)))
    table.add_row("Date range", date_range)
    table.add_row("Saved to", str(DEFAULT_DATA_PATH))
    console.print(table)

    if data.parse_warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in data.parse_warnings:
            console.print(f"  [yellow]•[/yellow] {w}")


# ------------------------------------------------------------------
# resume command
# ------------------------------------------------------------------

@app.command()
def resume(
    job_id: int = typer.Argument(..., help="Job ID to tailor a resume for."),
    lane: str = typer.Option(..., "--lane", "-l", help="Search lane, e.g. frontend_developer."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if cached."),
) -> None:
    """Generate a job-tailored resume (PDF + DOCX) using Claude API."""
    settings = _get_settings()

    if not settings.secrets.anthropic_api_key:
        console.print(
            "[red]Anthropic API key not configured.[/red]\n"
            "Add ANTHROPIC_API_KEY to config/secrets.env"
        )
        raise typer.Exit(1)

    async def _run() -> None:
        from app.polisher.claude_client import ClaudeClient
        from app.resume_generator.pipeline import JobNotFoundError, ResumePipeline

        async with Database(settings.db_path) as db:
            async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
                pipeline = ResumePipeline(db, claude, settings)
                try:
                    result = await pipeline.generate_for_job(job_id, lane, force=force)
                except (JobNotFoundError, ValueError, FileNotFoundError) as e:
                    console.print(f"[red]{e}[/red]")
                    raise typer.Exit(1)

        r = result.resume
        console.print(f"\n[bold green]Resume ready[/bold green] — {r.headline}")

        if r.matched_requirements:
            console.print("\n[green]Matched requirements:[/green]")
            for req in r.matched_requirements:
                console.print(f"  [green]✓[/green] {req}")
        if r.unmatched_requirements:
            console.print("\n[yellow]Unmatched requirements:[/yellow]")
            for req in r.unmatched_requirements:
                console.print(f"  [yellow]–[/yellow] {req}")
        if r.fabrication_warnings:
            console.print("\n[red]Fabrication warnings (removed from resume):[/red]")
            for w in r.fabrication_warnings:
                console.print(f"  [red]![/red] {w}")

        if result.from_cache:
            console.print("\n[dim]Served from cache — use --force to regenerate.[/dim]")
        elif result.gen_result:
            g = result.gen_result
            console.print(f"\n[dim]Model: {g.model_used} | "
                          f"Tokens: {g.input_tokens} in / {g.output_tokens} out | "
                          f"Cost: {g.cost_display}[/dim]")
        console.print(f"\n  PDF:  {result.pdf_path}\n  DOCX: {result.docx_path}")

    asyncio.run(_run())


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
