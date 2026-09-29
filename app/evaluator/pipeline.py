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

from app.config import ProfileConfig, SearchLaneConfig
from app.database import Database, EvaluationRecord, JobRecord
from app.evaluator.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score thresholds — these drive auto-filtering
# ---------------------------------------------------------------------------

@dataclass
class EvalThresholds:
    """Score thresholds for auto-categorization.

    Tuned down from 0.7/0.4 because the 3B model consistently underscores
    by ~0.25 — its reasoning is solid but the numbers are deflated.
    """
    ready_for_review: float = 0.55  # score >= this → "evaluated" (ready for review)
    maybe: float = 0.25             # score >= this → "maybe"
    auto_reject: float = 0.25       # score < this → "rejected"


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

SYSTEM_PROMPT = """You are a job matching assistant. You evaluate job postings against a candidate's profile and return a JSON score.

You MUST respond with valid JSON only. No markdown, no explanation outside the JSON."""

EVAL_PROMPT_TEMPLATE = """Score this job 0.0 to 1.0 for the candidate below. Follow the scoring tiers EXACTLY.

=== SCORING TIERS (use these as hard anchors) ===

0.85-1.0  EXCELLENT — Title matches a target role AND 3+ core skills AND seniority fits
0.70-0.84 STRONG   — Title is close to a target role AND 2+ core skills AND seniority fits
0.55-0.69 GOOD     — Related role, 2+ core skills, but title or seniority is slightly off
0.40-0.54 MAYBE    — Some skill overlap but role is tangential or seniority mismatch
0.20-0.39 WEAK     — Minimal overlap, wrong field, or wrong seniority level
0.00-0.19 NO MATCH — Completely different field (medical, legal, mechanical, etc.)

=== MANDATORY RULES ===

SENIORITY (critical — candidate has {experience_years} years experience):
- Target seniority: Lead, Senior, Manager, Principal, Staff, Director
- Jobs titled "Junior", "Associate", "Entry Level", or "I/II" without "Senior" → cap at 0.30
- Jobs with no seniority indicator → score normally based on skills and duties
- Federal GS grades: GS-13+ is senior-level, GS-12 is mid, GS-11 and below is junior

SKILLS (match on ANY, not ALL):
- Core skills: {must_have_skills}
- Bonus skills: {nice_to_have_skills}
- Gate skills (job must list at least ONE, or cap at 0.35): {must_have_any_skills}
- Matching 1 core skill = positive signal. 2-3 = strong. 4+ = excellent.
- Bonus skills add +0.05 each (max +0.15 total)

SALARY:
- Candidate minimum: ${salary_min:,}/year
- If salary listed and max is BELOW ${salary_min:,} → subtract 0.20 from score
- If salary not listed → neutral (no penalty)

LOCATION:
- Remote OK: {remote_ok} | Hybrid OK: {hybrid_ok} | Onsite OK: {onsite_ok}
- If job requires onsite and onsite_ok is False → subtract 0.15

FIELD RELEVANCE:
- Target field: {target_field}
- Medical, legal, clerical, mechanical, accounting, HR roles → cap at 0.15 regardless of skills listed
- Government IT/software roles ARE relevant — score them normally

=== CANDIDATE PROFILE ===
Target Roles: {target_roles}
Location: {location} (Max commute: {max_commute_miles} mi)

=== JOB POSTING ===
Title: {job_title}
Company: {job_company}
Location: {job_location}
Salary: {job_salary}
Source: {job_source}

Description:
{job_description}

=== RESPOND WITH THIS JSON ===
{{"score": 0.0, "reasoning": "2-3 sentences", "red_flags": ["concern1"], "highlights": ["positive1"]}}"""


def build_profile_context(
    profile: ProfileConfig, lane: SearchLaneConfig
) -> dict[str, str | int]:
    """Flatten a search lane + shared preferences into template-friendly strings."""
    return {
        "target_roles": ", ".join(lane.target_roles),
        "target_field": lane.target_field or "not specified",
        "must_have_skills": ", ".join(lane.skills.must_have) or "None specified",
        "must_have_any_skills": ", ".join(lane.skills.must_have_any) or "None (no gate)",
        "nice_to_have_skills": ", ".join(lane.skills.nice_to_have) or "None specified",
        "location": profile.preferences.location,
        "remote_ok": str(profile.preferences.remote_ok),
        "hybrid_ok": str(profile.preferences.hybrid_ok),
        "onsite_ok": str(profile.preferences.onsite_ok),
        "max_commute_miles": str(profile.preferences.max_commute_miles),
        "salary_min": profile.preferences.salary_min,
        "experience_years": profile.preferences.experience_years,
    }


def _smart_truncate(description: str, max_chars: int = 4000) -> str:
    """Truncate long descriptions while preserving the most useful sections.

    Strategy: keep the first chunk (usually role summary) and the last chunk
    (usually requirements/qualifications), trimming the middle filler.
    """
    if len(description) <= max_chars:
        return description

    # Keep first 60% and last 30% of budget, with a gap indicator
    head_budget = int(max_chars * 0.60)
    tail_budget = int(max_chars * 0.30)

    head = description[:head_budget]
    tail = description[-tail_budget:]

    return f"{head}\n[... middle section trimmed ...]\n{tail}"


def build_eval_prompt(profile: ProfileConfig, lane: SearchLaneConfig, job: JobRecord) -> str:
    """Build the full evaluation prompt for a single job against one search lane."""
    ctx = build_profile_context(profile, lane)

    # Format salary range for display
    if job.salary_min and job.salary_max:
        job_salary = f"${job.salary_min:,.0f} – ${job.salary_max:,.0f}"
    elif job.salary_min:
        job_salary = f"From ${job.salary_min:,.0f}"
    elif job.salary_max:
        job_salary = f"Up to ${job.salary_max:,.0f}"
    else:
        job_salary = "Not listed"

    # Smart truncation — keeps head (summary) and tail (requirements)
    description = job.description or "No description available"
    description = _smart_truncate(description, max_chars=4000)

    return EVAL_PROMPT_TEMPLATE.format(
        **ctx,
        job_title=job.title,
        job_company=job.company or "Unknown",
        job_location=job.location or "Not specified",
        job_salary=job_salary,
        job_description=description,
        job_source=job.source or "unknown",
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

    def _lanes_for_job(self, job: JobRecord) -> list[SearchLaneConfig]:
        """Resolve which enabled lanes a job should be evaluated against.

        'both' → every enabled lane. A single lane → that lane. Unknown or
        disabled lane (e.g. legacy rows) → fall back to the first enabled lane.
        """
        enabled = self.profile.enabled_lanes
        if job.search_lane == "both":
            return enabled
        for lane in enabled:
            if lane.name == job.search_lane:
                return [lane]
        if enabled:
            logger.warning(
                "Job #%s has lane '%s' which is not enabled — evaluating against '%s'",
                job.id, job.search_lane, enabled[0].name,
            )
            return [enabled[0]]
        return []

    async def _evaluate_lane(
        self, job: JobRecord, lane: SearchLaneConfig
    ) -> EvalResult | None:
        """Run a single LLM evaluation of a job against one lane and store it."""
        prompt = build_eval_prompt(self.profile, lane, job)

        try:
            raw_response = await self.ollama.generate(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                temperature=0.3,
                format_json=True,
            )
        except OllamaError as e:
            logger.error("Ollama error evaluating job %s [%s]: %s", job.id, lane.name, e)
            return None

        result = parse_eval_response(raw_response)

        # Store evaluation in DB
        eval_record = EvaluationRecord(
            job_id=job.id,  # type: ignore[arg-type]
            model_used=self.ollama.model,
            match_score=result.score,
            reasoning=result.reasoning,
            search_lane=lane.name,
        )
        await self.db.insert_evaluation(eval_record)

        logger.info(
            "Job #%d [%s @ %s] lane=%s → score=%.2f",
            job.id, job.title, job.company, lane.name, result.score,
        )
        return result

    async def evaluate_job(self, job: JobRecord) -> list[EvalResult]:
        """Evaluate a job against each lane that applies to it.

        Returns one EvalResult per successful lane evaluation (empty list on
        total failure). The job's status is set from the highest lane score.
        """
        lanes = self._lanes_for_job(job)
        if not lanes:
            logger.error("No enabled search lanes — cannot evaluate job %s", job.id)
            return []

        results: list[EvalResult] = []
        for lane in lanes:
            result = await self._evaluate_lane(job, lane)
            if result is not None:
                results.append(result)

        if not results:
            return []

        # Best lane wins — a job only needs to fit one of your role families
        best = max(results, key=lambda r: r.score)
        new_status = self._classify(best.score)

        # Build rejection reason for rejected jobs so we know why
        rejection_reason = None
        if new_status == "rejected":
            parts = [f"Score {best.score:.2f}"]
            if best.reasoning:
                parts.append(best.reasoning)
            if best.red_flags:
                parts.append(f"Red flags: {', '.join(best.red_flags)}")
            rejection_reason = " | ".join(parts)

        await self.db.update_job_status(
            job.id, new_status, rejection_reason=rejection_reason  # type: ignore[arg-type]
        )

        logger.info(
            "Job #%d [%s @ %s] lane=%s → best score=%.2f status=%s",
            job.id, job.title, job.company, job.search_lane, best.score, new_status,
        )

        return results

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
            results = await self.evaluate_job(job)

            if not results:
                stats.errors += 1
                continue

            # Count per job, not per lane-evaluation
            stats.evaluated += 1
            status = self._classify(max(r.score for r in results))
            if status == "evaluated":
                stats.ready_for_review += 1
            elif status == "maybe":
                stats.maybe += 1
            else:
                stats.auto_rejected += 1

        logger.info("Evaluation complete — %s", stats.summary())
        return stats
