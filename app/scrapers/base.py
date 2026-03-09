"""Base scraper — shared logic for all job board scrapers.

Every board-specific scraper inherits from BaseScraper and implements
the abstract methods for searching and parsing that board's HTML.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

import httpx

from app.config import HTML_SNAPSHOTS_DIR, BoardConfig, ProfileConfig
from app.database import Database, JobRecord

logger = logging.getLogger(__name__)


class FetchStatus(Enum):
    """Result status from a page fetch attempt."""
    SUCCESS = "success"
    BLOCKED = "blocked"       # 403 — hard stop, don't retry this source
    RATE_LIMITED = "rate_limited"  # 429 — slow down
    FAILED = "failed"         # network/other error


@dataclass
class FetchResult:
    """Wraps a fetch attempt so callers can distinguish 'blocked' from 'failed'."""
    status: FetchStatus
    html: str | None = None


class ScraperStats:
    """Tracks scraper run metrics."""

    def __init__(self) -> None:
        self.pages_fetched: int = 0
        self.jobs_found: int = 0
        self.jobs_parsed: int = 0
        self.jobs_inserted: int = 0
        self.jobs_skipped_duplicate: int = 0
        self.jobs_skipped_blacklist: int = 0
        self.errors: int = 0
        self.started_at: datetime = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def summary(self) -> str:
        return (
            f"Pages: {self.pages_fetched} | Found: {self.jobs_found} | "
            f"Parsed: {self.jobs_parsed} | Inserted: {self.jobs_inserted} | "
            f"Dupes: {self.jobs_skipped_duplicate} | Blacklisted: {self.jobs_skipped_blacklist} | "
            f"Errors: {self.errors} | Time: {self.duration_seconds:.1f}s"
        )


class BaseScraper(ABC):
    """Abstract base class for job board scrapers.

    Subclasses must implement:
        - build_search_url(query, location, page) -> str
        - parse_listing_page(html) -> list of partial JobRecords
        - parse_detail_page(html, job) -> JobRecord  (optional override)
    """

    SOURCE_NAME: str = "unknown"

    def __init__(
        self,
        board_config: BoardConfig,
        profile: ProfileConfig,
        db: Database,
    ) -> None:
        self.config = board_config
        self.profile = profile
        self.db = db
        self.stats = ScraperStats()

        # Build httpx client with sensible defaults
        default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        default_headers.update(self.config.headers)
        self.client = httpx.AsyncClient(
            headers=default_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            http2=True,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self) -> BaseScraper:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Abstract methods — subclasses implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def build_search_url(self, query: str, location: str, page: int) -> str:
        """Build the search results URL for the given query/location/page."""
        ...

    @abstractmethod
    def parse_listing_page(self, html: str) -> list[JobRecord]:
        """Parse a search results page and return partial JobRecords.

        These records may be missing description/raw_html if a detail
        page fetch is needed.
        """
        ...

    # ------------------------------------------------------------------
    # Shared logic
    # ------------------------------------------------------------------

    async def fetch_page(self, url: str) -> FetchResult:
        """Fetch a URL with httpx first, falling back to Playwright on 403.

        Returns a FetchResult so callers can distinguish blocked vs failed.
        """
        # --- Attempt 1: lightweight httpx ---
        result = await self._fetch_httpx(url)
        if result.status == FetchStatus.SUCCESS:
            return result

        # --- Attempt 2: Playwright browser if httpx got blocked ---
        if result.status == FetchStatus.BLOCKED:
            logger.info("[%s] httpx blocked — falling back to Playwright for %s", self.SOURCE_NAME, url)
            pw_result = await self._fetch_playwright(url)
            if pw_result.status == FetchStatus.SUCCESS:
                return pw_result
            logger.warning("[%s] Playwright fallback also failed for %s", self.SOURCE_NAME, url)

        self.stats.errors += 1
        return result

    async def _fetch_httpx(self, url: str) -> FetchResult:
        """Try fetching with httpx (fast, lightweight)."""
        for attempt in range(3):
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                return FetchResult(FetchStatus.SUCCESS, response.text)
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "[%s] HTTP %d for %s (attempt %d/3)",
                    self.SOURCE_NAME, e.response.status_code, url, attempt + 1,
                )
                if e.response.status_code == 403:
                    return FetchResult(FetchStatus.BLOCKED)
                if e.response.status_code == 429:
                    wait = 60 * (attempt + 1)
                    logger.warning("[%s] Rate limited. Waiting %ds...", self.SOURCE_NAME, wait)
                    await asyncio.sleep(wait)
                    continue
            except httpx.RequestError as e:
                logger.warning(
                    "[%s] Request error for %s: %s (attempt %d/3)",
                    self.SOURCE_NAME, url, e, attempt + 1,
                )
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))

        return FetchResult(FetchStatus.FAILED)

    async def _fetch_playwright(self, url: str) -> FetchResult:
        """Fetch with a real browser via Playwright. Heavier but bypasses bot detection."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "[%s] Playwright not installed — can't fall back to browser. "
                "Install with: pip install playwright && playwright install chromium",
                self.SOURCE_NAME,
            )
            return FetchResult(FetchStatus.FAILED)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                page = await context.new_page()

                # Block images/fonts/media to speed things up
                await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,webm}",
                                 lambda route: route.abort())

                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                if response and response.status == 403:
                    await browser.close()
                    return FetchResult(FetchStatus.BLOCKED)

                # Wait a moment for any JS rendering
                await page.wait_for_timeout(2000)

                html = await page.content()
                await browser.close()

                if html and len(html) > 500:
                    logger.info("[%s] Playwright fetch successful (%d bytes)", self.SOURCE_NAME, len(html))
                    return FetchResult(FetchStatus.SUCCESS, html)
                else:
                    return FetchResult(FetchStatus.FAILED)

        except Exception as e:
            logger.error("[%s] Playwright error for %s: %s", self.SOURCE_NAME, url, e)
            return FetchResult(FetchStatus.FAILED)

    def save_html_snapshot(self, html: str, source: str, identifier: str) -> Path:
        """Save raw HTML to disk for parser debugging.

        Returns the path to the saved file.
        """
        safe_id = hashlib.md5(identifier.encode()).hexdigest()[:12]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{source}_{safe_id}_{timestamp}.html"
        filepath = HTML_SNAPSHOTS_DIR / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(html, encoding="utf-8")
        return filepath

    def is_blacklisted(self, job: JobRecord) -> bool:
        """Check if a job matches any blacklist rules."""
        # Company blacklist
        if job.company:
            company_lower = job.company.lower()
            for blocked in self.profile.blacklist.companies:
                if blocked in company_lower:
                    return True

        # Keyword blacklist — check title and description
        text = f"{job.title} {job.description or ''}".lower()
        for keyword in self.profile.blacklist.keywords:
            if keyword in text:
                return True

        return False

    async def random_delay(self) -> None:
        """Sleep for a random duration between configured min/max."""
        delay = random.uniform(self.config.delay_min, self.config.delay_max)
        logger.debug("[%s] Sleeping %.1fs...", self.SOURCE_NAME, delay)
        await asyncio.sleep(delay)

    async def run(self) -> ScraperStats:
        """Execute the full scrape run across all configured queries and pages.

        Returns the stats for this run.
        """
        self.stats = ScraperStats()
        location = self.config.location or self.profile.preferences.location

        logger.info(
            "[%s] Starting scrape — %d queries, up to %d pages each",
            self.SOURCE_NAME, len(self.config.search_queries), self.config.max_pages,
        )

        blocked = False  # If we get hard-blocked, stop all queries for this source

        for query in self.config.search_queries:
            if blocked:
                logger.warning("[%s] Source is blocked — skipping remaining queries", self.SOURCE_NAME)
                break

            for page in range(self.config.max_pages):
                url = self.build_search_url(query, location, page)
                logger.info("[%s] Fetching page %d for '%s'", self.SOURCE_NAME, page + 1, query)

                result = await self.fetch_page(url)

                if result.status == FetchStatus.BLOCKED:
                    logger.error(
                        "[%s] Blocked by %s — aborting entire scrape run. "
                        "Try again later or check your IP/headers.",
                        self.SOURCE_NAME, self.SOURCE_NAME,
                    )
                    blocked = True
                    break

                if result.html is None:
                    logger.warning("[%s] Failed to fetch %s — skipping to next page", self.SOURCE_NAME, url)
                    continue

                html = result.html
                self.stats.pages_fetched += 1
                self.save_html_snapshot(html, self.SOURCE_NAME, url)

                try:
                    jobs = self.parse_listing_page(html)
                except Exception as e:
                    logger.error("[%s] Parse error on %s: %s", self.SOURCE_NAME, url, e)
                    self.stats.errors += 1
                    continue

                self.stats.jobs_found += len(jobs)

                if not jobs:
                    logger.info("[%s] No jobs found on page %d — stopping query", self.SOURCE_NAME, page + 1)
                    break

                for job in jobs:
                    self.stats.jobs_parsed += 1

                    # Check blacklist
                    if self.is_blacklisted(job):
                        self.stats.jobs_skipped_blacklist += 1
                        logger.debug("[%s] Blacklisted: %s at %s", self.SOURCE_NAME, job.title, job.company)
                        continue

                    # Check duplicate
                    if await self.db.job_url_exists(job.url):
                        self.stats.jobs_skipped_duplicate += 1
                        continue

                    # Insert
                    row_id = await self.db.insert_job(job)
                    if row_id is not None:
                        self.stats.jobs_inserted += 1
                    else:
                        self.stats.jobs_skipped_duplicate += 1

                await self.random_delay()

        logger.info("[%s] Scrape complete — %s", self.SOURCE_NAME, self.stats.summary())
        return self.stats
