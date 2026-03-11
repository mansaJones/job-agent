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

        # The link is usually on a separate <a> wrapping the card.
        # data-tracking-control-name is the most stable selector — LinkedIn
        # uses it for analytics and rarely changes it.
        link_el = (
            card.select_one("a.base-card__full-link")
            or card.select_one("a[data-tracking-control-name='public_jobs_jserp-result_search-card']")
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

        # Reject non-job URLs (company pages, profiles, etc.)
        if not self._is_job_url(url):
            logger.debug("[linkedin] Skipping non-job URL: %s", url)
            return None

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
        """Extract LinkedIn's numeric job ID from a URL.

        Handles both formats:
          - /jobs/view/12345678/             (clean numeric)
          - /jobs/view/some-title-slug-12345678  (slug with trailing ID)
        """
        # Try clean numeric first: /jobs/view/12345678
        match = re.search(r"/jobs/view/(\d+)/?$", url)
        if match:
            return match.group(1)

        # Slug-style: /jobs/view/senior-engineer-at-google-12345678
        # The numeric job ID is always the last number in the slug
        match = re.search(r"/jobs/view/[^?/]+-(\d{5,})/?", url)
        if match:
            return match.group(1)

        # Last resort: just find any long number sequence in a /jobs/view/ URL
        match = re.search(r"/jobs/view/.*?(\d{5,})", url)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_id_from_urn(urn: str) -> str | None:
        """Extract job ID from a LinkedIn URN like 'urn:li:jobPosting:12345678'."""
        match = re.search(r"jobPosting:(\d+)", urn)
        return match.group(1) if match else None

    @staticmethod
    def _is_job_url(url: str) -> bool:
        """Check if a URL is actually a job listing (not a company page, profile, etc)."""
        return "/jobs/view/" in url

    @staticmethod
    def _clean_url(url: str) -> str:
        """Strip tracking params from LinkedIn job URLs.

        Handles both formats:
          - /jobs/view/12345678/?tracking=stuff  → /jobs/view/12345678/
          - /jobs/view/slug-title-12345678?stuff  → /jobs/view/slug-title-12345678
        """
        # Strip query params first
        clean = url.split("?")[0]

        # If it's a /jobs/view/ URL, keep it as-is (with or without slug)
        if "/jobs/view/" in clean:
            return clean

        return clean

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
            logger.warning("[linkedin] Could not extract job ID from URL: %s", job.url)
            return job

        # --- Strategy 1: Guest detail API (lightweight, often works) ---
        detail_url = f"{self.DETAIL_API_BASE}/{job_id}"
        result = await self._fetch_httpx(detail_url)

        if result.status == FetchStatus.SUCCESS and result.html:
            html_len = len(result.html)
            logger.info("[linkedin] Guest API returned %d bytes for job #%s", html_len, job_id)

            # Save snapshot for debugging
            self.save_html_snapshot(result.html, "linkedin_detail_api", job_id)

            # Log a snippet so we can see what we're parsing
            snippet = result.html[:500].replace("\n", " ").strip()
            logger.info("[linkedin] Detail HTML preview: %s", snippet[:200])

            desc, salary_min, salary_max = self._extract_detail_data(result.html)
            if desc and len(desc) > 100:
                logger.info("[linkedin] Enriched job #%s via guest API (%d chars)", job_id, len(desc))
                job.description = desc
                if salary_min and not job.salary_min:
                    job.salary_min = salary_min
                if salary_max and not job.salary_max:
                    job.salary_max = salary_max
                return job
            else:
                logger.warning("[linkedin] Guest API HTML had no extractable description for job #%s "
                               "(html=%d bytes, desc=%s)", job_id, html_len,
                               f"{len(desc)} chars" if desc else "None")
        else:
            logger.warning("[linkedin] Guest API fetch failed for job #%s: status=%s",
                           job_id, result.status.value)

        # --- Strategy 2: Playwright for the full page ---
        logger.info("[linkedin] Trying Playwright for job #%s", job_id)
        pw_result = await self._fetch_playwright(job.url)

        if pw_result.status == FetchStatus.SUCCESS and pw_result.html:
            html_len = len(pw_result.html)
            logger.info("[linkedin] Playwright returned %d bytes for job #%s", html_len, job_id)

            # Save snapshot for debugging
            self.save_html_snapshot(pw_result.html, "linkedin_detail_pw", job_id)

            desc, salary_min, salary_max = self._extract_detail_data(pw_result.html)
            if desc and len(desc) > 100:
                logger.info("[linkedin] Enriched job #%s via Playwright (%d chars)", job_id, len(desc))
                job.description = desc
                if salary_min and not job.salary_min:
                    job.salary_min = salary_min
                if salary_max and not job.salary_max:
                    job.salary_max = salary_max
            else:
                logger.warning("[linkedin] Playwright HTML had no extractable description for job #%s "
                               "(html=%d bytes, desc=%s)", job_id, html_len,
                               f"{len(desc)} chars" if desc else "None")
        else:
            logger.warning("[linkedin] Playwright fetch also failed for job #%s: status=%s",
                           job_id, pw_result.status.value)

        return job

    def _extract_detail_data(
        self, html: str
    ) -> tuple[str | None, float | None, float | None]:
        """Extract description and salary from a LinkedIn detail page.

        Uses a 3-strategy approach in priority order:
          1. JSON-LD structured data (most stable — LinkedIn rarely changes this)
          2. Known CSS selectors for the guest API HTML
          3. Broad fallback selectors + full-page text extraction

        Returns (description, salary_min, salary_max).
        """
        soup = BeautifulSoup(html, "lxml")

        # ----------------------------------------------------------
        # Strategy 1: JSON-LD structured data (most reliable)
        # LinkedIn embeds <script type="application/ld+json"> with
        # the full description in the "description" field.
        # ----------------------------------------------------------
        desc_from_ld, sal_min_ld, sal_max_ld = self._parse_from_jsonld(soup)
        if desc_from_ld and len(desc_from_ld) > 100:
            logger.debug("[linkedin] Description extracted via JSON-LD (%d chars)", len(desc_from_ld))
            return desc_from_ld, sal_min_ld, sal_max_ld

        # ----------------------------------------------------------
        # Strategy 2: Known CSS selectors (guest API fragments)
        # These class names are the most commonly seen as of early 2026.
        # Ordered from most specific to least specific.
        # ----------------------------------------------------------
        description = None
        css_selectors = [
            # Guest API detail endpoint — primary container
            "div.show-more-less-html__markup",
            # Full page variants
            "div.jobs-description-content__text",
            "div.jobs-description__content",
            "div[class*='description__text']",
            "div.description__text",
            "div.decorated-job-posting__details",
            # ID-based selector (Selenium/Playwright rendered pages)
            "#job-details",
            # Section-level fallbacks
            "section[class*='description'] div",
            "section[class*='description']",
            "div[class*='show-more-less']",
        ]

        for selector in css_selectors:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if text and len(text) > 100:
                    description = text
                    logger.debug("[linkedin] Description extracted via CSS '%s' (%d chars)",
                                 selector, len(text))
                    break

        # ----------------------------------------------------------
        # Strategy 3: Broadest fallback — grab the largest text block
        # If CSS selectors all miss (HTML structure changed), find the
        # longest text-bearing element on the page as a heuristic.
        # ----------------------------------------------------------
        if not description:
            description = self._extract_longest_text_block(soup)
            if description:
                logger.warning(
                    "[linkedin] Description extracted via longest-text-block fallback "
                    "(%d chars) — CSS selectors may need updating",
                    len(description),
                )

        # --- Salary from detail page ---
        salary_min, salary_max = None, None
        salary_selectors = [
            "div[class*='salary']",
            "span[class*='compensation']",
            "div[class*='compensation']",
            "span[class*='salary']",
            "li[class*='salary']",
        ]
        for sel in salary_selectors:
            salary_el = soup.select_one(sel)
            if salary_el:
                salary_min, salary_max = self._parse_salary_text(
                    salary_el.get_text(strip=True)
                )
                if salary_min or salary_max:
                    break

        return description, salary_min, salary_max

    def _parse_from_jsonld(
        self, soup: BeautifulSoup
    ) -> tuple[str | None, float | None, float | None]:
        """Extract job data from JSON-LD structured data.

        LinkedIn embeds <script type="application/ld+json"> containing
        a JobPosting schema with description, salary, title, etc.
        This is the most stable extraction method because structured
        data formats change far less often than CSS class names.
        """
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            # Handle both single object and array of objects
            objects = data if isinstance(data, list) else [data]

            for obj in objects:
                if not isinstance(obj, dict):
                    continue

                # Look for JobPosting schema
                obj_type = obj.get("@type", "")
                if obj_type != "JobPosting" and "JobPosting" not in str(obj_type):
                    continue

                # --- Description ---
                raw_desc = obj.get("description", "")
                if raw_desc:
                    # Description may contain HTML — strip tags
                    desc_soup = BeautifulSoup(str(raw_desc), "lxml")
                    description = desc_soup.get_text(separator="\n", strip=True)
                else:
                    description = None

                # --- Salary from JSON-LD ---
                salary_min, salary_max = None, None
                base_salary = obj.get("baseSalary") or obj.get("estimatedSalary")
                if isinstance(base_salary, dict):
                    value = base_salary.get("value", {})
                    if isinstance(value, dict):
                        salary_min = self._safe_float(value.get("minValue"))
                        salary_max = self._safe_float(value.get("maxValue"))
                        # Check unit — annualize if hourly
                        unit = value.get("unitText", "").upper()
                        if unit == "HOUR":
                            if salary_min:
                                salary_min *= 2080
                            if salary_max:
                                salary_max *= 2080
                    elif isinstance(value, (int, float)):
                        salary_min = float(value)
                        salary_max = float(value)
                elif isinstance(base_salary, list) and base_salary:
                    # Sometimes it's an array of salary objects
                    first = base_salary[0] if isinstance(base_salary[0], dict) else {}
                    value = first.get("value", {})
                    if isinstance(value, dict):
                        salary_min = self._safe_float(value.get("minValue"))
                        salary_max = self._safe_float(value.get("maxValue"))

                if description and len(description) > 50:
                    return description, salary_min, salary_max

        return None, None, None

    @staticmethod
    def _safe_float(val: object) -> float | None:
        """Safely convert a value to float, returning None on failure."""
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _extract_longest_text_block(self, soup: BeautifulSoup) -> str | None:
        """Last-resort fallback: find the longest text-bearing element.

        This catches cases where LinkedIn has changed their class names
        entirely. We look for divs/sections with substantial text content
        and pick the longest one, filtering out obvious non-description
        elements (nav, header, footer, script, style).
        """
        skip_tags = {"script", "style", "nav", "header", "footer", "noscript", "meta", "link"}
        candidates: list[str] = []

        for el in soup.find_all(["div", "section", "article"]):
            # Skip elements with navigation/chrome class names
            el_classes = " ".join(el.get("class", []))
            if any(skip in el_classes.lower() for skip in
                   ["nav", "header", "footer", "topcard", "similar-jobs",
                    "sign-up", "login", "cta-modal", "contextual-sign-in"]):
                continue

            text = el.get_text(separator="\n", strip=True)
            if text and len(text) > 200:
                candidates.append(text)

        if not candidates:
            return None

        # Pick the longest one — most likely the job description
        best = max(candidates, key=len)
        # Sanity check — don't return something absurdly long (probably the whole page)
        if len(best) > 15000:
            best = best[:15000] + "\n[... truncated]"

        return best if len(best) > 200 else None

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
                # Save original length BEFORE enrich mutates the object
                original_desc_len = len(job.description or "")
                enriched_job = await self.enrich_job(job)
                if enriched_job.description and len(enriched_job.description) > original_desc_len:
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
