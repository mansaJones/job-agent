"""Tailored resume builder — asks Claude for a job-specific resume, then fact-checks it.

Claude supplies the headline, summary, reordered/rewritten positions, skills,
and requirement matching. Identity, contact line, education, and
certifications always come straight from the source data.

The anti-fabrication check strips any skill or position Claude produced that
can't be traced back to the candidate's LinkedIn data or supplemental bullets.
Problems are recorded in `fabrication_warnings` for the UI rather than failing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import SearchLaneConfig
from app.polisher.claude_client import ClaudeClient, ResumeGenResult
from app.resume_generator.models import LinkedInData, TailoredResume

logger = logging.getLogger(__name__)

MAX_POSITIONS = 5
MAX_SKILLS = 20


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _evidence_text(data: LinkedInData) -> str:
    """Everything the candidate has said about themselves, lowercased, for term lookup."""
    parts = [data.headline, data.summary]
    for p in data.positions:
        parts.extend([p.title, p.description, *p.bullets])
    parts.extend(c.name for c in data.certifications)
    return _norm("\n".join(parts))


def is_supported(term: str, data: LinkedInData, evidence: str | None = None) -> bool:
    """True if a skill/term is backed by the candidate's own data.

    Checks the skills list, supplemental bullet tags, and position/profile
    text. Text matches are whole-word so "Go" doesn't match "good".
    """
    term_n = _norm(term)
    if not term_n:
        return True
    if term_n in data.all_skills_lower:
        return True
    if term_n in {_norm(t) for t in data.supplemental_tags}:
        return True
    evidence = evidence if evidence is not None else _evidence_text(data)
    pattern = r"(?<![a-z0-9])" + re.escape(term_n) + r"(?![a-z0-9])"
    return re.search(pattern, evidence) is not None


def contact_line(data: LinkedInData) -> str:
    parts = [data.email, data.phone, data.location, data.linkedin_url]
    parts.extend(data.websites[:1])
    return " | ".join(p for p in parts if p)


def check_fabrication(resume: TailoredResume, data: LinkedInData) -> TailoredResume:
    """Remove unsupported skills and positions, recording a warning for each.

    Mutates and returns `resume`.
    """
    evidence = _evidence_text(data)
    warnings = list(resume.fabrication_warnings)

    def warn(msg: str) -> None:
        warnings.append(msg)
        logger.warning("Resume fabrication check: %s", msg)

    # Positions must exist in the source data (company + title)
    known = {(_norm(p.company), _norm(p.title)) for p in data.positions}
    kept_positions = []
    for position in resume.positions:
        if (_norm(position.company), _norm(position.title)) in known:
            kept_positions.append(position)
        else:
            warn(f"Position not in LinkedIn data, removed: {position.title} at {position.company}")
    resume.positions = kept_positions[:MAX_POSITIONS]

    # Skills list
    kept_skills = []
    for skill in resume.skills:
        if is_supported(skill, data, evidence):
            kept_skills.append(skill)
        else:
            warn(f"Skill not found in source data, removed: {skill}")
    resume.skills = list(dict.fromkeys(kept_skills))[:MAX_SKILLS]

    # Per-bullet matched skills
    for position in resume.positions:
        for bullet in position.bullets:
            kept = []
            for skill in bullet.matched_skills:
                if is_supported(skill, data, evidence):
                    kept.append(skill)
                else:
                    warn(f"Bullet claims unsupported skill '{skill}' ({position.company})")
            bullet.matched_skills = kept

    resume.fabrication_warnings = list(dict.fromkeys(warnings))
    return resume


def assemble_resume(resume_json: dict[str, Any], data: LinkedInData) -> TailoredResume:
    """Combine Claude's JSON with fixed fields from the source data and fact-check it."""
    payload = dict(resume_json)
    # Never trust the model with identity, contact, or credentials — use source data
    payload.update(
        full_name=data.full_name,
        contact_line=contact_line(data),
        education=[e.model_dump() for e in data.education],
        certifications=[c.model_dump() for c in data.certifications],
        fabrication_warnings=[],
    )
    payload.setdefault("headline", data.headline)
    payload.setdefault("summary", data.summary)
    payload.setdefault("positions", [])
    payload.setdefault("skills", [])
    resume = TailoredResume.model_validate(payload)
    return check_fabrication(resume, data)


async def build_tailored_resume(
    job: dict,
    lane: SearchLaneConfig,
    linkedin_data: LinkedInData,
    claude: ClaudeClient,
) -> tuple[TailoredResume, ResumeGenResult]:
    """Generate and fact-check a resume for one job + lane.

    Args:
        job: Row from db.get_job_with_evaluation().
        lane: The search lane this resume targets.
        linkedin_data: Candidate data with supplemental bullets merged.
        claude: An open ClaudeClient.
    """
    gen = await claude.generate_tailored_resume(
        job_title=job.get("title") or "Unknown Position",
        company=job.get("company") or "Unknown Company",
        job_description=job.get("description") or "",
        lane_name=lane.name,
        lane_target_field=lane.target_field,
        linkedin_data=linkedin_data,
    )
    resume = assemble_resume(gen.resume_json, linkedin_data)
    logger.info(
        "Tailored resume for job #%s [%s]: %d positions, %d skills, %d matched, "
        "%d unmatched, %d fabrication warnings",
        job.get("id"), lane.name, len(resume.positions), len(resume.skills),
        len(resume.matched_requirements), len(resume.unmatched_requirements),
        len(resume.fabrication_warnings),
    )
    return resume, gen
