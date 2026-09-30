"""Match a form field's label/attributes to a canonical applicant field, with confidence.

Confidence policy:
    HIGH   → fill silently
    MEDIUM → fill and flag (yellow outline)
    LOW    → leave blank and flag
    _never_* → leave blank, red outline, listed under "skipped"
    _eeo   → select the "decline to self-identify" option if present, else blank + flag
"""

from __future__ import annotations

import re
from functools import lru_cache

from apply_client.ats.synonyms import SYNONYMS
from apply_client.models import Action, Confidence, FormField

# Capped at MEDIUM even on an exact match — always worth a human look
CAPPED_AT_MEDIUM = {"salary_expectation"}

CHOICE_TYPES = {"select", "radio", "combobox"}


def normalize(text: str | None, split_camel: bool = False) -> str:
    """Lowercase, strip punctuation, collapse spaces; optionally split camelCase."""
    if not text:
        return ""
    if split_camel:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)  # firstName → first Name
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _normalized_synonyms() -> dict[str, list[str]]:
    return {canon: [normalize(v) for v in variants] for canon, variants in SYNONYMS.items()}


def _contains(haystack: str, phrase: str) -> bool:
    """Whole-word phrase match — "authorize" doesn't match "authorized", "name" not "username"."""
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
                                      haystack) is not None


def _best(texts: list[str], canonicals: list[str], exact_only: bool = False) -> tuple[str, int] | None:
    """Best (canonical, synonym length) matching any text — the longest synonym wins."""
    best: tuple[str, int] | None = None
    syn = _normalized_synonyms()
    for canon in canonicals:
        for variant in syn[canon]:
            for text in texts:
                if not text:
                    continue
                hit = (text == variant) if exact_only else _contains(text, variant)
                if hit and (best is None or len(variant) > best[1]):
                    best = (canon, len(variant))
    return best


def match_field(
    label_text: str | None,
    name_attr: str | None = None,
    id_attr: str | None = None,
    aria_label: str | None = None,
    placeholder: str | None = None,
) -> tuple[str | None, Confidence]:
    """Map a field to (canonical | None, Confidence).

    - Exact match on any of the five strings → HIGH
    - Whole-word match in label or aria-label → MEDIUM
    - Whole-word match only in name/id/placeholder → LOW
    - No match → (None, LOW)
    Never/EEO canonicals are checked first on all five strings and always win.
    """
    def forms(value: str | None) -> list[str]:
        # Both "linkedin url" and the camel-split "linked in url" — identifiers like
        # "firstName" need the split, brand names like "LinkedIn" must not get it
        return list(dict.fromkeys([normalize(value), normalize(value, split_camel=True)]))

    label_aria = forms(label_text) + forms(aria_label)
    attrs = forms(name_attr) + forms(id_attr) + forms(placeholder)
    everything = label_aria + attrs

    sensitive = [c for c in SYNONYMS if c.startswith("_never")]
    hit = _best(everything, sensitive)
    if hit:
        return hit[0], Confidence.HIGH
    if _best(everything, ["_eeo"]):
        return "_eeo", Confidence.HIGH

    regular = [c for c in SYNONYMS if not c.startswith("_")]
    for texts, exact, confidence in (
        (everything, True, Confidence.HIGH),
        (label_aria, False, Confidence.MEDIUM),
        (attrs, False, Confidence.LOW),
    ):
        hit = _best(texts, regular, exact_only=exact)
        if hit:
            canonical = hit[0]
            if canonical in CAPPED_AT_MEDIUM:
                confidence = min(confidence, Confidence.MEDIUM)
            return canonical, confidence
    return None, Confidence.LOW


def decide_action(f: FormField) -> Action:
    """What the filler should do with a field. Pure — used by fill() and --dry-run."""
    canon = f.canonical or ""
    if canon.startswith("_never") or f.input_type == "password":
        return Action.NEVER
    if canon == "_eeo":
        return Action.DECLINE if f.input_type in CHOICE_TYPES else Action.FLAG
    if f.input_type == "checkbox":
        return Action.FLAG  # never tick boxes — they're usually agreements
    if f.input_type == "file":
        if canon in ("resume", "cover_letter") and f.confidence >= Confidence.MEDIUM:
            return Action.UPLOAD
        return Action.FLAG
    if canon in ("resume", "cover_letter"):
        return Action.FLAG  # a text box asking for resume/letter text — human pastes it
    if not canon or f.confidence == Confidence.LOW:
        return Action.FLAG
    if f.input_type in ("textarea", "contenteditable") and f.confidence < Confidence.HIGH:
        return Action.FLAG  # free-text answers need a human
    return Action.FILL if f.confidence == Confidence.HIGH else Action.FILL_AND_FLAG
