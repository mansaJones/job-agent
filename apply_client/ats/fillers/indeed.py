"""Indeed Easy Apply / SmartApply — a multi-step modal.

Typical steps: contact info (prefilled from your Indeed profile) → resume →
screener questions → review. The client fills each step and waits; YOU click
Continue. Rapid auto-stepping looks like bot behavior to Indeed, and the
client never clicks Continue anyway.

Prefilled contact fields are verified, never overwritten — the base filler
already leaves non-empty values alone and flags mismatches. Screener
questions go through the generic matcher; anything not HIGH is flagged.
"""

from __future__ import annotations

from typing import Any

from apply_client.ats.fillers.base import BaseFiller
from apply_client.models import ATSType, Confidence, FormField


class IndeedFiller(BaseFiller):
    ats = ATSType.INDEED_EASY

    def classify(self, f: FormField, raw: dict[str, Any]) -> None:
        if f.canonical and f.canonical.startswith("_never"):
            return
        # Screener questions: only fill what we're sure about
        if f.confidence == Confidence.MEDIUM and f.input_type not in ("file",):
            in_screener = any("questions" in a.lower() for a in f.extra.get("ancestor_ids", []))
            if in_screener:
                f.confidence = Confidence.LOW
