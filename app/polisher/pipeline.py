"""Cover letter polishing pipeline — orchestrates resume + job + template → Claude API.

Picks the right template based on job title, loads the resume, sends everything
to Claude, and stores the result in the evaluations table.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import AppSettings, PROJECT_ROOT
from app.database import Database
from app.polisher.claude_client import ClaudeClient, PolishResult
from app.polisher.resume import load_resume

logger = logging.getLogger(__name__)

TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Keywords that indicate a leadership/management role
LEAD_KEYWORDS = {
    "lead", "manager", "director", "head", "principal", "vp",
    "engineering manager", "development manager", "team lead",
}


def _pick_template(job_title: str) -> str | None:
    """Pick the best cover letter template based on job title."""
    title_lower = job_title.lower()

    # Check for leadership keywords
    if any(kw in title_lower for kw in LEAD_KEYWORDS):
        template_path = TEMPLATES_DIR / "cover_letter_lead.txt"
    else:
        template_path = TEMPLATES_DIR / "cover_letter_senior_ic.txt"

    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    logger.debug("No template found at %s", template_path)
    return None


def _find_resume(settings: AppSettings) -> Path | None:
    """Locate the resume file. Checks resumes/ directory for PDF or text files."""
    resumes_dir = PROJECT_ROOT / "resumes"
    if not resumes_dir.exists():
        return None

    # Prefer PDF, then txt, then md
    for ext in (".pdf", ".txt", ".md"):
        for f in resumes_dir.glob(f"*{ext}"):
            return f

    return None


class PolishPipeline:
    """Orchestrates cover letter generation for approved jobs."""

    def __init__(
        self,
        db: Database,
        claude: ClaudeClient,
        settings: AppSettings,
        resume_path: Path | None = None,
    ) -> None:
        self.db = db
        self.claude = claude
        self.settings = settings
        self._resume_text: str | None = None

        # Find resume
        self._resume_path = resume_path or _find_resume(settings)
        if self._resume_path:
            logger.info("Using resume: %s", self._resume_path)
        else:
            logger.warning("No resume found in resumes/ — cover letters will use profile data only")

    def _get_resume_text(self) -> str:
        """Load resume text, falling back to profile summary if no file."""
        if self._resume_text is not None:
            return self._resume_text

        if self._resume_path:
            try:
                self._resume_text = load_resume(self._resume_path)
                return self._resume_text
            except Exception as e:
                logger.error("Failed to load resume: %s", e)

        # Fallback: build a summary from profile.yaml
        profile = self.settings.profile
        lanes = profile.enabled_lanes

        def _merged(values: list[list[str]]) -> str:
            # Union across lanes, order-preserving
            return ", ".join(dict.fromkeys(v for vs in values for v in vs))

        lines = [
            f"Target Roles: {_merged([l.target_roles for l in lanes])}",
            f"Years of Experience: {profile.preferences.experience_years}",
            f"Core Skills: {_merged([l.skills.must_have + l.skills.must_have_any for l in lanes])}",
            f"Additional Skills: {_merged([l.skills.nice_to_have for l in lanes])}",
            f"Location: {profile.preferences.location}",
        ]
        self._resume_text = "\n".join(lines)
        return self._resume_text

    async def polish_job(self, job_id: int) -> PolishResult | None:
        """Generate a cover letter for a specific job.

        Args:
            job_id: The job ID to generate a cover letter for.

        Returns:
            PolishResult on success, None on failure.
        """
        job_data = await self.db.get_job_with_evaluation(job_id)
        if not job_data:
            logger.error("Job #%d not found", job_id)
            return None

        title = job_data.get("title", "Unknown Position")
        company = job_data.get("company", "Unknown Company")
        description = job_data.get("description", "")

        if not description or len(description) < 50:
            logger.warning("Job #%d has no/short description — cover letter may be generic", job_id)

        # Get evaluation highlights if available
        highlights = []
        reasoning = job_data.get("eval_reasoning", "")
        if reasoning:
            highlights.append(reasoning)

        # Pick template
        template = _pick_template(title)

        # Get resume
        resume_text = self._get_resume_text()

        # Generate
        try:
            result = await self.claude.generate_cover_letter(
                job_title=title,
                company=company,
                job_description=description or "No description available.",
                resume_text=resume_text,
                template=template,
                eval_highlights=highlights if highlights else None,
            )
        except Exception as e:
            logger.error("Claude API error for job #%d: %s", job_id, e)
            return None

        # Store in evaluations table (cover_letter_draft column)
        await self.db.conn.execute(
            """
            UPDATE evaluations SET cover_letter_draft = ?
            WHERE job_id = ? AND evaluated_at = (
                SELECT MAX(evaluated_at) FROM evaluations WHERE job_id = ?
            )
            """,
            (result.cover_letter, job_id, job_id),
        )
        await self.db.conn.commit()

        logger.info(
            "Cover letter saved for job #%d (%s @ %s) — %s",
            job_id, title, company, result.cost_display,
        )

        return result
