"""USAJobs.gov job board scraper.

Uses the official USAJobs REST API — no HTML parsing needed.
Returns structured JSON with full job descriptions, salary ranges,
location, and more in a single API call per page.

API docs: https://developer.usajobs.gov/api-reference/get-api-search

Required config (in boards.yaml):
    api_key: "your-api-key-from-developer.usajobs.gov"
    api_email: "you@example.com"   # used as User-Agent header

Confidence: 95% — built from official API docs and verified examples.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from app.config import BoardConfig, ProfileConfig
from app.database import Database, JobRecord
from app.scrapers.base import BaseScraper, FetchResult, FetchStatus

logger = logging.getLogger(__name__)


class USAJobsScraper(BaseScraper):
    """Scraper for USAJobs.gov — official federal job listings."""

    SOURCE_NAME = "usajobs"

    API_BASE = "https://data.usajobs.gov/api/search"
    RESULTS_PER_PAGE = 50  # max 500, but keep it reasonable

    def __init__(
        self,
        board_config: BoardConfig,
        profile: ProfileConfig,
        db: Database,
    ) -> None:
        super().__init__(board_config, profile, db)

        # API key and email are required — stored in boards.yaml headers
        self.api_key = self.config.headers.get("Authorization-Key", "")
        self.api_email = self.config.headers.get("User-Agent", "")

        if not self.api_key:
            logger.error("[usajobs] No API key configured. Set 'Authorization-Key' "
                         "in boards.yaml headers. Get one at: "
                         "https://developer.usajobs.gov/APIRequest/Index")
        if not self.api_email:
            logger.error("[usajobs] No email configured. Set 'User-Agent' "
                         "in boards.yaml headers (your email address).")

        # Override the httpx client headers with API-specific ones
        self.client.headers.update({
            "Host": "data.usajobs.gov",
            "User-Agent": self.api_email,
            "Authorization-Key": self.api_key,
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, location: str, page: int) -> str:
        """Build a USAJobs API search URL.

        USAJobs uses 1-indexed `Page=` param for pagination.
        """
        params: dict[str, str | int] = {
            "Keyword": query,
            "ResultsPerPage": self.RESULTS_PER_PAGE,
            "Page": page + 1,  # USAJobs is 1-indexed; base run() sends 0-indexed
        }

        # Only include LocationName if actually set (empty string = nationwide)
        if location:
            params["LocationName"] = location

        # Add radius if configured (and location is present)
        if self.config.radius_miles and location:
            params["Radius"] = self.config.radius_miles

        # Remote jobs filter
        if self.profile.preferences.remote_ok:
            params["RemoteIndicator"] = "True"

        return f"{self.API_BASE}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Listing page parsing (JSON, not HTML)
    # ------------------------------------------------------------------

    def parse_listing_page(self, html: str) -> list[JobRecord]:
        """Parse USAJobs API JSON response.

        Despite the method name being 'html' (inherited from BaseScraper),
        USAJobs returns JSON. We parse it directly.
        """
        import json

        try:
            data = json.loads(html)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("[usajobs] Failed to parse API response as JSON: %s", e)
            return []

        search_result = data.get("SearchResult", {})
        items = search_result.get("SearchResultItems", [])

        if not items:
            count_str = search_result.get("SearchResultCount", "0")
            logger.info("[usajobs] API returned %s total results, 0 on this page", count_str)
            return []

        jobs: list[JobRecord] = []
        for item in items:
            try:
                job = self._parse_item(item)
                if job is not None:
                    jobs.append(job)
            except Exception as e:
                logger.debug("[usajobs] Failed to parse an item: %s", e)
                self.stats.errors += 1

        return jobs

    def _parse_item(self, item: dict) -> JobRecord | None:
        """Convert a single USAJobs SearchResultItem into a JobRecord.

        USAJobs response structure:
            {
              "MatchedObjectId": "...",
              "MatchedObjectDescriptor": {
                "PositionID": "...",
                "PositionTitle": "IT Specialist (APPSW)",
                "PositionURI": "https://www.usajobs.gov/job/...",
                "ApplyURI": ["https://..."],
                "PositionLocation": [
                  {"LocationName": "Washington, DC", ...}
                ],
                "OrganizationName": "Department of Veterans Affairs",
                "DepartmentName": "Department of Veterans Affairs",
                "PositionRemuneration": [
                  {"MinimumRange": "100000", "MaximumRange": "130000",
                   "RateIntervalCode": "PA"}
                ],
                "QualificationSummary": "...",
                "PositionFormattedDescription": [
                  {"Content": "full description...", "Label": "..."}
                ],
                "UserArea": {
                  "Details": {
                    "MajorDuties": ["duty1", "duty2", ...],
                    "Requirements": "...",
                    ...
                  }
                },
                "PublicationStartDate": "2026-03-01",
                "ApplicationCloseDate": "2026-04-01",
                ...
              }
            }
        """
        desc = item.get("MatchedObjectDescriptor", {})
        if not desc:
            return None

        title = desc.get("PositionTitle")
        if not title:
            return None

        # URL
        url = desc.get("PositionURI", "")
        if not url:
            return None

        # External ID
        external_id = desc.get("PositionID") or item.get("MatchedObjectId")

        # Company / Agency
        company = desc.get("OrganizationName") or desc.get("DepartmentName")

        # Location — may be a list of locations
        location = None
        pos_locations = desc.get("PositionLocation", [])
        if pos_locations and isinstance(pos_locations, list):
            loc_names = []
            for loc in pos_locations[:3]:  # cap at 3 locations
                name = loc.get("LocationName", "")
                if name:
                    loc_names.append(name)
            location = "; ".join(loc_names) if loc_names else None

        # Salary
        salary_min, salary_max = self._parse_remuneration(
            desc.get("PositionRemuneration", [])
        )

        # Description — combine multiple sources for completeness
        description = self._build_description(desc)

        # Date posted
        date_posted = desc.get("PublicationStartDate")

        return JobRecord(
            source="usajobs",
            external_id=str(external_id) if external_id else None,
            url=url,
            title=title,
            company=company,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            description=description,
            raw_html=None,  # No raw HTML — it's all JSON
            date_posted=date_posted,
            status="new",
        )

    @staticmethod
    def _parse_remuneration(
        remuneration: list[dict],
    ) -> tuple[float | None, float | None]:
        """Parse salary from PositionRemuneration array.

        RateIntervalCode:
          PA = Per Annum, PH = Per Hour, PW = Per Week, PM = Per Month,
          WC = Without Compensation, SA = Student Stipend
        """
        if not remuneration or not isinstance(remuneration, list):
            return None, None

        for entry in remuneration:
            min_str = entry.get("MinimumRange", "")
            max_str = entry.get("MaximumRange", "")
            rate_code = entry.get("RateIntervalCode", "PA")

            try:
                sal_min = float(min_str) if min_str else None
            except (ValueError, TypeError):
                sal_min = None

            try:
                sal_max = float(max_str) if max_str else None
            except (ValueError, TypeError):
                sal_max = None

            # Annualize non-annual rates
            if rate_code == "PH":
                if sal_min:
                    sal_min *= 2080
                if sal_max:
                    sal_max *= 2080
            elif rate_code == "PM":
                if sal_min:
                    sal_min *= 12
                if sal_max:
                    sal_max *= 12
            elif rate_code == "PW":
                if sal_min:
                    sal_min *= 52
                if sal_max:
                    sal_max *= 52

            if sal_min or sal_max:
                return sal_min, sal_max

        return None, None

    @staticmethod
    def _build_description(desc: dict) -> str | None:
        """Build a complete description from multiple API fields.

        Combines:
          - QualificationSummary (required qualifications)
          - PositionFormattedDescription (full description)
          - UserArea.Details.MajorDuties (key responsibilities)
          - UserArea.Details.Requirements
        """
        parts: list[str] = []

        # Full formatted description
        formatted = desc.get("PositionFormattedDescription", [])
        if isinstance(formatted, list):
            for entry in formatted:
                content = entry.get("Content", "")
                if content and isinstance(content, str):
                    # Strip HTML tags if present
                    clean = re.sub(r"<[^>]+>", " ", content)
                    clean = re.sub(r"\s+", " ", clean).strip()
                    if len(clean) > 50:
                        parts.append(clean)

        # Major duties
        user_area = desc.get("UserArea", {})
        details = user_area.get("Details", {}) if isinstance(user_area, dict) else {}

        duties = details.get("MajorDuties", [])
        if isinstance(duties, list) and duties:
            duties_text = "\n".join(f"- {d}" for d in duties if isinstance(d, str) and d.strip())
            if duties_text:
                parts.append(f"Major Duties:\n{duties_text}")

        # Qualification summary
        qual_summary = desc.get("QualificationSummary", "")
        if qual_summary and isinstance(qual_summary, str) and len(qual_summary) > 50:
            # Strip HTML
            clean = re.sub(r"<[^>]+>", " ", qual_summary)
            clean = re.sub(r"\s+", " ", clean).strip()
            parts.append(f"Qualifications:\n{clean}")

        # Requirements
        requirements = details.get("Requirements", "")
        if requirements and isinstance(requirements, str) and len(requirements) > 50:
            clean = re.sub(r"<[^>]+>", " ", requirements)
            clean = re.sub(r"\s+", " ", clean).strip()
            parts.append(f"Requirements:\n{clean}")

        if not parts:
            return None

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # No enrichment needed — API returns full data
    # ------------------------------------------------------------------

    async def run(self) -> "ScraperStats":
        """Run the scraper. No enrichment step needed — the API returns
        full descriptions, salary, location in the search response.

        Disables AdaptiveHealth halting since this is an official API —
        empty results are normal for specific queries, not a sign of
        HTML structure changes.
        """
        # Disable halt behavior — API returning 0 results is fine,
        # not an error condition like it would be for HTML scrapers
        self.health.max_consecutive_empty = 999
        self.health.max_consecutive_blocks = 999

        stats = await super().run()

        logger.info("[usajobs] Scrape complete — descriptions included in search results, "
                     "no enrichment needed")
        return stats
