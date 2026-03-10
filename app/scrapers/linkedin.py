"""LinkedIn job board scraper.

Uses LinkedIn's public (guest) job search pages — no login required.
LinkedIn serves job listings at two URL patterns:

  Search: https://www.linkedin.com/jobs/search/?keywords=...&location=...
  Detail: https://www.linkedin.com/jobs/view/{job_id}

LinkedIn is *very* aggressive about blocking scrapers, so:
  - We use their guest API endpoint (/jobs-guest/jobs/api/...) when possible
  - Playwright is the primary fetch method (JS-rendered content)
  - Rate limits are conservative (15-40s delays)
  - User-Agent rotation helps, but expect occasional blocks

The guest API returns paginated HTML fragments that are easier to parse
than the full SPA-rendered pages.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus, urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from app.config import BoardConfig, ProfileConfig
from app.database import Database, JobRecord
from app.scrapers.base import BaseScraper, FetchResult, FetchStatus

logger = logging.getLogger(__name__)


class LinkedInScraper(BaseScraper):
    """Scraper for LinkedIn public job listings."""

    SOURCE_NAME = "linkedin"

    # LinkedIn's guest API serves HTML fragments — lighter than the full SPA
    GUEST_API_BASE = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    DETAIL_API_BASE = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

    def __init__(
        self,
        board_config: BoardConfig,
        profile: ProfileConfig,
        db: Database,
    ) -> None:
        super().__init__(board_config, profile, db)
        # LinkedIn needs these headers to serve the guest API properly
        self.client.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/jobs/search/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        })

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, location: str, page: int) -> str:
        """Build a LinkedIn guest API search URL.

        LinkedIn's guest API uses `start=` for pagination (0, 25, 50, ...).
        Each page returns ~25 job cards as HTML fragments.
        """
        params: dict[str, str | int] = {
            "keywords": query,
            "location": location,
            "start": page * 25,
            "sortBy": "DD",  # Date Descending (most recent)
        }

        # Add distance filter if configured
        if self.config.radius_miles:
            params["distance"] = self.config.radius_miles

        # Add remote filter
        if self.profile.preferences.remote_ok:
            # LinkedIn f_WT=2 means remote jobs
            params["f_WT"] = 2

        return f"{self.GUEST_API_BASE}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Listing page parsing
    # ------------------------------------------------------------------

    def parse_listing_page(self, html: str) -> list[JobRecord]:
        """Parse a LinkedIn guest API search results page.

        The guest API returns a fragment of <li> elements, each containing
        a job card with a base-card structure.
        """
        soup = BeautifulSoup(html, "lxml")
        jobs: list[JobRecord] = []

        # LinkedIn guest API wraps each job in a <li> or <div> with class
        # base-card or similar
        job_cards = soup.select(
            "li div.base-card, "
            "div.base-card, "
            "div.job-search-card, "
            "li.jobs-search-results__list-item, "
            "div[class*='base-search-card']"
        )

        if not job_cards:
            # Broader fallback — look for anything with a data-entity-urn
            job_cards = soup.select("[data-entity-urn]")

        for card in job_cards:
            try:
                job = self._parse_card(card)
                if job is not None:
                    jobs.append(job)
            except Exception as e:
                logger.debug("[linkedin] Failed to parse a card: %s", e)
                self.stats.errors += 1

        return jobs

    def _parse_card(self, card: Tag) -> JobRecord | None:
        """Extract job data from a single LinkedIn job card element."""

        # --- Title and URL ---
        title_el = (
            card.select_one("h3.base-search-card__title")
            or card.select_one("h3[class*='title']")
            or card.select_one("a.base-card__full-link")
            or card.select_one("h3 a")
            or card.select_one("a[class*='job-card']")
        )

        # The link is usually on a separate <a> wrapping the card
        link_el = (
            card.select_one("a.base-card__full-link")
            or card.select_one("a[href*='/jobs/view/']")
            or card.select_one("a[data-tracking-control-name]")
        )

        if title_el is None and link_el is None:
            return None

        title = ""
        if title_el:
            title = title_el.get_text(strip=True)
        elif link_el:
            title = link_el.get_text(strip=True)

        if not title:
            return None

        # Build URL
        href = ""
        if link_el:
            href = str(link_el.get("href", ""))
        elif title_el and title_el.name == "a":
            href = str(title_el.get("href", ""))

        if not href:
            # Try data-entity-urn to construct URL
            urn = card.get("data-entity-urn", "")
            if urn:
                job_id = self._extract_id_from_urn(str(urn))
                if job_id:
                    href = f"https://www.linkedin.com/jobs/view/{job_id}/"

        if not href:
            return None

        # Normalize URL — strip tracking params
        url = self._clean_url(href)
        external_id = self._extract_job_id(url) or self._extract_id_from_urn(
            str(card.get("data-entity-urn", ""))
        )

        # --- Company ---
        company_el = (
            card.select_one("h4.base-search-card__subtitle")
            or card.select_one("h4[class*='subtitle']")
            or card.select_one("a[class*='company']")
            or card.select_one("a.hidden-nested-link")
        )
        company = company_el.get_text(strip=True) if company_el else None

        # --- Location ---
        location_el = (
            card.select_one("span.job-search-card__location")
            or card.select_one("span[class*='location']")
        )
        location = location_el.get_text(strip=True) if location_el else None

        # --- Salary (LinkedIn rarely shows this on cards, but sometimes does) ---
        salary_min, salary_max = self._parse_salary(card)

        # --- Date posted ---
        date_el = (
            card.select_one("time")
            or card.select_one("span[class*='date']")
        )
        date_posted = None
        if date_el:
            # LinkedIn uses <time datetime="2026-03-08">
            date_posted = str(date_el.get("datetime", "")) or date_el.get_text(strip=True)

        # --- Snippet (usually not on cards, will be enriched later) ---
        snippet_el = card.select_one("p[class*='description'], div[class*='description']")
        snippet = snippet_el.get_text(strip=True) if snippet_el else None

        return JobRecord(
            source="linkedin",
            external_id=external_id,
            url=url,
            title=title,
            company=company,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            description=snippet,
            raw_html=str(card),
            date_posted=date_posted,
            status="new",
        )

    # ------------------------------------------------------------------
    # URL / ID helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Extract LinkedIn's numeric job ID from a URL like /jobs/view/12345678/."""
        match = re.search(r"/jobs/view/(\d+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _extract_id_from_urn(urn: str) -> str | None:
        """Extract job ID from a LinkedIn URN like 'urn:li:jobPosting:12345678'."""
        match = re.search(r"jobPosting:(\d+)", urn)
        return match.group(1) if match else None

    @staticmethod
    def _clean_url(url: str) -> str:
        """Strip tracking params from LinkedIn job URLs.

        Keeps the clean /jobs/view/{id}/ form.
        """
        match = re.search(r"(https?://[^?]+/jobs/view/\d+/?)", url)
        if match:
            return match.group(1)
        # If it doesn't match the expected pattern, return as-is minus query string
        return url.split("?")[0]

    # ------------------------------------------------------------------
    # Salary parsing
    # ------------------------------------------------------------------

    def _parse_salary(self, card: Tag) -> tuple[float | None, float | None]:
        """Extract salary range from a LinkedIn job card.

        LinkedIn shows salary in formats like:
          - "$80,000/yr - $120,000/yr"
          - "$40.00/hr - $55.00/hr"
        But usually only on detail pages, not search cards.
        """
        salary_el = (
            card.select_one("span[class*='salary']")
            or card.select_one("div[class*='salary']")
        )
        if salary_el is None:
            return None, None

        text = salary_el.get_text(strip=True)
        return self._parse_salary_text(text)

    @staticmethod
    def _parse_salary_text(text: str) -> tuple[float | None, float | None]:
        """Parse salary numbers from text like '$80,000/yr - $120,000/yr'."""
        if not text:
            return None, None

        amounts = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
        if not amounts:
            return None, None

        def parse_amount(s: str) -> float:
            return float(s.replace("$", "").replace(",", ""))

        values = [parse_amount(a) for a in amounts]

        # Detect hourly and annualize
        is_hourly = "/hr" in text.lower() or "hour" in text.lower()
        if is_hourly:
            values = [v * 2080 for v in values]

        if len(values) >= 2:
            return min(values), max(values)
        else:
            return values[0], values[0]

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def enrich_job(self, job: JobRecord) -> JobRecord:
        """Fetch the full job detail page and extract the complete description.

        Uses LinkedIn's guest detail API which returns a lighter HTML fragment.
        Falls back to Playwright for the full page if the API fails.
        """
        job_id = self._extract_job_id(job.url)
        if not job_id:
            return job

        # --- Strategy 1: Guest detail API (lightweight, often works) ---
        detail_url = f"{self.DETAIL_API_BASE}/{job_id}"
        result = await self._fetch_httpx(detail_url)
        if result.status == FetchStatus.SUCCESS and result.html:
            desc, salary_min, salary_max = self._extract_detail_data(result.html)
            if desc and len(desc) > 100:
                job.description = desc
                if salary_min and not job.salary_min:
                    job.salary_min = salary_min
                if salary_max and not job.salary_max:
                    job.salary_max = salary_max
                return job

        # --- Strategy 2: Playwright for the full page ---
        pw_result = await self._fetch_playwright(job.url)
        if pw_result.status == FetchStatus.SUCCESS and pw_result.html:
            desc, salary_min, salary_max = self._extract_detail_data(pw_result.html)
            if desc and len(desc) > 100:
                job.description = desc
                if salary_min and not job.salary_min:
                    job.salary_min = salary_min
                if salary_max and not job.salary_max:
                    job.salary_max = salary_max

        return job

    def _extract_detail_data(
        self, html: str
    ) -> tuple[str | None, float | None, float | None]:
        """Extract description and salary from a LinkedIn detail page.

        Returns (description, salary_min, salary_max).
        """
        soup = BeautifulSoup(html, "lxml")

        # --- Description ---
        description = None
        for selector in [
            "div.show-more-less-html__markup",
            "div[class*='description__text']",
            "div.description__text",
            "section[class*='description']",
            "div[class*='show-more-less']",
            "article",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if text and len(text) > 100:
                    description = text
                    break

        # --- Salary from detail page ---
        salary_min, salary_max = None, None
        salary_el = (
            soup.select_one("div[class*='salary']")
            or soup.select_one("span[class*='compensation']")
        )
        if salary_el:
            salary_min, salary_max = self._parse_salary_text(
                salary_el.get_text(strip=True)
            )

        return description, salary_min, salary_max

    # ------------------------------------------------------------------
    # Run override — enrich jobs missing descriptions
    # ------------------------------------------------------------------

    async def run(self) -> "ScraperStats":
        """Override base run to add detail-page enrichment for jobs missing descriptions."""
        from app.scrapers.base import ScraperStats as _Stats

        stats = await super().run()

        # LinkedIn cards almost never have descriptions — enrich them
        new_jobs = await self.db.get_new_jobs(limit=50)
        enriched = 0

        for job in new_jobs:
            if job.id is None or job.source != "linkedin":
                continue
            if not job.description or len(job.description) < 200:
                logger.info("[linkedin] Enriching: %s @ %s", job.title, job.company)
                enriched_job = await self.enrich_job(job)
                if enriched_job.description and len(enriched_job.description) > len(
                    job.description or ""
                ):
                    await self.db.conn.execute(
                        "UPDATE jobs SET description = ?, salary_min = COALESCE(?, salary_min), "
                        "salary_max = COALESCE(?, salary_max) WHERE id = ?",
                        (
                            enriched_job.description,
                            enriched_job.salary_min,
                            enriched_job.salary_max,
                            job.id,
                        ),
                    )
                    await self.db.conn.commit()
                    enriched += 1
                await self.random_delay()

        if enriched:
            logger.info("[linkedin] Enriched %d jobs with full descriptions", enriched)

        return stats
