"""Anthropic Claude API client — generates tailored cover letters and resumes.

Uses the Anthropic Python SDK to send job description + resume to Claude
and get back a polished, targeted cover letter or a job-tailored resume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

from app.evaluator.pipeline import _smart_truncate
from app.utils.json_extract import extract_json_object

if TYPE_CHECKING:
    from app.resume_generator.models import LinkedInData

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"


@dataclass
class PolishResult:
    """Result from the cover letter generation."""
    cover_letter: str
    model_used: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0

    @property
    def cost_display(self) -> str:
        return f"${self.cost_estimate:.4f}"


@dataclass
class ResumeGenResult:
    """Result from tailored resume generation."""
    resume_json: dict[str, Any] = field(default_factory=dict)
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0

    @property
    def cost_display(self) -> str:
        return f"${self.cost_estimate:.4f}"


RESUME_SYSTEM_PROMPT = """You are a resume writer. You produce a tailored resume as JSON only.

NEVER invent skills, tools, companies, titles, dates, or achievements. Every claim must be traceable to the candidate data provided. If the job requires something the candidate has no evidence of, list it in `unmatched_requirements` — do not manufacture it.

Rewrite bullet points to mirror the job description's vocabulary where the candidate genuinely did that work. Reorder positions and bullets by relevance to this specific job. Prefer bullets with metrics.

Respond with a single JSON object matching the schema. No prose, no markdown."""

RESUME_JSON_SCHEMA = """{
  "headline": "one line, rewritten to target this job",
  "summary": "3-4 sentences targeted at this job",
  "positions": [
    {
      "company": "exactly as in the candidate data",
      "title": "exactly as in the candidate data",
      "location": "string or null",
      "start_date": "YYYY-MM, exactly as in the candidate data",
      "end_date": "YYYY-MM or null for current, exactly as in the candidate data",
      "bullets": [{"text": "rewritten bullet", "matched_skills": ["job requirement this bullet addresses"]}]
    }
  ],
  "skills": ["most job-relevant first, max 20, only skills the candidate has"],
  "matched_requirements": ["job requirements the resume addresses"],
  "unmatched_requirements": ["job requirements with no supporting experience"]
}"""


class ClaudeClient:
    """Async client for the Anthropic Claude API."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client: anthropic.AsyncAnthropic | None = None

    async def __aenter__(self) -> ClaudeClient:
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            raise RuntimeError("ClaudeClient not initialized — use async with")
        return self._client

    async def generate_cover_letter(
        self,
        job_title: str,
        company: str,
        job_description: str,
        resume_text: str,
        template: str | None = None,
        eval_highlights: list[str] | None = None,
    ) -> PolishResult:
        """Generate a tailored cover letter for a specific job.

        Args:
            job_title: The job title.
            company: The company name.
            job_description: Full job posting description.
            resume_text: The candidate's resume as plain text.
            template: Optional cover letter template to adapt.
            eval_highlights: Optional highlights from the local LLM evaluation.

        Returns:
            PolishResult with the generated cover letter and usage stats.
        """
        system = (
            "You are an expert career coach and professional writer. "
            "You write compelling, authentic cover letters that highlight "
            "relevant experience without sounding generic or AI-generated. "
            "Keep the tone professional but personable. "
            "The letter should be concise (3-4 paragraphs, under 400 words)."
        )

        prompt_parts = [
            f"Write a tailored cover letter for the following job:\n",
            f"**Position:** {job_title}",
            f"**Company:** {company}",
            f"\n**Job Description:**\n{job_description[:4000]}",
            f"\n**Candidate Resume:**\n{resume_text[:5000]}",
        ]

        if eval_highlights:
            prompt_parts.append(
                f"\n**Key strengths identified for this role:** {', '.join(eval_highlights)}"
            )

        if template:
            prompt_parts.append(
                f"\n**Use this template as a starting structure (adapt it, don't copy verbatim):**\n{template}"
            )

        prompt_parts.append(
            "\nWrite the cover letter now. Do not include placeholder brackets "
            "like [Your Name] — use the candidate's actual information from the resume. "
            "Focus on specific, relevant experience that maps to this job's requirements."
        )

        prompt = "\n".join(prompt_parts)

        logger.info("Generating cover letter for '%s' at %s via %s", job_title, company, self.model)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        cover_letter = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # Estimate cost (Sonnet pricing: $3/M input, $15/M output as of 2025)
        cost = (input_tokens * 3 / 1_000_000) + (output_tokens * 15 / 1_000_000)

        result = PolishResult(
            cover_letter=cover_letter,
            model_used=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
        )

        logger.info(
            "Cover letter generated — %d tokens in, %d out, est. cost %s",
            input_tokens, output_tokens, result.cost_display,
        )

        return result

    async def generate_tailored_resume(
        self,
        job_title: str,
        company: str,
        job_description: str,
        lane_name: str,
        lane_target_field: str,
        linkedin_data: LinkedInData,
    ) -> ResumeGenResult:
        """Generate a resume tailored to one job, as JSON.

        Args:
            job_title: The job title.
            company: The company name.
            job_description: Full job posting description (truncated to 6000 chars).
            lane_name: Search lane this resume targets (e.g. "marketing_manager").
            lane_target_field: The lane's target field, for framing.
            linkedin_data: Candidate data with supplemental bullets already merged.

        Returns:
            ResumeGenResult with the parsed resume JSON and usage stats.
        """
        # Contact details, parse metadata, and raw descriptions (duplicated by
        # bullets) aren't needed to write the resume — keep them out of the prompt.
        candidate = linkedin_data.model_dump(
            exclude={"email", "phone", "linkedin_url", "websites", "exported_at",
                     "parse_warnings", "first_name", "last_name"},
        )
        for position in candidate["positions"]:
            position.pop("description", None)

        prompt = "\n".join([
            "Write a resume tailored to the following job.\n",
            f"**Position:** {job_title}",
            f"**Company:** {company}",
            f"**Search lane:** {lane_name} (target field: {lane_target_field or 'not specified'})",
            f"\n**Job Description:**\n{_smart_truncate(job_description or '', max_chars=6000)}",
            f"\n**Candidate Data (the ONLY source of truth):**\n{json.dumps(candidate, indent=1)}",
            f"\n**Respond with JSON matching this schema:**\n{RESUME_JSON_SCHEMA}",
            "\nInclude at most 5 positions. Keep company, title, and dates exactly as given.",
        ])

        logger.info("Generating tailored resume for '%s' at %s [%s] via %s",
                    job_title, company, lane_name, self.model)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=RESUME_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # Estimate cost (Sonnet pricing: $3/M input, $15/M output as of 2025)
        cost = (input_tokens * 3 / 1_000_000) + (output_tokens * 15 / 1_000_000)

        result = ResumeGenResult(
            resume_json=extract_json_object(raw),
            model_used=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
        )

        logger.info(
            "Resume generated — %d tokens in, %d out, est. cost %s",
            input_tokens, output_tokens, result.cost_display,
        )

        return result
