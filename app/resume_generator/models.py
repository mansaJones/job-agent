"""Data models for the resume generator — source data (LinkedIn) and output (tailored resume)."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Source data — parsed from the LinkedIn export
# ---------------------------------------------------------------------------

class Position(BaseModel):
    company: str
    title: str
    location: str | None = None
    start_date: str             # normalized "YYYY-MM" ("" if unparseable)
    end_date: str | None = None  # None = current
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None


class Certification(BaseModel):
    name: str
    authority: str | None = None
    issued: str | None = None  # "YYYY-MM"


class AdditionalBullet(BaseModel):
    """A supplemental achievement not on LinkedIn — from resumes/additional_bullets.json."""

    text: str
    tags: list[str] = Field(default_factory=list)


class LinkedInData(BaseModel):
    first_name: str
    last_name: str
    headline: str = ""
    summary: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    websites: list[str] = Field(default_factory=list)
    positions: list[Position]
    education: list[Education]
    skills: list[str]
    certifications: list[Certification]
    exported_at: str  # ISO timestamp of when the parse ran
    # Tags from merged additional bullets — extra evidence for the anti-fabrication check
    supplemental_tags: list[str] = Field(default_factory=list)
    # Problems found while parsing (unparseable dates, missing contact info)
    parse_warnings: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def all_skills_lower(self) -> set[str]:
        return {s.lower().strip() for s in self.skills if s.strip()}


# ---------------------------------------------------------------------------
# Output — a resume tailored to one job
# ---------------------------------------------------------------------------

class ResumeBullet(BaseModel):
    text: str
    matched_skills: list[str] = Field(default_factory=list)  # job requirements it addresses


class ResumePosition(BaseModel):
    company: str
    title: str
    location: str | None = None
    start_date: str
    end_date: str | None = None
    bullets: list[ResumeBullet] = Field(default_factory=list)


class TailoredResume(BaseModel):
    full_name: str
    headline: str                     # rewritten to target the job
    contact_line: str                 # "email | phone | location | linkedin"
    summary: str                      # 3-4 sentences, job-targeted
    positions: list[ResumePosition]   # reordered by relevance, top 5 max
    skills: list[str]                 # job-relevant first, max 20
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    matched_requirements: list[str] = Field(default_factory=list)
    unmatched_requirements: list[str] = Field(default_factory=list)
    fabrication_warnings: list[str] = Field(default_factory=list)
