"""Shared fixtures. Playwright tests run headless and skip if no browser is installed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apply_client.models import ApplicantData, ApplicationAnswers, Position  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def applicant() -> ApplicantData:
    """Sample applicant — obviously fake data."""
    return ApplicantData(
        first_name="Ada", last_name="Lovelace", full_name="Ada Lovelace",
        email="ada@example.com", phone="555-0100", location="Homewood, IL",
        city="Homewood", state="IL", linkedin_url="https://www.linkedin.com/in/ada-example/",
        portfolio_url="https://example.com/portfolio", years_experience=15,
        current_title="Lead Developer", current_company="Acme Corp",
        positions=[Position(company="Acme Corp", title="Lead Developer", start_date="2022-01")],
        answers=ApplicationAnswers(
            work_authorization="Yes, I am authorized to work in the United States",
            sponsorship_needed="No", start_availability="Two weeks notice",
            willing_to_relocate="No", remote_preference="Remote or hybrid preferred",
            salary_expectation="", referral_source="Job board", previously_applied="No",
            currently_employed="Yes", ok_to_contact_employer="No",
        ),
    )


@pytest.fixture
async def page():
    """A fresh headless page. Prefers the installed Chrome, falls back to bundled Chromium."""
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as pw:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("No Chromium/Chrome available — run `playwright install chrome`")
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def load_fixture(page, name: str) -> None:  # type: ignore[no-untyped-def]
    await page.set_content((FIXTURES / name).read_text(encoding="utf-8"))
