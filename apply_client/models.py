"""Data models — a mirror of the Jetson's API payloads plus form-filling types.

ApplicantData mirrors app/applicator/applicant_data.py on the Jetson. It is
copied, not imported: this client runs on a different machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Jetson payloads
# ---------------------------------------------------------------------------

class Position(BaseModel):
    company: str
    title: str
    location: str | None = None
    start_date: str = ""
    end_date: str | None = None
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None


class ApplicationAnswers(BaseModel):
    work_authorization: str = ""
    sponsorship_needed: str = ""
    start_availability: str = ""
    willing_to_relocate: str = ""
    remote_preference: str = ""
    salary_expectation: str = ""
    referral_source: str = ""
    previously_applied: str = ""
    currently_employed: str = ""
    ok_to_contact_employer: str = ""


class ApplicantData(BaseModel):
    first_name: str
    last_name: str
    full_name: str
    email: str
    phone: str
    location: str = ""
    city: str = ""
    state: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    years_experience: int = 0
    current_title: str = ""
    current_company: str = ""
    positions: list[Position] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    answers: ApplicationAnswers = Field(default_factory=ApplicationAnswers)


class JobInfo(BaseModel):
    title: str | None = None
    company: str | None = None
    url: str
    source: str | None = None
    description: str = ""


class ApplyRequest(BaseModel):
    """One apply queue item as returned by /pending and /claim."""

    queue_id: int
    job_id: int
    lane: str | None = None
    status: str = "pending"
    queued_at: str | None = None
    job: JobInfo
    applicant: ApplicantData | None = None
    applicant_error: str | None = None
    documents: dict[str, dict[str, str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Form filling
# ---------------------------------------------------------------------------

class Confidence(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class ATSType(str, Enum):
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    WORKDAY = "WORKDAY"
    INDEED_EASY = "INDEED_EASY"
    LINKEDIN_EASY = "LINKEDIN_EASY"
    ICIMS = "ICIMS"
    SMARTRECRUITERS = "SMARTRECRUITERS"
    TALEO = "TALEO"
    ASHBY = "ASHBY"
    BAMBOOHR = "BAMBOOHR"
    UNKNOWN = "UNKNOWN"


class Action(str, Enum):
    """What the filler does with a field — decided purely from the match (see field_matcher)."""

    FILL = "fill"                  # HIGH confidence: fill silently
    FILL_AND_FLAG = "fill+flag"    # MEDIUM: fill, outline yellow for review
    UPLOAD = "upload"              # resume / cover letter file input
    DECLINE = "decline"            # EEO: pick "decline to self-identify"
    FLAG = "flag"                  # LOW / unknown: leave blank for the human
    NEVER = "never"                # sensitive: never touched, outlined red


@dataclass
class FormField:
    selector: str                  # unique CSS selector within its frame
    label: str
    input_type: str                # text, email, tel, select, radio, checkbox, file, textarea, combobox, ...
    canonical: str | None
    confidence: Confidence
    name: str = ""
    element_id: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)  # select / radio option labels
    option_selectors: list[str] = field(default_factory=list)  # radio: one per option
    current_value: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # ATS hints (automation ids, sections)
    frame: Any = field(default=None, repr=False, compare=False)  # Playwright Frame
    note: str = ""                 # why it was flagged / what was done

    @property
    def key(self) -> str:
        return self.selector


@dataclass
class FillResult:
    filled: list[FormField] = field(default_factory=list)
    flagged: list[FormField] = field(default_factory=list)
    skipped_never: list[FormField] = field(default_factory=list)

    def extend(self, other: FillResult) -> None:
        self.filled += other.filled
        self.flagged += other.flagged
        self.skipped_never += other.skipped_never
