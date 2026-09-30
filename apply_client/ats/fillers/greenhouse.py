"""Greenhouse — single-page form.

Native inputs, <select> for most dropdowns, a [role=combobox] location
autocomplete. Resume/cover letter each have an "Attach" button over a hidden
file input (we set the input directly — no clicks). Custom questions live in
#custom_fields; the EEO block is #eeoc_fields.
"""

from __future__ import annotations

from typing import Any

from apply_client.ats.fillers.base import BaseFiller
from apply_client.models import ATSType, Confidence, FormField

# Stable Greenhouse field ids/names → canonical
KNOWN = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "resume": "resume",
    "cover_letter": "cover_letter",
    "job_application[location]": "location",
    "candidate-location": "location",
}


class GreenhouseFiller(BaseFiller):
    ats = ATSType.GREENHOUSE

    def classify(self, f: FormField, raw: dict[str, Any]) -> None:
        if f.canonical and f.canonical.startswith("_never"):
            return
        ancestors = f.extra.get("ancestor_ids", [])
        if "eeoc_fields" in ancestors or "demographic_questions" in ancestors:
            f.canonical, f.confidence = "_eeo", Confidence.HIGH
            return

        keys = [f.element_id, f.name, *ancestors[:2]]
        for key in keys:
            # e.g. id="first_name", name="job_application[first_name]", container id="resume_fieldset"
            key = key.replace("job_application[", "").rstrip("]").replace("_fieldset", "")
            if key in KNOWN:
                if f.input_type != "textarea":  # "paste your resume" boxes stay with the human
                    f.canonical, f.confidence = KNOWN[key], Confidence.HIGH
                return
