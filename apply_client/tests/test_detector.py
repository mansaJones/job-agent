"""ATS detection from URLs (no browser needed)."""

from __future__ import annotations

import pytest

from apply_client.ats.detector import detect_url
from apply_client.models import ATSType, Confidence


@pytest.mark.parametrize("url, expected", [
    ("https://boards.greenhouse.io/acme/jobs/123", ATSType.GREENHOUSE),
    ("https://job-boards.greenhouse.io/acme/jobs/456", ATSType.GREENHOUSE),
    ("https://acme.greenhouse.io/careers/jobs/789", ATSType.GREENHOUSE),
    ("https://jobs.lever.co/acme/abc-123/apply", ATSType.LEVER),
    ("https://acme.myworkdayjobs.com/en-US/careers/job/Chicago/Lead_R123", ATSType.WORKDAY),
    ("https://acme.wd1.myworkdayjobs.com/External/job/Lead", ATSType.WORKDAY),
    ("https://acme.wd5.myworkdayjobs.com/External/job/Lead", ATSType.WORKDAY),
    ("https://www.indeed.com/applystart?jk=abc", ATSType.INDEED_EASY),
    ("https://smartapply.indeed.com/beta/indeedapply/form/contact-info", ATSType.INDEED_EASY),
    ("https://www.linkedin.com/jobs/view/12345/", ATSType.LINKEDIN_EASY),
    ("https://careers-acme.icims.com/jobs/1234/lead/job", ATSType.ICIMS),
    ("https://jobs.smartrecruiters.com/Acme/7432-lead", ATSType.SMARTRECRUITERS),
    ("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=123", ATSType.TALEO),
    ("https://jobs.ashbyhq.com/acme/3f2a", ATSType.ASHBY),
    ("https://acme.bamboohr.com/careers/42", ATSType.BAMBOOHR),
])
def test_url_patterns(url: str, expected: ATSType) -> None:
    assert detect_url(url) == (expected, Confidence.HIGH)


@pytest.mark.parametrize("url", [
    "https://careers.acme.com/jobs/123",
    "https://www.indeed.com/viewjob?jk=abc",           # a listing, not the apply flow
    "https://www.linkedin.com/in/someone/",             # a profile, not a job
    "https://notgreenhouse.io.evil.com/jobs",           # lookalike host
    "https://acme.com/careers?ref=boards.greenhouse.io",  # ATS only in the query string
    "file:///C:/tmp/form.html",
])
def test_unknown(url: str) -> None:
    assert detect_url(url) == (ATSType.UNKNOWN, Confidence.LOW)
