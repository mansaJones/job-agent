"""Lever — single-page, fairly generic markup; the base filler does most of the work.

Resume upload is input[name="resume"]. Additional questions sit in
.application-question. EEO selects are named eeo[...].
"""

from __future__ import annotations

from typing import Any

from apply_client.ats.fillers.base import BaseFiller
from apply_client.models import ATSType, Confidence, FormField

KNOWN_NAMES = {
    "resume": "resume",
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "org": "current_company",
    "location": "location",
    "urls[linkedin]": "linkedin_url",
    "urls[portfolio]": "portfolio_url",
    "urls[github]": "portfolio_url",
    "urls[other]": "portfolio_url",
}


class LeverFiller(BaseFiller):
    ats = ATSType.LEVER

    def classify(self, f: FormField, raw: dict[str, Any]) -> None:
        if f.canonical and f.canonical.startswith("_never"):
            return
        name = f.name.lower()
        if name.startswith("eeo[") or name.startswith("eeo."):
            f.canonical, f.confidence = "_eeo", Confidence.HIGH
            return
        if name in KNOWN_NAMES:
            f.canonical, f.confidence = KNOWN_NAMES[name], Confidence.HIGH
