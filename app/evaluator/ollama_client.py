"""Ollama HTTP API client — talks to the local LLM on the Jetson.

Handles request formatting, streaming responses, health checks, and
model availability. Ollama runs on localhost:11434 by default.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b-instruct-q4_K_M"


class OllamaError(Exception):
    """Raised when Ollama returns an error or is unreachable."""


class OllamaClient:
    """Async client for the Ollama HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Health / model management
    # ------------------------------------------------------------------

    async def is_healthy(self) -> bool:
        """Check if Ollama is running and responsive."""
        try:
            response = await self.client.get("/api/tags")
            return response.status_code == 200
        except httpx.RequestError:
            return False

    async def list_models(self) -> list[str]:
        """Get list of available model names."""
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            return [m["name"] for m in data.get("models", [])]
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error("Failed to list Ollama models: %s", e)
            return []

    async def model_available(self) -> bool:
        """Check if the configured model is pulled and ready."""
        models = await self.list_models()
        # Ollama model names can have tags — match flexibly
        return any(
            self.model in m or m.startswith(self.model.split(":")[0])
            for m in models
        )

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        format_json: bool = True,
    ) -> str:
        """Send a prompt to Ollama and return the full response text.

        Uses the non-streaming /api/generate endpoint for simplicity.
        On the Orin at ~10-15 tok/s, a typical eval takes 30-60 seconds.

        Args:
            prompt: The user prompt to send.
            system: Optional system prompt.
            temperature: Sampling temperature (lower = more deterministic).
            format_json: If True, request JSON output format from Ollama.

        Returns:
            The generated text response.

        Raises:
            OllamaError: If Ollama is unreachable or returns an error.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": 4096,
            },
        }

        if system:
            payload["system"] = system

        if format_json:
            payload["format"] = "json"

        try:
            response = await self.client.post("/api/generate", json=payload)
            response.raise_for_status()
        except httpx.RequestError as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.base_url} — is it running? Error: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise OllamaError(
                f"Ollama returned HTTP {e.response.status_code}: {e.response.text}"
            ) from e

        data = response.json()

        if "error" in data:
            raise OllamaError(f"Ollama error: {data['error']}")

        text = data.get("response", "")

        # Log performance stats if available
        total_duration = data.get("total_duration", 0)
        eval_count = data.get("eval_count", 0)
        if total_duration and eval_count:
            tokens_per_sec = eval_count / (total_duration / 1e9)
            logger.debug(
                "Ollama: %d tokens in %.1fs (%.1f tok/s)",
                eval_count, total_duration / 1e9, tokens_per_sec,
            )

        return text
