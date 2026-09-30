"""Workday — multi-page, everything keyed by data-automation-id.

The ids below drift between tenants and releases — verify them with
`python -m apply_client --dry-run --url <posting>` before trusting them.

Dropdowns are [data-automation-id*="Dropdown"] buttons opening a
[role=listbox]; the base filler's listbox handling covers them. On the
"My Experience" page only the first (current) position is filled; every
other experience row is flagged — Workday's add-row UI is too fragile to loop.
Sign-in / account-creation pages are handed to the human immediately.
"""

from __future__ import annotations

from typing import Any

from apply_client.ats.fillers.base import BaseFiller
from apply_client.models import ATSType, Confidence, FormField

KNOWN_AUTOMATION_IDS = {
    "legalNameSection_firstName": "first_name",
    "legalNameSection_lastName": "last_name",
    "email": "email",
    "phone-number": "phone",
    "addressSection_city": "city",
    "addressSection_countryRegion": "state",
    "file-upload-input-ref": "resume",
    "jobTitle": "current_title",
    "company": "current_company",
}

SIGN_IN_JS = """
() => !!document.querySelector(
  "[data-automation-id='signInContent'], [data-automation-id='createAccountLink'], " +
  "[data-automation-id='signInLink'], [data-automation-id='createAccountSubmitButton']")
"""


class WorkdayFiller(BaseFiller):
    ats = ATSType.WORKDAY

    def classify(self, f: FormField, raw: dict[str, Any]) -> None:
        if f.canonical and f.canonical.startswith("_never"):
            return
        auto = f.extra.get("automation_id", "")
        ancestors = f.extra.get("ancestor_automation_ids", [])

        # Only the first work-experience block (workExperience-1) is ours to fill
        experience = next((a for a in ancestors if a.startswith("workExperience-")), None)
        if experience and experience != "workExperience-1":
            f.canonical, f.confidence = None, Confidence.LOW
            f.note = "Additional work experience — add it yourself (only the current job is auto-filled)"
            return

        if auto in KNOWN_AUTOMATION_IDS:
            f.canonical, f.confidence = KNOWN_AUTOMATION_IDS[auto], Confidence.HIGH
        elif "Dropdown" in auto and f.input_type != "combobox":
            f.input_type = "combobox"  # Workday dropdowns are plain buttons

    async def detect_blockers(self, page) -> list[str]:  # type: ignore[no-untyped-def]
        reasons = await super().detect_blockers(page)
        try:
            if await page.evaluate(SIGN_IN_JS):
                reasons.insert(0, "Workday sign-in / account creation — do this yourself, "
                                  "then continue to the application")
        except Exception:
            pass
        return list(dict.fromkeys(reasons))

    async def detect_fields(self, page) -> list[FormField]:  # type: ignore[no-untyped-def]
        fields = await super().detect_fields(page)
        # Workday dropdown buttons aren't inputs, so discovery misses them — add them
        for frame in page.frames:
            try:
                buttons = await frame.evaluate(_DROPDOWN_JS)
            except Exception:
                continue
            for raw in buttons:
                f = self._to_field(raw, frame)
                f.input_type = "combobox"
                self.classify(f, raw)
                fields.append(f)
        return fields


_DROPDOWN_JS = r"""
() => {
  window.__jobAgentNextId = window.__jobAgentNextId || 1;
  window.__jobAgentDoc = window.__jobAgentDoc || Math.random().toString(36).slice(2, 8);
  const out = [];
  for (const el of document.querySelectorAll("button[aria-haspopup='listbox']")) {
    if (el.dataset.jobagentId || !el.getClientRects().length) continue;
    el.dataset.jobagentId = window.__jobAgentDoc + '-' + (window.__jobAgentNextId++);
    const container = el.closest("[data-automation-id*='formField']") || el.parentElement;
    const lab = container && container.querySelector("label, legend");
    const autos = [];
    for (let a = el.parentElement; a && autos.length < 6; a = a.parentElement) {
      const au = a.getAttribute && a.getAttribute("data-automation-id");
      if (au) autos.push(au);
    }
    const text = (el.innerText || "").trim();
    out.push({
      type: "combobox", selector: `[data-jobagent-id="${el.dataset.jobagentId}"]`,
      label: lab ? lab.innerText.trim() : (el.getAttribute("aria-label") || ""),
      name: el.getAttribute("name") || "", id: el.id || "",
      aria: el.getAttribute("aria-label") || "", placeholder: "",
      required: el.getAttribute("aria-required") === "true",
      automationId: el.getAttribute("data-automation-id") || "",
      ancestorIds: [], ancestorAutomationIds: autos, options: [],
      value: /^select/i.test(text) ? "" : text,
    });
  }
  return out;
}
"""
