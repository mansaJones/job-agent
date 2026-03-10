"""Base scraper — shared logic for all job board scrapers.

Every board-specific scraper inherits from BaseScraper and implements
the abstract methods for searching and parsing that board's HTML.

Includes AdaptiveHealth — a self-tuning monitor that tracks parse/fetch
success rates across runs and auto-adjusts:
  - Fetch strategy (httpx → Playwright escalation)
  - Request delays (back off when getting blocked)
  - Scraper disablement (halt if parse rate drops below threshold)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    used_playwright: bool = False


class ScraperStats:
    """Tracks scraper run metrics."""

    def __init__(self) -> None:
        self.pages_fetched: int = 0
        self.pages_blocked: int = 0
        self.pages_failed: int = 0
        self.jobs_found: int = 0
        self.jobs_parsed: int = 0
        self.jobs_inserted: int = 0
        self.jobs_skipped_duplicate: int = 0
        self.jobs_skipped_blacklist: int = 0
        self.errors: int = 0
        self.playwright_fetches: int = 0
        self.started_at: datetime = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    @property
    def parse_success_rate(self) -> float:
        """Fraction of fetched pages that yielded at least one job."""
        if self.pages_fetched == 0:
            return 0.0
        pages_with_jobs = self.pages_fetched - (
            self.pages_blocked + self.pages_failed
        )
        return max(0.0, pages_with_jobs / self.pages_fetched)

    @property
    def fetch_failure_rate(self) -> float:
        """Fraction of fetch attempts that failed or were blocked."""
        total_attempts = self.pages_fetched + self.pages_blocked + self.pages_failed
        if total_attempts == 0:
            return 0.0
        return (self.pages_blocked + self.pages_failed) / total_attempts

    def summary(self) -> str:
        return (
            f"Pages: {self.pages_fetched} | Found: {self.jobs_found} | "
            f"Parsed: {self.jobs_parsed} | Inserted: {self.jobs_inserted} | "
            f"Dupes: {self.jobs_skipped_duplicate} | Blacklisted: {self.jobs_skipped_blacklist} | "
            f"Errors: {self.errors} | PW: {self.playwright_fetches} | "
            f"Time: {self.duration_seconds:.1f}s"
        )


# ---------------------------------------------------------------------------
# Adaptive Health Monitor
# ---------------------------------------------------------------------------

@dataclass
class AdaptiveHealth:
    """Tracks real-time scraper health and auto-adjusts fetch strategy.

    Thresholds (all configurable):
      - parse_error_pct >= 50%  → escalate to Playwright-first
      - block_pct >= 30%        → increase delays by 1.5x
      - block_pct >= 60%        → increase delays by 2.5x, Playwright-first
      - consecutive_empty >= 3  → likely HTML structure change, halt + alert
      - consecutive_blocks >= 2 → Playwright-first for rest of run

    The monitor checks after every page fetch and mutates the scraper's
    behavior in real time. All decisions are logged and persisted to
    scraper_health table at end of run.
    """

    # Thresholds — tweak these per-board if needed
    parse_error_escalate_pct: float = 0.50
    block_escalate_pct: float = 0.30
    block_critical_pct: float = 0.60
    max_consecutive_empty: int = 3
    max_consecutive_blocks: int = 2
    delay_backoff_multiplier: float = 1.5
    delay_critical_multiplier: float = 2.5
    max_delay_cap: float = 120.0  # never sleep more than 2 min

    # Runtime state (reset each run)
    consecutive_empty_pages: int = 0
    consecutive_blocks: int = 0
    total_fetches: int = 0
    total_blocks: int = 0
    total_failures: int = 0
    total_parse_errors: int = 0
    total_pages_with_jobs: int = 0
    playwright_escalated: bool = False
    delay_multiplier: float = 1.0
    halted: bool = False
    halt_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Reset runtime state for a new run."""
        self.consecutive_empty_pages = 0
        self.consecutive_blocks = 0
        self.total_fetches = 0
        self.total_blocks = 0
        self.total_failures = 0
        self.total_parse_errors = 0
        self.total_pages_with_jobs = 0
        self.playwright_escalated = False
        self.delay_multiplier = 1.0
        self.halted = False
        self.halt_reason = ""
        self.notes = []

    @property
    def block_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.total_blocks / self.total_fetches

    @property
    def parse_error_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.total_parse_errors / self.total_fetches

    @property
    def effective_strategy(self) -> str:
        return "playwright" if self.playwright_escalated else "httpx"

    def record_fetch(self, status: FetchStatus, used_playwright: bool) -> None:
        """Record a fetch attempt and update counters."""
        self.total_fetches += 1

        if status == FetchStatus.BLOCKED:
            self.total_blocks += 1
            self.consecutive_blocks += 1
        elif status == FetchStatus.FAILED:
            self.total_failures += 1
            self.consecutive_blocks = 0
        else:
            self.consecutive_blocks = 0

    def record_parse_result(self, jobs_found: int) -> None:
        """Record a parse result (how many jobs a page yielded)."""
        if jobs_found > 0:
            self.total_pages_with_jobs += 1
            self.consecutive_empty_pages = 0
        else:
            self.consecutive_empty_pages += 1

    def record_parse_error(self) -> None:
        """Record a parse exception."""
        self.total_parse_errors += 1

    def evaluate(self, source_name: str) -> None:
        """Run all health checks and adjust strategy. Call after each page.

        Mutates self in place — the scraper reads playwright_escalated,
        delay_multiplier, and halted to adjust its behavior.
        """
        # --- Check: consecutive empty pages (HTML structure likely changed) ---
        if self.consecutive_empty_pages >= self.max_consecutive_empty:
            if not self.playwright_escalated:
                # Try Playwright first — maybe httpx is getting a different page
                self.playwright_escalated = True
                self._note(source_name,
                           f"ESCALATE: {self.consecutive_empty_pages} consecutive empty pages "
                           f"— switching to Playwright-first")
            else:
                # Already on Playwright and still empty — HTML structure changed
                self.halted = True
                self.halt_reason = (
                    f"HTML structure likely changed: {self.consecutive_empty_pages} "
                    f"consecutive pages returned 0 jobs (even via Playwright)"
                )
                self._note(source_name, f"HALT: {self.halt_reason}")
                return

        # --- Check: consecutive blocks ---
        if self.consecutive_blocks >= self.max_consecutive_blocks:
            if not self.playwright_escalated:
                self.playwright_escalated = True
                self._note(source_name,
                           f"ESCALATE: {self.consecutive_blocks} consecutive blocks "
                           f"— switching to Playwright-first")
            # Also bump delays
            self.delay_multiplier = max(
                self.delay_multiplier, self.delay_critical_multiplier
            )
            self._note(source_name,
                       f"BACKOFF: delays now {self.delay_multiplier:.1f}x due to blocks")

        # --- Check: overall block rate ---
        if self.total_fetches >= 3:  # need enough data
            if self.block_rate >= self.block_critical_pct:
                self.playwright_escalated = True
                self.delay_multiplier = max(
                    self.delay_multiplier, self.delay_critical_multiplier
                )
                self._note(source_name,
                           f"CRITICAL: block rate {self.block_rate:.0%} — "
                           f"Playwright + {self.delay_multiplier:.1f}x delay")
            elif self.block_rate >= self.block_escalate_pct:
                self.delay_multiplier = max(
                    self.delay_multiplier, self.delay_backoff_multiplier
                )
                self._note(source_name,
                           f"BACKOFF: block rate {self.block_rate:.0%} — "
                           f"delays now {self.delay_multiplier:.1f}x")

        # --- Check: parse error rate ---
        if self.total_fetches >= 3:
            if self.parse_error_rate >= self.parse_error_escalate_pct:
                if not self.playwright_escalated:
                    self.playwright_escalated = True
                    self._note(source_name,
                               f"ESCALATE: parse error rate {self.parse_error_rate:.0%} "
                               f"— switching to Playwright-first")

    def _note(self, source_name: str, msg: str) -> None:
        """Log and record an adaptive health decision."""
        full_msg = f"[{source_name}] AdaptiveHealth: {msg}"
        logger.warning(full_msg)
        self.notes.append(msg)

    async def persist(self, db: Database, source_name: str, stats: ScraperStats,
                      delay_min: float, delay_max: float) -> None:
        """Write this run's health metrics to scraper_health table."""
        try:
            await db.conn.execute(
                """INSERT INTO scraper_health
                   (source, pages_fetched, pages_blocked, pages_failed,
                    jobs_found, jobs_parsed, parse_errors, playwright_used,
                    delay_min_used, delay_max_used, fetch_strategy, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_name,
                    stats.pages_fetched,
                    stats.pages_blocked,
                    stats.pages_failed,
                    stats.jobs_found,
                    stats.jobs_parsed,
                    self.total_parse_errors,
                    stats.playwright_fetches,
                    delay_min * self.delay_multiplier,
                    delay_max * self.delay_multiplier,
                    self.effective_strategy,
                    "; ".join(self.notes) if self.notes else None,
                ),
            )
            await db.conn.commit()
        except Exception as e:
            logger.warning("[%s] Failed to persist health metrics: %s", source_name, e)


# ---------------------------------------------------------------------------
# Base Scraper
# ---------------------------------------------------------------------------

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
        self.health = AdaptiveHealth()

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
    # Shared logic — adaptive fetch
    # ------------------------------------------------------------------

    async def fetch_page(self, url: str) -> FetchResult:
        """Fetch a URL, respecting adaptive health decisions.

        If health monitor has escalated to Playwright-first, skip httpx.
        Otherwise try httpx first, fall back to Playwright on 403.
        """
        if self.health.playwright_escalated:
            # Playwright-first mode — skip httpx entirely
            logger.info("[%s] Playwright-first mode — fetching %s", self.SOURCE_NAME, url)
            result = await self._fetch_playwright(url)
            if result.status == FetchStatus.SUCCESS:
                self.stats.playwright_fetches += 1
                self.health.record_fetch(result.status, used_playwright=True)
                return FetchResult(result.status, result.html, used_playwright=True)
            # Playwright failed — record and bail
            if result.status == FetchStatus.BLOCKED:
                self.stats.pages_blocked += 1
            else:
                self.stats.pages_failed += 1
            self.health.record_fetch(result.status, used_playwright=True)
            self.stats.errors += 1
            return result

        # --- Normal mode: httpx first ---
        result = await self._fetch_httpx(url)
        if result.status == FetchStatus.SUCCESS:
            self.health.record_fetch(result.status, used_playwright=False)
            return FetchResult(result.status, result.html, used_playwright=False)

        # --- Fallback: Playwright on block/failure ---
        if result.status in (FetchStatus.BLOCKED, FetchStatus.FAILED):
            logger.info("[%s] httpx %s — falling back to Playwright for %s",
                        self.SOURCE_NAME, result.status.value, url)
            pw_result = await self._fetch_playwright(url)
            if pw_result.status == FetchStatus.SUCCESS:
                self.stats.playwright_fetches += 1
                self.health.record_fetch(pw_result.status, used_playwright=True)
                return FetchResult(pw_result.status, pw_result.html, used_playwright=True)
            logger.warning("[%s] Playwright fallback also failed for %s", self.SOURCE_NAME, url)

        if result.status == FetchStatus.BLOCKED:
            self.stats.pages_blocked += 1
        else:
            self.stats.pages_failed += 1
        self.health.record_fetch(result.status, used_playwright=False)
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
        """Sleep for a random duration, scaled by the health monitor's multiplier."""
        base_min = self.config.delay_min * self.health.delay_multiplier
        base_max = self.config.delay_max * self.health.delay_multiplier
        # Cap at max to avoid absurd waits
        base_min = min(base_min, self.health.max_delay_cap)
        base_max = min(base_max, self.health.max_delay_cap)
        delay = random.uniform(base_min, base_max)
        if self.health.delay_multiplier > 1.0:
            logger.info("[%s] Adaptive delay: %.1fs (%.1fx backoff)",
                        self.SOURCE_NAME, delay, self.health.delay_multiplier)
        else:
            logger.debug("[%s] Sleeping %.1fs...", self.SOURCE_NAME, delay)
        await asyncio.sleep(delay)

    async def run(self) -> ScraperStats:
        """Execute the full scrape run across all configured queries and pages.

        The adaptive health monitor evaluates after every page and may:
          - Escalate to Playwright-first fetching
          - Increase delays between requests
          - Halt the run entirely if HTML structure appears to have changed

        Returns the stats for this run.
        """
        self.stats = ScraperStats()
        self.health.reset()
        location = self.config.location or self.profile.preferences.location

        logger.info(
            "[%s] Starting scrape — %d queries, up to %d pages each",
            self.SOURCE_NAME, len(self.config.search_queries), self.config.max_pages,
        )

        blocked = False

        for query in self.config.search_queries:
            if blocked or self.health.halted:
                if self.health.halted:
                    logger.error("[%s] HALTED by adaptive health: %s",
                                 self.SOURCE_NAME, self.health.halt_reason)
                else:
                    logger.warning("[%s] Source is blocked — skipping remaining queries",
                                   self.SOURCE_NAME)
                break

            for page in range(self.config.max_pages):
                if self.health.halted:
                    break

                url = self.build_search_url(query, location, page)
                logger.info("[%s] Fetching page %d for '%s'%s",
                            self.SOURCE_NAME, page + 1, query,
                            " (Playwright-first)" if self.health.playwright_escalated else "")

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
                    logger.warning("[%s] Failed to fetch %s — skipping to next page",
                                   self.SOURCE_NAME, url)
                    # Let health monitor evaluate even on failure
                    self.health.evaluate(self.SOURCE_NAME)
                    continue

                html = result.html
                self.stats.pages_fetched += 1
                self.save_html_snapshot(html, self.SOURCE_NAME, url)

                try:
                    jobs = self.parse_listing_page(html)
                except Exception as e:
                    logger.error("[%s] Parse error on %s: %s", self.SOURCE_NAME, url, e)
                    self.stats.errors += 1
                    self.health.record_parse_error()
                    self.health.evaluate(self.SOURCE_NAME)
                    continue

                self.stats.jobs_found += len(jobs)
                self.health.record_parse_result(len(jobs))

                # Let health monitor evaluate after every page
                self.health.evaluate(self.SOURCE_NAME)

                if self.health.halted:
                    logger.error("[%s] HALTED mid-run: %s",
                                 self.SOURCE_NAME, self.health.halt_reason)
                    break

                if not jobs:
                    logger.info("[%s] No jobs found on page %d — stopping query",
                                self.SOURCE_NAME, page + 1)
                    break

                for job in jobs:
                    self.stats.jobs_parsed += 1

                    if self.is_blacklisted(job):
                        self.stats.jobs_skipped_blacklist += 1
                        logger.debug("[%s] Blacklisted: %s at %s",
                                     self.SOURCE_NAME, job.title, job.company)
                        continue

                    if await self.db.job_url_exists(job.url):
                        self.stats.jobs_skipped_duplicate += 1
                        continue

                    row_id = await self.db.insert_job(job)
                    if row_id is not None:
                        self.stats.jobs_inserted += 1
                    else:
                        self.stats.jobs_skipped_duplicate += 1

                await self.random_delay()

        # Persist health metrics for historical tracking
        await self.health.persist(
            self.db, self.SOURCE_NAME, self.stats,
            self.config.delay_min, self.config.delay_max,
        )

        logger.info("[%s] Scrape complete — %s", self.SOURCE_NAME, self.stats.summary())
        if self.health.notes:
            logger.info("[%s] Health notes: %s", self.SOURCE_NAME,
                        "; ".join(self.health.notes))

        return self.stats
