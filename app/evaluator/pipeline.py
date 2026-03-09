"""Evaluation pipeline — scores jobs against your profile using the local LLM.

Pulls unevaluated jobs from the DB, sends each one to Ollama with your
profile context, parses the JSON score, and updates statuses based on
configurable thresholds.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from app.config import ProfileConfig
from app.database import Database, EvaluationRecord, JobRecord
from app.evaluator.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score thresholds — these drive auto-filtering
# ---------------------------------------------------------------------------

@dataclass
class EvalThresholds:
    """Score thresholds for auto-categorization."""
    ready_for_review: float = 0.7   # score >= this → "evaluated" (ready for review)
    maybe: float = 0.4              # score >= this → "maybe"
    auto_reject: float = 0.4        # score < this → "rejected"


# ---------------------------------------------------------------------------
# Evaluation result from LLM
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Parsed evaluation from the LLM response."""
    score: float
    reasoning: str
    red_flags: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a job matching assistant. You evaluate job postings against a candidate's profile and provide structured scoring.

You MUST respond with valid JSON only. No markdown, no explanation outside the JSON object."""

EVAL_PROMPT_TEMPLATE = """Score how well this job matches the candidate profile from 0.0 to 1.0.

SCORING RULES (follow these exactly):
- The candidate does NOT need ALL must-have skills. Matching even ONE must-have skill is a positive signal.
- A job matching 2-3 must-have skills is a strong match (0.6+). Matching 4+ is excellent (0.8+).
- Nice-to-have skills are bonus points, not requirements.
- Role/title alignment: if the job title is similar to ANY target role, that's a strong positive.
- Remote/hybrid jobs should score well if remote_ok or hybrid_ok is True.
- If salary is not listed, do NOT penalize the score — treat it as neutral.
- Only give scores below 0.3 for jobs that are clearly irrelevant (wrong field entirely, junior when candidate is senior, etc.)

CANDIDATE PROFILE:
Target Roles: {target_roles}
Core Skills (match on ANY, not all): {must_have_skills}
Bonus Skills: {nice_to_have_skills}
Location: {location}
Remote OK: {remote_ok} | Hybrid OK: {hybrid_ok} | Onsite OK: {onsite_ok}
Max Commute: {max_commute_miles} miles
Minimum Salary: ${salary_min:,}/year
Experience: {experience_years} years

JOB POSTING:
Title: {job_title}
Company: {job_company}
Location: {job_location}
Salary Range: {job_salary}
Description:
{job_description}

Respond with this exact JSON structure:
{{"score": 0.0, "reasoning": "2-3 sentence explanation", "red_flags": ["list", "of", "concerns"], "highlights": ["list", "of", "positives"]}}"""


def build_profile_context(profile: ProfileConfig) -> dict[str, str]:
    """Flatten the profile config into template-friendly strings."""
    return {
        "target_roles": ", ".join(profile.target_roles),
        "must_have_skills": ", ".join(profile.skills.must_have),
        "nice_to_have_skills": ", ".join(profile.skills.nice_to_have),
        "location": profile.preferences.location,
        "remote_ok": str(profile.preferences.remote_ok),
        "hybrid_ok": str(profile.preferences.hybrid_ok),
        "onsite_ok": str(profile.preferences.onsite_ok),
        "max_commute_miles": str(profile.preferences.max_commute_miles),
        "salary_min": profile.preferences.salary_min,
        "experience_years": profile.preferences.experience_years,
    }


def build_eval_prompt(profile: ProfileConfig, job: JobRecord) -> str:
    """Build the full evaluation prompt for a single job."""
    ctx = build_profile_context(profile)

    # Format salary range for display
    if job.salary_min and job.salary_max:
        job_salary = f"${job.salary_min:,.0f} – ${job.salary_max:,.0f}"
    elif job.salary_min:
        job_salary = f"From ${job.salary_min:,.0f}"
    elif job.salary_max:
        job_salary = f"Up to ${job.salary_max:,.0f}"
    else:
        job_salary = "Not listed"

    # Truncate description to avoid blowing context window
    description = job.description or "No description available"
    if len(description) > 3000:
        description = description[:3000] + "\n[... truncated]"

    return EVAL_PROMPT_TEMPLATE.format(
        **ctx,
        job_title=job.title,
        job_company=job.company or "Unknown",
        job_location=job.location or "Not specified",
        job_salary=job_salary,
        job_description=description,
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_eval_response(raw: str) -> EvalResult:
    """Parse the LLM's JSON response into an EvalResult.

    Handles common LLM quirks: markdown fences, trailing commas, extra text
    around the JSON, etc.
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # Try to find JSON object in the response
    json_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, re.DOTALL)
    if json_match:
        cleaned = json_match.group(0)

    # Fix trailing commas (common LLM mistake)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse LLM JSON: %s\nRaw response: %s", e, raw[:500])
        # Fall back to regex extraction
        return _fallback_parse(raw)

    score = float(data.get("score", 0.0))
    # Clamp to valid range
    score = max(0.0, min(1.0, score))

    return EvalResult(
        score=score,
        reasoning=str(data.get("reasoning", "")),
        red_flags=data.get("red_flags", []) or [],
        highlights=data.get("highlights", []) or [],
        raw_response=raw,
    )


def _fallback_parse(raw: str) -> EvalResult:
    """Last-resort parser — regex out a score if JSON parsing fails entirely."""
    score_match = re.search(r'"?score"?\s*[:=]\s*([0-9]*\.?[0-9]+)', raw)
    score = float(score_match.group(1)) if score_match else 0.0
    score = max(0.0, min(1.0, score))

    reasoning_match = re.search(r'"?reasoning"?\s*[:=]\s*"([^"]+)"', raw)
    reasoning = reasoning_match.group(1) if reasoning_match else "Parse error — see raw response"

    return EvalResult(
        score=score,
        reasoning=reasoning,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

@dataclass
class PipelineStats:
    """Tracks evaluation run metrics."""
    total_jobs: int = 0
    evaluated: int = 0
    ready_for_review: int = 0
    maybe: int = 0
    auto_rejected: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def duration_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def summary(self) -> str:
        return (
            f"Evaluated: {self.evaluated}/{self.total_jobs} | "
            f"Review: {self.ready_for_review} | Maybe: {self.maybe} | "
            f"Rejected: {self.auto_rejected} | Errors: {self.errors} | "
            f"Time: {self.duration_seconds:.1f}s"
        )


class EvaluationPipeline:
    """Runs batch evaluations of new jobs against the candidate profile."""

    def __init__(
        self,
        db: Database,
        ollama: OllamaClient,
        profile: ProfileConfig,
        thresholds: EvalThresholds | None = None,
    ) -> None:
        self.db = db
        self.ollama = ollama
        self.profile = profile
        self.thresholds = thresholds or EvalThresholds()

    def _classify(self, score: float) -> str:
        """Map a score to a job status based on thresholds."""
        if score >= self.thresholds.ready_for_review:
            return "evaluated"  # ready for human review
        elif score >= self.thresholds.maybe:
            return "maybe"
        else:
            return "rejected"

    async def evaluate_job(self, job: JobRecord) -> EvalResult | None:
        """Evaluate a single job. Returns the result or None on failure."""
        prompt = build_eval_prompt(self.profile, job)

        try:
            raw_response = await self.ollama.generate(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                temperature=0.3,
                format_json=True,
            )
        except OllamaError as e:
            logger.error("Ollama error evaluating job %s: %s", job.id, e)
            return None

        result = parse_eval_response(raw_response)

        # Store evaluation in DB
        eval_record = EvaluationRecord(
            job_id=job.id,  # type: ignore[arg-type]
            model_used=self.ollama.model,
            match_score=result.score,
            reasoning=result.reasoning,
        )
        await self.db.insert_evaluation(eval_record)

        # Update job status based on score
        new_status = self._classify(result.score)

        # Build rejection reason for rejected jobs so we know why
        rejection_reason = None
        if new_status == "rejected":
            parts = [f"Score {result.score:.2f}"]
            if result.reasoning:
                parts.append(result.reasoning)
            if result.red_flags:
                parts.append(f"Red flags: {', '.join(result.red_flags)}")
            rejection_reason = " | ".join(parts)

        await self.db.update_job_status(
            job.id, new_status, rejection_reason=rejection_reason  # type: ignore[arg-type]
        )

        logger.info(
            "Job #%d [%s @ %s] → score=%.2f status=%s",
            job.id, job.title, job.company, result.score, new_status,
        )

        return result

    async def run(self, limit: int = 100) -> PipelineStats:
        """Evaluate all unevaluated ('new') jobs.

        Args:
            limit: Max number of jobs to evaluate in this batch.

        Returns:
            PipelineStats with run metrics.
        """
        stats = PipelineStats()

        # Check Ollama health first
        if not await self.ollama.is_healthy():
            logger.error("Ollama is not reachable at %s — aborting evaluation", self.ollama.base_url)
            raise OllamaError(f"Ollama not reachable at {self.ollama.base_url}")

        if not await self.ollama.model_available():
            available = await self.ollama.list_models()
            logger.error(
                "Model '%s' not found. Available models: %s",
                self.ollama.model, available,
            )
            raise OllamaError(
                f"Model '{self.ollama.model}' not available. "
                f"Pull it with: ollama pull {self.ollama.model}"
            )

        # Grab unevaluated jobs
        new_jobs = await self.db.get_jobs_by_status("new", limit=limit)
        stats.total_jobs = len(new_jobs)

        if not new_jobs:
            logger.info("No new jobs to evaluate")
            return stats

        logger.info("Starting evaluation of %d jobs with model '%s'", len(new_jobs), self.ollama.model)

        for job in new_jobs:
            result = await self.evaluate_job(job)

            if result is None:
                stats.errors += 1
                continue

            stats.evaluated += 1
            status = self._classify(result.score)
            if status == "evaluated":
                stats.ready_for_review += 1
            elif status == "maybe":
                stats.maybe += 1
            else:
                stats.auto_rejected += 1

        logger.info("Evaluation complete — %s", stats.summary())
        return stats
