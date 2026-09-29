"""Cover letter pipeline — lane-aware, resume-aligned, editable cover letters.

For a job + search lane: pick a template for the lane, resolve the best
available candidate context (the Phase 2 tailored resume if one exists), send
everything to Claude, and store the letter as files plus a
generated_documents row.

Output files per job + lane, in GENERATED_COVER_LETTERS_DIR:
    {job_id}_{lane}_cover_letter.txt        — editable source of truth
    {job_id}_{lane}_cover_letter.pdf        — recorded in generated_documents.file_path
    {job_id}_{lane}_cover_letter.docx
    {job_id}_{lane}_cover_letter.meta.json  — which candidate context was used

A letter is served from cache when the job description, lane, and candidate
context are unchanged. Manually edited (and migrated legacy) letters are
never overwritten without force.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import (
    GENERATED_COVER_LETTERS_DIR,
    GENERATED_RESUMES_DIR,
    PROJECT_ROOT,
    RESUMES_DIR,
    AppSettings,
    ContactConfig,
    SearchLaneConfig,
)
from app.database import Database
from app.polisher.claude_client import ClaudeClient, PolishResult
from app.polisher.resume import load_resume
from app.resume_generator.formatter import render_cover_letter_docx, render_cover_letter_pdf
from app.resume_generator.linkedin_parser import (
    DEFAULT_BULLETS_PATH,
    DEFAULT_DATA_PATH,
    load_additional_bullets,
    load_linkedin_data,
    merge_additional_bullets,
)
from app.resume_generator.models import LinkedInData
from app.resume_generator.pipeline import JobNotFoundError, load_saved_resume, resume_paths

logger = logging.getLogger(__name__)

TEMPLATES_DIR = PROJECT_ROOT / "templates"
DOC_TYPE = "cover_letter"

# Rows whose text came from a human (or a pre-v2 draft) — never overwrite without force
MANUAL_EDIT_MODEL = "manual-edit"
LEGACY_MODEL = "legacy-migration"
PROTECTED_MODELS = {MANUAL_EDIT_MODEL, LEGACY_MODEL}

# Keywords that indicate a leadership/management role
LEAD_KEYWORDS = {
    "lead", "manager", "director", "head", "principal", "vp",
    "engineering manager", "development manager", "team lead",
}


def _pick_template(lane_name: str, job_title: str) -> str | None:
    """Pick the cover letter template (structure + tone guidance) for a lane and title."""
    if lane_name == "marketing_manager":
        filename = "cover_letter_marketing.txt"
    elif lane_name == "frontend_developer":
        title_lower = job_title.lower()
        if any(kw in title_lower for kw in LEAD_KEYWORDS):
            filename = "cover_letter_frontend_lead.txt"
        else:
            filename = "cover_letter_frontend_ic.txt"
    else:
        filename = "cover_letter_general.txt"

    template_path = TEMPLATES_DIR / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    logger.debug("No template found at %s", template_path)
    return None


def cover_letter_paths(output_dir: Path, job_id: int, lane_name: str) -> tuple[Path, Path, Path]:
    """(txt, pdf, docx) paths for a job + lane."""
    stem = output_dir / f"{job_id}_{lane_name}_cover_letter"
    return stem.with_suffix(".txt"), stem.with_suffix(".pdf"), stem.with_suffix(".docx")


def _meta_path(txt_path: Path) -> Path:
    return txt_path.with_suffix(".meta.json")


def read_context_source(txt_path: Path) -> str:
    """Which candidate context a saved letter was generated from ('unknown' if not recorded)."""
    try:
        return json.loads(_meta_path(txt_path).read_text(encoding="utf-8"))["context_source"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return "unknown"


def _write_meta(txt_path: Path, context_source: str) -> None:
    _meta_path(txt_path).write_text(json.dumps({"context_source": context_source}),
                                    encoding="utf-8")


@dataclass
class CoverLetterResult:
    text: str
    txt_path: Path
    pdf_path: Path
    docx_path: Path
    doc_id: int
    context_source: str  # tailored_resume | linkedin_data | static_pdf | legacy
    gen_result: PolishResult | None  # None when served from cache or edited
    from_cache: bool
    model_used: str | None = None

    @property
    def edited(self) -> bool:
        return self.model_used == MANUAL_EDIT_MODEL


class PolishPipeline:
    """Generates, caches, and stores lane-aware cover letters."""

    def __init__(
        self,
        db: Database,
        claude: ClaudeClient | None,
        settings: AppSettings,
        output_dir: Path = GENERATED_COVER_LETTERS_DIR,
        resumes_output_dir: Path = GENERATED_RESUMES_DIR,
        linkedin_path: Path = DEFAULT_DATA_PATH,
        bullets_path: Path = DEFAULT_BULLETS_PATH,
        static_resumes_dir: Path = RESUMES_DIR,
    ) -> None:
        self.db = db
        self.claude = claude  # may be None for edit/migrate-only use
        self.settings = settings
        self.output_dir = output_dir
        self.resumes_output_dir = resumes_output_dir
        self.linkedin_path = linkedin_path
        self.bullets_path = bullets_path
        self.static_resumes_dir = static_resumes_dir

    # ------------------------------------------------------------------
    # Candidate data
    # ------------------------------------------------------------------

    def _load_linkedin(self) -> LinkedInData | None:
        try:
            return load_linkedin_data(self.linkedin_path)
        except FileNotFoundError:
            return None

    def _signature(self) -> tuple[str, ContactConfig]:
        """(full_name, contact) — profile.yaml contact, with LinkedIn email/phone as fallback."""
        contact = self.settings.profile.contact.model_copy()
        data = self._load_linkedin()
        if data is None:
            return "", contact
        contact.email = contact.email or data.email
        contact.phone = contact.phone or data.phone
        contact.linkedin_url = contact.linkedin_url or data.linkedin_url
        return data.full_name, contact

    async def _resolve_candidate_context(
        self, job_id: int, lane: SearchLaneConfig
    ) -> tuple[str, str]:
        """Best available candidate context as (context_text, source_label).

        Priority: the tailored resume for this job + lane → parsed LinkedIn
        data → a static resume PDF matching lane.resume_version.
        """
        pdf_path, _, _ = resume_paths(self.resumes_output_dir, job_id, lane.name)
        tailored = load_saved_resume(pdf_path)
        if tailored is not None:
            return tailored.to_prompt_text(), "tailored_resume"

        data = self._load_linkedin()
        if data is not None:
            merged = merge_additional_bullets(data, load_additional_bullets(self.bullets_path))
            return merged.to_prompt_text(), "linkedin_data"

        version = lane.resume_version.lower()
        if version and self.static_resumes_dir.exists():
            for pdf in sorted(self.static_resumes_dir.glob("*.pdf")):
                if version in pdf.stem.lower():
                    return load_resume(pdf), "static_pdf"

        raise FileNotFoundError(
            "No candidate data for a cover letter — run `job-agent parse-linkedin` "
            "(and ideally `job-agent resume` for this job) first."
        )

    # ------------------------------------------------------------------
    # Files + DB helpers
    # ------------------------------------------------------------------

    def _render(self, text: str, txt_path: Path, pdf_path: Path, docx_path: Path) -> None:
        full_name, contact = self._signature()
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(text, encoding="utf-8")
        render_cover_letter_pdf(text, full_name, contact, pdf_path)
        render_cover_letter_docx(text, full_name, contact, docx_path)

    def _cached_result(self, doc: dict, job_id: int, lane_name: str) -> CoverLetterResult:
        txt_path, pdf_path, docx_path = cover_letter_paths(self.output_dir, job_id, lane_name)
        text = txt_path.read_text(encoding="utf-8")
        if not pdf_path.exists() or not docx_path.exists():
            self._render(text, txt_path, pdf_path, docx_path)
        return CoverLetterResult(
            text=text, txt_path=txt_path, pdf_path=pdf_path, docx_path=docx_path,
            doc_id=doc["id"], context_source=read_context_source(txt_path),
            gen_result=None, from_cache=True, model_used=doc["model_used"],
        )

    async def _load_job_and_lane(self, job_id: int, lane_name: str) -> tuple[dict, SearchLaneConfig]:
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
        return job, lane

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def polish_job(
        self, job_id: int, lane_name: str, force: bool = False
    ) -> CoverLetterResult:
        """Generate a cover letter for a job + lane, or return the cached/edited one.

        Raises:
            JobNotFoundError: job doesn't exist.
            ValueError: unknown lane, or the job doesn't belong to that lane.
            FileNotFoundError: no candidate data available.
        """
        job, lane = await self._load_job_and_lane(job_id, lane_name)
        txt_path, pdf_path, docx_path = cover_letter_paths(self.output_dir, job_id, lane_name)
        existing = await self.db.get_generated_document(job_id, lane_name, DOC_TYPE)

        # Human-edited or migrated text wins unless explicitly forced
        if (existing and not force and existing["model_used"] in PROTECTED_MODELS
                and txt_path.exists()):
            logger.info("Cover letter for job #%d [%s] is %s — kept (use force to regenerate)",
                        job_id, lane_name, existing["model_used"])
            return self._cached_result(existing, job_id, lane_name)

        context_text, context_source = await self._resolve_candidate_context(job_id, lane)
        description = job.get("description") or ""
        content_hash = hashlib.sha256(
            (description + lane_name + context_text).encode("utf-8")
        ).hexdigest()[:16]

        if (existing and not force and existing["content_hash"] == content_hash
                and txt_path.exists()):
            logger.info("Cover letter for job #%d [%s] served from cache", job_id, lane_name)
            return self._cached_result(existing, job_id, lane_name)

        if self.claude is None:
            raise RuntimeError("PolishPipeline needs a ClaudeClient to generate cover letters")

        title = job.get("title") or "Unknown Position"
        company = job.get("company") or "Unknown Company"
        if len(description) < 50:
            logger.warning("Job #%d has no/short description — cover letter may be generic", job_id)

        highlights: list[str] = []
        evaluation = await self.db.get_evaluation(job_id, lane=lane_name)
        reasoning = evaluation.reasoning if evaluation else job.get("eval_reasoning")
        if reasoning:
            highlights.append(reasoning)

        if context_source != "tailored_resume":
            logger.warning("Cover letter for job #%d [%s] uses %s — generate a resume first "
                           "for better alignment", job_id, lane_name, context_source)

        full_name, contact = self._signature()
        gen = await self.claude.generate_cover_letter(
            job_title=title,
            company=company,
            job_description=description or "No description available.",
            candidate_context=context_text,
            contact=contact,
            full_name=full_name,
            template=_pick_template(lane_name, title),
            eval_highlights=highlights or None,
            lane_target_field=lane.target_field,
        )

        self._render(gen.cover_letter, txt_path, pdf_path, docx_path)
        _write_meta(txt_path, context_source)

        await self.db.delete_generated_document(job_id, lane_name, DOC_TYPE)
        doc_id = await self.db.insert_generated_document(
            job_id=job_id, search_lane=lane_name, doc_type=DOC_TYPE,
            file_path=str(pdf_path), model_used=gen.model_used, content_hash=content_hash,
        )

        logger.info("Cover letter saved for job #%d (%s @ %s) [%s, %s] — %s",
                    job_id, title, company, lane_name, context_source, gen.cost_display)
        return CoverLetterResult(
            text=gen.cover_letter, txt_path=txt_path, pdf_path=pdf_path, docx_path=docx_path,
            doc_id=doc_id, context_source=context_source, gen_result=gen, from_cache=False,
            model_used=gen.model_used,
        )

    async def save_edited_cover_letter(
        self, job_id: int, lane_name: str, text: str
    ) -> CoverLetterResult:
        """Save a hand-edited letter: overwrite the .txt, re-render, mark the row 'manual-edit'."""
        await self._load_job_and_lane(job_id, lane_name)
        text = text.replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("Cover letter text is empty")

        txt_path, pdf_path, docx_path = cover_letter_paths(self.output_dir, job_id, lane_name)
        context_source = read_context_source(txt_path)
        self._render(text, txt_path, pdf_path, docx_path)
        if context_source == "unknown":
            _write_meta(txt_path, "manual")
            context_source = "manual"

        edit_hash = "edited-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        existing = await self.db.get_generated_document(job_id, lane_name, DOC_TYPE)
        if existing:
            await self.db.update_generated_document(existing["id"], edit_hash, MANUAL_EDIT_MODEL)
            doc_id = existing["id"]
        else:
            doc_id = await self.db.insert_generated_document(
                job_id=job_id, search_lane=lane_name, doc_type=DOC_TYPE,
                file_path=str(pdf_path), model_used=MANUAL_EDIT_MODEL, content_hash=edit_hash,
            )

        logger.info("Cover letter for job #%d [%s] saved with manual edits", job_id, lane_name)
        return CoverLetterResult(
            text=text, txt_path=txt_path, pdf_path=pdf_path, docx_path=docx_path,
            doc_id=doc_id, context_source=context_source, gen_result=None, from_cache=False,
            model_used=MANUAL_EDIT_MODEL,
        )

    async def migrate_legacy_cover_letters(self) -> int:
        """Move pre-v2 drafts from evaluations.cover_letter_draft into generated_documents.

        One letter per job + lane (the newest draft wins). Lane = the
        evaluation's lane, else the job's lane; 'both' or unknown →
        frontend_developer. Idempotent: skips job + lane pairs that already
        have a cover letter row. Returns the number migrated.
        """
        cursor = await self.db.conn.execute(
            """
            SELECT e.job_id, e.search_lane AS eval_lane, e.cover_letter_draft,
                   j.search_lane AS job_lane
            FROM evaluations e JOIN jobs j ON j.id = e.job_id
            WHERE e.cover_letter_draft IS NOT NULL AND TRIM(e.cover_letter_draft) != ''
            ORDER BY e.id DESC
            """
        )
        rows = await cursor.fetchall()

        seen: set[tuple[int, str]] = set()
        migrated = 0
        for row in rows:
            lane_name = row["eval_lane"] or row["job_lane"]
            if lane_name in (None, "both"):
                lane_name = "frontend_developer"
            key = (row["job_id"], lane_name)
            if key in seen:
                continue
            seen.add(key)

            if await self.db.get_generated_document(row["job_id"], lane_name, DOC_TYPE):
                continue

            text = row["cover_letter_draft"].replace("\r\n", "\n").strip()
            txt_path, pdf_path, docx_path = cover_letter_paths(
                self.output_dir, row["job_id"], lane_name)
            self._render(text, txt_path, pdf_path, docx_path)
            _write_meta(txt_path, "legacy")
            await self.db.insert_generated_document(
                job_id=row["job_id"], search_lane=lane_name, doc_type=DOC_TYPE,
                file_path=str(pdf_path), model_used=LEGACY_MODEL,
                content_hash="legacy-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10],
            )
            migrated += 1
            logger.info("Migrated legacy cover letter for job #%d [%s]", row["job_id"], lane_name)

        return migrated
