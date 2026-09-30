"""Extract a JSON object from an LLM response.

Handles common LLM quirks: markdown code fences, prose before/after the
JSON, and trailing commas. Used by the evaluator (Ollama) and the resume
builder (Claude).
"""

from __future__ import annotations

import json
import re
from typing import Any


def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def extract_json_object(raw: str) -> dict[str, Any]:
    """Parse the first JSON object out of an LLM response.

    Raises:
        ValueError: if no JSON object can be parsed.
    """
    cleaned = _strip_fences(raw)

    candidates = [cleaned]
    # Slice from the first '{' to the last '}' to drop surrounding prose.
    # Works for arbitrarily nested objects, unlike a brace-matching regex.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])

    for candidate in candidates:
        for text in (candidate, _remove_trailing_commas(candidate)):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data

    # Last resort: decode the first complete object and ignore anything after it
    if start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(_remove_trailing_commas(cleaned[start:]))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No JSON object found in response: {raw[:200]!r}")
