"""Resume pipeline — generate (or serve cached) tailored resumes for a job + lane.

Output files per job + lane, in GENERATED_RESUMES_DIR:
    {job_id}_{lane}_resume.pdf    — recorded in generated_documents.file_path
    {job_id}_{lane}_resume.docx
    {job_id}_{lane}_resume.json   — TailoredResume, for the UI and cover letters

A result is served from cache when the job description, lane, and LinkedIn
data are unchanged (content_hash) and the files are still on disk.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import GENERATED_RESUMES_DIR, AppSettings
from app.database import Database
from app.polisher.claude_client import ClaudeClient, ResumeGenResult
from app.resume_generator.builder import build_tailored_resume
from app.resume_generator.formatter import render_docx, render_pdf
from app.resume_generator.linkedin_parser import (
    DEFAULT_BULLETS_PATH,
    DEFAULT_DATA_PATH,
    load_additional_bullets,
    load_linkedin_data,
    merge_additional_bullets,
)
from app.resume_generator.models import LinkedInData, TailoredResume

logger = logging.getLogger(__name__)

DOC_TYPE = "resume"


class JobNotFoundError(LookupError):
    """The requested job doesn't exist."""


@dataclass
class ResumeResult:
    resume: TailoredResume
    pdf_path: Path
    docx_path: Path
    doc_id: int
    gen_result: ResumeGenResult | None  # None when served from cache
    from_cache: bool


def resume_paths(output_dir: Path, job_id: int, lane_name: str) -> tuple[Path, Path, Path]:
    """(pdf, docx, json) paths for a job + lane."""
    stem = output_dir / f"{job_id}_{lane_name}_resume"
    return stem.with_suffix(".pdf"), stem.with_suffix(".docx"), stem.with_suffix(".json")


def load_saved_resume(pdf_path: Path) -> TailoredResume | None:
    """Reload the structured resume saved next to a generated PDF."""
    json_path = pdf_path.with_suffix(".json")
    if not json_path.exists():
        return None
    return TailoredResume.model_validate_json(json_path.read_text(encoding="utf-8"))


class ResumePipeline:
    """Generates job-tailored resumes and records them in generated_documents."""

    def __init__(
        self,
        db: Database,
        claude: ClaudeClient,
        settings: AppSettings,
        linkedin_path: Path = DEFAULT_DATA_PATH,
        bullets_path: Path = DEFAULT_BULLETS_PATH,
        output_dir: Path = GENERATED_RESUMES_DIR,
    ) -> None:
        self.db = db
        self.claude = claude
        self.settings = settings
        self.linkedin_path = linkedin_path
        self.bullets_path = bullets_path
        self.output_dir = output_dir

    def _load_candidate(self) -> LinkedInData:
        data = load_linkedin_data(self.linkedin_path)
        return merge_additional_bullets(data, load_additional_bullets(self.bullets_path))

    async def generate_for_job(
        self, job_id: int, lane_name: str, force: bool = False
    ) -> ResumeResult:
        """Generate a tailored resume for a job + lane, or return the cached one.

        Raises:
            JobNotFoundError: job doesn't exist.
            ValueError: unknown lane, or the job doesn't belong to that lane.
            FileNotFoundError: LinkedIn data hasn't been parsed yet.
        """
        job = await self.db.get_job_with_evaluation(job_id)
        if not job:
            raise JobNotFoundError(f"Job #{job_id} not found")

        lane = self.settings.profile.search_lanes.get(lane_name)
        if lane is None:
            raise ValueError(f"Unknown search lane: {lane_name}")
        if job.get("search_lane") not in (lane_name, "both"):
            raise ValueError(
                f"Job #{job_id} is in lane '{job.get('search_lane')}', not '{lane_name}'"
            )

        candidate = self._load_candidate()
        # exported_at changes on every re-parse even when nothing else did
        candidate_json = candidate.model_dump_json(exclude={"exported_at", "parse_warnings"})
        content_hash = hashlib.sha256(
            ((job.get("description") or "") + lane_name + candidate_json).encode("utf-8")
        ).hexdigest()[:16]

        pdf_path, docx_path, json_path = resume_paths(self.output_dir, job_id, lane_name)

        existing = await self.db.get_generated_document(job_id, lane_name, DOC_TYPE)
        if (
            existing
            and not force
            and existing["content_hash"] == content_hash
            and pdf_path.exists() and docx_path.exists() and json_path.exists()
        ):
            cached = load_saved_resume(pdf_path)
            if cached is not None:
                logger.info("Resume for job #%d [%s] served from cache", job_id, lane_name)
                return ResumeResult(
                    resume=cached, pdf_path=pdf_path, docx_path=docx_path,
                    doc_id=existing["id"], gen_result=None, from_cache=True,
                )

        resume, gen = await build_tailored_resume(job, lane, candidate, self.claude)

        render_pdf(resume, pdf_path)
        render_docx(resume, docx_path)
        json_path.write_text(resume.model_dump_json(indent=2), encoding="utf-8")

        await self.db.delete_generated_document(job_id, lane_name, DOC_TYPE)
        doc_id = await self.db.insert_generated_document(
            job_id=job_id,
            search_lane=lane_name,
            doc_type=DOC_TYPE,
            file_path=str(pdf_path),
            model_used=gen.model_used,
            content_hash=content_hash,
        )

        logger.info("Resume for job #%d [%s] generated — %s, cost %s",
                    job_id, lane_name, pdf_path.name, gen.cost_display)
        return ResumeResult(
            resume=resume, pdf_path=pdf_path, docx_path=docx_path,
            doc_id=doc_id, gen_result=gen, from_cache=False,
        )
