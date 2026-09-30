"""Pre-flight checks before a job goes on the apply queue.

Blocking checks must pass to queue. Warnings (unreviewed cover letter,
fabrication warnings on the resume) need an explicit "queue anyway".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.applicator.applicant_data import build_applicant_data
from app.config import AppSettings
from app.database import Database
from app.resume_generator.linkedin_parser import DEFAULT_DATA_PATH, load_linkedin_data
from app.resume_generator.models import LinkedInData
from app.resume_generator.pipeline import load_saved_resume

BLOCK = "block"
WARNING = "warning"


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    severity: str = BLOCK  # "block" (must pass) or "warning" (queue anyway allowed)

    @property
    def blocking(self) -> bool:
        return not self.ok and self.severity == BLOCK

    @property
    def warning(self) -> bool:
        return not self.ok and self.severity == WARNING


def _lane_label(lane: str) -> str:
    return lane.replace("_", " ").title()


def _doc(docs: list[dict], lane: str, doc_type: str) -> dict | None:
    """Newest document of a type for a lane (docs may be in any order)."""
    matches = [d for d in docs if d["search_lane"] == lane and d["doc_type"] == doc_type]
    return max(matches, key=lambda d: d["id"]) if matches else None


def run_preflight(
    job: dict,
    lane: str,
    docs: list[dict],
    settings: AppSettings,
    linkedin_data: LinkedInData | None,
    active_request: dict | None = None,
) -> list[PreflightCheck]:
    """Run every check, in order. Never short-circuits so the UI can show the full list."""
    checks: list[PreflightCheck] = []
    label = _lane_label(lane)

    # 1. Apply client token
    token_ok = bool(settings.secrets.apply_client_token)
    checks.append(PreflightCheck(
        "Apply client token", token_ok,
        "Configured" if token_ok else
        "Set APPLY_CLIENT_TOKEN in config/secrets.env (see secrets.env.example)",
    ))

    # 2. Resume
    resume = _doc(docs, lane, "resume")
    checks.append(PreflightCheck(
        "Tailored resume", resume is not None,
        "Generated" if resume else f"Generate a resume for the {label} lane first",
    ))

    # 3. Cover letter
    letter = _doc(docs, lane, "cover_letter")
    checks.append(PreflightCheck(
        "Cover letter", letter is not None,
        "Generated" if letter else f"Generate a cover letter for the {label} lane first",
    ))

    # 4. Applicant data
    if linkedin_data is None:
        checks.append(PreflightCheck(
            "Applicant data", False, "Run `job-agent parse-linkedin` first"))
    else:
        try:
            build_applicant_data(settings, linkedin_data)
            checks.append(PreflightCheck("Applicant data", True, "Complete"))
        except ValueError as e:
            checks.append(PreflightCheck("Applicant data", False, str(e)))

    # 5. Not already applied / queued
    if job.get("status") == "applied":
        checks.append(PreflightCheck("Not yet applied", False, "This job is already marked applied"))
    elif active_request:
        checks.append(PreflightCheck(
            "Not yet applied", False,
            f"Already in the apply queue (#{active_request['id']}, {active_request['status']})",
        ))
    else:
        checks.append(PreflightCheck("Not yet applied", True, "Not applied or queued"))

    # 6. Resume fabrication warnings (warning only)
    saved = load_saved_resume(Path(resume["file_path"])) if resume else None
    fab = saved.fabrication_warnings if saved else []
    checks.append(PreflightCheck(
        "Resume fact-check", not fab,
        "No fabrication warnings" if not fab else
        f"{len(fab)} item(s) were removed from the resume: " + "; ".join(fab),
        severity=WARNING,
    ))

    # 7. Cover letter reviewed (warning only)
    reviewed = bool(letter) and letter["model_used"] == "manual-edit"
    checks.append(PreflightCheck(
        "Cover letter reviewed", reviewed or letter is None,
        "Reviewed (saved with edits)" if reviewed else
        "You haven't reviewed this letter — open it, check it, and click Save edits",
        severity=WARNING,
    ))
    return checks


def has_blockers(checks: list[PreflightCheck]) -> bool:
    return any(c.blocking for c in checks)


def has_warnings(checks: list[PreflightCheck]) -> bool:
    return any(c.warning for c in checks)


async def preflight_and_enqueue(
    db: Database,
    settings: AppSettings,
    job_id: int,
    lane: str,
    force: bool = False,
    linkedin_path: Path = DEFAULT_DATA_PATH,
) -> tuple[list[PreflightCheck], int | None]:
    """Run pre-flight and, if it passes, queue the job.

    Blocking failures always stop. Warnings stop unless force=True.

    Returns:
        (checks, queue_id) — queue_id is None when nothing was queued.

    Raises:
        LookupError: job doesn't exist.
        ValueError: unknown lane, or the job isn't in that lane.
    """
    job = await db.get_job(job_id)
    if job is None:
        raise LookupError(f"Job #{job_id} not found")
    if lane not in settings.profile.search_lanes:
        raise ValueError(f"Unknown search lane: {lane}")
    if job.search_lane not in (lane, "both"):
        raise ValueError(f"Job #{job_id} is in lane '{job.search_lane}', not '{lane}'")

    try:
        linkedin_data = load_linkedin_data(linkedin_path)
    except FileNotFoundError:
        linkedin_data = None

    docs = await db.get_generated_documents_for_job(job_id)
    checks = run_preflight(
        job.model_dump(), lane, docs, settings, linkedin_data,
        active_request=await db.get_active_apply_request(job_id),
    )
    if has_blockers(checks) or (has_warnings(checks) and not force):
        return checks, None

    queue_id = await db.enqueue_apply(
        job_id, lane,
        resume_doc_id=_doc(docs, lane, "resume")["id"],  # type: ignore[index]
        cover_letter_doc_id=_doc(docs, lane, "cover_letter")["id"],  # type: ignore[index]
    )
    return checks, queue_id
