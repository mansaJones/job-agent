"""Anthropic Claude API client — generates tailored cover letters.

Uses the Anthropic Python SDK to send job description + resume to Claude
and get back a polished, targeted cover letter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import anthropic

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
