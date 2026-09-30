"""Identify which applicant tracking system (ATS) a page belongs to.

URL match first (HIGH confidence), DOM markers second (MEDIUM). UNKNOWN pages
go to the generic filler.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from apply_client.models import ATSType, Confidence

# (ATS, host regex, optional path regex)
_URL_RULES: list[tuple[ATSType, str, str | None]] = [
    (ATSType.GREENHOUSE, r"(^|\.)greenhouse\.io$", None),
    (ATSType.LEVER, r"^jobs\.lever\.co$", None),
    (ATSType.WORKDAY, r"(^|\.)myworkdayjobs\.com$", None),
    (ATSType.WORKDAY, r"(^|\.)myworkdaysite\.com$", None),
    (ATSType.INDEED_EASY, r"^smartapply\.indeed\.com$", None),
    (ATSType.INDEED_EASY, r"(^|\.)apply\.indeed\.com$", None),
    (ATSType.INDEED_EASY, r"(^|\.)indeed\.com$", r"^/applystart"),
    (ATSType.LINKEDIN_EASY, r"(^|\.)linkedin\.com$", r"^/jobs/"),
    (ATSType.ICIMS, r"(^|\.)icims\.com$", None),
    (ATSType.SMARTRECRUITERS, r"^jobs\.smartrecruiters\.com$", None),
    (ATSType.TALEO, r"(^|\.)taleo\.net$", None),
    (ATSType.ASHBY, r"^jobs\.ashbyhq\.com$", None),
    (ATSType.BAMBOOHR, r"(^|\.)bamboohr\.com$", r"^/(careers|jobs)"),
]

# DOM fallbacks, checked in order
_DOM_RULES: list[tuple[ATSType, str]] = [
    (ATSType.GREENHOUSE, '#application_form, form[action*="greenhouse"], #grnhse_app'),
    (ATSType.LEVER, '.application-form, form[action*="lever.co"]'),
    (ATSType.LINKEDIN_EASY, ".jobs-easy-apply-modal"),
    (ATSType.INDEED_EASY, '[data-testid*="ia-"]'),
    (ATSType.ICIMS, "#icims_content_iframe"),
    (ATSType.WORKDAY, "[data-automation-id]"),
    (ATSType.SMARTRECRUITERS, 'form[class*="smart"], [class*="smartrecruiters"] form'),
]


def detect_url(url: str) -> tuple[ATSType, Confidence]:
    """ATS from the URL alone. (UNKNOWN, LOW) when nothing matches."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    for ats, host_re, path_re in _URL_RULES:
        if re.search(host_re, host) and (path_re is None or re.search(path_re, path)):
            return ats, Confidence.HIGH
    return ATSType.UNKNOWN, Confidence.LOW


async def detect(page) -> tuple[ATSType, Confidence]:  # type: ignore[no-untyped-def]
    """ATS for a live page: URL first, then DOM markers in any frame."""
    ats, confidence = detect_url(page.url)
    if ats == ATSType.LINKEDIN_EASY:
        # A LinkedIn job page only counts once the Easy Apply modal is open
        if await page.query_selector(".jobs-easy-apply-modal"):
            return ats, Confidence.HIGH
        return ATSType.UNKNOWN, Confidence.LOW
    if ats != ATSType.UNKNOWN:
        return ats, confidence

    # Embedded ATS iframes (e.g. Greenhouse on a company careers page)
    for frame in page.frames[1:]:
        frame_ats, _ = detect_url(frame.url)
        if frame_ats not in (ATSType.UNKNOWN, ATSType.LINKEDIN_EASY):
            return frame_ats, Confidence.MEDIUM

    for ats, selector in _DOM_RULES:
        for frame in page.frames:
            try:
                if await frame.query_selector(selector):
                    return ats, Confidence.MEDIUM
            except Exception:
                continue  # detached / cross-process frame mid-navigation
    return ATSType.UNKNOWN, Confidence.LOW
