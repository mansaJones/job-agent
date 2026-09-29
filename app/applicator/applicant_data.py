"""Applicant data — everything the apply client needs to fill an application.

Built from profile.yaml (contact, preferences, application_answers) plus the
parsed LinkedIn export. Deliberately has no fields for SSN, date of birth,
government IDs, or EEO/demographic answers — the client must never fill those.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.config import AppSettings, ApplicationAnswersConfig
from app.resume_generator.models import Education, LinkedInData, Position

REQUIRED_FIELDS = ("email", "phone", "linkedin_url")

_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


class ApplicantData(BaseModel):
    first_name: str
    last_name: str
    full_name: str
    email: str
    phone: str
    location: str           # profile.preferences.location
    city: str
    state: str              # two-letter, e.g. "IL"
    linkedin_url: str
    portfolio_url: str
    years_experience: int   # profile.preferences.experience_years
    current_title: str      # most recent LinkedIn position
    current_company: str
    positions: list[Position]  # for Workday-style "add experience" UIs
    education: list[Education]
    skills: list[str]
    answers: ApplicationAnswersConfig


def parse_city_state(location: str) -> tuple[str, str]:
    """"Homewood, IL" → ("Homewood", "IL"); "Chicago, Illinois" → ("Chicago", "IL")."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) < 2:
        return (parts[0] if parts else ""), ""
    city, region = parts[0], parts[1]
    region_clean = re.sub(r"\s+\d{5}(-\d{4})?$", "", region).strip()  # drop a trailing ZIP
    if re.fullmatch(r"[A-Za-z]{2}", region_clean):
        return city, region_clean.upper()
    return city, _STATE_ABBR.get(region_clean.lower(), "")


def build_applicant_data(settings: AppSettings, linkedin_data: LinkedInData) -> ApplicantData:
    """Assemble ApplicantData.

    Contact comes from profile.yaml's `contact` block, falling back to the
    LinkedIn export for email/phone.

    Raises:
        ValueError: listing every empty required field (email, phone, linkedin_url).
    """
    profile = settings.profile
    contact = profile.contact
    location = profile.preferences.location
    city, state = parse_city_state(location)
    current = linkedin_data.positions[0] if linkedin_data.positions else None

    values = {
        "email": contact.email or linkedin_data.email,
        "phone": contact.phone or linkedin_data.phone,
        "linkedin_url": contact.linkedin_url or linkedin_data.linkedin_url,
    }
    missing = [name for name in REQUIRED_FIELDS if not values[name].strip()]
    if missing:
        raise ValueError(
            "Missing applicant fields: " + ", ".join(missing)
            + " — set them under `contact:` in config/profile.yaml"
        )

    portfolio = contact.portfolio_url or (linkedin_data.websites[0]
                                          if linkedin_data.websites else "")
    return ApplicantData(
        first_name=linkedin_data.first_name,
        last_name=linkedin_data.last_name,
        full_name=linkedin_data.full_name,
        email=values["email"],
        phone=values["phone"],
        location=location,
        city=city,
        state=state,
        linkedin_url=values["linkedin_url"],
        portfolio_url=portfolio,
        years_experience=profile.preferences.experience_years,
        current_title=current.title if current else "",
        current_company=current.company if current else "",
        positions=linkedin_data.positions,
        education=linkedin_data.education,
        skills=linkedin_data.skills,
        answers=profile.application_answers,
    )
