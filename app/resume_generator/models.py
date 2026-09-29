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

    def to_prompt_text(self) -> str:
        """Compact plain-text rendering for LLM prompts (no contact details)."""
        lines = [self.full_name]
        if self.headline:
            lines.append(self.headline)
        if self.summary:
            lines += ["", "SUMMARY", self.summary]
        if self.positions:
            lines += ["", "EXPERIENCE"]
            for p in self.positions:
                lines.append(_position_header(p.title, p.company, p.location,
                                              p.start_date, p.end_date))
                lines += [f"- {b}" for b in p.bullets]
        if self.skills:
            lines += ["", "SKILLS", ", ".join(self.skills)]
        lines += _education_and_certs(self.education, self.certifications)
        return "\n".join(lines).strip()


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

    def to_prompt_text(self) -> str:
        """Compact plain-text rendering for LLM prompts (headline, summary, positions, skills)."""
        lines = [self.full_name]
        if self.headline:
            lines.append(self.headline)
        if self.summary:
            lines += ["", "SUMMARY", self.summary]
        if self.positions:
            lines += ["", "EXPERIENCE"]
            for p in self.positions:
                lines.append(_position_header(p.title, p.company, p.location,
                                              p.start_date, p.end_date))
                lines += [f"- {b.text}" for b in p.bullets]
        if self.skills:
            lines += ["", "SKILLS", ", ".join(self.skills)]
        lines += _education_and_certs(self.education, self.certifications)
        return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Prompt-text helpers
# ---------------------------------------------------------------------------

def _position_header(title: str, company: str, location: str | None,
                     start: str | None, end: str | None) -> str:
    where = f" ({location})" if location else ""
    return f"{title} — {company}{where}, {start or '?'} to {end or 'present'}"


def _education_and_certs(education: list[Education],
                         certifications: list[Certification]) -> list[str]:
    lines: list[str] = []
    if education:
        lines += ["", "EDUCATION"]
        for e in education:
            detail = ", ".join(x for x in (e.degree, e.field_of_study) if x)
            years = "–".join(y for y in (e.start_year, e.end_year) if y)
            lines.append(" — ".join(x for x in (e.school, detail, years) if x))
    if certifications:
        lines += ["", "CERTIFICATIONS"]
        lines += [" — ".join(x for x in (c.name, c.authority, c.issued) if x)
                  for c in certifications]
    return lines
