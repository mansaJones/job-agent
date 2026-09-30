"""LinkedIn data export parser.

Reads the CSVs straight out of the export ZIP (LinkedIn → Settings & Privacy →
Data privacy → Get a copy of your data) and normalizes them into LinkedInData.

Column names below were taken from a real export (Sep 2026). LinkedIn doesn't
document them and they vary by account, so each field accepts a few aliases:

    Profile.csv         First Name, Last Name, Headline, Summary, Geo Location, Websites, ...
    Positions.csv       Company Name, Title, Description, Location, Started On, Finished On
    Skills.csv          Name
    Education.csv       School Name, Start Date, End Date, Notes, Degree Name, Activities
    Certifications.csv  Name, Url, Authority, Started On, Finished On, License Number
    Email Addresses.csv Email Address, Confirmed, Primary, Updated On
    PhoneNumbers.csv    Extension, Number, Type

Position descriptions come through as one line with inline "•" bullets.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import RESUMES_DIR, ContactConfig, load_profile
from app.resume_generator.models import (
    AdditionalBullet,
    Certification,
    Education,
    LinkedInData,
    Position,
)

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_PATH = RESUMES_DIR / "linkedin_export.zip"
DEFAULT_DATA_PATH = RESUMES_DIR / "linkedin_data.json"
DEFAULT_BULLETS_PATH = RESUMES_DIR / "additional_bullets.json"

_MONTHS = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _read_csv(zf: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    """Read a CSV from the ZIP by basename (it may sit in a subfolder). Missing → []."""
    for name in zf.namelist():
        if name.rsplit("/", 1)[-1].lower() == filename.lower():
            # LinkedIn puts a UTF-8 BOM at the start of some files
            text = zf.read(name).decode("utf-8-sig", errors="replace")
            return [
                {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(io.StringIO(text))
            ]
    logger.debug("%s not found in LinkedIn export", filename)
    return []


def _col(row: dict[str, str], *names: str) -> str:
    """First non-empty value among the given column aliases."""
    for name in names:
        value = row.get(name, "")
        if value:
            return " ".join(value.split()) if "\n" not in value else value.strip()
    return ""


def normalize_date(value: str) -> str | None:
    """Normalize LinkedIn dates to "YYYY-MM".

    Accepts "Apr 2022", "April 2022", "2022", "2022-04", "04/2022".
    Empty → None. Unparseable → raises ValueError.
    """
    value = value.strip()
    if not value:
        return None

    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", value)
    if m:
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            return f"{m.group(2)}-{month:02d}"
    m = re.fullmatch(r"(\d{4})", value)
    if m:
        return f"{m.group(1)}-01"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", value)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", value)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    raise ValueError(f"Unrecognized date format: {value!r}")


def _year(value: str) -> str | None:
    m = re.search(r"\d{4}", value or "")
    return m.group(0) if m else None


def split_bullets(description: str) -> list[str]:
    """Split a position description into bullets.

    Splits on newlines and on "•" anywhere (LinkedIn exports inline bullets
    on one line). "-" and "*" only count as bullet markers at line start, so
    hyphenated words like "day-to-day" survive.
    """
    if not description:
        return []
    bullets: list[str] = []
    for line in description.splitlines():
        for chunk in line.split("•"):
            chunk = re.sub(r"^\s*[-*]\s+", "", chunk).strip(" \t-*")
            if chunk:
                bullets.append(chunk)
    return bullets


def _parse_websites(raw: str) -> list[str]:
    """Websites looks like "[PORTFOLIO:https://a.com,COMPANY:https://b.com]"."""
    return re.findall(r"https?://[^\s,\]]+", raw or "")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_linkedin_export(
    zip_path: Path = DEFAULT_EXPORT_PATH,
    contact: ContactConfig | None = None,
    out_path: Path | None = DEFAULT_DATA_PATH,
) -> LinkedInData:
    """Parse a LinkedIn data export ZIP into LinkedInData and save it as JSON.

    Args:
        zip_path: The export ZIP from LinkedIn.
        contact: Contact overrides (defaults to profile.yaml's `contact` block).
        out_path: Where to write the parsed JSON. None = don't write.
    """
    if contact is None:
        contact = load_profile().contact

    warnings: list[str] = []

    with zipfile.ZipFile(zip_path) as zf:
        profile_rows = _read_csv(zf, "Profile.csv")
        position_rows = _read_csv(zf, "Positions.csv")
        skill_rows = _read_csv(zf, "Skills.csv")
        education_rows = _read_csv(zf, "Education.csv")
        cert_rows = _read_csv(zf, "Certifications.csv")
        email_rows = _read_csv(zf, "Email Addresses.csv")
        phone_rows = _read_csv(zf, "PhoneNumbers.csv")
        summary_rows = _read_csv(zf, "Profile Summary.csv")

    if not profile_rows:
        raise ValueError(f"Profile.csv missing or empty in {zip_path} — is this a LinkedIn export?")
    profile = profile_rows[0]

    # --- Positions ---
    positions: list[Position] = []
    for row in position_rows:
        company = _col(row, "Company Name", "Company")
        title = _col(row, "Title", "Position")
        if not company or not title:
            continue
        description = row.get("Description", "").strip()
        try:
            start = normalize_date(_col(row, "Started On", "Start Date")) or ""
        except ValueError as e:
            warnings.append(f"{company} / {title}: start date — {e}")
            start = ""
        try:
            end = normalize_date(_col(row, "Finished On", "End Date"))
        except ValueError as e:
            warnings.append(f"{company} / {title}: end date — {e}")
            end = None
        if not start:
            warnings.append(f"{company} / {title}: no start date")
        positions.append(Position(
            company=company,
            title=title,
            location=_col(row, "Location") or None,
            start_date=start,
            end_date=end,
            description=description,
            bullets=split_bullets(description),
        ))
    # Newest first; unparseable start dates ("") sink to the bottom
    positions.sort(key=lambda p: p.start_date, reverse=True)

    # --- Education ---
    education = [
        Education(
            school=_col(row, "School Name", "School"),
            degree=_col(row, "Degree Name", "Degree") or None,
            field_of_study=_col(row, "Field Of Study", "Field of Study") or None,
            start_year=_year(_col(row, "Start Date", "Started On")),
            end_year=_year(_col(row, "End Date", "Finished On")),
        )
        for row in education_rows
        if _col(row, "School Name", "School")
    ]

    # --- Certifications ---
    certifications: list[Certification] = []
    for row in cert_rows:
        name = _col(row, "Name")
        if not name:
            continue
        try:
            issued = normalize_date(_col(row, "Started On", "Issued On"))
        except ValueError as e:
            warnings.append(f"Certification {name}: {e}")
            issued = None
        certifications.append(Certification(
            name=name, authority=_col(row, "Authority") or None, issued=issued,
        ))

    # --- Skills (de-duplicated, order kept) ---
    skills = list(dict.fromkeys(_col(row, "Name", "Skill") for row in skill_rows if _col(row, "Name", "Skill")))

    # --- Contact: profile.yaml wins, export is the fallback ---
    email = contact.email
    if not email and email_rows:
        primary = [r for r in email_rows if r.get("Primary", "").lower() == "yes"]
        email = _col((primary or email_rows)[0], "Email Address")
    phone = contact.phone
    if not phone and phone_rows:
        mobile = [r for r in phone_rows if r.get("Type", "").lower() == "mobile"]
        phone = _col((mobile or phone_rows)[0], "Number")

    for field_name, value in (("email", email), ("phone", phone),
                              ("linkedin_url", contact.linkedin_url)):
        if not value:
            warnings.append(
                f"Missing contact {field_name} — set contact.{field_name} in config/profile.yaml"
            )

    websites = _parse_websites(profile.get("Websites", ""))
    if contact.portfolio_url and contact.portfolio_url not in websites:
        websites.insert(0, contact.portfolio_url)

    summary = profile.get("Summary", "").strip()
    if not summary and summary_rows:
        summary = _col(summary_rows[0], "Profile Summary")

    data = LinkedInData(
        first_name=_col(profile, "First Name"),
        last_name=_col(profile, "Last Name"),
        headline=_col(profile, "Headline"),
        summary=summary,
        location=_col(profile, "Geo Location", "Location", "Address"),
        email=email,
        phone=phone,
        linkedin_url=contact.linkedin_url,
        websites=websites,
        positions=positions,
        education=education,
        skills=skills,
        certifications=certifications,
        exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        parse_warnings=warnings,
    )

    for w in warnings:
        logger.warning("LinkedIn parse: %s", w)

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Parsed LinkedIn export: %d positions, %d skills → %s",
                    len(positions), len(skills), out_path)

    return data


def load_linkedin_data(path: Path = DEFAULT_DATA_PATH) -> LinkedInData:
    """Load previously parsed LinkedIn data."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `job-agent parse-linkedin` first."
        )
    return LinkedInData.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Supplemental bullets
# ---------------------------------------------------------------------------

def load_additional_bullets(
    path: Path = DEFAULT_BULLETS_PATH,
) -> dict[str, list[AdditionalBullet]]:
    """Load supplemental bullets keyed by company. Missing file → {}."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        company: [AdditionalBullet(**b) for b in bullets]
        for company, bullets in raw.items()
        if not company.startswith("_")
    }


def merge_additional_bullets(
    data: LinkedInData, bullets: dict[str, list[AdditionalBullet]]
) -> LinkedInData:
    """Return a copy of data with supplemental bullets appended to matching companies.

    Company names match case-insensitively. Tags are collected into
    supplemental_tags as evidence for the anti-fabrication check.
    """
    merged = data.model_copy(deep=True)
    by_company = {k.strip().lower(): v for k, v in bullets.items()}
    matched: set[str] = set()
    tags: list[str] = list(merged.supplemental_tags)

    for position in merged.positions:
        extra = by_company.get(position.company.strip().lower())
        if not extra:
            continue
        matched.add(position.company.strip().lower())
        for bullet in extra:
            if bullet.text not in position.bullets:
                position.bullets.append(bullet.text)
            tags.extend(bullet.tags)

    for company in by_company.keys() - matched:
        logger.warning("additional_bullets.json: no position at company %r — bullets ignored",
                       company)

    merged.supplemental_tags = list(dict.fromkeys(tags))
    return merged
