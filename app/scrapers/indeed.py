"""Indeed job board scraper.

Scrapes Indeed search results and job detail pages using httpx + BeautifulSoup4.
Indeed's HTML structure changes periodically, so the parsing logic here targets
the current (early 2026) layout with fallback heuristics.

Rate limiting is aggressive by default — Indeed is known to block scrapers fast.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus, urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from app.config import BoardConfig, ProfileConfig
from app.database import Database, JobRecord
from app.scrapers.base import BaseScraper, FetchStatus

logger = logging.getLogger(__name__)


class IndeedScraper(BaseScraper):
    """Scraper for Indeed.com job listings."""

    SOURCE_NAME = "indeed"

    def __init__(
        self,
        board_config: BoardConfig,
        profile: ProfileConfig,
        db: Database,
    ) -> None:
        super().__init__(board_config, profile, db)

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, location: str, page: int) -> str:
        """Build an Indeed search URL.

        Indeed uses `start=` param for pagination (0, 10, 20, ...).
        """
        params: dict[str, str | int] = {
            "q": query,
            "l": location,
            "start": page * 10,
            "sort": "date",  # newest first
        }

        # Add radius if configured
        if self.config.radius_miles:
            params["radius"] = self.config.radius_miles

        # Add remote filter if preferred
        if self.profile.preferences.remote_ok:
            params["remotejob"] = "032b3046-06a3-4876-8dfd-474eb5e7ed11"

        base = self.config.base_url or "https://www.indeed.com"
        return f"{base}/jobs?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Listing page parsing
    # ------------------------------------------------------------------

    def parse_listing_page(self, html: str) -> list[JobRecord]:
        """Parse an Indeed search results page.

        Indeed renders job cards in a few different structures. We try
        multiple selectors to handle layout variations.
        """
        soup = BeautifulSoup(html, "lxml")
        jobs: list[JobRecord] = []

        # Strategy 1: Look for job cards via data attributes
        job_cards = soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div[data-jk]")

        # Strategy 2: Fallback — look for mosaic provider script data
        if not job_cards:
            jobs_from_script = self._parse_from_script_data(soup)
            if jobs_from_script:
                return jobs_from_script

        # Strategy 3: Broader selector
        if not job_cards:
            job_cards = soup.select("div[class*='cardOutline'], div[class*='result']")

        for card in job_cards:
            try:
                job = self._parse_card(card)
                if job is not None:
                    jobs.append(job)
            except Exception as e:
                logger.debug("[indeed] Failed to parse a card: %s", e)
                self.stats.errors += 1

        return jobs

    def _parse_card(self, card: Tag) -> JobRecord | None:
        """Extract job data from a single Indeed job card element."""
        # --- Title and URL ---
        title_el = (
            card.select_one("h2.jobTitle a")
            or card.select_one("a[data-jk]")
            or card.select_one("h2 a")
            or card.select_one("a.jcs-JobTitle")
        )
        if title_el is None:
            return None

        title = title_el.get_text(strip=True)
        if not title:
            return None

        # Build the full URL
        href = title_el.get("href", "")
        if not href:
            # Try data-jk attribute for job key
            jk = title_el.get("data-jk") or card.get("data-jk", "")
            if jk:
                href = f"/viewjob?jk={jk}"
            else:
                return None

        base = self.config.base_url or "https://www.indeed.com"
        url = urljoin(base, str(href))

        # Extract external_id (Indeed's jk parameter)
        external_id = self._extract_job_key(url, card)

        # --- Company ---
        company_el = (
            card.select_one("[data-testid='company-name']")
            or card.select_one("span.companyName")
            or card.select_one("span[class*='company']")
        )
        company = company_el.get_text(strip=True) if company_el else None

        # --- Location ---
        location_el = (
            card.select_one("[data-testid='text-location']")
            or card.select_one("div.companyLocation")
            or card.select_one("span[class*='location']")
        )
        location = location_el.get_text(strip=True) if location_el else None

        # --- Salary ---
        salary_min, salary_max = self._parse_salary(card)

        # --- Snippet / description ---
        snippet_el = (
            card.select_one("div.job-snippet")
            or card.select_one("div[class*='snippet']")
            or card.select_one("table.jobCardShelfContainer")
        )
        snippet = snippet_el.get_text(strip=True) if snippet_el else None

        # --- Date posted ---
        date_el = card.select_one("span.date, span[class*='date']")
        date_posted = date_el.get_text(strip=True) if date_el else None

        return JobRecord(
            source="indeed",
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

    def _extract_job_key(self, url: str, card: Tag) -> str | None:
        """Extract Indeed's job key (jk) from URL or card attributes."""
        # From URL parameter
        match = re.search(r"[?&]jk=([a-f0-9]+)", url)
        if match:
            return match.group(1)

        # From card data attribute
        jk = card.get("data-jk")
        if jk:
            return str(jk)

        return None

    def _parse_salary(self, card: Tag) -> tuple[float | None, float | None]:
        """Extract salary range from a job card.

        Indeed shows salary in various formats:
          - "$80,000 - $100,000 a year"
          - "$40 - $50 an hour"
          - "From $90,000 a year"
          - "Up to $120,000 a year"
        """
        salary_el = (
            card.select_one("[data-testid='attribute_snippet_testid']")
            or card.select_one("div.salary-snippet-container")
            or card.select_one("span[class*='salary']")
            or card.select_one("div[class*='salary']")
        )
        if salary_el is None:
            return None, None

        text = salary_el.get_text(strip=True)
        return self._parse_salary_text(text)

    @staticmethod
    def _parse_salary_text(text: str) -> tuple[float | None, float | None]:
        """Parse salary numbers from text like '$80,000 - $100,000 a year'."""
        if not text:
            return None, None

        # Find all dollar amounts
        amounts = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
        if not amounts:
            return None, None

        def parse_amount(s: str) -> float:
            return float(s.replace("$", "").replace(",", ""))

        values = [parse_amount(a) for a in amounts]

        # Detect hourly rates and annualize
        is_hourly = "hour" in text.lower() or "hr" in text.lower()
        if is_hourly:
            values = [v * 2080 for v in values]  # 40hr/week * 52 weeks

        if len(values) >= 2:
            return min(values), max(values)
        elif "from" in text.lower() or "at least" in text.lower():
            return values[0], None
        elif "up to" in text.lower():
            return None, values[0]
        else:
            return values[0], values[0]

    # ------------------------------------------------------------------
    # Script data extraction (fallback)
    # ------------------------------------------------------------------

    def _parse_from_script_data(self, soup: BeautifulSoup) -> list[JobRecord]:
        """Try to extract job data from Indeed's embedded JSON scripts.

        Indeed sometimes embeds job data in script tags as part of their
        mosaic/next.js rendering. This is more reliable than HTML parsing
        when it works.
        """
        jobs: list[JobRecord] = []

        for script in soup.find_all("script", type="application/json"):
            try:
                data = json.loads(script.string or "")
                jobs.extend(self._extract_jobs_from_json(data))
            except (json.JSONDecodeError, TypeError):
                continue

        # Also check for window.__MOSAIC_DATA or similar globals
        for script in soup.find_all("script"):
            text = script.string or ""
            if "mosaic-provider-jobcards" in text or "jobResults" in text:
                try:
                    # Try to find JSON blob in the script
                    match = re.search(r"window\.__[A-Z_]+=(\{.+?\});", text)
                    if match:
                        data = json.loads(match.group(1))
                        jobs.extend(self._extract_jobs_from_json(data))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue

        return jobs

    def _extract_jobs_from_json(self, data: object, depth: int = 0) -> list[JobRecord]:
        """Recursively search a JSON structure for job listings.

        Indeed's JSON is deeply nested and changes structure frequently.
        We look for objects that have job-like fields (title, company, etc).
        """
        if depth > 10:
            return []

        jobs: list[JobRecord] = []

        if isinstance(data, dict):
            # Check if this dict looks like a job record
            if "title" in data and ("company" in data or "companyName" in data):
                job = self._dict_to_job(data)
                if job:
                    jobs.append(job)
            else:
                # Recurse into values
                for value in data.values():
                    jobs.extend(self._extract_jobs_from_json(value, depth + 1))

        elif isinstance(data, list):
            for item in data:
                jobs.extend(self._extract_jobs_from_json(item, depth + 1))

        return jobs

    def _dict_to_job(self, d: dict) -> JobRecord | None:  # type: ignore[type-arg]
        """Convert a JSON dict that looks like a job into a JobRecord."""
        title = d.get("title") or d.get("jobTitle") or d.get("displayTitle")
        if not title or not isinstance(title, str):
            return None

        company = d.get("company") or d.get("companyName") or d.get("employer", {}).get("name")
        location = d.get("location") or d.get("formattedLocation") or d.get("jobLocationCity")

        # Build URL from job key
        jk = d.get("jobkey") or d.get("jk") or d.get("id")
        if jk:
            base = self.config.base_url or "https://www.indeed.com"
            url = f"{base}/viewjob?jk={jk}"
        else:
            return None

        # Salary
        salary_text = d.get("salary") or d.get("salaryText") or ""
        if isinstance(salary_text, dict):
            salary_text = salary_text.get("text", "")
        salary_min, salary_max = self._parse_salary_text(str(salary_text))

        # Description snippet
        description = d.get("snippet") or d.get("description") or d.get("jobSnippet")
        if isinstance(description, dict):
            description = description.get("text", "")

        return JobRecord(
            source="indeed",
            external_id=str(jk),
            url=url,
            title=str(title),
            company=str(company) if company else None,
            location=str(location) if location else None,
            salary_min=salary_min,
            salary_max=salary_max,
            description=str(description) if description else None,
            raw_html=json.dumps(d, default=str),
            date_posted=d.get("formattedRelativeTime") or d.get("pubDate"),
            status="new",
        )

    # ------------------------------------------------------------------
    # Detail page fetching (enrich listings with full descriptions)
    # ------------------------------------------------------------------

    async def enrich_job(self, job: JobRecord) -> JobRecord:
        """Fetch the full job detail page and extract the complete description.

        Call this after initial listing parse to get the full job text
        instead of just the snippet.
        """
        result = await self.fetch_page(job.url)
        if result.status != FetchStatus.SUCCESS or result.html is None:
            return job

        html = result.html

        soup = BeautifulSoup(html, "lxml")

        # Save full HTML snapshot
        self.save_html_snapshot(html, "indeed_detail", job.url)

        # Extract full description
        desc_el = (
            soup.select_one("#jobDescriptionText")
            or soup.select_one("div[id*='jobDescription']")
            or soup.select_one("div.jobsearch-jobDescriptionText")
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)
            job.raw_html = str(desc_el)

        # Try to get salary if we didn't have it from listing
        if job.salary_min is None and job.salary_max is None:
            salary_el = soup.select_one("#salaryInfoAndJobType, div[class*='salary']")
            if salary_el:
                job.salary_min, job.salary_max = self._parse_salary_text(
                    salary_el.get_text(strip=True)
                )

        return job

    async def run(self) -> "ScraperStats":
        """Override base run to add detail-page enrichment for jobs missing descriptions."""
        stats = await super().run()

        # Enrich jobs that are missing descriptions or only have short snippets
        new_jobs = await self.db.get_new_jobs(limit=50)
        enriched = 0
        for job in new_jobs:
            if job.id is None:
                continue
            # Fetch full description if missing or just a snippet
            if not job.description or len(job.description) < 200:
                logger.info("[indeed] Enriching: %s", job.title)
                enriched_job = await self.enrich_job(job)
                if enriched_job.description and len(enriched_job.description) > len(job.description or ""):
                    await self.db.conn.execute(
                        "UPDATE jobs SET description = ?, raw_html = ? WHERE id = ?",
                        (enriched_job.description, enriched_job.raw_html, job.id),
                    )
                    await self.db.conn.commit()
                    enriched += 1
                await self.random_delay()

        if enriched:
            logger.info("[indeed] Enriched %d jobs with full descriptions", enriched)

        return stats
