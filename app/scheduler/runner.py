"""Scheduler runner — orchestrates periodic scraping and evaluation.

This is the long-running process that kicks off scrape and eval jobs
on a schedule. Run it via: python -m app.scheduler.runner
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import LOGS_DIR, load_settings, AppSettings
from app.database import Database
from app.evaluator.ollama_client import OllamaClient
from app.evaluator.pipeline import EvaluationPipeline
from app.logging_config import setup_logging
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class AgentRunner:
    """Manages scheduled scraping and evaluation runs."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.scheduler = AsyncIOScheduler()
        self._running = True

    def _load_scraper(self, module_path: str, board_cfg, db: Database) -> BaseScraper:
        """Dynamically load and instantiate a scraper."""
        module = importlib.import_module(module_path)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseScraper)
                and attr is not BaseScraper
            ):
                return attr(board_cfg, self.settings.profile, db)
        raise ImportError(f"No BaseScraper subclass found in {module_path}")

    async def run_all_scrapers(self) -> None:
        """Run all enabled scrapers sequentially."""
        logger.info("Scheduled scrape run starting at %s", datetime.now(timezone.utc).isoformat())

        async with Database(self.settings.db_path) as db:
            for name, board_cfg in self.settings.boards.boards.items():
                if not board_cfg.enabled:
                    continue

                logger.info("Running scraper: %s", name)
                try:
                    scraper = self._load_scraper(board_cfg.module, board_cfg, db)
                    async with scraper:
                        stats = await scraper.run()
                    logger.info("Scraper %s finished — %s", name, stats.summary())
                except Exception as e:
                    logger.error("Scraper %s failed: %s", name, e, exc_info=True)

            db_stats = await db.get_stats()
            logger.info("Post-scrape DB stats: %s", db_stats)

    async def run_evaluations(self) -> None:
        """Evaluate all unevaluated jobs using the local LLM."""
        logger.info("Scheduled evaluation run starting at %s", datetime.now(timezone.utc).isoformat())

        ollama_url = self.settings.secrets.ollama_base_url or "http://localhost:11434"

        async with Database(self.settings.db_path) as db:
            async with OllamaClient(base_url=ollama_url) as ollama:
                if not await ollama.is_healthy():
                    logger.error("Ollama not reachable at %s — skipping evaluation", ollama_url)
                    return

                pipeline = EvaluationPipeline(db, ollama, self.settings.profile)

                try:
                    stats = await pipeline.run(limit=100)
                    logger.info("Evaluation complete — %s", stats.summary())
                except Exception as e:
                    logger.error("Evaluation failed: %s", e, exc_info=True)

    async def scrape_then_evaluate(self) -> None:
        """Run scrapers first, then evaluate new jobs — the full nightly pipeline."""
        await self.run_all_scrapers()
        await self.run_evaluations()

    def setup_schedule(self) -> None:
        """Configure the APScheduler jobs.

        Schedule:
          - Full scrape every 6 hours on weekdays
          - Nightly scrape + evaluation at 2am daily (off-peak for LLM batch)
          - Standalone evaluation at 3am (catch any stragglers)
        """
        # Main scrape — every 6 hours on weekdays
        self.scheduler.add_job(
            self.run_all_scrapers,
            CronTrigger(hour="*/6", day_of_week="mon-fri"),
            id="scrape_weekday",
            name="Weekday scrape (every 6h)",
            replace_existing=True,
        )

        # Nightly full pipeline — scrape + evaluate at 2am
        self.scheduler.add_job(
            self.scrape_then_evaluate,
            CronTrigger(hour=2, minute=0),
            id="nightly_pipeline",
            name="Nightly scrape + evaluate (2am)",
            replace_existing=True,
        )

        # Catch-up evaluation — 3am (in case daytime scrapes left unevaluated jobs)
        self.scheduler.add_job(
            self.run_evaluations,
            CronTrigger(hour=3, minute=30),
            id="eval_catchup",
            name="Evaluation catch-up (3:30am)",
            replace_existing=True,
        )

        logger.info("Scheduler configured with %d jobs", len(self.scheduler.get_jobs()))

    async def start(self) -> None:
        """Start the scheduler and block until shutdown."""
        self.setup_schedule()
        self.scheduler.start()
        logger.info("Scheduler started — press Ctrl+C to stop")

        # Block until signal
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            self._running = False
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        await stop_event.wait()
        self.scheduler.shutdown(wait=True)
        logger.info("Scheduler shut down cleanly")


async def main() -> None:
    """Entry point for the scheduler process."""
    settings = load_settings()
    setup_logging(LOGS_DIR, settings.log_level)

    runner = AgentRunner(settings)

    # Run full pipeline once on startup, then hand off to schedule
    logger.info("Running initial scrape + evaluate on startup...")
    await runner.scrape_then_evaluate()

    await runner.start()


if __name__ == "__main__":
    asyncio.run(main())
