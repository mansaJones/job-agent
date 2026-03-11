"""Dice.com job board scraper.

Dice is a tech-focused job board. Their search pages are a React SPA,
so we default to Playwright for search result rendering. Job detail
pages also require JS rendering.

Search URL format (as of early 2026):
    https://www.dice.com/jobs?q=QUERY&location=LOCATION
        &radius=RADIUS&radiusUnit=mi&page=PAGE
        &pageSize=20&language=en&countryCode=US

Job detail URL format:
    https://www.dice.com/job-detail/UUID

Confidence note:
    CSS selectors were built from Dice's known data-cy attribute pattern
    and common React SPA conventions. If selectors break, check the
    HTML snapshots saved in data/html_snapshots/ and update accordingly.
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


class DiceScraper(BaseScraper):
    """Scraper for Dice.com job listings."""

    SOURCE_NAME = "dice"

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
        """Build a Dice search URL.

        Dice uses 1-indexed `page=` param for pagination with `pageSize=20`.
        """
        params: dict[str, str | int] = {
            "q": query,
            "location": location,
            "countryCode": "US",
            "radius": self.config.radius_miles or 30,
            "radiusUnit": "mi",
            "page": page + 1,  # Dice is 1-indexed; base run() sends 0-indexed
            "pageSize": 20,
            "language": "en",
        }

        # Add filters for remote if preferred
        if self.profile.preferences.remote_ok:
            params["filters.isRemote"] = "true"

        base = self.config.base_url or "https://www.dice.com"
        return f"{base}/jobs?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Listing page parsing
    # ------------------------------------------------------------------

    def parse_listing_page(self, html: str) -> list[JobRecord]:
        """Parse a Dice search results page.

        Dice renders job cards as a React SPA. We try multiple strategies:
          1. Embedded JSON (Next.js __NEXT_DATA__ or similar hydration payloads)
          2. HTML card parsing with known CSS selectors
          3. Broad fallback: any <a> tags that link to /job-detail/
        """
        soup = BeautifulSoup(html, "lxml")

        # Strategy 1: Try embedded JSON data (Next.js hydration)
        jobs_from_json = self._parse_from_next_data(soup)
        if jobs_from_json:
            logger.info("[dice] Extracted %d jobs from embedded JSON", len(jobs_from_json))
            return jobs_from_json

        # Strategy 2: HTML card parsing with known selectors
        jobs: list[JobRecord] = []
        job_cards = self._find_job_cards(soup)

        for card in job_cards:
            try:
                job = self._parse_card(card)
                if job is not None:
                    jobs.append(job)
            except Exception as e:
                logger.debug("[dice] Failed to parse a card: %s", e)
                self.stats.errors += 1

        if jobs:
            logger.info("[dice] Extracted %d jobs from HTML cards", len(jobs))
            return jobs

        # Strategy 3: Fallback — find any links to /job-detail/
        jobs_from_links = self._parse_from_links(soup)
        if jobs_from_links:
            logger.info("[dice] Extracted %d jobs from job-detail links (fallback)",
                        len(jobs_from_links))
        else:
            logger.warning("[dice] No jobs found on page — HTML may have changed. "
                           "Check snapshots in data/html_snapshots/")

        return jobs_from_links

    def _find_job_cards(self, soup: BeautifulSoup) -> list[Tag]:
        """Find job card elements using multiple selector strategies."""
        # Dice commonly uses data-cy attributes for test IDs
        # and wraps cards in custom elements or specific divs
        selectors = [
            # data-cy based selectors (Dice's testing convention)
            "[data-cy='search-card']",
            # Common Dice card patterns
            "div.card.search-card",
            "dhi-search-card",  # custom web component
            "div[class*='search-card']",
            "div[class*='SearchCard']",
            # Generic card containers that link to job details
            "div[class*='job-card']",
            "div[class*='JobCard']",
            "a[href*='/job-detail/']",
            # Broader: any card-like container
            "div[class*='card'][class*='job']",
        ]

        for selector in selectors:
            cards = soup.select(selector)
            if cards:
                logger.debug("[dice] Found %d cards with selector: %s", len(cards), selector)
                return cards

        return []

    def _parse_card(self, card: Tag) -> JobRecord | None:
        """Extract job data from a single Dice job card element."""
        # --- Title and URL ---
        title_el = (
            card.select_one("[data-cy='card-title-link']")
            or card.select_one("a[class*='cardTitle']")
            or card.select_one("a[class*='card-title']")
            or card.select_one("h5 a")
            or card.select_one("a[href*='/job-detail/']")
        )

        # If the card itself is an <a> tag pointing to job-detail
        if title_el is None and card.name == "a":
            href = card.get("href", "")
            if "/job-detail/" in str(href):
                title_el = card

        if title_el is None:
            return None

        title = title_el.get_text(strip=True)
        if not title:
            return None

        # Build the full URL
        href = title_el.get("href", "")
        if not href:
            return None

        base = self.config.base_url or "https://www.dice.com"
        url = urljoin(base, str(href))

        # Only keep /job-detail/ URLs
        if "/job-detail/" not in url:
            return None

        # Extract external_id (UUID from the URL)
        external_id = self._extract_job_id(url)

        # --- Company ---
        company_el = (
            card.select_one("[data-cy='search-result-company-name']")
            or card.select_one("[data-cy='card-company']")
            or card.select_one("a[class*='companyName']")
            or card.select_one("span[class*='company']")
            or card.select_one("[class*='Company']")
        )
        company = company_el.get_text(strip=True) if company_el else None

        # --- Location ---
        location_el = (
            card.select_one("[data-cy='search-result-location']")
            or card.select_one("[data-cy='card-location']")
            or card.select_one("span[class*='location']")
            or card.select_one("[class*='Location']")
        )
        location = location_el.get_text(strip=True) if location_el else None

        # --- Salary ---
        salary_min, salary_max = self._parse_salary_from_card(card)

        # --- Date posted ---
        date_el = (
            card.select_one("[data-cy='card-posted-date']")
            or card.select_one("span[class*='posted']")
            or card.select_one("[class*='Posted']")
            or card.select_one("[class*='date']")
        )
        date_posted = date_el.get_text(strip=True) if date_el else None

        # --- Description snippet ---
        snippet_el = (
            card.select_one("[data-cy='card-summary']")
            or card.select_one("[class*='summary']")
            or card.select_one("[class*='description']")
        )
        snippet = snippet_el.get_text(separator=" ", strip=True) if snippet_el else None

        return JobRecord(
            source="dice",
            external_id=external_id,
            url=url,
            title=title,
            company=company,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            description=snippet,
            raw_html=str(card)[:5000],  # cap raw HTML size
            date_posted=date_posted,
            status="new",
        )

    @staticmethod
    def _extract_job_id(url: str) -> str | None:
        """Extract Dice's job UUID from a URL like /job-detail/abc123-def456."""
        match = re.search(r"/job-detail/([a-f0-9-]+)", url, re.IGNORECASE)
        return match.group(1) if match else None

    def _parse_salary_from_card(self, card: Tag) -> tuple[float | None, float | None]:
        """Extract salary range from a Dice job card."""
        salary_el = (
            card.select_one("[data-cy='card-salary']")
            or card.select_one("[class*='salary']")
            or card.select_one("[class*='Salary']")
            or card.select_one("[class*='compensation']")
        )
        if salary_el is None:
            return None, None

        text = salary_el.get_text(strip=True)
        return self._parse_salary_text(text)

    @staticmethod
    def _parse_salary_text(text: str) -> tuple[float | None, float | None]:
        """Parse salary numbers from text like '$120,000 - $150,000/yr'."""
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
        is_hourly = any(kw in text.lower() for kw in ("hour", "/hr", "per hr", "hourly"))
        if is_hourly:
            values = [v * 2080 for v in values]  # 40hr/week * 52 weeks

        if len(values) >= 2:
            return min(values), max(values)
        elif len(values) == 1:
            return values[0], values[0]
        return None, None

    # ------------------------------------------------------------------
    # JSON extraction from embedded scripts
    # ------------------------------------------------------------------

    def _parse_from_next_data(self, soup: BeautifulSoup) -> list[JobRecord]:
        """Try to extract job data from Next.js __NEXT_DATA__ or similar hydration scripts.

        Dice is a React SPA that may embed search results as JSON in a script tag
        during server-side rendering.
        """
        jobs: list[JobRecord] = []

        # Try __NEXT_DATA__ (Next.js convention)
        next_data_script = soup.select_one("script#__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                data = json.loads(next_data_script.string)
                jobs.extend(self._extract_jobs_from_json(data))
                if jobs:
                    return jobs
            except (json.JSONDecodeError, TypeError):
                pass

        # Try other embedded JSON patterns
        for script in soup.find_all("script", type="application/json"):
            try:
                data = json.loads(script.string or "")
                jobs.extend(self._extract_jobs_from_json(data))
            except (json.JSONDecodeError, TypeError):
                continue

        # Try window.__APP_DATA__ or similar globals
        for script in soup.find_all("script"):
            text = script.string or ""
            if "jobSearch" in text or "searchResults" in text or "jobList" in text:
                # Try to find JSON blob in the script
                for pattern in [
                    r"window\.__[A-Z_]+=(\{.+?\});",
                    r"self\.__next_f\.push\(\[.*?,\"(.+?)\"\]\)",
                ]:
                    match = re.search(pattern, text, re.DOTALL)
                    if match:
                        try:
                            data = json.loads(match.group(1))
                            jobs.extend(self._extract_jobs_from_json(data))
                        except (json.JSONDecodeError, TypeError):
                            continue

        return jobs

    def _extract_jobs_from_json(self, data: object, depth: int = 0) -> list[JobRecord]:
        """Recursively search a JSON structure for job listings.

        Looks for objects that have job-like fields typical of Dice's API:
            - title/jobTitle + company/companyName
            - detailsPageUrl or id (UUID)
        """
        if depth > 12:
            return []

        jobs: list[JobRecord] = []

        if isinstance(data, dict):
            # Check if this dict looks like a Dice job record
            has_title = any(k in data for k in ("title", "jobTitle", "displayTitle"))
            has_company = any(k in data for k in ("company", "companyName",
                                                   "employerName", "hiringCompany"))
            if has_title and has_company:
                job = self._dict_to_job(data)
                if job:
                    jobs.append(job)
            else:
                for value in data.values():
                    jobs.extend(self._extract_jobs_from_json(value, depth + 1))

        elif isinstance(data, list):
            for item in data:
                jobs.extend(self._extract_jobs_from_json(item, depth + 1))

        return jobs

    def _dict_to_job(self, d: dict) -> JobRecord | None:
        """Convert a JSON dict that looks like a Dice job into a JobRecord."""
        title = d.get("title") or d.get("jobTitle") or d.get("displayTitle")
        if not title or not isinstance(title, str):
            return None

        company = (
            d.get("company") or d.get("companyName") or d.get("employerName")
            or d.get("hiringCompany")
        )
        if isinstance(company, dict):
            company = company.get("name") or company.get("companyName")

        location = (
            d.get("location") or d.get("jobLocation") or d.get("formattedLocation")
        )
        if isinstance(location, dict):
            location = location.get("displayName") or location.get("city")

        # Build URL from job ID or detailsPageUrl
        details_url = d.get("detailsPageUrl") or d.get("jobDetailUrl")
        job_id = d.get("id") or d.get("jobId")

        if details_url:
            base = self.config.base_url or "https://www.dice.com"
            url = urljoin(base, str(details_url))
        elif job_id:
            base = self.config.base_url or "https://www.dice.com"
            url = f"{base}/job-detail/{job_id}"
        else:
            return None

        external_id = self._extract_job_id(url) or str(job_id) if job_id else None

        # Salary
        salary_min, salary_max = None, None
        salary_data = d.get("salary") or d.get("compensation") or d.get("salaryRange")
        if isinstance(salary_data, dict):
            salary_min = self._safe_float(salary_data.get("min") or salary_data.get("minimum"))
            salary_max = self._safe_float(salary_data.get("max") or salary_data.get("maximum"))
        elif isinstance(salary_data, str):
            salary_min, salary_max = self._parse_salary_text(salary_data)

        # Description
        description = d.get("description") or d.get("summary") or d.get("snippet")
        if isinstance(description, dict):
            description = description.get("text", "")

        # Date posted
        date_posted = d.get("postedDate") or d.get("datePosted") or d.get("formattedDate")

        return JobRecord(
            source="dice",
            external_id=external_id,
            url=url,
            title=str(title),
            company=str(company) if company else None,
            location=str(location) if location else None,
            salary_min=salary_min,
            salary_max=salary_max,
            description=str(description) if description else None,
            raw_html=json.dumps(d, default=str)[:5000],
            date_posted=str(date_posted) if date_posted else None,
            status="new",
        )

    @staticmethod
    def _safe_float(val: object) -> float | None:
        """Safely convert to float, returning None on failure."""
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Link-based fallback parsing
    # ------------------------------------------------------------------

    def _parse_from_links(self, soup: BeautifulSoup) -> list[JobRecord]:
        """Last resort: find all links to /job-detail/ and build minimal records."""
        jobs: list[JobRecord] = []
        seen_urls: set[str] = set()

        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag["href"])
            if "/job-detail/" not in href:
                continue

            base = self.config.base_url or "https://www.dice.com"
            url = urljoin(base, href)

            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = a_tag.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            external_id = self._extract_job_id(url)

            jobs.append(JobRecord(
                source="dice",
                external_id=external_id,
                url=url,
                title=title,
                company=None,
                location=None,
                salary_min=None,
                salary_max=None,
                description=None,
                raw_html=str(a_tag.parent)[:3000] if a_tag.parent else str(a_tag),
                date_posted=None,
                status="new",
            ))

        return jobs

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def enrich_job(self, job: JobRecord) -> JobRecord:
        """Fetch the full Dice job detail page and extract the complete description.

        Dice detail pages are React-rendered, so Playwright is the primary strategy.
        We try httpx first as a cheap shot (sometimes SSR works).
        """
        # --- Strategy 1: httpx (fast, may get SSR content) ---
        result = await self._fetch_httpx(job.url)
        if result.status == FetchStatus.SUCCESS and result.html:
            logger.info("[dice] httpx detail fetch got %d bytes for %s", len(result.html), job.url)
            self.save_html_snapshot(result.html, "dice_detail_httpx", job.url)
            desc, sal_min, sal_max = self._extract_detail_data(result.html)
            if desc and len(desc) > 100:
                logger.info("[dice] Enriched via httpx (%d chars): %s", len(desc), job.title)
                job.description = desc
                if sal_min and not job.salary_min:
                    job.salary_min = sal_min
                if sal_max and not job.salary_max:
                    job.salary_max = sal_max
                return job

        # --- Strategy 2: Playwright (needed for React SPA) ---
        logger.info("[dice] Trying Playwright for detail: %s", job.title)
        pw_result = await self._fetch_playwright(job.url)
        if pw_result.status == FetchStatus.SUCCESS and pw_result.html:
            logger.info("[dice] Playwright detail fetch got %d bytes", len(pw_result.html))
            self.save_html_snapshot(pw_result.html, "dice_detail_pw", job.url)
            desc, sal_min, sal_max = self._extract_detail_data(pw_result.html)
            if desc and len(desc) > 100:
                logger.info("[dice] Enriched via Playwright (%d chars): %s", len(desc), job.title)
                job.description = desc
                if sal_min and not job.salary_min:
                    job.salary_min = sal_min
                if sal_max and not job.salary_max:
                    job.salary_max = sal_max
            else:
                logger.warning("[dice] Playwright HTML had no extractable description for: %s "
                               "(html=%d bytes, desc=%s)",
                               job.title, len(pw_result.html),
                               f"{len(desc)} chars" if desc else "None")
        else:
            logger.warning("[dice] Playwright fetch failed for: %s (status=%s)",
                           job.title, pw_result.status.value if pw_result else "None")

        return job

    def _extract_detail_data(
        self, html: str
    ) -> tuple[str | None, float | None, float | None]:
        """Extract description and salary from a Dice detail page.

        Uses a 3-strategy approach:
          1. JSON-LD structured data
          2. Known CSS selectors for the detail page
          3. Largest text block fallback
        """
        soup = BeautifulSoup(html, "lxml")

        # ----------------------------------------------------------
        # Strategy 1: JSON-LD structured data
        # ----------------------------------------------------------
        desc_ld, sal_min_ld, sal_max_ld = self._parse_jsonld(soup)
        if desc_ld and len(desc_ld) > 100:
            logger.debug("[dice] Description extracted via JSON-LD (%d chars)", len(desc_ld))
            return desc_ld, sal_min_ld, sal_max_ld

        # ----------------------------------------------------------
        # Strategy 2: Known CSS selectors
        # ----------------------------------------------------------
        description = None
        css_selectors = [
            # data-cy based (Dice convention)
            "[data-cy='jobDescription']",
            "[data-testid='jobDescriptionHtml']",
            # Common class-based selectors
            "div#jobDescription",
            "div.job-description",
            "div[class*='jobDescription']",
            "div[class*='JobDescription']",
            "div[class*='job-description']",
            # Section-level
            "section[class*='description']",
            "div[class*='description__text']",
            # Broader
            "div[class*='details'] div[class*='content']",
            "article",
        ]

        for selector in css_selectors:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if text and len(text) > 100:
                    description = text
                    logger.debug("[dice] Description via CSS '%s' (%d chars)", selector, len(text))
                    break

        if description:
            # Try to find salary on the page too
            sal_min, sal_max = self._extract_salary_from_detail(soup)
            return description, sal_min, sal_max

        # ----------------------------------------------------------
        # Strategy 3: Largest text block fallback
        # ----------------------------------------------------------
        description = self._extract_longest_text_block(soup)
        if description:
            logger.debug("[dice] Description via longest-block fallback (%d chars)", len(description))
            sal_min, sal_max = self._extract_salary_from_detail(soup)
            return description, sal_min, sal_max

        return None, None, None

    def _parse_jsonld(
        self, soup: BeautifulSoup
    ) -> tuple[str | None, float | None, float | None]:
        """Extract job data from JSON-LD structured data."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            # Handle both single objects and arrays
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "JobPosting":
                    continue

                desc = item.get("description", "")
                if isinstance(desc, str) and desc:
                    # Strip HTML tags if the description contains them
                    if "<" in desc:
                        desc_soup = BeautifulSoup(desc, "lxml")
                        desc = desc_soup.get_text(separator="\n", strip=True)

                sal_min, sal_max = None, None
                base_salary = item.get("baseSalary") or item.get("estimatedSalary")
                if isinstance(base_salary, dict):
                    value = base_salary.get("value", {})
                    if isinstance(value, dict):
                        sal_min = self._safe_float(value.get("minValue"))
                        sal_max = self._safe_float(value.get("maxValue"))

                return desc, sal_min, sal_max

        return None, None, None

    def _extract_salary_from_detail(
        self, soup: BeautifulSoup
    ) -> tuple[float | None, float | None]:
        """Try to extract salary from a detail page."""
        for selector in [
            "[data-cy='salary']",
            "[class*='salary']",
            "[class*='Salary']",
            "[class*='compensation']",
            "[class*='Compensation']",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                sal_min, sal_max = self._parse_salary_text(text)
                if sal_min or sal_max:
                    return sal_min, sal_max
        return None, None

    @staticmethod
    def _extract_longest_text_block(soup: BeautifulSoup) -> str | None:
        """Fallback: find the longest text block on the page.

        Skips nav, header, footer, and script elements.
        """
        skip_tags = {"nav", "header", "footer", "script", "style", "noscript", "meta", "link"}
        candidates: list[str] = []

        for el in soup.find_all(["div", "section", "article", "main"]):
            # Skip if inside a nav/header/footer
            if any(parent.name in skip_tags for parent in el.parents):
                continue

            text = el.get_text(separator="\n", strip=True)
            if text and len(text) > 200:
                candidates.append(text)

        if not candidates:
            return None

        # Return the longest block
        longest = max(candidates, key=len)
        return longest if len(longest) > 200 else None

    # ------------------------------------------------------------------
    # Override run() for enrichment
    # ------------------------------------------------------------------

    async def run(self) -> "ScraperStats":
        """Override base run to add detail-page enrichment for jobs missing descriptions.

        Since Dice is a SPA, search results often come with only title/company/URL.
        We enrich each new job by fetching its detail page.
        """
        from app.scrapers.base import ScraperStats as _Stats

        # Force Playwright-first for search pages — Dice is a React SPA
        # and httpx will get an empty HTML shell
        self.health.playwright_escalated = True
        logger.info("[dice] Playwright-first mode enabled (React SPA)")

        stats = await super().run()

        # Enrich jobs missing descriptions
        new_jobs = await self.db.get_new_jobs(limit=50)
        enriched = 0

        for job in new_jobs:
            if job.id is None or job.source != "dice":
                continue
            if not job.description or len(job.description) < 200:
                logger.info("[dice] Enriching: %s @ %s", job.title, job.company)
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
            logger.info("[dice] Enriched %d jobs with full descriptions", enriched)

        return stats
